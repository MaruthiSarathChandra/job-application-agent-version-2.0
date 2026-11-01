import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent.dashboard import Service
from agent.resume_worker import Worker, build_request
from agent.resume_artifact import Packager, save_artifact, render_docx, validate_docx


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=str(Path(self.temp.name)/'agent.db')
        self.artifacts=Path(self.temp.name)/'resumes'
        self.profile={'profile_id':'fixture','_resume_header':{
            'display_name':'Example Candidate','contact_lines':['candidate@example.com | Example City'],
            'links':[{'label':'GitHub','url':'https://github.com/example'}]},
            'facts':[{'id':'p1','section':'Projects','text':'Built a Java API with transactional persistence. https://github.com/example/project'}]}
        job=dict(url='https://example.com/jobs/1',title='Software Engineer',company='Example',
                 location='US',description='Build Java APIs',employer_key='example',requisition_id='1')
        self.service=Service(self.db,lambda _:[job])
        self.service.start({'prompt':'software engineer','sources':['lever:example']},background=False)
        self.worker=Worker(self.db,self.profile,'v1','fixture-model',lambda *args:{'ordered_fact_ids':['p1'],'missing_requirements':[]})
        self.worker.process_one()
    def tearDown(self):
        self.temp.cleanup()
    def packager(self,version='v1'):
        return Packager(self.db,self.profile,version,self.artifacts)

    def test_complete_agent2_roundtrip_and_no_duplicate_output(self):
        packager=self.packager()
        self.assertTrue(packager.process_one())
        self.assertFalse(packager.process_one())
        state=self.service.snapshot()
        self.assertEqual(state['queue'][0]['status'],'resume_ready')
        self.assertEqual(len(state['applications']),1)
        payload=json.loads(state['applications'][0]['payload'])
        self.assertEqual(payload['job']['description'],'Build Java APIs')
        self.assertEqual(payload['resume']['validation']['status'],'passed')
        self.assertFalse(payload['resume']['validation']['page_count_verified'])
        with packager.connect() as db:
            artifact=db.execute('SELECT * FROM resume_artifacts').fetchone()
            self.assertEqual(hashlib.sha256(Path(artifact['path']).read_bytes()).hexdigest(),payload['resume']['sha256'])
            self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE topic='applications.ready.v1'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM consumer_receipts WHERE consumer='resume-packager-v1'").fetchone()[0],1)
        self.assertFalse(self.packager().process_one())

    def test_multiline_and_tabbed_facts_preserved_and_tampering_rejected(self):
        draft=json.loads(self.service.snapshot()['drafts'][0]['result'])
        header=self.profile['_resume_header']
        for separator in ('\n', '\t', '\r', '\r\n'):
            with self.subTest(separator=repr(separator)):
                text='Built Java APIs.'+separator+'Repository: https://github.com/example/project'
                draft['sections'][0]['facts'][0]['text']=text
                raw=render_docx(draft,header)
                self.assertEqual(validate_docx(raw,draft,header)['status'],'passed')
                draft['sections'][0]['facts'][0]['text']=text.replace(separator,'')
                with self.assertRaisesRegex(ValueError,'exactly preserve'):
                    validate_docx(raw,draft,header)

    def test_docx_text_links_and_stable_checksum(self):
        draft=json.loads(self.service.snapshot()['drafts'][0]['result'])
        first=save_artifact(draft,self.profile['_resume_header'],self.artifacts)
        second=save_artifact(draft,self.profile['_resume_header'],self.artifacts)
        self.assertEqual(first['sha256'],second['sha256'])
        with self.assertRaises(ValueError):
            validate_docx(Path(first['path']).read_bytes(),draft,{**self.profile['_resume_header'],'display_name':'Changed name'})

    def test_failed_publish_rolls_back_ready_queue_and_receipt(self):
        packager=self.packager()
        with packager.connect() as db:
            db.execute("CREATE TRIGGER fail_ready BEFORE INSERT ON outbox WHEN NEW.topic='applications.ready.v1' BEGIN SELECT RAISE(ABORT,'fixture'); END")
        packager.process_one()
        self.assertEqual(self.service.snapshot()['applications'],[])
        with packager.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM consumer_receipts WHERE consumer='resume-packager-v1'").fetchone()[0],0)
            db.execute('DROP TRIGGER fail_ready')
            db.execute('UPDATE render_tasks SET next_attempt_at=0')
        packager.process_one()
        self.assertEqual(len(self.service.snapshot()['applications']),1)

    def test_profile_mismatch_does_not_publish(self):
        self.packager('different-version').process_one()
        self.assertEqual(self.service.snapshot()['applications'],[])
        self.assertEqual(self.service.snapshot()['queue'][0]['status'],'resume_render_error')

    def test_draft_tampering_does_not_publish(self):
        packager=self.packager()
        with packager.connect() as db:
            draft=json.loads(db.execute('SELECT result FROM resume_drafts').fetchone()[0])
            draft['sections'][0]['facts'][0]['text']='Invented 10 years of experience'
            db.execute('UPDATE resume_drafts SET result=?',(json.dumps(draft),))
        packager.process_one()
        self.assertEqual(self.service.snapshot()['applications'],[])

    def test_header_is_not_sent_to_gpt(self):
        request=build_request({'description':'Example JD'},self.profile,'model')
        self.assertNotIn('candidate@example.com',request['input'])
        self.assertNotIn('Example Candidate',request['input'])

    def test_claims_are_exclusive_and_recoverable(self):
        first,second=self.packager(),self.packager()
        task=first.claim()
        self.assertIsNone(second.claim())
        with first.connect() as db:
            db.execute('UPDATE render_tasks SET lease_until=0')
        self.assertNotEqual(task['worker_token'],second.claim()['worker_token'])

    def test_download_verifies_checksum(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import threading
        from agent.dashboard import make_handler
        packager=self.packager()
        packager.process_one()
        app=self.service.snapshot()['applications'][0]
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.service))
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        conn=HTTPConnection('127.0.0.1',server.server_port)
        try:
            endpoint='/api/artifacts/'+app['resume_artifact_id']
            conn.request('GET',endpoint)
            response=conn.getresponse()
            self.assertEqual(response.status,200)
            self.assertIn('.docx',response.getheader('Content-Disposition'))
            self.assertEqual(hashlib.sha256(response.read()).hexdigest(),app['resume_artifact_id'])
            with packager.connect() as db:
                path=Path(db.execute('SELECT path FROM resume_artifacts').fetchone()[0])
            path.write_bytes(b'changed')
            conn.request('GET',endpoint)
            response=conn.getresponse()
            self.assertEqual(response.status,409)
            response.read()
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_old_schema_queue_survives_new_tables(self):
        packager=self.packager()
        original=self.service.snapshot()['queue'][0]['job_id']
        packager.process_one()
        self.assertEqual(self.service.snapshot()['queue'][0]['job_id'],original)

    def test_render_only_cli_requires_no_api_key(self):
        import os
        from agent.resume_worker import load_profile, main
        profile_file=Path(self.temp.name)/'profile.json'
        profile_file.write_text(json.dumps({'profile_id':'fixture','verified':True,
            'resume_header':self.profile['_resume_header'],
            'facts':[{**fact,'verified':True} for fact in self.profile['facts']]}),encoding='utf-8')
        profile,version=load_profile(profile_file)
        db=str(Path(self.temp.name)/'cli.db')
        job=dict(url='https://example.com/jobs/2',title='Software Engineer',company='Example',location='US',description='Build Java APIs')
        service=Service(db,lambda _:[job])
        service.start({'prompt':'software engineer','sources':['lever:example']},background=False)
        Worker(db,profile,version,'fixture-model',lambda *args:{'ordered_fact_ids':['p1'],'missing_requirements':[]}).process_one()
        with patch.dict(os.environ,{},clear=True),patch('sys.argv',['resume_worker','--profile',str(profile_file),
                '--database',db,'--artifact-dir',str(self.artifacts),'--render-only','--limit','1']):
            main()
        self.assertEqual(len(service.snapshot()['applications']),1)
