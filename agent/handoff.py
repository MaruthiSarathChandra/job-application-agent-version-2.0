"""Atomic, idempotent resume/JD handoff. Queue readiness is not human approval."""
import hashlib
import json
from pathlib import Path


def current_decision(owner, job):
    from .intake import qualify
    with owner.connect() as db:
        columns={r[1] for r in db.execute("PRAGMA table_info(discovery_runs)")}
        where="WHERE trigger='manual'" if "trigger" in columns else ""
        row=db.execute("SELECT config FROM discovery_runs "+where+" ORDER BY rowid DESC LIMIT 1").fetchone()
    rules=json.loads(row['config'])['rules'] if row else job.get('qualification_rules',{})
    if rules.get('min_match_percent'):
        from .matching import profile_skills
        profile=getattr(owner,'profile',None)
        if profile is None and getattr(owner,'profile_path',None):
            from .resume_worker import load_profile
            profile,_=load_profile(owner.profile_path)
        if profile:rules={**rules,'profile_skills':profile_skills(profile)}
    return qualify(job,rules)


def promote_existing(owner):
    """Recover checked artifacts without a model call; preserve old/failed records."""
    with owner.connect() as db:
        rows=[dict(r) for r in db.execute('''SELECT a.*,q.sequence AS queue_order,q.snapshot,q.run_id,
            f.path,f.sha256 FROM application_queue a JOIN resume_queue q ON a.job_id=q.job_id
            JOIN resume_artifacts f ON f.id=a.resume_artifact_id WHERE a.status='review_required' ''')]
    count=0
    for row in rows:
        job=json.loads(row['snapshot'])
        decision=current_decision(owner,job)
        if decision['status']!='matched':
            with owner.connect() as db:
                db.execute("UPDATE application_queue SET status='requalification_required' WHERE sequence=? AND status='review_required'",(row['sequence'],))
                db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',(row['run_id'],'handoff_blocked',f"#{row['queue_order']}: existing resume retained; job no longer matches current filters"))
            continue
        try:
            if hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()!=row['sha256']:
                raise ValueError('Artifact checksum failed')
        except (OSError,ValueError):
            with owner.connect() as db:
                db.execute("UPDATE application_queue SET status='artifact_error' WHERE sequence=? AND status='review_required'",(row['sequence'],))
                db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',(row['run_id'],'handoff_error',f"#{row['queue_order']}: artifact missing or corrupt; no handoff"))
            continue
        with owner.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            payload=json.loads(row['payload'])
            payload.update(status='ready_for_application',handoff_mode='automatic',review_status='not_reviewed',job_url=job['url'])
            changed=db.execute("UPDATE application_queue SET status='ready_for_application',payload=? WHERE sequence=? AND status='review_required' AND resume_artifact_id=?",(json.dumps(payload),row['sequence'],row['resume_artifact_id']))
            if not changed.rowcount: continue
            db.execute('INSERT OR IGNORE INTO outbox(event_key,topic,payload) VALUES(?,?,?)',('applications.ready:'+row['job_id'],'applications.ready.v1',json.dumps(payload)))
            db.execute("UPDATE resume_queue SET status='resume_ready' WHERE sequence=?",(row['queue_order'],))
            db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',(row['run_id'],'handoff_complete',f"#{row['queue_order']}: existing resume + JD + job link sent to Agent 3 queue"))
            count+=1
    return count
