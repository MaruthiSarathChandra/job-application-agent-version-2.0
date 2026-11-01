import tempfile
import unittest
from pathlib import Path
from agent.intake import IntakeStore, normalize_url, detect_ats, qualify


class IntakeTests(unittest.TestCase):
    def test_workday_aliases(self):
        base = 'https://acme.myworkdayjobs.com/Careers/job/US/Engineer_R1'
        self.assertEqual(normalize_url(f'[{base}]({base}/apply?utm_source=x#top)'), base)

    def test_reject_credentials_and_invalid_url(self):
        for url in ('javascript:alert(1)', 'https://user:secret@example.com/job', 'not a url'):
            with self.assertRaises(ValueError):
                normalize_url(url)

    def test_preserve_requisition_query(self):
        self.assertEqual(normalize_url('https://example.com/jobs?req=12&utm_source=x'),
                         'https://example.com/jobs?req=12')

    def test_ats_host_boundary(self):
        self.assertEqual(detect_ats('https://ats.rippling.com/acme/jobs/1'), 'rippling')
        self.assertEqual(detect_ats('https://evilmyworkdayjobs.com/job'), 'career_site')

    def test_exclude_senior(self):
        self.assertEqual(qualify({'title':'Senior Software Engineer','location':'US'},
            {'excluded_title_keywords':['senior']})['status'], 'excluded')

    def test_repeated_import_and_requisition_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'agent.db'
            job = dict(url='https://example.com/job/1', title='Software Engineer',
                company='Example', location='US', description='Build APIs',
                employer_key='example', requisition_id='1')
            store = IntakeStore(path)
            first = store.import_job(job, {})
            store.close()
            store = IntakeStore(path)
            second = store.import_job({**job,'url':'https://example.com/alternate/1'}, {})
            self.assertEqual(first['job_id'], second['job_id'])
            self.assertEqual(store.connection.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM intake_attempts').fetchone()[0], 2)
            store.close()

    def test_missing_jd_is_not_imported(self):
        store = IntakeStore(':memory:')
        with self.assertRaises(ValueError):
            store.import_job({'url':'https://example.com'}, {})
        self.assertEqual(store.connection.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        store.close()
