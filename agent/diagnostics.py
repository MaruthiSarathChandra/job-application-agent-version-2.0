"""Read-only local diagnostics without contact details, profile text, credentials, or API calls."""
import json
import sqlite3
from collections import Counter
from pathlib import Path

def report(database, profile_path):
    database, profile_path=Path(database).resolve(),Path(profile_path).resolve()
    result={'database_path':str(database),'database_exists':database.exists(),
            'profile_path':str(profile_path),'profile_exists':profile_path.exists()}
    if profile_path.exists():
        try:
            from .resume_worker import load_profile
            profile,version=load_profile(profile_path)
            result['profile_version']=version
            result['profile_sections']=dict(Counter(f['section'] for f in profile['facts']))
        except (ValueError,OSError):
            result['profile_error']='Profile cannot be read or is not valid and verified'
    if not database.exists():
        return result
    db=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    try:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'resume_queue' in tables:
            result['resume_queue']=[dict(r) for r in db.execute('SELECT sequence,status FROM resume_queue ORDER BY sequence')]
        if 'resume_regenerations' in tables:
            result['regenerations']=[]
            for row in db.execute('SELECT id,queue_order,status,error,lease_until,profile,profile_version FROM resume_regenerations ORDER BY created_at DESC,rowid DESC LIMIT 10'):
                item={k:row[k] for k in ('id','queue_order','status','error','lease_until','profile_version')}
                item['profile_sections']=dict(Counter(f['section'] for f in json.loads(row['profile'])['facts']))
                result['regenerations'].append(item)
        return result
    finally:
        db.close()
