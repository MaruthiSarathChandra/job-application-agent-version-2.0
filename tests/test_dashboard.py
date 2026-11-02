import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from agent.dashboard import Service, make_handler, rules_from_request
from agent.sources import fetch_board, validate_sources


def job(identifier='1', **changes):
    return dict(url='https://example.com/jobs/'+identifier, title='Software Engineer',
                company='Example', location='US', description='Build Java APIs',
                employer_key='example', requisition_id=identifier, **changes)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name)/'agent.db')
        self.body = dict(prompt='Find software engineer jobs', sources=['lever:example'],
                         jd_keywords=['Java'], excluded_title_keywords=['Senior'])
    def tearDown(self):
        self.temp.cleanup()

    def test_queue_and_outbox_are_deduplicated_across_restart(self):
        service = Service(self.path, lambda _: [job()])
        service.start(self.body, background=False)
        service = Service(self.path, lambda _: [job()])
        service.start(self.body, background=False)
        state = service.snapshot()
        self.assertEqual(len(state['queue']), 1)
        self.assertEqual(state['queue'][0]['sequence'], 1)
        with service.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)
            event = json.loads(db.execute('SELECT payload FROM outbox').fetchone()[0])
            self.assertEqual(event['job']['description'], 'Build Java APIs')
        self.assertEqual(state['runs'][0]['status'], 'completed')

    def test_jd_and_title_exclusion_persist_and_do_not_enqueue(self):
        jobs = [dict(job(), title='Senior Software Engineer'),
                dict(job('2'), description='Build Python APIs')]
        service = Service(self.path, lambda _: jobs)
        service.start(self.body, background=False)
        self.assertFalse(service.snapshot()['queue'])
        self.assertTrue(all(j['qualification']['status'] == 'excluded' for j in service.snapshot()['jobs']))
        with service.connect() as db:
            decisions = [json.loads(x[0]) for x in db.execute('SELECT result FROM intake_attempts')]
            self.assertTrue(all(x['status'] == 'excluded' for x in decisions))

    def test_failed_source_continues_and_is_visible(self):
        def fetch(source):
            if source.startswith('greenhouse'):
                raise OSError('Network unavailable')
            return [job()]
        service = Service(self.path, fetch)
        service.start({**self.body, 'sources':['greenhouse:bad','lever:example']}, background=False)
        self.assertEqual(len(service.snapshot()['queue']), 1)
        self.assertEqual(service.snapshot()['runs'][0]['status'], 'completed_with_errors')
        self.assertTrue(any(e['kind']=='error' for e in service.snapshot()['activity']))

    def test_overlapping_run_rejected(self):
        service = Service(self.path)
        service.lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, 'already active'):
                service.start(self.body)
        finally:
            service.lock.release()

    def test_recovery_does_not_duplicate_queue(self):
        service = Service(self.path)
        with service.connect() as db:
            db.execute("INSERT INTO discovery_runs(id,prompt,config,status) VALUES('old','x','{}','running')")
        service = Service(self.path)
        self.assertEqual(service.snapshot()['runs'][0]['status'], 'interrupted')

    def test_adapters_extract_full_jd(self):
        gh = fetch_board('greenhouse:example', lambda _: {'jobs':[{'id':1,'title':'Engineer',
             'absolute_url':'https://example.com/1','location':{'name':'US'},'content':'&lt;p&gt;Java APIs&lt;/p&gt;'}]})
        self.assertEqual(gh[0]['description'], 'Java APIs')
        lever = fetch_board('lever:example', lambda _: [{'id':'1','text':'Engineer',
            'hostedUrl':'https://jobs.lever.co/example/1','descriptionPlain':'Build Java',
            'lists':[{'text':'Responsibilities','content':'<li>REST APIs</li>'}]}])
        self.assertIn('REST APIs', lever[0]['description'])

    def test_bad_source_cannot_fetch_arbitrary_url(self):
        for source in ['http://localhost/admin', 'lever:../secret', 'greenhouse:foo/bar']:
            with self.assertRaises(ValueError):
                validate_sources([source])

    def test_http_dashboard_and_same_origin_run(self):
        service = Service(self.path, lambda _: [job()])
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = HTTPConnection('127.0.0.1', server.server_port)
        try:
            original_read_text = Path.read_text
            def windows_read_text(path, *args, **kwargs):
                # Reproduce Windows' CP1252 default on every test platform.
                kwargs.setdefault('encoding', 'cp1252')
                return original_read_text(path, *args, **kwargs)
            with patch.object(Path, 'read_text', windows_read_text):
                conn.request('GET','/')
                response = conn.getresponse()
                self.assertEqual(response.status,200)
                self.assertIn('charset=utf-8', response.getheader('Content-Type'))
                html = response.read().decode('utf-8')
                self.assertIn('Opportunity pipeline', html)
                self.assertIn('→', html)
            conn.request('POST','/api/runs', json.dumps(self.body), {'Origin':'https://evil.example'})
            response = conn.getresponse()
            self.assertEqual(response.status,403)
            response.read()
            conn.request('POST','/api/runs', json.dumps(self.body), {'Content-Type':'application/json'})
            response = conn.getresponse()
            self.assertEqual(response.status,202)
            response.read()
            # Wait for the bounded fixture worker before removing its database.
            with service.lock:
                pass
            conn.request('GET','/api/state')
            response = conn.getresponse()
            self.assertEqual(len(json.loads(response.read())['queue']),1)
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_queue_insert_rolls_back_if_outbox_write_fails(self):
        service = Service(self.path, lambda _: [job()])
        with service.connect() as db:
            db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        service.start(self.body, background=False)
        self.assertEqual(service.snapshot()['queue'], [])
        self.assertEqual(service.snapshot()['runs'][0]['status'], 'completed_with_errors')

    def test_search_ui_has_explicit_provider_and_and_filters(self):
        html = Path(__file__).resolve().parent.parent.joinpath('agent/web/index.html').read_text(encoding='utf-8')
        for identifier in ('board-provider','region','posted-window','required-years','match-threshold','prompt','appliedtab'):
            self.assertIn('id="'+identifier+'"',html)
        self.assertIn('Layer 1 · find jobs by the job titles you enter',html)
        self.assertIn('Layer 2 · match those title candidates before resume generation',html)
        self.assertIn('selected criteria below are AND conditions',html)
        self.assertIn('title_first:true',html)
        self.assertIn("option.disabled=!board.available",html)
        self.assertIn("batch_size:55",html)
        self.assertIn('Agent 4 confirmed submissions',html)

    def test_lever_paginates(self):
        calls = []
        item = {'id':'1','text':'Engineer','hostedUrl':'https://jobs.lever.co/example/1',
                'descriptionPlain':'Java','categories':{'location':'US'}}
        def reader(url):
            calls.append(url)
            return [item]*100 if len(calls)==1 else []
        self.assertEqual(len(fetch_board('lever:example', reader)),100)
        self.assertIn('skip=100',calls[1])
