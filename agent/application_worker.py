"""Agent 3: continuously verify application-ready resume bundles.

This is *not* a form submission agent. Its durable output is a verified,
resume-specific plan for a browser adapter. It never marks an application
prepared or submitted without a separate actual employer interaction.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

SCHEMA = '''
CREATE TABLE IF NOT EXISTS agent3_tasks (
 application_order INTEGER PRIMARY KEY REFERENCES application_queue(sequence),
 ready_event_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0,
 worker_token TEXT, lease_until REAL,
 next_attempt_at REAL NOT NULL DEFAULT 0,
 last_error TEXT, plan TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
'''
MAX_ATTEMPTS = 3
LEASE_SECONDS = 300


class Agent3:
    def __init__(self, database):
        self.database = str(database)
        # Create only missing local tables, never reset historical jobs/resumes.
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        from .intake import IntakeStore
        from .dashboard import SCHEMA as DASHBOARD_SCHEMA
        from .resume_artifact import SCHEMA as ARTIFACT_SCHEMA
        store=IntakeStore(self.database)
        store.close()
        with self.connect() as db:
            db.executescript(DASHBOARD_SCHEMA + ARTIFACT_SCHEMA + SCHEMA)
        self.sync()

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

    def sync(self):
        """Create exactly one preflight task per application queue position."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("""SELECT a.sequence,o.payload
                FROM application_queue a JOIN outbox o
                ON o.event_key='applications.ready:'||a.job_id
                WHERE a.status='ready_for_application'
                AND o.topic='applications.ready.v1'""").fetchall()
            for row in rows:
                try:
                    event = json.loads(row['payload'])
                    event_id = event['event_id']
                except (ValueError, TypeError, KeyError):
                    continue
                if not isinstance(event_id, str) or not event_id:
                    continue
                db.execute("""INSERT OR IGNORE INTO agent3_tasks(application_order,ready_event_id)
                              VALUES(?,?)""", (row['sequence'], event_id))
                # A new resume revision requires a new preflight; never reset
                # an already prepared/submitted or uncertain external action.
                db.execute("""UPDATE agent3_tasks SET ready_event_id=?,status='queued',
                    attempts=0,worker_token=NULL,lease_until=NULL,next_attempt_at=0,
                    last_error=NULL,plan=NULL,updated_at=CURRENT_TIMESTAMP
                    WHERE application_order=? AND ready_event_id<>?
                      AND status IN ('queued','checking','error','blocked','awaiting_browser_adapter')""",
                    (event_id,row['sequence'],event_id))
            db.execute("""UPDATE agent3_tasks SET status='blocked',
                last_error='Resume handoff is no longer application-ready',
                worker_token=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE status IN ('queued','checking','error','awaiting_browser_adapter')
                  AND EXISTS(SELECT 1 FROM application_queue a
                             WHERE a.sequence=application_order
                               AND a.status<>'ready_for_application')""")

    def status(self):
        self.sync()
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT t.application_order,t.status,
                t.attempts,t.last_error,t.plan,a.job_id,a.resume_artifact_id
                FROM agent3_tasks t JOIN application_queue a
                ON a.sequence=t.application_order ORDER BY t.application_order""")]

    def claim(self):
        self.sync()
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""UPDATE agent3_tasks SET status='error',
                last_error='Preflight lease expired; retry limit reached',
                worker_token=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE status='checking' AND lease_until<? AND attempts>=?""",
                (now,MAX_ATTEMPTS))
            row=db.execute("""SELECT t.*,a.job_id,a.payload,a.resume_artifact_id,
                o.payload AS ready_payload,q.run_id,r.path AS resume_path,
                r.sha256 AS stored_sha,r.profile_version AS stored_profile_version
                FROM agent3_tasks t JOIN application_queue a
                ON a.sequence=t.application_order
                JOIN outbox o ON o.event_key='applications.ready:'||a.job_id
                           AND o.topic='applications.ready.v1'
                JOIN resume_queue q ON q.job_id=a.job_id
                JOIN resume_artifacts r ON r.id=a.resume_artifact_id
                WHERE a.status='ready_for_application' AND t.attempts<?
                AND ((t.status IN ('queued','error') AND t.next_attempt_at<=?)
                  OR (t.status='checking' AND t.lease_until<?))
                ORDER BY t.application_order LIMIT 1""",
                (MAX_ATTEMPTS,now,now)).fetchone()
            if not row:return None
            token=str(uuid.uuid4())
            db.execute("""UPDATE agent3_tasks SET status='checking',
                attempts=attempts+1,worker_token=?,lease_until=?,
                updated_at=CURRENT_TIMESTAMP WHERE application_order=?""",
                (token,now+LEASE_SECONDS,row['application_order']))
            return {**dict(row),'worker_token':token}

    @staticmethod
    def preflight(task):
        try:
            event=json.loads(task['payload'])
            ready=json.loads(task['ready_payload'])
        except (ValueError,TypeError):
            raise ValueError('Application handoff JSON is invalid') from None
        if (event!=ready or event.get('schema_version')!=1
                or event.get('job_id')!=task['job_id']
                or event.get('application_queue_order')!=task['application_order']):
            raise ValueError('Ready event does not match this exact application bundle')
        artifact=event.get('resume') or {}
        if (artifact.get('artifact_id')!=task['resume_artifact_id']
                or artifact.get('sha256')!=task['stored_sha']
                or event.get('profile_version')!=task['stored_profile_version']):
            raise ValueError('Resume ID, SHA-256, or verified profile version disagree')
        mode=event.get('handoff_mode')
        review=event.get('review') or {}
        explicit=review.get('status')=='approved' and review.get('artifact_id')==task['resume_artifact_id']
        automatic=mode=='automatic' and event.get('review_status')=='not_reviewed'
        if not (explicit or automatic):
            raise ValueError('Resume is not approved or explicitly configured for automatic handoff')
        job=event.get('job') or {}
        if ((job.get('qualification') or {}).get('status')!='matched'
                or job.get('description_completeness')=='excerpt'):
            raise ValueError('Full qualified JD not present')
        url=job.get('url')
        parts=urlsplit(url if isinstance(url,str) else '')
        if parts.scheme!='https' or not parts.hostname or parts.username or parts.password:
            raise ValueError('Only HTTPS employer job URLs are supported')
        if not all(isinstance(job.get(k),str) and job[k].strip() for k in ('title','company','description')):
            raise ValueError('Job title, company and full JD are required')
        try:
            file=Path(task['resume_path'])
            if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=task['stored_sha']:
                raise ValueError('Saved resume is missing or checksum does not match')
        except OSError:
            raise ValueError('Saved resume file cannot be read') from None
        from .intake import detect_ats
        return {'schema_version':1,'application_order':task['application_order'],
                'job_id':task['job_id'],'job_url':url,'job_title':job['title'],
                'company':job['company'],'ats':detect_ats(url),
                'resume_artifact_id':task['resume_artifact_id'],
                'resume_sha256':task['stored_sha'],
                'profile_version':event['profile_version'],
                'handoff_mode':'approved' if explicit else 'automatic_unreviewed',
                'submission_performed':False,'prepared':False,
                'next_step':'employer_browser_interaction_required'}

    def process_one(self):
        task=self.claim()
        if not task:return False
        try:
            plan=self.preflight(task)
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                changed=db.execute("""UPDATE agent3_tasks
                    SET status='awaiting_browser_adapter',plan=?,last_error=NULL,
                    worker_token=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP
                    WHERE application_order=? AND worker_token=? AND status='checking'""",
                    (json.dumps(plan),task['application_order'],task['worker_token']))
                if changed.rowcount!=1:
                    raise ValueError('Stale worker lease; preflight discarded')
                db.execute("""INSERT INTO activity(run_id,kind,message)
                              VALUES(?,?,?)""",
                           (task['run_id'],'agent3_preflight',
                            f"Application #{task['application_order']}: verified DOCX and JD; browser interaction required"))
        except Exception as exc:
            msg=str(exc) if isinstance(exc,ValueError) else 'Preflight failed; check local task details'
            with self.connect() as db:
                db.execute("""UPDATE agent3_tasks SET status='error',
                    last_error=?,worker_token=NULL,lease_until=NULL,
                    next_attempt_at=?,updated_at=CURRENT_TIMESTAMP
                    WHERE application_order=? AND worker_token=? AND status='checking'""",
                    (msg,time.time()+30*(task['attempts']+1),
                     task['application_order'],task['worker_token']))
        return True


def main():
    parser=argparse.ArgumentParser(description='Agent 3 verified application preflight; no external submissions')
    parser.add_argument('--database',default='data/agent.db')
    parser.add_argument('--watch',action='store_true',help='Keep consuming new approved or auto-handoff bundles')
    parser.add_argument('--limit',type=int,default=None,help='Task-attempt budget; watch without limit is continuous')
    parser.add_argument('--status',action='store_true')
    args=parser.parse_args()
    try:
        if args.limit is not None and not 1<=args.limit<=10000:
            raise ValueError('Limit must be 1–10000')
        worker=Agent3(args.database)
        if args.status:
            print(json.dumps(worker.status(),indent=2))
            return
        count=0
        while args.limit is None or count<args.limit:
            if worker.process_one():
                count+=1
                print(f'Agent 3 preflight processed: {count}',flush=True)
            elif args.watch:
                time.sleep(2)
            else:
                break
        if not args.watch:
            print(json.dumps(worker.status(),indent=2))
    except (ValueError,OSError,sqlite3.Error) as exc:
        parser.exit(1,f'Agent 3 error: {exc}\n')
    except KeyboardInterrupt:
        return


if __name__=='__main__':
    main()
