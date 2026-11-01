import copy
import json
import tempfile
import unittest
from pathlib import Path
from agent.resume_writer import request, validate, validate_draft
from agent.dashboard import Service
from agent.resume_worker import Worker, load_profile
from agent.resume_artifact import Packager
from agent.resume_regeneration import Regenerator, enqueue
from agent.resume_preview import preview_html
from agent.resume_review import approve

def fixture():
    profile={'profile_id':'writer-test','_resume_header':{'display_name':'Example Candidate',
             'contact_lines':['example@example.com'],'links':[]},'facts':[
        {'id':'work','section':'Experience','text':'Engineer at Example, 2025\nBuilt Java APIs. Tested requests with Postman.',
         'layout':[{'kind':'heading','text':'Engineer at Example, 2025'},
                   {'kind':'bullet','text':'Built Java APIs. Tested requests with Postman.'}]},
        {'id':'project','section':'Projects','text':'File service\nBuilt Python file storage and checked uploads. https://example.com/project',
         'layout':[{'kind':'heading','text':'File service'},
                   {'kind':'bullet','text':'Built Python file storage and checked uploads. https://example.com/project'}]},
        {'id':'degree','section':'Education','text':'M.S. Computer Science, Example University, 2024'}]}
    result={'summary':{'text':'Software engineer building Java APIs and Python file-storage projects, with experience testing requests using Postman.',
                       'source_ids':['work','project']},
            'skills':[{'text':'Java','source_ids':['work']},{'text':'Python','source_ids':['project']},
                      {'text':'Postman','source_ids':['work']}],
            'entries':[{'source_id':'work','bullets':['Developed Java APIs.','Tested API requests with Postman.']},
                       {'source_id':'project','bullets':['Built a Python file-storage project.','Checked file uploads.']}],
            'missing_requirements':['Production Kubernetes experience is not documented.']}
    return profile,result

class WriterTests(unittest.TestCase):
    def test_complete_sections_rewritten_bullets_and_locked_education(self):
        p,r=fixture();draft=validate(r,p);validate_draft(draft,p)
        self.assertEqual([s['title'] for s in draft['sections']],['Summary','Skills','Experience','Projects','Education'])
        self.assertEqual(draft['sections'][-1]['facts'][0],p['facts'][-1])
        self.assertIn('Engineer at Example, 2025',draft['sections'][2]['facts'][0]['text'])
        self.assertIn('https://example.com/project',draft['sections'][3]['facts'][0]['text'])
        self.assertTrue(draft['requires_review'])
        schema=request({},p,'test')['text']['format']['schema']
        self.assertEqual(schema['properties']['entries']['items']['properties']['source_id']['enum'],['work','project','degree'])

    def test_invalid_sources_missing_sections_numbers_and_tampering(self):
        p,r=fixture()
        changes=[
            lambda x:x['entries'].pop(),
            lambda x:x['entries'].pop(0),
            lambda x:x['entries'].append(copy.deepcopy(x['entries'][0])),
            lambda x:x['entries'][0]['bullets'].__setitem__(0,'Improved performance by 99 percent.'),
            lambda x:x['summary'].__setitem__('source_ids',['unknown']),
            lambda x:x['skills'][0].__setitem__('text','Kubernetes')]
        for change in changes:
            with self.subTest(change=change):
                value=copy.deepcopy(r);change(value)
                with self.assertRaises(ValueError): validate(value,p)
        draft=validate(r,p);draft['sections'][0]['facts'][0]['text']='Tampered'
        with self.assertRaises(ValueError):validate_draft(draft,p)

    def test_worker_to_docx_preview_review_and_regeneration(self):
        profile,result=fixture()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);service=Service(str(root/'agent.db'))
            raw={'profile_id':profile['profile_id'],'resume_header':profile['_resume_header'],'verified':True,
                 'facts':[dict(f,verified=True) for f in profile['facts']]}
            service.save_profile({'profile':raw,'revision':None})
            safe,version=load_profile(service.profile_path)
            service.fetcher=lambda _:[{'url':'https://example.com/job','title':'Engineer','company':'Example',
                                       'location':'US','description':'Build Java APIs and Python services'}]
            service.start({'prompt':'Engineer','sources':['lever:example']},background=False)
            Worker(service.database,safe,version,'fixture',lambda *args:result).process_one()
            Packager(service.database,safe,version,root/'resumes').process_one()
            app=service.snapshot()['applications'][0]
            self.assertEqual(app['status'],'review_required')
            with service.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE topic='applications.ready.v1'").fetchone()[0],0)
                artifact=db.execute('SELECT path FROM resume_artifacts WHERE id=?',(app['resume_artifact_id'],)).fetchone()
            html=preview_html(Path(artifact['path']).read_bytes())
            for expected in ('SUMMARY','TECHNICAL SKILLS','PROFESSIONAL EXPERIENCE','SELECTED ENGINEERING PROJECTS','EDUCATION','Developed Java APIs'):
                self.assertIn(expected,html)
            with self.assertRaises(ValueError): approve(service,1,'stale-artifact')
            approve(service,1,app['resume_artifact_id'])
            self.assertEqual(service.snapshot()['applications'][0]['status'],'ready_for_application')
            enqueue(service,1)
            Regenerator(service,'fixture',root/'resumes',lambda *args:result).process_one()
            self.assertEqual(service.snapshot()['applications'][0]['status'],'review_required')
            with service.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE topic='applications.ready.v1'").fetchone()[0],0)
