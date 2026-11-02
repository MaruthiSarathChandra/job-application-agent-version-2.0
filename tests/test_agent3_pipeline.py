"""Agent 3 tests: exact auto/review handoffs, checksum, concurrency and false-success prevention."""
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from agent.application_worker import Agent3
from agent.dashboard import Service
from agent.resume_artifact import SCHEMA as RESUME_SCHEMA
from agent.resume_review import approve


class StageThreeTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.database=str(Path(self.folder.name)/'agent.db')
        self.file=Path(self.folder.name)/'approved.docx'
        self.file.write_bytes(b'deterministic artifact bytes')
        self.sha=hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.job={'url':'https://careers.example.com/jobs/java-1',
                  'title':'Backend Software Engineer','company':'Example',
                  'location':'Kansas City, MO, US',
                  'description':'Develop Java Spring Boot APIs',
                  'employer_key':'example','requisition_id':'java-1'}
        self.service=Service(self.database,fetcher=lambda _:[self.job])
        self.service.start({'prompt':'Backend Software Engineer',
                            'sources':['lever:example'],'country_codes':['US']},
                           background=False)
        self.queue=self.service.snapshot()['queue'][0]
        with self.service.connect() as db:
            db.executescript(RESUME_SCHEMA)
            db.execute("""INSERT INTO resume_artifacts(id,path,sha256,profile_version,
                      template_version,validation) VALUES(?,?,?,?,?,?)""",
                      (self.sha,str(self.file),self.sha,'profile-v1','fixture',
                       json.dumps({'status':'passed'})))

    def tearDown(self):
        self.folder.cleanup()

    def ready_bundle(self, automatic=True):
        job=json.loads(self.queue['snapshot'])
        event={'schema_version':1,'event_id':'first-ready-event',
               'job_id':self.queue['job_id'],'profile_version':'profile-v1',
               'job':job,'resume':{'artifact_id':self.sha,'sha256':self.sha},
               'handoff_mode':'automatic' if automatic else 'review',
               'review_status':'not_reviewed',
               'status':'ready_for_application' if automatic else 'review_required'}
        with self.service.connect() as db:
            cursor=db.execute("""INSERT INTO application_queue(job_id,resume_artifact_id,
                 status,payload) VALUES(?,?,?,?)""",
                 (self.queue['job_id'],self.sha,event['status'],json.dumps(event)))
            event['application_queue_order']=cursor.lastrowid
            db.execute('UPDATE application_queue SET payload=? WHERE sequence=?',
                       (json.dumps(event),cursor.lastrowid))
            if automatic:
                db.execute("""INSERT INTO outbox(event_key,topic,payload)
                       VALUES(?,?,?)""",
                       ('applications.ready:'+self.queue['job_id'],
                        'applications.ready.v1',json.dumps(event)))
        return event

    def test_automatic_handoff_is_verified_not_submitted(self):
        self.ready_bundle()
        agent=Agent3(self.database)
        self.assertEqual(agent.status()[0]['status'],'queued')
        self.assertTrue(agent.process_one())
        result=agent.status()[0]
        self.assertEqual(result['status'],'awaiting_browser_adapter')
        plan=json.loads(result['plan'])
        self.assertEqual(plan['handoff_mode'],'automatic_unreviewed')
        self.assertFalse(plan['submission_performed'])
        self.assertFalse(plan['prepared'])
        self.assertEqual(plan['resume_sha256'],self.sha)
        with agent.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM application_queue').fetchone()[0],
                             'ready_for_application')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM applied_history").fetchone()[0],0)
        self.assertFalse(agent.process_one())

    def test_review_required_is_not_claimable_until_exact_artifact_approval(self):
        self.ready_bundle(automatic=False)
        agent=Agent3(self.database)
        self.assertEqual(agent.status(),[])
        self.assertFalse(agent.process_one())
        approve(self.service,self.queue['sequence'],self.sha)
        self.assertTrue(agent.process_one())
        self.assertEqual(json.loads(agent.status()[0]['plan'])['handoff_mode'],'approved')

    def test_bad_bytes_fail_without_publishing_submitted(self):
        self.ready_bundle()
        self.file.write_bytes(b'tampered')
        agent=Agent3(self.database)
        self.assertTrue(agent.process_one())
        result=agent.status()[0]
        self.assertEqual(result['status'],'error')
        self.assertIn('checksum',result['last_error'])
        self.assertIsNone(result['plan'])

    def test_two_workers_claim_once(self):
        self.ready_bundle()
        first=Agent3(self.database)
        second=Agent3(self.database)
        a=first.claim()
        self.assertIsNotNone(a)
        self.assertIsNone(second.claim())
        with first.connect() as db:
            db.execute('UPDATE agent3_tasks SET lease_until=?',(time.time()-1,))
        b=second.claim()
        self.assertIsNotNone(b)
        self.assertNotEqual(a['worker_token'],b['worker_token'])

    def test_starting_agent3_does_not_interfere_with_running_agent1(self):
        with self.service.connect() as db:
            db.execute("UPDATE discovery_runs SET status='running'")
        Agent3(self.database)
        with self.service.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM discovery_runs').fetchone()[0],
                             'running')


if __name__=='__main__':
    unittest.main()
