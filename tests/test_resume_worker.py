import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from agent.dashboard import Service
from agent.resume_worker import Worker, load_profile, validate_result, build_request
from agent.sources import fetch_board, fetch_source, validate_sources


class ResumeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name)/'agent.db')
        self.profile = {'profile_id':'fixture','facts':[{'id':'p1','section':'Projects','text':'Built Java APIs in a personal project.'}]}
        self.job = dict(url='https://example.com/jobs/1',title='Software Engineer',company='Example',
                        location='US',description='Build Java APIs',employer_key='example',requisition_id='1')
        self.service = Service(self.db,lambda _: [self.job])
        self.service.start({'prompt':'software engineer','sources':['lever:example']},background=False)
    def tearDown(self):
        self.temp.cleanup()
    def worker(self, generator=None):
        return Worker(self.db,self.profile,'profile-hash','test-model',generator or (lambda *args: {'ordered_fact_ids':['p1'],'missing_requirements':['Commercial experience']}))

    def test_round_trip_and_ack_once(self):
        worker = self.worker()
        self.assertTrue(worker.process_one())
        self.assertFalse(worker.process_one())
        state = self.service.snapshot()
        self.assertEqual(state['queue'][0]['status'],'resume_draft_ready')
        result = json.loads(state['drafts'][0]['result'])
        self.assertEqual(result['sections'][0]['facts'][0]['text'],self.profile['facts'][0]['text'])
        with worker.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM consumer_receipts').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE topic='resume.drafted.v1'").fetchone()[0],1)
        self.assertFalse(self.worker().process_one())

    def test_reject_unknown_fact_and_revalidate_without_regenerating(self):
        worker=self.worker(lambda *args: {'ordered_fact_ids':['made-up-job'],'missing_requirements':[]})
        worker.process_one()
        self.assertEqual(self.service.snapshot()['queue'][0]['status'],'resume_error')
        self.assertFalse(worker.process_one())
        with worker.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM consumer_receipts').fetchone()[0],0)
            db.execute('UPDATE resume_queue SET next_attempt_at=0')
        from unittest.mock import Mock
        generator=Mock()
        self.worker(generator).process_one()
        generator.assert_not_called()
        self.assertEqual(self.service.snapshot()['queue'][0]['status'],'resume_error')

    def test_two_workers_do_not_claim_same_request(self):
        first, second = self.worker(),self.worker()
        task = first.claim()
        self.assertIsNotNone(task)
        self.assertIsNone(second.claim())
        with first.connect() as db:
            db.execute('UPDATE resume_queue SET lease_until=?',(time.time()-1,))
        recovered=second.claim()
        self.assertNotEqual(task['worker_token'],recovered['worker_token'])

    def test_profile_requires_verified_facts_and_excludes_contact(self):
        path=Path(self.temp.name)/'profile.json'
        path.write_text(json.dumps({'profile_id':'fixture','verified':False,'facts':[]}),encoding='utf-8')
        with self.assertRaises(ValueError):
            load_profile(path)
        path.write_text(json.dumps({'profile_id':'fixture','verified':True,'email':'private@example.com',
            'facts':[{**self.profile['facts'][0],'verified':True}]}),encoding='utf-8')
        profile, version=load_profile(path)
        self.assertNotIn('email',profile)
        self.assertEqual(len(version),64)

    def test_api_request_is_structured_and_does_not_store(self):
        request=build_request(self.job,self.profile,'test-model')
        self.assertFalse(request['store'])
        self.assertEqual(request['text']['format']['type'],'json_schema')
        self.assertTrue(request['text']['format']['strict'])
        self.assertIn('untrusted',request['instructions'])

    def test_worker_error_is_redacted(self):
        def broken(*args):
            raise OSError('key=must-not-be-logged')
        self.worker(broken).process_one()
        self.assertNotIn('must-not-be-logged',json.dumps(self.service.snapshot()))

    def test_ashby_listed_only(self):
        row={'title':'Engineer','location':'US','jobUrl':'https://jobs.ashbyhq.com/example/1',
             'descriptionPlain':'Java APIs','isListed':True}
        rows=fetch_board('ashby:example',lambda _: {'jobs':[row,{**row,'isListed':False}]})
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['description'],'Java APIs')

    def test_market_search_uses_query_and_never_queues_excerpts(self):
        urls=[]
        def reader(url):
            urls.append(url)
            return {'results':[{'id':'1','title':'Software Engineer','redirect_url':'https://example.com/job/1',
                'description':'Build APIs','company':{'display_name':'Example'},'location':{'display_name':'US'}}]}
        with patch.dict('os.environ',{'ADZUNA_APP_ID':'fixture-id','ADZUNA_APP_KEY':'fixture-key'}):
            rows=fetch_source('adzuna:us',{'title_keywords':['software engineer']},reader)
        self.assertIn('what=software+engineer',urls[0])
        self.assertEqual(rows[0]['description_completeness'],'excerpt')
        service=Service(self.db,lambda _: rows)
        service.start({'prompt':'software engineer','sources':['adzuna:us']},background=False)
        self.assertTrue(any(j['qualification']['status']=='needs_full_jd' for j in service.snapshot()['jobs']))
        # Only the existing fixture request remains queued.
        self.assertEqual(len(service.snapshot()['queue']),1)

    def test_market_error_does_not_log_query_credentials(self):
        def broken(url):
            raise OSError(url)
        with patch.dict('os.environ',{'ADZUNA_APP_ID':'secret-id','ADZUNA_APP_KEY':'secret-key'}):
            with self.assertRaises(ValueError) as exc:
                fetch_source('adzuna:us',{'title_keywords':['software']},broken)
        self.assertNotIn('secret',str(exc.exception))

    def test_default_sources_used_when_input_is_empty(self):
        service=Service(self.db,lambda _: [self.job],default_sources=['ashby:example'])
        service.start({'prompt':'software engineer','sources':[]},background=False)
        config=json.loads(service.snapshot()['runs'][0]['config'])
        self.assertEqual(config['sources'],['ashby:example'])

    def test_api_configuration_missing_does_not_claim_a_task(self):
        from agent.resume_worker import main
        with patch.dict('os.environ',{},clear=True), patch('sys.argv',['resume_worker','--profile','missing.json']):
            with self.assertRaises(SystemExit) as exc:
                main()
        self.assertEqual(exc.exception.code,1)
        self.assertEqual(self.service.snapshot()['queue'][0]['status'],'resume_waiting')

    def test_stale_worker_cannot_save_over_new_lease(self):
        worker=self.worker()
        def stale(*args):
            with worker.connect() as db:
                db.execute("UPDATE resume_queue SET worker_token='new-owner'")
            return {'ordered_fact_ids':['p1'],'missing_requirements':[]}
        worker.generator=stale
        worker.process_one()
        state=self.service.snapshot()
        self.assertEqual(state['queue'][0]['worker_token'],'new-owner')
        self.assertEqual(state['drafts'],[])
