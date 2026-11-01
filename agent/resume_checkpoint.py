"""Local, immutable model-output checkpoints shared by initial and replacement workers.

Checkpoints contain private professional evidence. Never expose them in public logs
or commit the database. This is a validation replay boundary, not an approval API.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

SCHEMA = '''
CREATE TABLE IF NOT EXISTS resume_generation_checkpoints(
 id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
 task_key TEXT NOT NULL, model TEXT NOT NULL, writer_version TEXT NOT NULL,
 profile TEXT NOT NULL, job TEXT NOT NULL, selection TEXT NOT NULL,
 validation_status TEXT NOT NULL DEFAULT 'pending', error TEXT,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP);
'''


def generate_and_validate(owner, task_key, job, profile, model, generator):
    from .resume_writer import VERSION
    from .resume_worker import validate_result
    # Header stays local; source evidence is retained for reproducible validation.
    evidence = {k: profile[k] for k in ('profile_id', 'facts')}
    inputs = dict(task_key=task_key, job=job, profile=evidence, model=model, writer_version=VERSION)
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    with owner.connect() as db:
        db.executescript(SCHEMA)
        row = db.execute('SELECT id,selection FROM resume_generation_checkpoints WHERE request_key=?', (key,)).fetchone()
    if row:
        identifier, selection = row['id'], json.loads(row['selection'])
    else:
        selection = generator(job, profile, model)
        identifier = str(uuid.uuid4())
        with owner.connect() as db:
            # Save BEFORE validation/rendering. A recovered task reuses this result.
            db.execute('''INSERT OR IGNORE INTO resume_generation_checkpoints
                (id,request_key,task_key,model,writer_version,profile,job,selection)
                VALUES(?,?,?,?,?,?,?,?)''',
                (identifier,key,task_key,model,VERSION,json.dumps(evidence),json.dumps(job),json.dumps(selection)))
            saved = db.execute('SELECT id,selection FROM resume_generation_checkpoints WHERE request_key=?', (key,)).fetchone()
            identifier, selection = saved['id'], json.loads(saved['selection'])
    try:
        draft = validate_result(selection, profile)
    except ValueError as exc:
        # The validator reports controlled errors, never provider credentials.
        with owner.connect() as db:
            db.execute("UPDATE resume_generation_checkpoints SET validation_status='failed',error=? WHERE id=?", (str(exc),identifier))
        raise ValueError(f'{exc}; saved checkpoint {identifier}. Inspect with python -m agent.resume_checkpoint --database <database> --id {identifier}') from None
    with owner.connect() as db:
        db.execute("UPDATE resume_generation_checkpoints SET validation_status='passed',error=NULL WHERE id=?", (identifier,))
    return draft


def inspect(database, identifier=None):
    """Read-only diagnostics/revalidation; never calls GPT, modifies queues or approves."""
    from .resume_worker import validate_result
    db = sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='resume_generation_checkpoints'").fetchone():
            return {'checkpoints': [], 'note': 'No saved responses. Older failures cannot be recovered.'}
        if identifier is None:
            return {'checkpoints':[dict(r) for r in db.execute('''SELECT id,task_key,model,writer_version,
                validation_status,error,created_at FROM resume_generation_checkpoints ORDER BY rowid DESC LIMIT 20''')]}
        row = db.execute('SELECT * FROM resume_generation_checkpoints WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('Unknown checkpoint ID')
        selection = json.loads(row['selection'])
        result = {'id':identifier, 'stored_validation_status':row['validation_status'],
                  'stored_error':row['error'], 'writer_version':row['writer_version']}
        if isinstance(selection,dict) and isinstance(selection.get('entries'),list):
            result['entries'] = [{'source_id':e.get('source_id'),
                                  'bullet_count':len(e['bullets']) if isinstance(e.get('bullets'),list) else None}
                                 for e in selection['entries'] if isinstance(e,dict)]
        try:
            validate_result(selection,json.loads(row['profile']))
            result['current_validation_status'] = 'passed'
        except ValueError as exc:
            result.update(current_validation_status='failed',current_error=str(exc))
        result['note'] = 'Offline validation only. No model call, queue change, or factual approval.'
        return result
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description='Inspect and revalidate saved model results without API calls')
    parser.add_argument('--database',default='data/agent.db')
    parser.add_argument('--id')
    args = parser.parse_args()
    try:
        print(json.dumps(inspect(args.database,args.id),indent=2))
    except (ValueError,OSError,sqlite3.Error) as exc:
        parser.exit(1,f'Checkpoint inspection failed: {exc}\n')


if __name__ == '__main__':
    main()
