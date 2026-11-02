"""Local dashboard and durable job-discovery producer. Resume consumer is separate."""
import argparse
from contextlib import contextmanager
import json
import os
import re
import sqlite3
import threading
import tempfile
import hashlib
import uuid
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .intake import IntakeStore
from .sources import fetch_source, source_catalog, validate_sources

SCHEMA = '''
CREATE TABLE IF NOT EXISTS discovery_runs(
 id TEXT PRIMARY KEY, prompt TEXT NOT NULL, config TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS source_scans(
 run_id TEXT NOT NULL REFERENCES discovery_runs(id), source TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'running', fetched INTEGER NOT NULL DEFAULT 0,
 new_jobs INTEGER NOT NULL DEFAULT 0, existing_jobs INTEGER NOT NULL DEFAULT 0,
 matched INTEGER NOT NULL DEFAULT 0, excluded INTEGER NOT NULL DEFAULT 0,
 needs_full_jd INTEGER NOT NULL DEFAULT 0, queued INTEGER NOT NULL DEFAULT 0,
 title_filtered INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0,
 date_unknown INTEGER NOT NULL DEFAULT 0, too_old INTEGER NOT NULL DEFAULT 0,
 errors INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL,
 finished_at TEXT, PRIMARY KEY(run_id,source));
CREATE TABLE IF NOT EXISTS activity(
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
 kind TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS resume_queue(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL REFERENCES jobs(id),
 run_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'resume_waiting',
 snapshot TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS outbox(
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
 topic TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 published_at TEXT);
'''


def rules_from_request(body):
    prompt = body.get('prompt', '')
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 1000:
        raise ValueError('Enter a title search phrase, up to 1,000 characters')
    # Explicit limited grammar. Never silently pretend to understand arbitrary prose.
    title = re.sub(r'^find\s+', '', prompt.strip(), flags=re.I)
    title = re.sub(r'\s+jobs?\.?$', '', title, flags=re.I)
    titles = [x.strip() for x in title.split('|') if x.strip()]
    if not titles:
        raise ValueError('Enter at least one job title')
    rules = {'title_keywords': titles}
    for key in ('excluded_title_keywords', 'locations', 'jd_keywords'):
        value = body.get(key, [])
        if not isinstance(value, list) or len(value) > 30 or any(not isinstance(x, str) or not x.strip() or len(x) > 100 for x in value):
            raise ValueError(f'{key} must be a list of short phrases')
        rules[key] = value
    countries = body.get('country_codes', [])
    if not isinstance(countries, list) or len(countries)>10 or any(
            not isinstance(x,str) or not re.fullmatch(r'[A-Za-z]{2}',x) for x in countries):
        raise ValueError('country_codes must be 2-letter country codes, e.g. US')
    rules['country_codes'] = list(dict.fromkeys(x.upper() for x in countries))
    # Users sometimes enter a country under Exact locations. Country is
    # already handled by country_codes; retaining it rejects valid US cities.
    country_names = {'united states': 'US', 'united states of america':'US',
                     'usa':'US', 'us':'US'}
    normalized_locations = []
    for location in rules['locations']:
        mapped = country_names.get(location.strip().casefold())
        if mapped:
            if mapped not in rules['country_codes']:
                rules['country_codes'].append(mapped)
        else:
            normalized_locations.append(location)
    rules['locations'] = normalized_locations
    hours = body.get('posted_within_hours', 0)
    if type(hours) is not int or hours not in (0, 7, 24, 72):
        raise ValueError('Posting window must be Any, 7 hours, 24 hours, or 3 days')
    rules['posted_within_hours'] = hours
    basis=body.get('time_basis','published')
    if basis not in ('published','first_seen'):
        raise ValueError('Choose published or first_seen time basis')
    rules['time_basis']=basis
    ceiling=body.get('max_required_years')
    if ceiling is not None and (type(ceiling) is not int or not 0<=ceiling<=20):
        raise ValueError('Experience ceiling must be 0–20 years or blank')
    threshold=body.get('min_match_percent',0)
    if type(threshold) is not int or threshold not in (0,70,75,80,85,90,95,100):
        raise ValueError('Choose a match threshold from 70 to 100, or no filter')
    # Existing API callers keep their original scan contract; the dashboard
    # opts into title-first discovery and the same existing match layer.
    title_first=body.get('title_first',False)
    if type(title_first) is not bool:
        raise ValueError('title_first must be true or false')
    rules['title_first']=title_first
    size=body.get('batch_size')
    if size is not None and size!=55:
        raise ValueError('Discovery batches contain up to 55 postings per company')
    rules.update(max_required_years=ceiling,min_match_percent=threshold)
    if size is not None:rules['batch_size']=55
    return prompt.strip(), rules, validate_sources(body.get('sources'))


class Service:
    def __init__(self, database, fetcher=None, default_sources=None, profile_path=None):
        self.profile_path = Path(profile_path) if profile_path else Path(database).parent / "profile.json"
        self.profile_lock = threading.Lock()
        self.database = database
        self.fetcher = fetcher
        self.default_sources = default_sources or []
        self.lock = threading.Lock()
        self.scan_interval_minutes = None
        self.next_scan_epoch = None
        self._scan_stop = threading.Event()
        store = IntakeStore(database)
        store.close()
        with self.connect() as db:
            from .resume_regeneration import SCHEMA as REGEN_SCHEMA
            from .application_history import SCHEMA as HISTORY_SCHEMA
            db.executescript(SCHEMA + REGEN_SCHEMA + HISTORY_SCHEMA)
            db.execute('CREATE TABLE IF NOT EXISTS workspace_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            columns = {row[1] for row in db.execute('PRAGMA table_info(discovery_runs)')}
            if 'trigger' not in columns:
                db.execute("ALTER TABLE discovery_runs ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'")
            if 'finished_at' not in columns:
                db.execute('ALTER TABLE discovery_runs ADD COLUMN finished_at TEXT')
            scan_columns = {row[1] for row in db.execute('PRAGMA table_info(source_scans)')}
            for name in ('title_filtered','processed','date_unknown','too_old'):
                if name not in scan_columns:
                    db.execute(f'ALTER TABLE source_scans ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
            db.execute("UPDATE discovery_runs SET status='interrupted' WHERE status='running'")
        
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def read_profile(self):
        with self.profile_lock:
            if not self.profile_path.exists():
                return {'profile': None, 'revision': None}
            raw = self.profile_path.read_bytes()
            return {'profile': json.loads(raw.decode('utf-8')),
                    'revision': hashlib.sha256(raw).hexdigest()}

    def save_profile(self, body):
        from .resume_worker import validate_profile
        profile = body.get('profile')
        safe, version = validate_profile(profile)
        if '_resume_header' not in safe:
            raise ValueError('Please enter your name and resume contact details')
        # Keep only the validated fields used by the worker.
        saved = {'profile_id': safe['profile_id'], 'verified': True,
                 'resume_header': safe['_resume_header'],
                 'facts': [dict(f, verified=True) for f in safe['facts']]}
        raw = json.dumps(saved, ensure_ascii=False, indent=2).encode('utf-8')
        with self.profile_lock:
            current = hashlib.sha256(self.profile_path.read_bytes()).hexdigest() if self.profile_path.exists() else None
            if body.get('revision') != current:
                raise ValueError('Profile changed in another window. Reload before saving.')
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            name = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.profile_path.parent, delete=False) as file:
                    name = file.name
                    file.write(raw)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(name, self.profile_path)
            finally:
                if name and os.path.exists(name):
                    os.unlink(name)
        return {'revision': hashlib.sha256(raw).hexdigest(), 'profile_version': version}

    def event(self, run_id, kind, message):
        with self.connect() as db:
            db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)', (run_id, kind, message))

    def start(self, body, background=True, trigger='manual'):
        if trigger not in ('manual', 'scheduled'):
            raise ValueError('Unknown scan trigger')
        body = dict(body)
        if 'board_provider' in body:
            from .discovery_pages import resolve_sources
            body['sources']=resolve_sources(body['board_provider'],body.get('sources'),self.default_sources)
        if not body.get('sources'):
            body['sources'] = self.default_sources or (['adzuna:us'] if os.getenv('ADZUNA_APP_ID') and os.getenv('ADZUNA_APP_KEY') else ['directory:verified'])
        prompt, rules, sources = rules_from_request(body)
        from .company_directory import discovery_sources
        rules['allowed_sources']=list(dict.fromkeys(board for source in sources for board in (discovery_sources() if source=='directory:verified' else [source])))
        if rules.get('min_match_percent'):
            from .resume_worker import load_profile
            from .matching import profile_skills
            if not self.profile_path.exists():
                raise ValueError('Create and verify your resume profile before using the match filter')
            profile,_=load_profile(self.profile_path)
            rules['profile_skills']=profile_skills(profile)
            if not rules['profile_skills']:
                raise ValueError('Save verified technical skills in your profile before using a match threshold')
        if not self.lock.acquire(blocking=False):
            raise ValueError('A discovery run is already active')
        run_id = str(uuid.uuid4())
        try:
            with self.connect() as db:
                db.execute('INSERT INTO discovery_runs(id,prompt,config,status,trigger) VALUES(?,?,?,?,?)',
                           (run_id, prompt, json.dumps({'rules': rules, 'sources': sources}), 'running',trigger))
            if background:
                threading.Thread(target=self.run, args=(run_id, rules, sources), daemon=True).start()
            else:
                self.run(run_id, rules, sources)
        except Exception:
            if self.lock.locked():
                self.lock.release()
            raise
        return run_id

    @staticmethod
    def utc_now():
        return datetime.now(timezone.utc).isoformat(timespec='seconds')

    def last_manual_request(self):
        """Use the exact last manual request. No guessed filters or locations."""
        with self.connect() as db:
            row = db.execute("""SELECT prompt,config FROM discovery_runs WHERE trigger='manual'
                                ORDER BY rowid DESC LIMIT 1""").fetchone()
        if not row:
            return None
        config = json.loads(row['config'])
        rules = config['rules']
        return {'prompt':row['prompt'], 'sources':config['sources'],
                **{name:rules.get(name,[]) for name in
                   ('excluded_title_keywords','locations','jd_keywords','country_codes')},
                'posted_within_hours':rules.get('posted_within_hours',0),'time_basis':rules.get('time_basis','published'),
                'max_required_years':rules.get('max_required_years'),'min_match_percent':rules.get('min_match_percent',0),
                **({'batch_size':55} if rules.get('batch_size') else {})}

    def scheduled_scan(self, background=True):
        request = self.last_manual_request()
        if request is None:
            return None
        return self.start(request,background=background,trigger='scheduled')

    def set_schedule(self, every_minutes):
        if type(every_minutes) is not int or (every_minutes!=0 and not 5<=every_minutes<=1440):
            raise ValueError('Schedule must be 0 (off) or 5–1440 minutes')
        self.stop_schedule()
        if every_minutes:
            self.enable_schedule(every_minutes)
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO workspace_settings VALUES(?,?)',('scan_every_minutes',str(every_minutes)))
        return {'scan_every_minutes':every_minutes}

    def saved_schedule(self):
        with self.connect() as db:
            row=db.execute("SELECT value FROM workspace_settings WHERE key='scan_every_minutes'").fetchone()
        return int(row['value']) if row else 15

    def enable_schedule(self, every_minutes):
        """Opt-in periodic discovery; only runs while this dashboard is running."""
        if type(every_minutes) is not int or not 5 <= every_minutes <= 1440:
            raise ValueError('Schedule must be 5–1440 minutes')
        if self.scan_interval_minutes is not None:
            raise ValueError('Scanner already enabled')
        self.scan_interval_minutes = every_minutes
        self.next_scan_epoch = time.time() + every_minutes*60
        self._scan_stop.clear()
        thread = threading.Thread(target=self._scheduler_loop,daemon=True,name='job-discovery-scheduler')
        thread.start()
        self._scan_thread = thread

    def _scheduler_loop(self):
        seconds = self.scan_interval_minutes*60
        while not self._scan_stop.wait(seconds):
            self.next_scan_epoch = time.time()+seconds
            try:
                run_id = self.scheduled_scan()
                if run_id is None:
                    print('Automatic discovery skipped: run a manual search first',flush=True)
            except ValueError as exc:
                print(f'Automatic discovery skipped: {exc}',flush=True)

    def stop_schedule(self):
        self._scan_stop.set()
        thread=getattr(self,'_scan_thread',None)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        self.next_scan_epoch = None
        self.scan_interval_minutes = None

    def _source_progress(self, run_id, source, counts, status='running', finished=False):
        """Commit progress after each batch, so a long scan is not frozen in UI."""
        with self.connect() as db:
            db.execute("""UPDATE source_scans SET status=?, fetched=?,processed=?,
                title_filtered=?,new_jobs=?,existing_jobs=?,matched=?,excluded=?,
                needs_full_jd=?,date_unknown=?,too_old=?,queued=?,errors=?,
                finished_at=? WHERE run_id=? AND source=?""",
                (status, *(counts[k] for k in (
                    'fetched','processed','title_filtered','new_jobs','existing_jobs',
                    'matched','excluded','needs_full_jd','date_unknown','too_old',
                    'queued','errors')),
                 self.utc_now() if finished else None,run_id,source))

    def run(self, run_id, rules, sources):
        """Sequential public-board scan. One failing source never stops later ones."""
        store = None
        failures = 0
        counters = ('fetched','processed','title_filtered','new_jobs','existing_jobs',
                    'matched','excluded','needs_full_jd','date_unknown','too_old','queued')
        totals = {key:0 for key in counters}
        try:
            store = IntakeStore(self.database)
            from .company_directory import discovery_sources
            work_sources = []
            for source in sources:
                work_sources.extend(discovery_sources() if source == 'directory:verified' else [source])
            from .discovery_pages import round_robin_sources
            work_sources = round_robin_sources(list(dict.fromkeys(work_sources)))
            self.event(run_id,'scan_started',f'Scanning {len(work_sources)} verified sources in sequence.')
            from .title_search import title_candidate
            self.event(run_id,'architecture',
                'Layer 1 searches requested role titles; Layer 2 evaluates JD, '
                'location, experience, skills and publication time before any resume is queued.')
            for ordinal,source in enumerate(work_sources,1):
                counts = {key:0 for key in (*counters,'errors')}
                result_status = 'completed'
                with self.connect() as db:
                    db.execute("""INSERT INTO source_scans(run_id,source,status,started_at)
                                  VALUES(?,?,?,?)""",(run_id,source,'running',self.utc_now()))
                self.event(run_id,'fetching',f'Source {ordinal}/{len(work_sources)}: {source}')
                try:
                    if self.fetcher:
                        jobs=self.fetcher(source)
                    elif rules.get('batch_size'):
                        from .discovery_pages import fetch_slice
                        jobs=fetch_slice(self,source,rules)
                    else:
                        jobs=fetch_source(source,rules)
                    if not isinstance(jobs,list):
                        raise ValueError('Source returned an invalid result')
                    counts['fetched'] = len(jobs)
                    self._source_progress(run_id,source,counts)
                    for index,job in enumerate(jobs,1):
                        try:
                            if not isinstance(job,dict):
                                raise ValueError('Malformed job')
                            if 'source_error' in job:
                                counts['errors'] += 1
                                self.event(run_id,'error',f"{source}: provider reported a malformed posting")
                                continue
                            title = job.get('title')
                            if not isinstance(title,str) or not title.strip():
                                raise ValueError('Missing title')
                            # Layer 1 is only the user-requested role titles.
                            # These candidates enter the existing Layer 2 matcher
                            # before any resume_queue/outbox side effects occur.
                            candidate=title_candidate(job,rules)
                            if candidate is None:
                                counts['title_filtered'] += 1
                                continue
                            result = store.import_job({**candidate,'source':candidate.get('source') or source,
                                                       'qualification_rules':rules,
                                                       'intake_run_id':run_id},rules)
                            counts['new_jobs' if result['is_new'] else 'existing_jobs'] += 1
                            decision = result['qualification']['status']
                            if decision not in ('matched','excluded','needs_full_jd','date_unknown'):
                                raise ValueError('Invalid qualification status')
                            counts[decision] += 1
                            reasons = result['qualification']['reasons']
                            if decision == 'excluded' and reasons and 'predates the' in reasons[0]:
                                counts['too_old'] += 1
                            if reasons:
                                # Limit per-posting activity noise while preserving
                                # exact numeric counters for all decisions.
                                if counts[decision] <= 12 or decision == 'date_unknown':
                                    self.event(run_id,decision,f"{title}: {'; '.join(reasons)}")
                                continue
                            with self.connect() as db:
                                db.execute('BEGIN IMMEDIATE')
                                # Only tasks excluded before GPT may be requeued after filters change.
                                # Completed/processing tasks and their artifacts are never overwritten.
                                excluded=db.execute("SELECT sequence FROM resume_queue WHERE job_id=? AND status='resume_excluded'",(result['job_id'],)).fetchone()
                                if excluded:
                                    db.execute("DELETE FROM consumer_receipts WHERE consumer='resume-gpt-v1' AND event_key=?",('resume.requested:'+result['job_id'],))
                                    db.execute("DELETE FROM outbox WHERE event_key=?",('resume.requested:'+result['job_id'],))
                                    cursor=db.execute("UPDATE resume_queue SET status='resume_waiting',run_id=?,snapshot=?,attempts=0,next_attempt_at=0,last_error=NULL,worker_token=NULL,lease_until=NULL WHERE sequence=?",(run_id,json.dumps(result),excluded['sequence']))
                                else:
                                    cursor = db.execute('INSERT OR IGNORE INTO resume_queue(job_id,run_id,snapshot) VALUES(?,?,?)',
                                                        (result['job_id'],run_id,json.dumps(result)))
                                if cursor.rowcount:
                                    number = excluded['sequence'] if excluded else cursor.lastrowid
                                    payload = {'schema_version':1, 'event_id':str(uuid.uuid4()),
                                               'correlation_id':result['job_id'],'queue_order':number,
                                               'job_id':result['job_id'],'job':result,
                                               'status':'resume_waiting'}
                                    db.execute('INSERT INTO outbox(event_key,topic,payload) VALUES(?,?,?)',
                                               ('resume.requested:'+result['job_id'],'resume.requested.v1',json.dumps(payload)))
                                    counts['queued'] += 1
                                    message = f"#{number} · {title} → waiting for resume"
                            if cursor.rowcount:
                                self.event(run_id,'queued',message)
                        except sqlite3.Error:
                            # Keep the pending page for replay after a transactional failure.
                            raise
                        except (ValueError,TypeError,KeyError) as exc:
                            counts['errors'] += 1
                            self.event(run_id,'error',f'{source}: job import failed ({type(exc).__name__})')
                        finally:
                            counts['processed'] += 1
                            if index % 25 == 0:
                                self._source_progress(run_id,source,counts)
                    if rules.get('batch_size') and not self.fetcher:
                        from .discovery_pages import acknowledge_slice
                        acknowledge_slice(self,source,rules)
                    self.event(run_id,'source_finished',f"{source}: fetched {counts['fetched']}, "
                        f"title-irrelevant {counts['title_filtered']}, new {counts['new_jobs']}, "
                        f"previously seen {counts['existing_jobs']}, matched {counts['matched']}, "
                        f"unknown publication dates {counts['date_unknown']}, "
                        f"queued {counts['queued']}, errors {counts['errors']}.")
                except Exception as exc:
                    result_status = 'failed'
                    counts['errors'] += 1
                    # Only log safe type / HTTP status, never exception URLs with
                    # API credential parameters such as Adzuna.
                    code = getattr(exc,'code',None)
                    detail = f' HTTP {code}' if isinstance(code,int) else ''
                    self.event(run_id,'error',f'{source}: provider failed ({type(exc).__name__}{detail})')
                finally:
                    if counts['errors'] and result_status != 'failed':
                        result_status = 'completed_with_errors'
                    failures += counts['errors']
                    self._source_progress(run_id,source,counts,result_status,finished=True)
                    for key in totals:
                        totals[key] += counts[key]
            status = 'completed_with_errors' if failures else 'completed'
            self.event(run_id,'finished',
                f"Scan finished: {totals['fetched']} returned "
                f"({'title candidates inspected' if rules.get('title_first') else 'raw API postings'}), "
                f"{totals['title_filtered']} unrelated titles skipped, "
                f"{totals['new_jobs']} new requested-title candidates (Layer 1), "
                f"{totals['existing_jobs']} previously seen matching-title jobs, "
                f"{totals['matched']} qualified, {totals['date_unknown']} unverified dates, "
                f"{totals['queued']} newly queued; {failures} errors.")
        except Exception as exc:
            status = 'failed'
            self.event(run_id,'error',f'Discovery failed ({type(exc).__name__})')
        finally:
            if store:
                store.close()
            try:
                with self.connect() as db:
                    db.execute('UPDATE discovery_runs SET status=?,finished_at=? WHERE id=?',
                               (locals().get('status','failed'),self.utc_now(),run_id))
            finally:
                self.lock.release()

    def snapshot(self):
        from .worker_health import status as worker_status
        from .discovery_pages import board_options
        with self.connect() as db:
            latest = db.execute('SELECT id FROM discovery_runs ORDER BY rowid DESC LIMIT 1').fetchone()
            latest_id = latest['id'] if latest else ''
            def public_draft(x):
                draft=json.loads(x['result'])
                draft.pop('_profile_snapshot',None)
                return {**dict(x),'result':json.dumps(draft)}
            def row_job(x):
                return {**json.loads(x['payload']), 'id':x['id'], 'first_seen_at':x['first_seen_at'],
                            'last_seen_at':x['last_seen_at'], 'last_run_id':x['last_run_id']}
            saved_stats = {'total':db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]}
            for status in ('matched','excluded','needs_full_jd','date_unknown'):
                saved_stats[status] = db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE json_extract(payload,'$.qualification.status')=?",
                    (status,)).fetchone()[0]
            pipeline_counts = {}
            for row in db.execute("SELECT status,COUNT(*) AS n FROM application_queue GROUP BY status") if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_queue'").fetchone() else []:
                pipeline_counts[row['status']] = row['n']
            prepared_count = db.execute("SELECT COUNT(*) FROM applied_history").fetchone()[0]
            return {
                'pipeline_counts':{**pipeline_counts,'confirmed_submissions':prepared_count},
                'agent3_tasks':[dict(x) for x in db.execute(
                    'SELECT application_order,status,last_error,attempts,updated_at FROM agent3_tasks ORDER BY application_order')
                    ] if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent3_tasks'").fetchone() else [],
                'saved_stats':saved_stats,
                'build_version':'2026.11-search-and-handoff',
                'board_options':board_options(self.default_sources),
                'searched_count':db.execute('SELECT coalesce(sum(processed),0) FROM source_scans').fetchone()[0],
                'applied_history':[dict(x) for x in db.execute('SELECT * FROM applied_history ORDER BY submitted_at DESC')],
                'latest_jobs_count':db.execute('SELECT COUNT(*) FROM jobs WHERE last_run_id=?',
                                               (latest_id,)).fetchone()[0],
                'latest_jobs':[row_job(x) for x in db.execute(
                    'SELECT * FROM jobs WHERE last_run_id=? ORDER BY updated_at DESC LIMIT 250',
                    (latest_id,))],
                'worker':worker_status(db),
                'queue_counts':{r['status']:r['n'] for r in db.execute('SELECT status,count(*) n FROM resume_queue GROUP BY status')},
                'regenerations': [dict(x) for x in db.execute('SELECT id,queue_order,status,error,created_at FROM resume_regenerations ORDER BY created_at DESC,rowid DESC')],
                'sources': source_catalog(),
                'applications': [dict(x) for x in db.execute('SELECT sequence,job_id,resume_artifact_id,status,payload FROM application_queue ORDER BY sequence')] if db.execute("SELECT 1 FROM sqlite_master WHERE name='application_queue'").fetchone() else [],
                'default_sources': self.default_sources,
                'drafts': [public_draft(x) for x in db.execute('SELECT queue_order,profile_version,result,created_at FROM resume_drafts ORDER BY queue_order')] if db.execute("SELECT 1 FROM sqlite_master WHERE name='resume_drafts'").fetchone() else [],
                'runs': [dict(x) for x in db.execute('SELECT * FROM discovery_runs ORDER BY created_at DESC,rowid DESC LIMIT 20')],
                'activity': [dict(x) for x in db.execute('SELECT * FROM activity ORDER BY id DESC LIMIT 200')],
                'jobs':[row_job(x) for x in db.execute(
                    'SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 300')],
                'source_scans': [dict(x) for x in db.execute('''SELECT * FROM source_scans WHERE run_id=
                    (SELECT id FROM discovery_runs ORDER BY rowid DESC LIMIT 1)
                    ORDER BY source''')],
                'scanner': {
                    'enabled': self.scan_interval_minutes is not None,
                    'interval_minutes': self.scan_interval_minutes,
                    'next_scan_at': (datetime.fromtimestamp(self.next_scan_epoch,timezone.utc)
                                     .isoformat(timespec='seconds') if self.next_scan_epoch else None),
                    'has_manual_search': bool(db.execute("SELECT 1 FROM discovery_runs WHERE trigger='manual' LIMIT 1").fetchone()),
                },
                'queue': [dict(x) for x in db.execute('SELECT * FROM resume_queue ORDER BY sequence')],
            }


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, value, content_type='application/json', headers=None):
            payload = value if isinstance(value,bytes) else value.encode() if isinstance(value,str) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length',str(len(payload)))
            for key,value in (headers or {}).items():
                self.send_header(key,value)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == '/':
                self.reply(200, Path(__file__).with_name('web').joinpath('index.html').read_text(encoding='utf-8'), 'text/html')
            elif self.path == '/profile':
                self.reply(200, Path(__file__).with_name('web').joinpath('profile.html').read_text(encoding='utf-8'), 'text/html')
            elif self.path == '/api/profile/resume':
                try:
                    from .resume_worker import validate_profile, validate_result
                    from .resume_artifact import render_docx, validate_docx
                    profile, _ = validate_profile(service.read_profile()['profile'])
                    draft = validate_result({'ordered_fact_ids':[f['id'] for f in profile['facts']],
                                             'missing_requirements':[]}, profile)
                    raw = render_docx(draft, profile['_resume_header'])
                    validate_docx(raw, draft, profile['_resume_header'])
                    self.reply(200, raw, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                               {'Content-Disposition':'attachment; filename="profile-resume.docx"'})
                except (ValueError, OSError, KeyError) as exc:
                    self.reply(400, {'error':str(exc)})
            elif urlsplit(self.path).path == '/api/companies':
                from urllib.parse import parse_qs
                from .company_directory import list_companies
                args = parse_qs(urlsplit(self.path).query)
                try:
                    self.reply(200, list_companies(
                        query=args.get('q',[''])[0],category=args.get('category',[''])[0],
                        limit=min(int(args.get('limit',['40'])[0]),100)))
                except (ValueError, OSError, KeyError) as exc:
                    self.reply(400, {'error':str(exc)})
            elif self.path == '/api/profile':
                self.reply(200, service.read_profile())
            elif re.fullmatch(r'/api/artifacts/[a-f0-9]{64}/preview',self.path):
                self.download_artifact(self.path.split('/')[-2], preview=True)
            elif re.fullmatch(r'/api/artifacts/[a-f0-9]{64}',self.path):
                self.download_artifact(self.path.rsplit('/',1)[1])
            elif self.path == '/api/diagnostics':
                from .diagnostics import report
                self.reply(200, report(service.database, service.profile_path))
            elif self.path == '/api/state':
                self.reply(200, service.snapshot())
            else:
                self.reply(404, {'error': 'Not found'})

        def download_artifact(self, identifier, preview=False):
            import hashlib
            with service.connect() as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='resume_artifacts'").fetchone():
                    self.reply(404, {'error':'No resume artifact'})
                    return
                row=db.execute('SELECT path,sha256 FROM resume_artifacts WHERE id=?',(identifier,)).fetchone()
            if not row:
                self.reply(404, {'error':'No resume artifact'})
                return
            try:
                data=Path(row['path']).read_bytes()
            except OSError:
                self.reply(404, {'error':'Resume artifact is unavailable in configured local storage'})
                return
            if hashlib.sha256(data).hexdigest()!=row['sha256']:
                self.reply(409, {'error':'Resume artifact checksum failed'})
                return
            if preview:
                from .resume_preview import preview_html
                self.reply(200, preview_html(data), 'text/html',
                           {'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'",
                            'X-Frame-Options':'SAMEORIGIN'})
                return
            self.reply(200,data,'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                       {'Content-Disposition':f'attachment; filename="resume-{identifier[:12]}.docx"'})

        def do_POST(self):
            # No cross-origin writes to the local agent.
            origin = self.headers.get('Origin')
            host = self.headers.get('Host', '')
            if host != f'127.0.0.1:{self.server.server_port}' or (origin and origin != f'http://{host}'):
                self.reply(403, {'error': 'Only same-origin local requests are allowed'})
                return
            if self.path not in ('/api/runs', '/api/profile', '/api/profile/import', '/api/resumes/regenerate', '/api/resumes/approve', '/api/jobs/enrich','/api/discovery/schedule','/api/resumes/handoff','/api/applications/prepared','/api/applications/submitted'):
                self.reply(404, {'error': 'Not found'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                maximum = (2_000_000 if self.path.startswith('/api/profile') else
                           100_000 if self.path == '/api/jobs/enrich' else 16384)
                if not 0 < length <= maximum:
                    raise ValueError(f'Request must be 1–{maximum} bytes')
                raw = self.rfile.read(length)
                if self.path == '/api/profile/import':
                    from .profile_import import import_docx
                    self.reply(200, import_docx(raw))
                    return
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError('Expected a JSON object')
                if self.path == '/api/resumes/handoff':
                    from .handoff import promote_existing
                    exists=service.snapshot()['applications']
                    self.reply(200,{'promoted':promote_existing(service) if exists else 0})
                elif self.path == '/api/applications/prepared':
                    from .application_history import prepared
                    self.reply(200,prepared(service,body))
                elif self.path == '/api/applications/submitted':
                    from .application_history import submitted
                    self.reply(200,submitted(service,body))
                elif self.path == '/api/discovery/schedule':
                    self.reply(200, service.set_schedule(body.get('every_minutes')))
                elif self.path == '/api/jobs/enrich':
                    from .enrichment import enrich_job
                    self.reply(200, enrich_job(service, body))
                elif self.path == '/api/resumes/approve':
                    sequence=body.get('queue_order')
                    artifact_id=body.get('artifact_id')
                    if type(sequence) is not int or not isinstance(artifact_id,str):
                        raise ValueError('Select the exact resume to review')
                    from .resume_review import approve
                    self.reply(200, approve(service,sequence,artifact_id))
                elif self.path == '/api/resumes/regenerate':
                    sequence = body.get('queue_order')
                    if type(sequence) is not int or sequence < 1:
                        raise ValueError('Choose a valid resume queue order')
                    from .resume_regeneration import enqueue
                    self.reply(202, {'request_id':enqueue(service, sequence)})
                elif self.path == '/api/profile':
                    self.reply(200, service.save_profile(body))
                else:
                    self.reply(202, {'run_id': service.start(body)})
            except sqlite3.Error:
                self.reply(500, {'error': 'Database operation failed; completion was not recorded. Check storage and retry.'})
            except OSError:
                self.reply(500, {'error': 'Could not read or save local data. Check file availability and permissions.'})
            except (ValueError, TypeError) as exc:
                self.reply(400, {'error': str(exc)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Local job discovery dashboard')
    parser.add_argument('--database', default='data/agent.db')
    parser.add_argument('--profile', help='Profile save path; default: profile.json beside the database')
    parser.add_argument('--port', default=8765, type=int)
    parser.add_argument('--sources-file', help='JSON array of default company-board and market-search sources')
    parser.add_argument('--scan-every-minutes',type=int,default=None,
                        help='Opt into periodic scans of the last manual search (5–1440 min); 0 disables scheduling')
    args = parser.parse_args()
    Path(args.database).parent.mkdir(parents=True, exist_ok=True)
    store = IntakeStore(args.database)
    store.close()
    defaults = json.loads(Path(args.sources_file).read_text(encoding='utf-8')) if args.sources_file else []
    if defaults:
        defaults = validate_sources(defaults)
    service = Service(args.database, default_sources=defaults, profile_path=args.profile)
    service.set_schedule(service.saved_schedule() if args.scan_every_minutes is None else args.scan_every_minutes)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(service))
    print(f'Job dashboard: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop_schedule()
        server.server_close()


if __name__ == '__main__':
    main()
