"""Agent 3 regression tests; no employer websites, Gmail or model calls."""
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from agent.application_worker import Agent3
from agent.dashboard import Service
from agent.resume_artifact import SCHEMA as ARTIFACT_SCHEMA
from agent.resume_review import approve


class Agent3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'agent.db')
        self.path = Path(self.temp.name) / 'resume.docx'
        self.path.write_bytes(b'known verified DOCX fixture bytes')
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.job = {'url': 'https://careers.example.com/jobs/1',
                    'title': 'Software Engineer', 'company': 'Example',
                    'location': 'US', 'description': 'Build and test Java services',
                    'employer_key': 'example', 'requisition_id': '1'}
        self.service = Service(self.db, fetcher=lambda _: [self.job])
        self.service.start({'prompt':'software engineer','sources':['lever:example']}, background=False)
        self.queue = self.service.snapshot()['queue'][0]
        self.job_id = self.queue['job_id']
        with self.service.connect() as db:
            db.executescript(ARTIFACT_SCHEMA)
            db.execute('''INSERT INTO resume_artifacts
                (id,path,sha256,profile_version,template_version,validation) VALUES(?,?,?,?,?,?)''',
                (self.digest,str(self.path),self.digest,'profile-v1','fixture',json.dumps({'status':'passed'})))
            event = {'schema_version':1, 'event_id':'fixture-ready-event',
                'application_queue_order':1, 'job_id':self.job_id,
                'profile_version':'profile-v1', 'job':json.loads(self.queue['snapshot']),
                'resume':{'artifact_id':self.digest,'sha256':self.digest},
                'status':'review_required'}
            db.execute('''INSERT INTO application_queue(job_id,resume_artifact_id,status,payload)
                VALUES(?,?,?,?)''',
                (self.job_id,self.digest,'review_required',json.dumps(event)))

    def tearDown(self):
        self.temp.cleanup()

    def approved(self):
        return approve(self.service, self.queue['sequence'], self.digest)

    def test_unreviewed_draft_is_visible_but_not_executable(self):
        state = self.service.snapshot()
        self.assertEqual(state['applications'][0]['status'], 'review_required')
        worker = Agent3(self.db)
        self.assertEqual(worker.status(), [])
        self.assertFalse(worker.process_one())

    def test_approved_resume_becomes_durable_preflight_task(self):
        self.approved()
        worker = Agent3(self.db)
        self.assertEqual(worker.status()[0]['status'],'queued')
        self.assertTrue(worker.process_one())
        result=worker.status()[0]
        self.assertEqual(result['status'],'awaiting_browser_adapter')
        plan=json.loads(result['plan'])
        self.assertFalse(plan['submission_performed'])
        self.assertEqual(plan['job_url'], self.job['url'])
        self.assertEqual(plan['resume_sha256'],self.digest)
        self.assertEqual(plan['next_step'],'browser_adapter_required')
        self.assertFalse(worker.process_one())
        self.assertEqual(len(Agent3(self.db).status()),1)
        with worker.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agent3_tasks').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM activity WHERE kind='agent3_preflight'").fetchone()[0],1)

    def test_tampered_resume_fails_no_success_plan(self):
        self.approved()
        self.path.write_bytes(b'changed bytes')
        worker=Agent3(self.db)
        self.assertTrue(worker.process_one())
        result=worker.status()[0]
        self.assertEqual(result['status'],'error')
        self.assertIsNone(result['plan'])
        self.assertIn('checksum',result['last_error'])

    def test_mismatched_review_never_prepares_application(self):
        self.approved()
        with self.service.connect() as db:
            event=json.loads(db.execute('SELECT payload FROM application_queue').fetchone()['payload'])
            event['review']['artifact_id']='different'
            db.execute('UPDATE application_queue SET payload=?',(json.dumps(event),))
            db.execute('UPDATE outbox SET payload=? WHERE topic=?',(json.dumps(event),'applications.ready.v1'))
        worker=Agent3(self.db)
        self.assertTrue(worker.process_one())
        self.assertEqual(worker.status()[0]['status'],'error')

    def test_retracted_handoff_cannot_be_claimed(self):
        self.approved()
        worker=Agent3(self.db)
        with self.service.connect() as db:
            db.execute("UPDATE application_queue SET status='regenerating'")
        self.assertIsNone(worker.claim())
        self.assertEqual(worker.status()[0]['status'],'blocked')

    def test_lease_cannot_be_taken_by_two_workers(self):
        self.approved()
        a,b=Agent3(self.db),Agent3(self.db)
        first=a.claim()
        self.assertIsNotNone(first)
        self.assertIsNone(b.claim())
        with a.connect() as db:
            db.execute('UPDATE agent3_tasks SET lease_until=?',(time.time()-1,))
        second=b.claim()
        self.assertIsNotNone(second)
        self.assertNotEqual(first['worker_token'],second['worker_token'])


    def test_newly_approved_revision_requeues_only_preflight(self):
        self.approved()
        worker=Agent3(self.db)
        self.assertTrue(worker.process_one())
        self.assertEqual(worker.status()[0]['status'],'awaiting_browser_adapter')
        # Resume regeneration retracts the prior approval.
        with self.service.connect() as db:
            db.execute("UPDATE application_queue SET status='regenerating'")
        self.assertEqual(worker.status()[0]['status'],'blocked')
        # An explicit new approval publishes a distinct, validated ready event.
        with self.service.connect() as db:
            event=json.loads(db.execute('SELECT payload FROM application_queue').fetchone()['payload'])
            event['event_id']='replacement-approved-event'
            db.execute("UPDATE application_queue SET status='ready_for_application',payload=?",
                       (json.dumps(event),))
            db.execute("UPDATE outbox SET payload=? WHERE topic='applications.ready.v1'",
                       (json.dumps(event),))
        self.assertEqual(worker.status()[0]['status'],'queued')
        self.assertTrue(worker.process_one())
        self.assertEqual(worker.status()[0]['status'],'awaiting_browser_adapter')
        self.assertEqual(len(worker.status()),1)


if __name__ == '__main__':
    unittest.main()
