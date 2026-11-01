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

class ImportTests(unittest.TestCase):
    def resume(self):
        import io
        from docx import Document
        from agent.resume_artifact import add_link
        d=Document()
        d.add_paragraph('Example Candidate')
        d.add_paragraph('Software Engineer | Java')
        p=d.add_paragraph('Example City | ')
        add_link(p,'GitHub','https://github.com/example')
        d.add_paragraph('SUMMARY')
        d.add_paragraph('Builds backend systems.')
        d.add_paragraph('TECHNICAL SKILLS')
        d.add_paragraph('Languages: Java, Python')
        d.add_paragraph('PROFESSIONAL EXPERIENCE')
        d.add_paragraph().add_run('Engineer — Example\t2024–Present').bold=True
        d.add_paragraph('Example City')
        d.add_paragraph('Built tested APIs.',style='List Bullet')
        d.add_paragraph('PROJECTS')
        p=d.add_paragraph()
        p.add_run('Project\t').bold=True
        add_link(p,'GitHub','https://github.com/example/project')
        d.add_paragraph('Improved reliability.',style='List Bullet')
        d.add_paragraph('EDUCATION')
        d.add_paragraph('M.S. Computer Science')
        out=io.BytesIO()
        d.save(out)
        return out.getvalue()

    def test_import_review_validation_and_render(self):
        from agent.profile_import import import_docx
        from agent.resume_worker import validate_profile, validate_result, build_request
        from agent.resume_artifact import render_docx, validate_docx
        p=import_docx(self.resume())['profile']
        self.assertFalse(p['verified'])
        with self.assertRaises(ValueError):
            validate_profile(p)
        p['verified']=True
        for f in p['facts']:
            f['verified']=True
        safe,_=validate_profile(p)
        role=next(f for f in safe['facts'] if f['section']=='Experience')
        self.assertEqual([b['kind'] for b in role['layout']],['heading','metadata','bullet'])
        draft=validate_result({'ordered_fact_ids':[f['id'] for f in safe['facts']],'missing_requirements':[]},safe)
        raw=render_docx(draft,safe['_resume_header'])
        self.assertEqual(validate_docx(raw,draft,safe['_resume_header'])['status'],'passed')
        self.assertNotIn('layout',build_request({},safe,'test')['input'])
        p['facts'][0]['layout'][0]['text']='Altered claim'
        with self.assertRaisesRegex(ValueError,'differs'):
            validate_profile(p)

    def test_bad_archive_and_tables_rejected(self):
        import io
        from docx import Document
        from agent.profile_import import import_docx
        with self.assertRaises(ValueError):
            import_docx(b'not a DOCX')
        d=Document();d.add_table(rows=1,cols=1);out=io.BytesIO();d.save(out)
        with self.assertRaisesRegex(ValueError,'table'):
            import_docx(out.getvalue())

    def test_http_import_does_not_save_and_preview_uses_saved_profile(self):
        from agent.profile_import import import_docx
        with tempfile.TemporaryDirectory() as directory:
            service=Service(str(Path(directory)/'agent.db'))
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(service))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            client=HTTPConnection('127.0.0.1',server.server_port)
            try:
                client.request('POST','/api/profile/import',self.resume())
                response=client.getresponse();data=json.loads(response.read())
                self.assertEqual(response.status,200)
                self.assertIsNone(service.read_profile()['profile'])
                p=data['profile'];p['verified']=True
                for f in p['facts']: f['verified']=True
                service.save_profile({'profile':p,'revision':None})
                client.request('GET','/api/profile/resume')
                response=client.getresponse()
                self.assertEqual(response.status,200)
                self.assertTrue(response.read().startswith(b'PK'))
                self.assertEqual(service.snapshot()['applications'],[])
            finally:
                client.close();server.shutdown();server.server_close();thread.join()
