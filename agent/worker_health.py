"""Heartbeat independent of slow provider calls; stale workers are reported offline."""
import threading
import time
import uuid

SCHEMA = '''CREATE TABLE IF NOT EXISTS worker_health(
 id TEXT PRIMARY KEY, heartbeat REAL NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL);'''

class Heartbeat:
    def __init__(self, owner):
        self.owner=owner;self.id=str(uuid.uuid4());self.stop=threading.Event();self.stage='idle'
        with owner.connect() as db: db.executescript(SCHEMA)
    def beat(self, status='online'):
        with self.owner.connect() as db:
            db.execute('INSERT OR REPLACE INTO worker_health VALUES(?,?,?,?)',
                       (self.id,time.time(),status,self.stage))
    def __enter__(self):
        self.beat()
        def loop():
            while not self.stop.wait(5):
                try: self.beat()
                except Exception: pass  # A failed heartbeat becomes stale, never falsely healthy.
        self.thread=threading.Thread(target=loop,daemon=True);self.thread.start();return self
    def __exit__(self,*args):
        self.stop.set();self.thread.join(timeout=6);self.beat('stopped')

def status(db, now=None):
    now=time.time() if now is None else now
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='worker_health'").fetchone():
        return {'online':False,'workers':[]}
    rows=[dict(r) for r in db.execute('SELECT * FROM worker_health ORDER BY heartbeat DESC LIMIT 20')]
    live=[r for r in rows if r['status']=='online' and now-r['heartbeat']<20]
    return {'online':bool(live),'workers':live,'last_seen':rows[0]['heartbeat'] if rows else None}
