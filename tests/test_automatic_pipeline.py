import copy
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
from agent.dashboard import Service
from agent.resume_worker import Worker
from agent.resume_artifact import Packager
from agent.handoff import promote_existing
from agent.freshness import date_decision
from agent.worker_health import Heartbeat, status
from test_resume_writer import fixture


class AutomaticPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.job=dict(url='https://example.com/jobs/1',title='Software Engineer',
                      company='Example',location='US',description='Build Java APIs and Python services')
        self.service=Service(str(self.root/'agent.db'),lambda _:[self.job])
        self.search={'prompt':'Software Engineer','sources':['lever:example'],'country_codes':['US']}
        self.service.start(self.search,background=False)
        self.profile,self.result=fixture()
        self.generator=Mock(return_value=self.result)
        self.worker=Worker(self.service.database,self.profile,'v1','fixture',self.generator)
    def tearDown(self):
        self.service.stop_schedule()
        self.temp.cleanup()
    def package(self,automatic=True):
        return Packager(self.service.database,self.profile,'v1',self.root/'resumes',auto_handoff=automatic)
    def ready(self,automatic=True):
        self.worker.process_one()
        self.package(automatic).process_one()
        return self.service.snapshot()['applications'][0]
    def test_one_call_atomic_handoff_contains_jd_link_and_artifact(self):
        app=self.ready()
        self.assertEqual(app['status'],'ready_for_application')
        payload=json.loads(app['payload'])
        self.assertEqual(payload['job_url'],self.job['url'])
        self.assertEqual(payload['job']['description'],self.job['description'])
        self.assertEqual(payload['review_status'],'not_reviewed')
        self.assertTrue(payload['resume']['sha256'])
        self.assertFalse(self.worker.process_one())
        self.assertFalse(self.package().process_one())
        self.generator.assert_called_once()
        with self.service.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE topic='applications.ready.v1'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT status FROM resume_queue').fetchone()[0],'resume_ready')
    def test_existing_resume_recovered_without_new_gpt_call_and_idempotent(self):
        self.assertEqual(self.ready(False)['status'],'review_required')
        self.assertEqual(promote_existing(self.worker),1)
        self.assertEqual(promote_existing(self.worker),0)
        self.generator.assert_called_once()
        self.assertEqual(self.service.snapshot()['applications'][0]['status'],'ready_for_application')
    def test_existing_corrupt_artifact_never_published(self):
        self.ready(False)
        with self.service.connect() as db:
            path=db.execute('SELECT path FROM resume_artifacts').fetchone()[0]
        Path(path).write_bytes(b'corrupt')
        self.assertEqual(promote_existing(self.worker),0)
        self.assertEqual(self.service.snapshot()['applications'][0]['status'],'artifact_error')
    def test_existing_resume_obeys_current_country_filters(self):
        self.ready(False)
        self.service.start({**self.search,'country_codes':['CA']},background=False)
        self.assertEqual(promote_existing(self.worker),0)
        self.assertEqual(self.service.snapshot()['applications'][0]['status'],'requalification_required')
    def test_current_filters_checked_before_paid_call_and_requeue_if_changed(self):
        self.service.start({**self.search,'country_codes':['CA']},background=False)
        self.worker.process_one()
        self.generator.assert_not_called()
        self.assertEqual(self.service.snapshot()['queue'][0]['status'],'resume_excluded')
        self.service.start(self.search,background=False)
        self.assertEqual(len(self.service.snapshot()['queue']),1)
        self.assertEqual(self.service.snapshot()['queue'][0]['status'],'resume_waiting')
        self.worker.process_one()
        self.package().process_one()
        self.generator.assert_called_once()
        self.assertEqual(self.service.snapshot()['applications'][0]['status'],'ready_for_application')
    def test_profile_changes_do_not_break_already_saved_draft(self):
        self.worker.process_one()
        changed=copy.deepcopy(self.profile)
        changed['facts'][0]['text']='New profile text'
        Packager(self.service.database,changed,'v2',self.root/'resumes',auto_handoff=True).process_one()
        app=self.service.snapshot()['applications'][0]
        self.assertEqual(app['status'],'ready_for_application')
        self.assertEqual(json.loads(app['payload'])['profile_version'],'v1')
    def test_transaction_failure_keeps_draft_recoverable_without_paid_retry(self):
        self.worker.process_one()
        with self.service.connect() as db:
            db.execute("CREATE TRIGGER reject_ready BEFORE INSERT ON outbox WHEN NEW.topic='applications.ready.v1' BEGIN SELECT RAISE(ABORT,'test'); END")
        self.package().process_one()
        self.assertEqual(self.service.snapshot()['applications'],[])
        with self.service.connect() as db:
            db.execute('DROP TRIGGER reject_ready')
            db.execute('UPDATE render_tasks SET next_attempt_at=0')
        self.package().process_one()
        self.generator.assert_called_once()
        self.assertEqual(self.service.snapshot()['applications'][0]['status'],'ready_for_application')
    def test_schedule_persists_and_invalid_value_keeps_current_schedule(self):
        self.assertEqual(self.service.saved_schedule(),15)
        self.service.set_schedule(30)
        with self.assertRaises(ValueError):self.service.set_schedule(1)
        self.assertEqual(self.service.snapshot()['scanner']['interval_minutes'],30)
        self.assertEqual(Service(self.service.database).saved_schedule(),30)
        self.service.set_schedule(0)
        self.assertEqual(Service(self.service.database).saved_schedule(),0)
    def test_heartbeat_distinguishes_worker_from_dashboard(self):
        self.assertFalse(self.service.snapshot()['worker']['online'])
        with Heartbeat(self.worker):
            self.assertTrue(self.service.snapshot()['worker']['online'])
            with self.worker.connect() as db:
                self.assertFalse(status(db,now=10**12)['online'])
        self.assertFalse(self.service.snapshot()['worker']['online'])
    def test_first_discovered_is_explicit_and_preserved_between_scans(self):
        first=self.service.snapshot()['jobs'][0]['first_seen_at']
        self.service.start({**self.search,'posted_within_hours':7,'time_basis':'first_seen'},background=False)
        saved=self.service.snapshot()['jobs'][0]
        self.assertEqual(saved['first_seen_at'],first)
        self.assertEqual(saved['qualification']['status'],'matched')
        self.assertEqual(date_decision(saved,{'posted_within_hours':7})['status'],'date_unknown')
        old=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()
        self.assertEqual(date_decision({**saved,'first_seen_at':old},{'posted_within_hours':7,'time_basis':'first_seen'})['status'],'excluded')
        self.assertEqual(date_decision({**saved,'first_seen_at':None},{'posted_within_hours':7,'time_basis':'first_seen'})['status'],'date_unknown')

    def test_regeneration_automatically_replaces_unpublished_bundle(self):
        from agent.resume_regeneration import enqueue,Regenerator
        original=self.ready()
        raw={'profile_id':self.profile['profile_id'],'resume_header':self.profile['_resume_header'],
             'verified':True,'facts':[dict(f,verified=True) for f in self.profile['facts']]}
        self.service.save_profile({'profile':raw,'revision':None})
        enqueue(self.service,1)
        regen=Regenerator(self.service,'fixture',self.root/'resumes',self.generator,auto_handoff=True)
        self.assertTrue(regen.process_one())
        app=self.service.snapshot()['applications'][0]
        self.assertEqual(app['status'],'ready_for_application')
        self.assertEqual(json.loads(app['payload'])['job_url'],self.job['url'])
        with self.service.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE topic='applications.ready.v1'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM resume_revision_history').fetchone()[0],1)

    def test_watch_without_limit_processes_more_than_one_task(self):
        from unittest.mock import patch
        from agent.resume_worker import main
        raw={'profile_id':self.profile['profile_id'],'resume_header':self.profile['_resume_header'],
             'verified':True,'facts':[dict(f,verified=True) for f in self.profile['facts']]}
        self.service.save_profile({'profile':raw,'revision':None})
        with patch('sys.argv',['worker','--database',self.service.database,'--profile',str(self.service.profile_path),'--watch']), \
             patch.dict('os.environ',{'OPENAI_API_KEY':'fixture','OPENAI_MODEL':'fixture'}), \
             patch('agent.resume_regeneration.Regenerator.process_one',return_value=False), \
             patch('agent.resume_artifact.Packager.process_one',return_value=False), \
             patch('agent.resume_worker.Worker.process_one',side_effect=[True,True,KeyboardInterrupt]) as process:
            main()
        self.assertEqual(process.call_count,3)
