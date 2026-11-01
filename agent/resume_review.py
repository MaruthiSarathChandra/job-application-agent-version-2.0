"""Approve the exact reviewed artifact before downstream handoff."""
import hashlib
import json
from pathlib import Path

def approve(service, sequence, artifact_id):
    with service.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("""SELECT a.*,q.run_id FROM application_queue a JOIN resume_queue q ON q.job_id=a.job_id
                          WHERE q.sequence=?""",(sequence,)).fetchone()
        if not row or row['status']!='review_required' or row['resume_artifact_id']!=artifact_id:
            raise ValueError('Resume changed or is not awaiting review. Open the current resume before approving.')
        artifact=db.execute('SELECT path,sha256 FROM resume_artifacts WHERE id=?',(artifact_id,)).fetchone()
        if hashlib.sha256(Path(artifact['path']).read_bytes()).hexdigest()!=artifact['sha256']:
            raise ValueError('Stored resume checksum failed')
        payload=json.loads(row['payload'])
        payload['status']='ready_for_application'
        payload['review']={'status':'approved','artifact_id':artifact_id}
        encoded=json.dumps(payload)
        db.execute("UPDATE application_queue SET status='ready_for_application',payload=? WHERE sequence=?",(encoded,row['sequence']))
        db.execute('INSERT INTO outbox(event_key,topic,payload) VALUES(?,?,?)',
                   ('applications.ready:'+row['job_id'],'applications.ready.v1',encoded))
        db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',
                   (row['run_id'],'resume_approved',f'#{sequence}: reviewed resume approved for Agent 3 queue'))
    return {'status':'approved'}
