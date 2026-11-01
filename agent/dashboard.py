"""Local dashboard and durable job-discovery producer. Resume consumer is separate."""
import argparse
from contextlib import contextmanager
import json
import re
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .intake import IntakeStore
from .sources import fetch_board, validate_sources

SCHEMA = '''
CREATE TABLE IF NOT EXISTS discovery_runs(
 id TEXT PRIMARY KEY, prompt TEXT NOT NULL, config TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
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
    return prompt.strip(), rules, validate_sources(body.get('sources'))


class Service:
    def __init__(self, database, fetcher=fetch_board):
        self.database = database
        self.fetcher = fetcher
        self.lock = threading.Lock()
        store = IntakeStore(database)
        store.close()
        with self.connect() as db:
            db.executescript(SCHEMA)
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

    def event(self, run_id, kind, message):
        with self.connect() as db:
            db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)', (run_id, kind, message))

    def start(self, body, background=True):
        prompt, rules, sources = rules_from_request(body)
        if not self.lock.acquire(blocking=False):
            raise ValueError('A discovery run is already active')
        run_id = str(uuid.uuid4())
        try:
            with self.connect() as db:
                db.execute('INSERT INTO discovery_runs(id,prompt,config,status) VALUES(?,?,?,?)',
                           (run_id, prompt, json.dumps({'rules': rules, 'sources': sources}), 'running'))
            if background:
                threading.Thread(target=self.run, args=(run_id, rules, sources), daemon=True).start()
            else:
                self.run(run_id, rules, sources)
        except Exception:
            if self.lock.locked():
                self.lock.release()
            raise
        return run_id

    def run(self, run_id, rules, sources):
        store = None
        errors = 0
        try:
            store = IntakeStore(self.database)
            for source in sources:
                self.event(run_id, 'fetching', f'Fetching {source}')
                try:
                    jobs = self.fetcher(source)
                    self.event(run_id, 'fetched', f'{source}: fetched {len(jobs)} postings')
                    for job in jobs:
                        try:
                            self.event(run_id, 'reading', f"Reading JD: {job.get('title', 'invalid posting')}")
                            result = store.import_job(job, rules)
                            reasons = result['qualification']['reasons']
                            if reasons:
                                self.event(run_id, 'excluded', f"{job['title']}: {'; '.join(reasons)}")
                                continue
                            with self.connect() as db:
                                cursor = db.execute('INSERT OR IGNORE INTO resume_queue(job_id,run_id,snapshot) VALUES(?,?,?)',
                                                    (result['job_id'], run_id, json.dumps(result)))
                                if cursor.rowcount:
                                    number = cursor.lastrowid
                                    payload = {'schema_version': 1, 'queue_order': number, 'job_id': result['job_id'],
                                               'job': result, 'status': 'resume_waiting'}
                                    db.execute('INSERT INTO outbox(event_key,topic,payload) VALUES(?,?,?)',
                                               ('resume.requested:' + result['job_id'], 'resume.requested.v1', json.dumps(payload)))
                                    message = f"#{number} · {job['title']} → waiting for resume"
                                else:
                                    message = f"{job['title']}: already queued; duplicate skipped"
                            self.event(run_id, 'queued', message)
                        except (ValueError, TypeError, KeyError) as exc:
                            errors += 1
                            self.event(run_id, 'error', str(exc))
                except Exception as exc:
                    errors += 1
                    self.event(run_id, 'error', f'{source}: {type(exc).__name__}: {exc}')
            status = 'completed_with_errors' if errors else 'completed'
            self.event(run_id, 'finished', 'Discovery finished. Resume consumer has not started.')
        except Exception as exc:
            status = 'failed'
            self.event(run_id, 'error', f'{type(exc).__name__}: {exc}')
        finally:
            if store:
                store.close()
            try:
                with self.connect() as db:
                    db.execute('UPDATE discovery_runs SET status=? WHERE id=?', (locals().get('status', 'failed'), run_id))
            finally:
                self.lock.release()

    def snapshot(self):
        with self.connect() as db:
            return {
                'runs': [dict(x) for x in db.execute('SELECT * FROM discovery_runs ORDER BY created_at DESC,rowid DESC LIMIT 20')],
                'activity': [dict(x) for x in db.execute('SELECT * FROM activity ORDER BY id DESC LIMIT 200')],
                'jobs': [dict(id=x['id'], **json.loads(x['payload'])) for x in db.execute('SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 500')],
                'queue': [dict(x) for x in db.execute('SELECT * FROM resume_queue ORDER BY sequence')],
            }


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, value, content_type='application/json'):
            payload = value.encode() if isinstance(value, str) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == '/':
                self.reply(200, Path(__file__).with_name('web').joinpath('index.html').read_text(encoding='utf-8'), 'text/html')
            elif self.path == '/api/state':
                self.reply(200, service.snapshot())
            else:
                self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            # No cross-origin writes to the local agent.
            origin = self.headers.get('Origin')
            host = self.headers.get('Host', '')
            if host != f'127.0.0.1:{self.server.server_port}' or (origin and origin != f'http://{host}'):
                self.reply(403, {'error': 'Only same-origin local requests are allowed'})
                return
            if self.path != '/api/runs':
                self.reply(404, {'error': 'Not found'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError('Request must be 1–16,384 bytes')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Expected a JSON object')
                self.reply(202, {'run_id': service.start(body)})
            except (ValueError, TypeError) as exc:
                self.reply(400, {'error': str(exc)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Local job discovery dashboard')
    parser.add_argument('--database', default='data/agent.db')
    parser.add_argument('--port', default=8765, type=int)
    args = parser.parse_args()
    Path(args.database).parent.mkdir(parents=True, exist_ok=True)
    store = IntakeStore(args.database)
    store.close()
    service = Service(args.database)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(service))
    print(f'Job dashboard: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
