"""Agent 3 preflight consumer: prepare approved applications, never submit silently.

This worker deliberately does not attempt to impersonate an ATS, click Submit,
or infer sensitive application answers. Provider-specific browser adapters and
reconciliation must be added before any live submission can be claimed.
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

from .intake import detect_ats

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
CLAIM_SECONDS = 300


class Agent3:
    def __init__(self, database):
        self.database = str(database)
        if not Path(database).exists():
            raise ValueError('Agent database does not exist. Run discovery and Agent 2 first.')
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_queue'").fetchone():
                raise ValueError('Agent 2 has not created the application queue. Generate a resume first.')
            db.executescript(SCHEMA)
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
        """Queue only approved events; safely reset preflight after a new revision."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("""SELECT a.sequence,o.payload FROM application_queue a
                JOIN outbox o ON o.event_key='applications.ready:'||a.job_id
                WHERE a.status='ready_for_application'
                  AND o.topic='applications.ready.v1'""").fetchall()
            for row in rows:
                try:
                    event_id = json.loads(row['payload'])['event_id']
                except (ValueError, TypeError, KeyError):
                    continue
                if not isinstance(event_id, str) or not event_id:
                    continue
                db.execute('INSERT OR IGNORE INTO agent3_tasks(application_order,ready_event_id) VALUES(?,?)',
                           (row['sequence'], event_id))
                # A new, separately approved resume revision must get a new
                # preflight. Never reset a submitted or uncertain application.
                db.execute("""UPDATE agent3_tasks SET status='queued',attempts=0,
                    ready_event_id=?,plan=NULL,last_error=NULL,worker_token=NULL,
                    lease_until=NULL,next_attempt_at=0,updated_at=CURRENT_TIMESTAMP
                    WHERE application_order=? AND ready_event_id<>?
                    AND status NOT IN ('submitted','submission_unknown')""",
                    (event_id, row['sequence'], event_id))
            # A withdrawn, regenerating or unapproved resume cannot retain an
            # active browser-preflight plan.
            db.execute("""UPDATE agent3_tasks SET status='blocked',
                last_error='Resume handoff is no longer approved',worker_token=NULL,lease_until=NULL,
                updated_at=CURRENT_TIMESTAMP
                WHERE status IN ('queued','checking','error','awaiting_browser_adapter')
                  AND application_order IN
                (SELECT t.application_order FROM agent3_tasks t
                 JOIN application_queue a ON a.sequence=t.application_order
                 WHERE a.status<>'ready_for_application')""")

    def status(self):
        self.sync()
        with self.connect() as db:
            return [dict(row) for row in db.execute('''
                SELECT t.application_order,t.status,t.attempts,t.last_error,
                    t.plan,a.job_id,a.resume_artifact_id
                FROM agent3_tasks t JOIN application_queue a ON a.sequence=t.application_order
                ORDER BY t.application_order''')]

    def claim(self):
        self.sync()
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""UPDATE agent3_tasks SET status='error',
                last_error='Preflight worker lease expired; retry budget exhausted',
                worker_token=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE status='checking' AND lease_until<? AND attempts>=?""",
                (now, MAX_ATTEMPTS))
            row = db.execute('''SELECT t.*,a.job_id,a.payload,a.status AS application_status,
                a.resume_artifact_id,o.payload AS ready_payload, q.run_id,
                r.path AS resume_path,r.sha256 AS stored_sha,r.profile_version AS stored_profile_version
                FROM agent3_tasks t JOIN application_queue a ON a.sequence=t.application_order
                JOIN outbox o ON o.event_key='applications.ready:'||a.job_id
                    AND o.topic='applications.ready.v1'
                JOIN resume_artifacts r ON r.id=a.resume_artifact_id
                JOIN resume_queue q ON q.job_id=a.job_id
                WHERE a.status='ready_for_application' AND t.attempts<?
                  AND ((t.status IN ('queued','error') AND t.next_attempt_at<=?)
                    OR (t.status='checking' AND t.lease_until<?))
                ORDER BY t.application_order LIMIT 1''', (MAX_ATTEMPTS, now, now)).fetchone()
            if not row:
                return None
            token = str(uuid.uuid4())
            db.execute("""UPDATE agent3_tasks SET status='checking',attempts=attempts+1,
                worker_token=?,lease_until=?,updated_at=CURRENT_TIMESTAMP
                WHERE application_order=?""", (token, now + CLAIM_SECONDS, row['application_order']))
            return {**dict(row), 'worker_token': token}

    @staticmethod
    def preflight(task):
        try:
            event = json.loads(task['payload'])
            ready = json.loads(task['ready_payload'])
        except (ValueError, TypeError):
            raise ValueError('Application handoff payload is invalid') from None
        if (event != ready or event.get('schema_version') != 1
                or event.get('job_id') != task['job_id']
                or event.get('application_queue_order') != task['application_order']):
            raise ValueError('Application handoff and approved queue snapshot disagree')
        resume = event.get('resume') or {}
        approval = event.get('review') or {}
        if (approval.get('status') != 'approved'
                or approval.get('artifact_id') != task['resume_artifact_id']
                or resume.get('artifact_id') != task['resume_artifact_id']
                or resume.get('sha256') != task['stored_sha']
                or event.get('profile_version') != task['stored_profile_version']):
            raise ValueError('Exact artifact approval, checksum or profile version missing')
        job = event.get('job') or {}
        if (job.get('qualification') or {}).get('status') != 'matched' or job.get('description_completeness') == 'excerpt':
            raise ValueError('Job lacks a full qualified description')
        url = job.get('url')
        parts = urlsplit(url if isinstance(url, str) else '')
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            raise ValueError('Application requires a secure HTTPS employer URL')
        if not isinstance(job.get('title'), str) or not isinstance(job.get('company'), str):
            raise ValueError('Application snapshot is incomplete')
        try:
            path = Path(task['resume_path'])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != task['stored_sha']:
                raise ValueError('Saved resume is missing or its checksum changed')
        except OSError:
            raise ValueError('Saved resume cannot be read or verified') from None
        return {'schema_version': 1, 'application_order': task['application_order'],
                'job_id': task['job_id'], 'job_url': url, 'ats': detect_ats(url),
                'job_title': job['title'], 'company': job['company'],
                'resume_artifact_id': task['resume_artifact_id'],
                'resume_sha256': task['stored_sha'],
                'profile_version': event['profile_version'],
                'next_step': 'browser_adapter_required',
                'submission_performed': False,
                'otp_policy': 'gmail_read_only_verified_sender_on_demand',
                'requires_factual_answers': True}

    def process_one(self):
        task = self.claim()
        if not task:
            return False
        try:
            plan = self.preflight(task)
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                changed = db.execute("""UPDATE agent3_tasks
                    SET status='awaiting_browser_adapter',plan=?,last_error=NULL,
                        worker_token=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP
                    WHERE application_order=? AND worker_token=? AND status='checking'""",
                    (json.dumps(plan),task['application_order'],task['worker_token']))
                if changed.rowcount != 1:
                    raise ValueError('Agent 3 lease lost; stale worker result discarded')
                db.execute('''INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)''',
                           (task['run_id'], 'agent3_preflight',
                            f"Application #{task['application_order']}: resume verified; ATS browser integration needed"))
        except Exception as exc:
            # Do not store credential-bearing exception messages in durable state.
            message = str(exc) if isinstance(exc, ValueError) else 'Agent 3 preflight failed'
            with self.connect() as db:
                db.execute("""UPDATE agent3_tasks SET status='error',last_error=?,
                    worker_token=NULL,lease_until=NULL,next_attempt_at=?,updated_at=CURRENT_TIMESTAMP
                    WHERE application_order=? AND worker_token=? AND status='checking'""",
                    (message,time.time()+30*(task['attempts']+1),
                     task['application_order'],task['worker_token']))
        return True


def main():
    parser = argparse.ArgumentParser(description='Agent 3: verify approved application handoffs; does not submit')
    parser.add_argument('--database', default='data/agent.db')
    parser.add_argument('--limit', type=int, default=1)
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    try:
        if not 1 <= args.limit <= 100:
            raise ValueError('Limit must be 1–100')
        agent = Agent3(args.database)
        if not args.status:
            for _ in range(args.limit):
                if not agent.process_one():
                    break
        print(json.dumps(agent.status(), indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'Agent 3 error: {exc}\n')


if __name__ == '__main__':
    main()
