import json
import tempfile
import unittest
from pathlib import Path
from agent.dashboard import Service
from agent.resume_worker import Worker, load_profile
from agent.resume_artifact import Packager
from agent.resume_regeneration import enqueue, Regenerator
from agent.resume_preview import preview_html

class RegenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        self.service=Service(str(root/'agent.db'))
        self.artifacts=root/'resumes'
        self.raw={'profile_id':'example','verified':True,
                  'resume_header':{'display_name':'Example','contact_lines':[],'links':[]},
                  'facts':[{'id':'one','section':'Skills','text':'Java','verified':True}]}
        self.service.save_profile({'profile':self.raw,'revision':None})
        profile,version=load_profile(self.service.profile_path)
        self.service.fetcher=lambda _: [dict(url='https://example.com/job',title='Engineer',company='Example',location='US',description='Java backend APIs')]
        self.service.start({'prompt':'Engineer','sources':['lever:example']},background=False)
        generator=lambda *args:{'ordered_fact_ids':['one'],'missing_requirements':[]}
        Worker(self.service.database,profile,version,'test',generator).process_one()
        Packager(self.service.database,profile,version,self.artifacts).process_one()
        self.old=self.service.snapshot()['applications'][0]
        self.raw['facts'][0]['text']='Java and Python <script>alert(1)</script>'
        self.service.save_profile({'profile':self.raw,'revision':self.service.read_profile()['revision']})

    def tearDown(self):
        self.temp.cleanup()

    def test_new_profile_replaces_once_keeps_old_and_preview_escapes(self):
        identifier=enqueue(self.service,1)
        self.assertEqual(enqueue(self.service,1),identifier)
        def generator(job,profile,model):
            self.assertIn('Python',profile['facts'][0]['text'])
            return {'ordered_fact_ids':['one'],'missing_requirements':[]}
        worker=Regenerator(self.service,'test',self.artifacts,generator)
        self.assertTrue(worker.process_one())
        self.assertFalse(worker.process_one())
        app=self.service.snapshot()['applications'][0]
        self.assertNotEqual(app['resume_artifact_id'],self.old['resume_artifact_id'])
        self.assertEqual(app['sequence'],self.old['sequence'])
        self.assertEqual(app['status'],'ready_for_application')
        with self.service.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM resume_revision_history').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM resume_artifacts').fetchone()[0],2)
            current=db.execute('SELECT path FROM resume_artifacts WHERE id=?',(app['resume_artifact_id'],)).fetchone()
            html=preview_html(Path(current['path']).read_bytes())
            self.assertNotIn('<script>alert',html)
            self.assertIn('&lt;script&gt;',html)
            event=json.loads(db.execute("SELECT payload FROM outbox WHERE topic='applications.ready.v1'").fetchone()[0])
            self.assertEqual(event['resume']['artifact_id'],app['resume_artifact_id'])

    def test_failure_preserves_old_and_allows_retry(self):
        enqueue(self.service,1)
        def fail(*args): raise ValueError('Provider unavailable')
        Regenerator(self.service,'test',self.artifacts,fail).process_one()
        app=self.service.snapshot()['applications'][0]
        self.assertEqual(app['resume_artifact_id'],self.old['resume_artifact_id'])
        self.assertEqual(app['status'],'ready_for_application')
        self.assertEqual(self.service.snapshot()['regenerations'][0]['status'],'failed')
        self.assertTrue(enqueue(self.service,1))

    def test_published_event_rejected(self):
        with self.service.connect() as db:
            db.execute("UPDATE outbox SET published_at=CURRENT_TIMESTAMP WHERE topic='applications.ready.v1'")
        with self.assertRaisesRegex(ValueError,'downstream'):
            enqueue(self.service,1)

    def test_stale_worker_cannot_replace_new_result(self):
        enqueue(self.service,1)
        def generator(*args):
            with self.service.connect() as db:
                db.execute("UPDATE resume_regenerations SET token='replacement-worker'")
            return {'ordered_fact_ids':['one'],'missing_requirements':[]}
        Regenerator(self.service,'test',self.artifacts,generator).process_one()
        self.assertEqual(self.service.snapshot()['applications'][0]['resume_artifact_id'],self.old['resume_artifact_id'])

    def test_http_regenerate_and_view(self):
        import threading
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        from agent.dashboard import make_handler
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.service))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        client=HTTPConnection('127.0.0.1',server.server_port)
        try:
            path='/api/artifacts/'+self.old['resume_artifact_id']+'/preview'
            client.request('GET',path)
            response=client.getresponse()
            self.assertEqual(response.status,200)
            self.assertIn("default-src 'none'",response.getheader('Content-Security-Policy'))
            self.assertIn(b'Java',response.read())
            client.request('POST','/api/resumes/regenerate',json.dumps({'queue_order':1}),{'Origin':'https://untrusted.example'})
            response=client.getresponse();response.read();self.assertEqual(response.status,403)
            client.request('POST','/api/resumes/regenerate',json.dumps({'queue_order':1}))
            response=client.getresponse();response.read();self.assertEqual(response.status,202)
        finally:
            client.close();server.shutdown();server.server_close();thread.join()

    def test_diagnostics_reports_counts_without_profile_text(self):
        from agent.diagnostics import report
        enqueue(self.service,1)
        result=report(self.service.database,self.service.profile_path)
        self.assertEqual(result['profile_sections'],{'Skills':1})
        self.assertEqual(result['regenerations'][0]['status'],'queued')
        self.assertNotIn('Java and Python',json.dumps(result))
        self.assertEqual(result['regenerations'][0]['profile_sections'],{'Skills':1})

    def test_repeat_claim_guard_prevents_another_model_call(self):
        from unittest.mock import Mock
        identifier=enqueue(self.service,1)
        generator=Mock(return_value={'ordered_fact_ids':['one'],'missing_requirements':[]})
        worker=Regenerator(self.service,'test',self.artifacts,generator)
        worker.process_one()
        with self.service.connect() as db:
            db.execute("UPDATE resume_regenerations SET status='queued' WHERE id=?",(identifier,))
        with self.assertRaisesRegex(ValueError,'claimed twice'):
            worker.process_one()
        self.assertEqual(generator.call_count,1)
