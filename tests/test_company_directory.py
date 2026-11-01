"""Offline tests: company list is a watchlist, not 299 imaginary live APIs."""
import json
import tempfile
import unittest
from pathlib import Path

from agent.company_directory import load_directory, verified_boards, list_companies, discovery_sources
from agent.sources import fetch_source, fetch_smartrecruiters, validate_sources, source_catalog


class DirectoryTests(unittest.TestCase):
    def test_pdf_directory_is_complete_and_does_not_invent_feeds(self):
        companies=load_directory()
        self.assertEqual(len(companies),299)
        self.assertEqual(len({x['id'] for x in companies}),299)
        self.assertEqual(len({x['career_url'] for x in companies}),296)
        self.assertEqual(sum(c['category']=='Technology' for c in companies),44)
        self.assertEqual(len(verified_boards()),10)
        item=list_companies(query='Cloudflare')
        self.assertEqual(item['matched_companies'],1)
        self.assertEqual(item['companies'][0]['board_source'],'greenhouse:cloudflare')
        unknown=list_companies(query='Microsoft')
        self.assertEqual(unknown['matched_companies'],1)
        self.assertFalse(unknown['companies'][0]['board_verified'])
        self.assertEqual(len(discovery_sources()),10)
        self.assertEqual(discovery_sources()[:2],['greenhouse:cloudflare','lever:palantir'])

    def test_dangerous_or_incorrect_ats_mapping_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'mapping.json'
            original=list(verified_boards().values())[0]
            for source, board_url in (
                ('greenhouse:../bad','https://job-boards.greenhouse.io/cloudflare'),
                ('greenhouse:microsoft','https://job-boards.greenhouse.io/cloudflare'),
                ('greenhouse:cloudflare','http://localhost/'),
                ('linkedin:cloudflare','https://job-boards.greenhouse.io/cloudflare')):
                file.write_text(json.dumps({'boards':[{'company_id':'cloudflare','source':source,
                    'board_url':board_url,
                    'proof':'https://job-boards.greenhouse.io/cloudflare/jobs/8212060'}]}))
                with self.assertRaises(ValueError):
                    verified_boards(path=file)

    def test_watchlist_is_not_universal_linkedin_or_indeed_scraper(self):
        validate_sources(['directory:verified'])
        validate_sources(['smartrecruiters:example'])
        for bad in ('linkedin:jobs','indeed:us','directory:all','smartrecruiters:../etc'):
            with self.assertRaises(ValueError):
                validate_sources([bad])
        self.assertTrue(any(x['syntax']=='directory:verified' and x['configured']
                            for x in source_catalog()))

    def test_directory_reads_real_public_board_adapters(self):
        seen=[]
        def reader(url):
            seen.append(url)
            if 'boards-api.greenhouse.io' in url:
                return {'jobs':[{'id':1,'title':'Backend Software Engineer','location':{'name':'Remote - US'},
                                 'absolute_url':'https://job-boards.greenhouse.io/'+url.split('/boards/')[1].split('/jobs')[0]+'/jobs/1',
                                 'content':'<p>Spring Boot APIs</p>'}]}
            if 'api.lever.co' in url:
                return [{'id':'abc','text':'Backend Software Engineer',
                         'hostedUrl':'https://jobs.lever.co/palantir/abc',
                         'categories':{'location':'New York, NY'},'descriptionPlain':'Build REST APIs'}]
            raise AssertionError('Unexpected API host: '+url)
        result=fetch_source('directory:verified',{'title_keywords':['Backend Software Engineer']},reader)
        self.assertEqual(len(result),10)
        self.assertEqual({x['source'] for x in result},set(discovery_sources()))
        self.assertEqual(len(seen),10)
        self.assertTrue(all(x['description'] for x in result))

    def test_one_failed_board_does_not_hide_other_jobs(self):
        def reader(url):
            if '/cloudflare/' in url:
                raise OSError('unavailable')
            if 'boards-api.greenhouse.io' in url:
                return {'jobs': []}
            return []
        rows=fetch_source('directory:verified',{'title_keywords':['Engineer']},reader)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['source'],'greenhouse:cloudflare')
        self.assertIn('source_error',rows[0])


class SmartRecruitersTests(unittest.TestCase):
    def test_full_job_detail_and_location_from_public_api(self):
        called=[]
        def reader(url):
            called.append(url)
            if '?limit=' in url:
                return {'content':[{'id':'12345','name':'Backend Software Engineer'},
                                   {'id':'12','name':'Office Manager'}],'totalFound':2}
            self.assertIn('/postings/12345',url)
            return {'id':'12345','name':'Backend Software Engineer',
                    'applyUrl':'https://jobs.smartrecruiters.com/Example/12345',
                    'company':{'name':'Example Engineering'},
                    'location':{'city':'Kansas City','region':'MO','country':'US'},
                    'jobAd':{'sections':{'jobDescription':{'text':'<p>Build Java services</p>'},
                                          'qualifications':{'text':'Spring Boot'},
                                          'additionalInformation':{'text':'REST APIs'}}}}
        jobs=fetch_smartrecruiters('smartrecruiters:example',
                                   {'title_keywords':['Software Engineer']},reader)
        self.assertEqual(len(jobs),1)
        job=jobs[0]
        self.assertEqual(job['company'],'Example Engineering')
        self.assertEqual(job['location'],'Kansas City, MO, US')
        self.assertEqual(job['country_code'],'US')
        self.assertIn('Spring Boot',job['description'])
        self.assertEqual(job['description_completeness'],'full')
        self.assertEqual(len(called),2)
        self.assertTrue(all(url.startswith('https://api.smartrecruiters.com/') for url in called))

    def test_no_full_detail_cannot_be_misrepresented_as_complete(self):
        def reader(url):
            if '?limit=' in url:
                return {'content':[{'id':'123','name':'Software Engineer'}]}
            return {'id':'123','name':'Software Engineer',
                    'applyUrl':'https://jobs.smartrecruiters.com/Acme/123',
                    'company':{'name':'Acme'},'location':{'country':'US'},'jobAd':{'sections':{}}}
        job=fetch_source('smartrecruiters:acme',{'title_keywords':['Software Engineer']},reader)[0]
        self.assertEqual(job['description_completeness'],'excerpt')
        self.assertEqual(job['description'],'Full JD unavailable')

    def test_invalid_detail_is_not_queued_as_job(self):
        def reader(url):
            if '?limit=' in url:
                return {'content':[{'id':'123','name':'Software Engineer'}]}
            return {'id':'123','jobAd':{'sections':{}}}
        jobs=fetch_smartrecruiters('smartrecruiters:acme',
                                  {'title_keywords':['Software Engineer']},reader)
        self.assertEqual(len(jobs),1)
        self.assertIn('source_error',jobs[0])


if __name__=='__main__':
    unittest.main()
