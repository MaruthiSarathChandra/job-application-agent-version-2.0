"""Durable replacement requests; retain the previous artifact until replacement succeeds."""
import json
import time
import uuid
from .resume_worker import call_gpt, validate_result
from .resume_artifact import save_artifact, TEMPLATE_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS resume_regenerations(
 id TEXT PRIMARY KEY, queue_order INTEGER NOT NULL, profile TEXT NOT NULL,
 profile_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 token TEXT, lease_until REAL, error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_regeneration
 ON resume_regenerations(queue_order) WHERE status IN ('queued','processing');
CREATE TABLE IF NOT EXISTS resume_revision_history(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
 draft TEXT NOT NULL, application_payload TEXT NOT NULL,
 ready_event TEXT, replaced_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

def enqueue(service, sequence):
    from .resume_worker import validate_profile
    profile, version = validate_profile(service.read_profile()['profile'])
    if '_resume_header' not in profile:
        raise ValueError('Save your complete resume profile first')
    with service.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        q = db.execute('SELECT * FROM resume_queue WHERE sequence=?',(sequence,)).fetchone()
        if not q or q['status'] != 'resume_ready':
            raise ValueError('Regeneration is available after a resume is ready')
        active = db.execute("SELECT id FROM resume_regenerations WHERE queue_order=? AND status IN ('queued','processing')",(sequence,)).fetchone()
        if active:
            return active['id']
        app = db.execute('SELECT * FROM application_queue WHERE job_id=?',(q['job_id'],)).fetchone()
        if not app or app['status'] not in ('ready_for_application','review_required'):
            raise ValueError('Resume is already being regenerated or used for an application')
        event = db.execute("SELECT published_at FROM outbox WHERE event_key=?",('applications.ready:'+q['job_id'],)).fetchone()
        if event and event['published_at']:
            raise ValueError('This resume has already been handed to a downstream consumer')
        identifier = str(uuid.uuid4())
        db.execute('INSERT INTO resume_regenerations(id,queue_order,profile,profile_version) VALUES(?,?,?,?)',
                   (identifier,sequence,json.dumps(profile),version))
        db.execute("UPDATE application_queue SET status='regenerating' WHERE job_id=?",(q['job_id'],))
        db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',
                   (q['run_id'],'resume_regeneration_queued',f'#{sequence}: regeneration queued with latest saved profile; waiting for Agent 2'))
        return identifier

class Regenerator:
    def __init__(self, service, model, artifact_dir, generator=call_gpt, auto_handoff=False):
        self.auto_handoff=auto_handoff
        self.service,self.model,self.artifact_dir,self.generator=service,model,artifact_dir,generator
        self.last_result = "idle"
        self.seen_requests = set()

    def process_one(self):
        with self.service.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT r.*,q.job_id,q.run_id,q.snapshot FROM resume_regenerations r
                JOIN resume_queue q ON q.sequence=r.queue_order
                WHERE r.status='queued' OR (r.status='processing' AND r.lease_until<?)
                ORDER BY r.created_at,r.rowid LIMIT 1""",(time.time(),)).fetchone()
            if not row:
                return False
            task = dict(row)
            if task['id'] in self.seen_requests:
                raise ValueError('Same regeneration request was claimed twice in one worker run; stopped to avoid repeated API calls. Run --status.')
            self.seen_requests.add(task['id'])
            self.last_result = 'processing request '+task['id']
            token = str(uuid.uuid4())
            db.execute("UPDATE resume_regenerations SET status='processing',token=?,lease_until=? WHERE id=?",
                       (token,time.time()+300,task['id']))
            db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',
                       (task['run_id'],'resume_regenerating',f"#{task['queue_order']}: Agent 2 is regenerating the resume"))
        try:
            profile=json.loads(task['profile'])
            job=json.loads(task['snapshot'])
            from .handoff import current_decision
            if current_decision(self.service,job)['status']!='matched':
                raise ValueError('Job no longer matches current search filters; regeneration blocked')
            from .resume_checkpoint import generate_and_validate
            draft=generate_and_validate(self.service, 'regeneration:'+task['id'], job,
                                        profile, self.model, self.generator)
            draft['_profile_snapshot']=profile
            draft.update(job_id=task['job_id'],queue_order=task['queue_order'],
                         profile_version=task['profile_version'],model=self.model,
                         schema_version=1,event_id=str(uuid.uuid4()),correlation_id=task['job_id'])
            artifact=save_artifact(draft,profile['_resume_header'],self.artifact_dir)
            with self.service.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current=db.execute("SELECT token,status FROM resume_regenerations WHERE id=?",(task['id'],)).fetchone()
                if current['token']!=token or current['status']!='processing':
                    self.last_result = 'failed: worker lease changed; result discarded'
                    return True
                app=db.execute('SELECT * FROM application_queue WHERE job_id=?',(task['job_id'],)).fetchone()
                old=db.execute('SELECT result FROM resume_drafts WHERE queue_order=?',(task['queue_order'],)).fetchone()
                event_key='applications.ready:'+task['job_id']
                event=db.execute('SELECT payload,published_at FROM outbox WHERE event_key=?',(event_key,)).fetchone()
                if app['status']!='regenerating' or (event and event['published_at']):
                    raise ValueError('Application handoff changed; replacement was not published')
                db.execute('INSERT INTO resume_revision_history(id,job_id,artifact_id,draft,application_payload,ready_event) VALUES(?,?,?,?,?,?)',
                           (task['id'],task['job_id'],app['resume_artifact_id'],old['result'],app['payload'],event['payload'] if event else None))
                db.execute('INSERT OR IGNORE INTO resume_artifacts(id,path,sha256,profile_version,template_version,validation) VALUES(?,?,?,?,?,?)',
                           (artifact['artifact_id'],artifact['path'],artifact['sha256'],task['profile_version'],TEMPLATE_VERSION,json.dumps(artifact['validation'])))
                payload={'schema_version':1,'event_id':str(uuid.uuid4()),'correlation_id':task['job_id'],
                         'job_id':task['job_id'],'profile_version':task['profile_version'],'job':job,
                         'resume':{k:v for k,v in artifact.items() if k!='path'},
                         'unsupported_requirements':draft['missing_requirements'],'eligibility_status':'not_assessed',
                         'job_url':job['url'],'handoff_mode':'automatic' if self.auto_handoff else 'review','review_status':'not_reviewed',
                         'status':'review_required' if draft.get('requires_review') and not self.auto_handoff else 'ready_for_application','application_queue_order':app['sequence'],
                         'revision_id':task['id'],'supersedes_artifact_id':app['resume_artifact_id']}
                db.execute('UPDATE resume_drafts SET profile_version=?,result=? WHERE queue_order=?',
                           (task['profile_version'],json.dumps(draft),task['queue_order']))
                db.execute("UPDATE application_queue SET resume_artifact_id=?,payload=?,status=? WHERE job_id=?",
                           (artifact['artifact_id'],json.dumps(payload),payload['status'],task['job_id']))
                # This local outbox has not been published. Archive its old value above
                # and replace it atomically with the current application revision.
                if draft.get('requires_review') and not self.auto_handoff:
                    db.execute('DELETE FROM outbox WHERE event_key=? AND published_at IS NULL',(event_key,))
                else:
                    db.execute('INSERT INTO outbox(event_key,topic,payload) VALUES(?,?,?) ON CONFLICT(event_key) DO UPDATE SET payload=excluded.payload',
                               (event_key,'applications.ready.v1',json.dumps(payload)))
                db.execute("UPDATE resume_regenerations SET status='completed',token=NULL,lease_until=NULL WHERE id=?",(task['id'],))
                db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',
                           (task['run_id'],'resume_regenerated',f"#{task['queue_order']}: regenerated resume ready to view"))
            self.last_result = 'completed request '+task['id']+'; artifact '+artifact['artifact_id'][:12]
        except Exception as exc:
            message=str(exc) if isinstance(exc,ValueError) else 'Resume regeneration failed; previous resume retained'
            self.last_result = 'failed request '+task['id']+': '+message
            with self.service.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                changed=db.execute("UPDATE resume_regenerations SET status='failed',error=?,token=NULL,lease_until=NULL WHERE id=? AND token=?",
                                   (message,task['id'],token))
                if changed.rowcount:
                    previous=db.execute('SELECT payload FROM application_queue WHERE job_id=?',(task['job_id'],)).fetchone()
                    previous_status=json.loads(previous['payload']).get('status','ready_for_application')
                    db.execute("UPDATE application_queue SET status=? WHERE job_id=? AND status='regenerating'",(previous_status,task['job_id']))
                    db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',
                               (task['run_id'],'resume_regeneration_error',message))
        return True
