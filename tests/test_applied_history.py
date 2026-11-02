import json
from datetime import datetime,timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import threading
from agent.application_history import prepared,submitted
from agent.dashboard import make_handler
import test_automatic_pipeline as fixtures

# Reuse the fixture only; do not inherit and rerun its tests.
class AppliedHistoryTests(__import__('unittest').TestCase):
    setUp=fixtures.AutomaticPipelineTests.setUp
    tearDown=fixtures.AutomaticPipelineTests.tearDown
    package=fixtures.AutomaticPipelineTests.package
    ready=fixtures.AutomaticPipelineTests.ready
    def body(self,app):
        return {'job_id':app['job_id'],'artifact_id':app['resume_artifact_id'],
                'submitted':True,'confirmed':True,'submission_id':'employer-receipt-123',
                'submitted_at':datetime.now(timezone.utc).isoformat(),
                'confirmation_url':'https://example.com/applications/123'}
    def test_history_empty_until_confirmed_submission_and_retains_exact_resume(self):
        app=self.ready();body=self.body(app)
        self.assertEqual(self.service.snapshot()['applied_history'],[])
        with self.assertRaises(ValueError):submitted(self.service,body)
        prepared(self.service,{**body,'prepared':True})
        self.assertEqual(self.service.snapshot()['applied_history'],[])
        with self.assertRaises(ValueError):submitted(self.service,{**body,'confirmed':False})
        self.assertEqual(submitted(self.service,body)['status'],'submitted')
        history=self.service.snapshot()['applied_history']
        self.assertEqual(len(history),1)
        payload=json.loads(history[0]['payload'])
        self.assertEqual(history[0]['artifact_id'],app['resume_artifact_id'])
        self.assertEqual(payload['job_url'],self.job['url'])
        self.assertEqual(payload['submission']['id'],body['submission_id'])
        self.assertTrue(submitted(self.service,body)['duplicate'])
        with self.assertRaises(ValueError):submitted(self.service,{**body,'submission_id':'conflicting'})
        with self.service.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE topic='applications.submitted.v1'").fetchone()[0],1)
    def test_bad_confirmation_and_wrong_resume_never_enter_history(self):
        app=self.ready();body=self.body(app)
        prepared(self.service,{**body,'prepared':True})
        for changes in ({'artifact_id':'wrong'},{'submission_id':''},{'confirmation_url':'javascript:alert(1)'},{'submitted_at':'bad'},{'submitted_at':'2099-01-01T12:00:00Z'}):
            with self.assertRaises(ValueError):submitted(self.service,{**body,**changes})
        self.assertEqual(self.service.snapshot()['applied_history'],[])
    def test_failed_publish_rolls_back_history(self):
        app=self.ready();body=self.body(app)
        prepared(self.service,{**body,'prepared':True})
        with self.service.connect() as db:
            db.execute("CREATE TRIGGER fail_submission BEFORE INSERT ON outbox WHEN NEW.topic='applications.submitted.v1' BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(Exception):submitted(self.service,body)
        self.assertEqual(self.service.snapshot()['applied_history'],[])
        self.assertEqual(self.service.snapshot()['applications'][0]['status'],'ready_for_submission')
    def test_http_handoff_and_confirmation_contract(self):
        app=self.ready(False)
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.service))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            conn=HTTPConnection('127.0.0.1',server.server_port)
            def post(path,body):
                conn.request('POST',path,json.dumps(body),headers={'Content-Type':'application/json'})
                response=conn.getresponse();value=json.loads(response.read());return response.status,value
            code,result=post('/api/resumes/handoff',{})
            self.assertEqual(code,200);self.assertEqual(result['promoted'],1)
            body=self.body(app)
            self.assertEqual(post('/api/applications/prepared',{**body,'prepared':True})[0],200)
            self.assertEqual(post('/api/applications/submitted',{**body,'confirmed':False})[0],400)
            self.assertEqual(post('/api/applications/submitted',body)[0],200)
            self.assertEqual(len(self.service.snapshot()['applied_history']),1)
            conn.close()
        finally:server.shutdown();server.server_close();thread.join()
