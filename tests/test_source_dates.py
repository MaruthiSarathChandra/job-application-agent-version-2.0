"""Provider timestamp meanings: never equate API modification/listing with publication."""
from unittest.mock import patch
import unittest
from agent.sources import fetch_board, fetch_source


class ProviderTimestampTests(unittest.TestCase):
    def test_greenhouse_update_is_not_publication_time(self):
        rows={'jobs':[{'id':1,'title':'Software Engineer','absolute_url':'https://job-boards.greenhouse.io/example/jobs/1',
                       'location':{'name':'San Francisco, CA'},'content':'<p>Build APIs</p>',
                       'updated_at':'2026-11-01T12:00:00Z'}]}
        job=fetch_board('greenhouse:example',lambda _:rows)[0]
        self.assertEqual(job['source_updated_at'],'2026-11-01T12:00:00Z')
        self.assertNotIn('source_posted_at',job)

    def test_lever_created_and_modified_not_publication(self):
        rows=[{'id':'one','text':'Software Engineer',
               'hostedUrl':'https://jobs.lever.co/company/one',
               'descriptionPlain':'Build APIs',
               'createdAt':1793534400000,'updatedAt':1793538000000,
               'categories':{'location':'United States'}}]
        job=fetch_board('lever:company',lambda _:rows)[0]
        self.assertIn('source_created_at',job)
        self.assertIn('source_updated_at',job)
        self.assertNotIn('source_posted_at',job)

    def test_ashby_publication_is_precise_datetime(self):
        rows={'jobs':[{'title':'Software Engineer','isListed':True,
                       'jobUrl':'https://jobs.ashbyhq.com/company/engineer',
                       'location':'San Francisco, CA','descriptionPlain':'Build APIs',
                       'publishedAt':'2026-11-01T12:00:00Z'}]}
        job=fetch_board('ashby:company',lambda _:rows)[0]
        self.assertEqual(job['source_posted_at'],'2026-11-01T12:00:00Z')

    def test_smartrecruiters_released_datetime_is_employer_provided(self):
        def reader(url):
            if 'limit=100' in url:
                return {'content':[{'id':'123','name':'Software Engineer'}]}
            return {'name':'Software Engineer','applyUrl':'https://jobs.smartrecruiters.com/Company/123',
                    'company':{'name':'Company'},'location':{'city':'Austin','country':'US'},
                    'releasedDate':'2026-11-01T12:00:00Z',
                    'jobAd':{'sections':{'jobDescription':{'text':'Build software'}}}}
        jobs=fetch_source('smartrecruiters:company',{'title_keywords':['Software Engineer']},reader)
        self.assertEqual(jobs[0]['source_posted_at'],'2026-11-01T12:00:00Z')

    def test_adzuna_created_is_aggregator_listing_not_employer_published(self):
        def reader(url):
            return {'results':[{'id':123,'title':'Software Engineer',
                'redirect_url':'https://example.com/job/123',
                'company':{'display_name':'Example'},'location':{'display_name':'United States'},
                'description':'Build a REST API','created':'2026-11-01T12:00:00Z'}]}
        with patch.dict('os.environ',{'ADZUNA_APP_ID':'placeholder','ADZUNA_APP_KEY':'placeholder'}):
            job=fetch_source('adzuna:us',{'title_keywords':['Software Engineer']},reader)[0]
        self.assertEqual(job['source_listed_at'],'2026-11-01T12:00:00Z')
        self.assertNotIn('source_posted_at',job)


if __name__=='__main__':
    unittest.main()
