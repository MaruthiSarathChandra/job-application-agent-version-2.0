import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from agent.dashboard import Service, make_handler
from agent.resume_worker import load_profile

class ProfileTests(unittest.TestCase):
    def test_form_save_reload_worker_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(str(Path(directory)/'agent.db'))
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = HTTPConnection('127.0.0.1', server.server_port)
            def request(method, path, body=None, headers=None):
                client.request(method, path, json.dumps(body) if body else None, headers or {})
                response = client.getresponse()
                return response.status, response.read()
            try:
                status, html = request('GET', '/profile')
                self.assertEqual(status, 200)
                self.assertIn(b'Save profile', html)
                self.assertEqual(json.loads(request('GET', '/api/profile')[1])['profile'], None)
                profile = {'profile_id':'test', 'verified':True,
                           'resume_header':{'display_name':'José', 'contact_lines':[], 'links':[]},
                           'facts':[{'id':'one','section':'Skills','text':'Python','verified':True}]}
                body = {'profile':profile,'revision':None}
                self.assertEqual(request('POST','/api/profile',body,{'Origin':'https://elsewhere.example'})[0],403)
                status, raw = request('POST','/api/profile',body)
                self.assertEqual(status,200)
                safe, version = load_profile(service.profile_path)
                self.assertEqual(version,json.loads(raw)['profile_version'])
                self.assertEqual(safe['_resume_header']['display_name'],'José')
                self.assertEqual(request('POST','/api/profile',body)[0],400)
                current=json.loads(request('GET','/api/profile')[1])
                self.assertEqual(current['profile'],profile)
                profile['facts'][0]['verified']=False
                self.assertEqual(request('POST','/api/profile',{'profile':profile,'revision':current['revision']})[0],400)
                self.assertEqual(json.loads(request('GET','/api/profile')[1]),current)
            finally:
                client.close()
                server.shutdown()
                server.server_close()
                thread.join()
