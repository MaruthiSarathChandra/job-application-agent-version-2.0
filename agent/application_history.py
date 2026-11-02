"""Agent 3/4 integration contracts. Only confirmed submissions enter applied history.

These functions record real worker outcomes; they do not create accounts or submit forms.
"""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime,timezone,timedelta

SCHEMA='''CREATE TABLE IF NOT EXISTS applied_history(
 job_id TEXT PRIMARY KEY,artifact_id TEXT NOT NULL,submission_id TEXT NOT NULL UNIQUE,
 submitted_at TEXT NOT NULL,confirmation_url TEXT NOT NULL,payload TEXT NOT NULL,
 recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);'''


def _application(db,body):
    job_id,artifact_id=body.get('job_id'),body.get('artifact_id')
    if not isinstance(job_id,str) or not isinstance(artifact_id,str):
        raise ValueError('Specify the exact job_id and artifact_id used')
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='application_queue'").fetchone():
        raise ValueError('No completed resume bundles exist yet')
    app=db.execute('SELECT * FROM application_queue WHERE job_id=?',(job_id,)).fetchone()
    if not app or app['resume_artifact_id']!=artifact_id:
        raise ValueError('Job or resume revision does not match the application bundle')
    artifact=db.execute('SELECT path,sha256 FROM resume_artifacts WHERE id=?',(artifact_id,)).fetchone()
    if not artifact or hashlib.sha256(Path(artifact['path']).read_bytes()).hexdigest()!=artifact['sha256']:
        raise ValueError('Submitted resume file is missing or corrupt')
    return app


def prepared(owner,body):
    if body.get('prepared') is not True:
        raise ValueError('Agent 3 must confirm application preparation completed')
    with owner.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        app=_application(db,body)
        if app['status']=='ready_for_submission':return {'status':'ready_for_submission'}
        if app['status']!='ready_for_application':raise ValueError('Application is not ready for Agent 3')
        payload=json.loads(app['payload']);payload['status']='ready_for_submission'
        db.execute("UPDATE application_queue SET status='ready_for_submission',payload=? WHERE job_id=?",(json.dumps(payload),app['job_id']))
        db.execute("UPDATE outbox SET published_at=CURRENT_TIMESTAMP WHERE event_key=?",('applications.ready:'+app['job_id'],))
        db.execute('INSERT INTO outbox(event_key,topic,payload) VALUES(?,?,?)',('applications.prepared:'+app['job_id'],'applications.prepared.v1',json.dumps(payload)))
        db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',('', 'application_prepared',app['job_id']+': Agent 3 confirmed preparation; waiting for Agent 4 submission'))
    return {'status':'ready_for_submission'}


def submitted(owner,body):
    if body.get('submitted') is not True or body.get('confirmed') is not True:
        raise ValueError('Only a confirmed successful submission can enter applied history')
    receipt=body.get('submission_id');url=body.get('confirmation_url');stamp=body.get('submitted_at')
    if not isinstance(receipt,str) or not 1<=len(receipt.strip())<=200:
        raise ValueError('A nonempty submission receipt/reference is required')
    if not isinstance(url,str) or len(url)>2048:
        raise ValueError('A confirmation URL is required')
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Confirmation URL must use HTTPS without credentials')
    from .freshness import parse_published_at
    instant=parse_published_at(stamp)
    if instant is None or instant>datetime.now(timezone.utc)+timedelta(minutes=5):
        raise ValueError('Use an actual submission timestamp with timezone')
    with owner.connect() as db:
        db.executescript(SCHEMA)
        db.execute('BEGIN IMMEDIATE')
        app=_application(db,body)
        prior=db.execute('SELECT * FROM applied_history WHERE job_id=?',(app['job_id'],)).fetchone()
        if prior:
            if (prior['submission_id'],prior['artifact_id'],prior['submitted_at'],prior['confirmation_url'])!=(receipt,app['resume_artifact_id'],stamp,url):
                raise ValueError('Conflicting submission receipt; existing history preserved')
            return {'status':'submitted','duplicate':True}
        if app['status']!='ready_for_submission':raise ValueError('Agent 3 must prepare this exact bundle before Agent 4 records submission')
        payload=json.loads(app['payload'])
        payload.update(status='submitted',submission={'id':receipt,'submitted_at':stamp,'confirmation_url':url,'confirmed_by':'agent4'})
        encoded=json.dumps(payload)
        if db.execute('SELECT 1 FROM applied_history WHERE submission_id=?',(receipt,)).fetchone():raise ValueError('Submission receipt already belongs to another job')
        db.execute('INSERT INTO applied_history(job_id,artifact_id,submission_id,submitted_at,confirmation_url,payload) VALUES(?,?,?,?,?,?)',
                   (app['job_id'],app['resume_artifact_id'],receipt,stamp,url,encoded))
        db.execute("UPDATE application_queue SET status='submitted',payload=? WHERE job_id=?",(encoded,app['job_id']))
        db.execute("UPDATE outbox SET published_at=CURRENT_TIMESTAMP WHERE event_key=?",('applications.prepared:'+app['job_id'],))
        db.execute('INSERT INTO outbox(event_key,topic,payload) VALUES(?,?,?)',('applications.submitted:'+app['job_id'],'applications.submitted.v1',encoded))
        db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',('', 'application_submitted',app['job_id']+': Agent 4 reported confirmed submission; resume archived'))
    return {'status':'submitted','duplicate':False}
