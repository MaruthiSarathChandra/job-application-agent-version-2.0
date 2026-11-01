"""Explicit, local full-JD enrichment for market-search excerpts.

No automated scraping or model calls. A user must paste and confirm the complete
employer description before an excerpt can enter the resume pipeline.
"""
import json
import re
import uuid

from .intake import detect_ats, normalize_url, qualify


def enrich_job(service, body):
    if not isinstance(body, dict) or set(body) - {'job_id', 'description', 'source_url', 'confirmed_complete'}:
        raise ValueError('Unsupported enrichment fields')
    job_id = body.get('job_id')
    description = body.get('description')
    source_url = body.get('source_url', '')
    if not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{24}', job_id):
        raise ValueError('Select an existing job')
    if not isinstance(description, str) or not 300 <= len(description.strip()) <= 80_000:
        raise ValueError('Paste the complete job description (300–80,000 characters)')
    if body.get('confirmed_complete') is not True:
        raise ValueError('Confirm that the pasted text is the complete employer job description')
    if not isinstance(source_url, str) or len(source_url) > 2048:
        raise ValueError('Employer posting URL must be a short HTTP(S) URL')
    source_url = normalize_url(source_url) if source_url.strip() else ''

    with service.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('Job no longer exists')
        old = json.loads(row['payload'])
        if old.get('description_completeness') != 'excerpt' or old.get('qualification', {}).get('status') != 'needs_full_jd':
            raise ValueError('Only pending search excerpts can be enriched')
        rules, run_id = old.get('qualification_rules'), old.get('intake_run_id')
        if not isinstance(rules, dict) or not isinstance(run_id, str) or not db.execute(
                'SELECT 1 FROM discovery_runs WHERE id=?', (run_id,)).fetchone():
            raise ValueError('This lead predates saved qualification rules. Re-run discovery first.')

        job = dict(old)
        job.update(description=description.strip(), description_completeness='user_supplied_full',
                   description_provenance='user_pasted_unverified')
        if source_url:
            job['discovery_url'] = old.get('discovery_url', old['url'])
            job['url'] = source_url
            job['description_source_url'] = source_url
        decision = qualify(job, rules)
        job['qualification'] = decision
        db.execute('''UPDATE jobs SET url=?,ats=?,payload=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                   (job['url'], detect_ats(job['url']), json.dumps(job), job_id))
        db.execute('INSERT INTO intake_attempts(job_id,result) VALUES(?,?)',
                   (job_id, json.dumps(decision)))

        queued = False
        queue_order = None
        if decision['status'] == 'matched':
            cursor = db.execute('INSERT OR IGNORE INTO resume_queue(job_id,run_id,snapshot) VALUES(?,?,?)',
                                (job_id, run_id, json.dumps(job)))
            queued = bool(cursor.rowcount)
            existing = db.execute('SELECT sequence FROM resume_queue WHERE job_id=?', (job_id,)).fetchone()
            queue_order = existing['sequence']
            if queued:
                event = {'schema_version': 1, 'event_id': str(uuid.uuid4()),
                         'correlation_id': job_id, 'queue_order': queue_order,
                         'job_id': job_id, 'job': job, 'status': 'resume_waiting'}
                db.execute('INSERT INTO outbox(event_key,topic,payload) VALUES(?,?,?)',
                           ('resume.requested:' + job_id, 'resume.requested.v1', json.dumps(event)))
        message = (f'#{queue_order}: manually supplied full JD queued for resume'
                   if queued else f'{job["title"]}: JD supplied; {decision["status"]}')
        db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',
                   (run_id, 'queued' if queued else decision['status'], message))
        return {'job_id': job_id, 'qualification': decision, 'queued': queued,
                'queue_order': queue_order, 'provenance': 'user_pasted_unverified'}
