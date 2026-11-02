"""Two-layer architecture: title-first provider discovery then unchanged qualification."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs,urlsplit

from agent.dashboard import Service, rules_from_request
from agent.discovery_pages import fetch_slice, acknowledge_slice
from agent.sources import fetch_source
from agent.title_search import requested_title, title_candidate


class TitleOnlyUnitTests(unittest.TestCase):
    def test_title_boundary_case_and_punctuation(self):
        titles=['Software Engineer','Java Developer']
        for title in ('Software Engineer, Backend','software-engineer (platform)',
                      'Java Developer', 'Junior Software Engineer'):
            self.assertIsNotNone(requested_title(title,titles),title)
        for title in ('Senior Software Engineer','Staff Software Engineer',
                      'Principal Software Engineer','Software Engineer, Lead',
                      'Sr. Java Developer','Software Engineering Manager',
                      'Bioengineering Specialist','Mechanical Designer'):
            self.assertIsNone(requested_title(title,titles),title)
        self.assertEqual(requested_title('Senior Software Engineer',
                         ['Software Engineer','Senior Software Engineer']),
                         'Senior Software Engineer')
        self.assertEqual(requested_title('Sr. Java Developer',
                         ['Sr Java Developer']),'Sr Java Developer')
        candidate=title_candidate({'title':'Software Engineer, Platform',
                                    'description':'Cloud'}, {'title_keywords':titles})
        self.assertEqual(candidate['discovery_layer'],'title_candidate_v1')
        self.assertEqual(candidate['requested_role'],'Software Engineer')
        self.assertIsNone(title_candidate({'title':'Senior Software Engineer'},
                                            {'title_keywords':titles}))

    def test_layer_one_only_uses_title_not_location_or_jd(self):
        job={'title':'Software Engineer','location':'Argentina','description':'No relevant skills'}
        rules={'title_keywords':['Software Engineer'],'country_codes':['US'],
               'jd_keywords':['Spring Boot'],'min_match_percent':100}
        self.assertIsNotNone(title_candidate(job,rules))
        self.assertIsNone(title_candidate({**job,'title':'Marketing Manager'},rules))


class TitleFirstProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.service=Service(str(Path(self.temp.name)/'agent.db'))
        self.rules={'title_keywords':['Software Engineer','Java Developer'],
                    'title_first':True,'batch_size':55}
    def tearDown(self):
        self.temp.cleanup()

    def test_smartrecruiters_sends_real_q_get_request_and_rotates_titles(self):
        calls=[]
        def reader(url):
            calls.append(url)
            if url.endswith('/postings/one'):
                return {'id':'one','name':'Software Engineer','applyUrl':'https://jobs.example.org/one',
                        'location':{'country':'US'},'jobAd':{'sections':{'jobDescription':{'text':'Java and APIs'}}}}
            if url.endswith('/postings/two'):
                return {'id':'two','name':'Java Developer','applyUrl':'https://jobs.example.org/two',
                        'location':{'country':'US'},'jobAd':{'sections':{'jobDescription':{'text':'Java'}}}}
            q=parse_qs(urlsplit(url).query)['q'][0]
            self.assertIn(q,self.rules['title_keywords'])
            return {'content':[{'id':'one','name':'Software Engineer'}] if q=='Software Engineer'
                              else [{'id':'two','name':'Java Developer'}]}
        jobs=fetch_slice(self.service,'smartrecruiters:example',self.rules,reader)
        self.assertEqual([j['title'] for j in jobs],['Software Engineer'])
        self.assertEqual(jobs[0]['source_title_query'],'Software Engineer')
        self.assertTrue(any('q=Software+Engineer' in url for url in calls))
        acknowledge_slice(self.service,'smartrecruiters:example',self.rules)
        jobs=fetch_slice(self.service,'smartrecruiters:example',self.rules,reader)
        self.assertEqual([j['title'] for j in jobs],['Java Developer'])
        self.assertTrue(any('q=Java+Developer' in url for url in calls))

    def test_greenhouse_title_first_indexes_board_then_slices_matching_only(self):
        calls=[]
        raw=[{'id':i,'title':'Software Engineer' if i%10==0 else 'Marketing Manager',
              'absolute_url':f'https://jobs.example.org/{i}',
              'location':{'name':'US'},'content':'Java'} for i in range(120)]
        def reader(url):
            calls.append(url)
            return {'jobs':raw}
        jobs=fetch_slice(self.service,'greenhouse:example',self.rules,reader)
        self.assertEqual(len(jobs),12)
        self.assertTrue(all(j['title']=='Software Engineer' for j in jobs))
        acknowledge_slice(self.service,'greenhouse:example',self.rules)
        self.assertEqual(len(fetch_slice(self.service,'greenhouse:example',self.rules,reader)),12)
        self.assertEqual(len(calls),2)  # source list re-fetches on a new traversal

    def test_lever_still_uses_bounded_pages_and_local_title_screening(self):
        calls=[]
        def reader(url):
            calls.append(url)
            offset=int(parse_qs(urlsplit(url).query).get('skip',['0'])[0])
            rows=[{'id':str(i),'text':'Software Engineer' if i%10==0 else 'Marketing Manager',
                   'hostedUrl':f'https://jobs.example.org/{i}',
                   'descriptionPlain':'Java','categories':{'location':'US'}}
                  for i in range(offset,55 if offset==0 else 65)]
            return rows
        jobs=fetch_slice(self.service,'lever:example',self.rules,reader)
        self.assertEqual(len(jobs),6)
        self.assertIn('skip=0',calls[0])
        acknowledge_slice(self.service,'lever:example',self.rules)
        later=fetch_slice(self.service,'lever:example',self.rules,reader)
        self.assertEqual([j['requisition_id'] for j in later],['60'])
        self.assertIn('skip=55',calls[1])

    def test_direct_smartrecruiters_queries_titles_and_deduplicates(self):
        calls=[]
        def reader(url):
            calls.append(url)
            if '/postings/same' in url:
                return {'id':'same','name':'Software Engineer',
                        'applyUrl':'https://jobs.example.org/same',
                        'location':{'country':'US'},
                        'jobAd':{'sections':{'jobDescription':{'text':'Java'}}}}
            return {'content':[{'id':'same','name':'Software Engineer'}]}
        result=fetch_source('smartrecruiters:example',self.rules,reader)
        self.assertEqual(len(result),1)
        self.assertEqual(sum('q=' in url for url in calls),2)


class TwoLayerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.service=Service(str(Path(self.temp.name)/'agent.db'))
    def tearDown(self):
        self.temp.cleanup()
    def test_candidate_match_is_distinct_from_eligibility_match(self):
        today=datetime.now(timezone.utc).isoformat()
        rows=[
            {'url':'https://careers.example.org/1','title':'Software Engineer',
             'company':'Example','location':'US','description':'Build Java Spring Boot APIs',
             'source_posted_at':today},
            {'url':'https://careers.example.org/2','title':'Senior Software Engineer',
             'company':'Example','location':'US','description':'Build Java Spring Boot APIs',
             'source_posted_at':today},
            {'url':'https://careers.example.org/3','title':'Marketing Manager',
             'company':'Example','location':'US','description':'Build Java Spring Boot APIs',
             'source_posted_at':today},
            {'url':'https://careers.example.org/4','title':'Software Engineer',
             'company':'Example','location':'Argentina','description':'Build Java Spring Boot APIs',
             'source_posted_at':today}
        ]
        self.service.fetcher=lambda _:rows
        self.service.start({'prompt':'Software Engineer','sources':['lever:example'],
                            'title_first':True,
                            'excluded_title_keywords':[],
                            'country_codes':['US'],'jd_keywords':['Java']},
                           background=False)
        state=self.service.snapshot()
        self.assertEqual(state['saved_stats']['total'],2)
        self.assertEqual(state['saved_stats']['matched'],1)
        self.assertEqual(state['saved_stats']['excluded'],1)
        self.assertEqual(len(state['queue']),1)
        self.assertTrue(all(j['requested_role']=='Software Engineer' for j in state['jobs']))
        self.assertEqual(state['source_scans'][0]['title_filtered'],2)
        self.assertEqual(state['source_scans'][0]['queued'],1)
        self.assertEqual(state['qualified_jobs_count'],1)
        self.assertEqual(len(state['qualified_jobs']),1)
        self.assertEqual(state['qualified_jobs'][0]['title'],'Software Engineer')
        self.assertEqual(state['latest_jobs_count'],2)
        self.assertTrue(self.service.last_manual_request()['title_first'])
        self.service.scheduled_scan(background=False)
        repeated=self.service.snapshot()
        self.assertTrue(__import__('json').loads(repeated['runs'][0]['config'])['rules']['title_first'])
        self.assertEqual(len(repeated['queue']),1)  # no repeat GPT work

    def test_existing_senior_resume_is_blocked_when_search_changes(self):
        from agent.handoff import current_decision
        senior={'url':'https://careers.example.org/senior','title':'Senior Software Engineer',
                'company':'Example','location':'US','description':'Java and Spring Boot'}
        self.service.fetcher=lambda _: [senior]
        self.service.start({'prompt':'Senior Software Engineer',
                            'sources':['lever:example'],'title_first':True},background=False)
        old_queue=self.service.snapshot()['queue']
        self.assertEqual(len(old_queue),1)
        old_job=__import__('json').loads(old_queue[0]['snapshot'])
        self.service.start({'prompt':'Software Engineer',
                            'sources':['lever:example'],'title_first':True},background=False)
        self.assertEqual(current_decision(self.service,old_job)['status'],'excluded')
        self.assertEqual(len(self.service.snapshot()['queue']),1)
        self.assertEqual(self.service.snapshot()['qualified_jobs_count'],0)

    def test_strict_search_filters_are_all_required_even_for_100_percent_skills(self):
        from agent.intake import qualify
        from datetime import timedelta
        now=datetime.now(timezone.utc)
        rules={'title_first':True,'title_keywords':['Software Engineer'],
               'country_codes':['US'],'posted_within_hours':7,
               'max_required_years':1,'min_match_percent':85,
               'profile_skills':['Java','Spring Boot']}
        job={'title':'Software Engineer','location':'United States',
             'description':'At least 1 year of software development experience using Java and Spring Boot.',
             'source_posted_at':now.isoformat()}
        self.assertEqual(qualify(job,rules)['status'],'matched')
        for change in (
            {'title':'Senior Software Engineer'},
            {'location':'Toronto, Canada'},
            {'description':'At least 3 years of software development experience using Java and Spring Boot.'},
            {'description':'At least 1 year of software development experience using Python and Rust.'},
            {'source_posted_at':(now-timedelta(hours=10)).isoformat()}
        ):
            with self.subTest(change=change):
                self.assertNotEqual(qualify({**job,**change},rules)['status'],'matched')

    def test_title_first_param_must_be_boolean(self):
        base={'prompt':'Software Engineer','sources':['lever:example']}
        with self.assertRaises(ValueError):
            rules_from_request({**base,'title_first':'yes'})
        self.assertTrue(rules_from_request({**base,'title_first':True})[1]['title_first'])


if __name__=='__main__':
    unittest.main()
