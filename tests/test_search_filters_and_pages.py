import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone
from agent.dashboard import Service,rules_from_request
from agent.intake import qualify
from agent.matching import required_years,assess
from agent.discovery_pages import fetch_slice,acknowledge_slice,resolve_sources,round_robin_sources

class FiltersTests(unittest.TestCase):
    def test_experience_extracts_requirements_not_company_age_or_preference(self):
        self.assertEqual(required_years('Founded 20 years ago. Must have 2–4 years of Java development experience.')['minimum'],2)
        self.assertEqual(required_years('Two years of software engineering experience required. 5 years of experience preferred.')['minimum'],2)
        self.assertIsNone(required_years('Founded in 2020. 3 years of experience preferred.')['minimum'])
        self.assertEqual(required_years("At least 4+ years' experience with Java.")['minimum'],4)
        self.assertEqual(required_years('No prior experience required.')['minimum'],0)
    def test_all_selected_filters_are_required_and_titles_are_alternatives(self):
        job=dict(title='Java Developer',location='US',description='2 years of experience with Java and Python.',source_posted_at=datetime.now(timezone.utc).isoformat())
        rules=dict(title_keywords=['Software Engineer','Java Developer'],country_codes=['US'],posted_within_hours=24,max_required_years=2,min_match_percent=70,profile_skills=['Java','Python'])
        self.assertEqual(qualify(job,rules)['status'],'matched')
        for change in ({'country_codes':['CA']},{'max_required_years':1},{'profile_skills':['Java']},{'title_keywords':['Designer']}):
            self.assertNotEqual(qualify(job,{**rules,**change})['status'],'matched')
        self.assertNotEqual(qualify({**job,'source_posted_at':None},rules)['status'],'matched')
        self.assertEqual(assess(job,{'profile_skills':['Java']})['score'],50)
        self.assertIsNone(assess({'description':'Be a nice person'},rules)['score'])
    def test_unknown_experience_does_not_pass_selected_ceiling(self):
        self.assertEqual(qualify(dict(title='Engineer',location='US',description='Build Java APIs'),{'max_required_years':2})['status'],'excluded')
    def test_invalid_threshold_and_year_values_rejected(self):
        base={'prompt':'Engineer','sources':['lever:test']}
        for value in (True,-1,101,71,'70'):
            with self.assertRaises(ValueError):rules_from_request({**base,'min_match_percent':value})
        for value in (True,-1,21,'2'):
            with self.assertRaises(ValueError):rules_from_request({**base,'max_required_years':value})
    def test_provider_selection_is_exact_and_missing_connectors_fail(self):
        configured=['greenhouse:a','lever:b','greenhouse:c']
        self.assertEqual(resolve_sources('lever',defaults=configured),['lever:b'])
        self.assertEqual(resolve_sources('all',defaults=configured),configured)
        with self.assertRaises(ValueError):resolve_sources('linkedin',defaults=configured)
        with self.assertRaises(ValueError):resolve_sources('ashby',defaults=configured)
        self.assertEqual(round_robin_sources(configured),['greenhouse:a','lever:b','greenhouse:c'])

class PageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.service=Service(str(Path(self.tmp.name)/'agent.db'))
        self.rules={'title_keywords':['Engineer'],'batch_size':55}
    def tearDown(self):self.tmp.cleanup()
    def lever(self,index):
        return {'id':str(index),'text':'Engineer','hostedUrl':f'https://example.com/{index}','descriptionPlain':'Java','categories':{'location':'US'}}
    def test_lever_requests_55_and_restart_replays_before_advancing(self):
        calls=[]
        def reader(url):
            calls.append(url)
            return [self.lever(i) for i in range(55)] if 'skip=0' in url else [self.lever(55)]
        first=fetch_slice(self.service,'lever:example',self.rules,reader)
        self.assertEqual(len(first),55);self.assertIn('limit=55&skip=0',calls[0])
        other=Service(self.service.database)
        self.assertEqual(fetch_slice(other,'lever:example',self.rules,reader),first)
        self.assertEqual(len(calls),1)
        acknowledge_slice(other,'lever:example',self.rules)
        second=fetch_slice(other,'lever:example',self.rules,reader)
        self.assertEqual(len(second),1);self.assertIn('skip=55',calls[-1])
    def test_full_board_cache_slices_without_refetch_and_resets_after_end(self):
        calls=[]
        def reader(url):
            calls.append(url)
            return {'jobs':[{'id':i,'title':'Engineer','absolute_url':f'https://example.com/{i}','content':'Java','location':{'name':'US'}} for i in range(120)]}
        for size in (55,55,10):
            self.assertEqual(len(fetch_slice(self.service,'greenhouse:example',self.rules,reader)),size)
            acknowledge_slice(self.service,'greenhouse:example',self.rules)
        self.assertEqual(len(calls),1)
        fetch_slice(self.service,'greenhouse:example',self.rules,reader)
        self.assertEqual(len(calls),2)
    def test_greenhouse_fetches_real_publication_date_only_for_relevant_titles(self):
        calls=[];stamp=datetime.now(timezone.utc).isoformat()
        def reader(url):
            calls.append(url)
            if '/jobs/1' in url:return {'first_published':stamp}
            return {'jobs':[{'id':i,'title':'Engineer' if i==1 else 'Lawyer','absolute_url':f'https://example.com/{i}','content':'Java','location':{'name':'US'}} for i in (1,2)]}
        rows=fetch_slice(self.service,'greenhouse:example',{**self.rules,'posted_within_hours':24},reader)
        self.assertEqual(rows[0]['source_posted_at'],stamp)
        self.assertNotIn('source_posted_at',rows[1]);self.assertEqual(len(calls),2)
    def test_failed_request_does_not_advance_cursor(self):
        def broken(url):raise OSError('offline')
        with self.assertRaises(OSError):fetch_slice(self.service,'lever:example',self.rules,broken)
        calls=[]
        fetch_slice(self.service,'lever:example',self.rules,lambda url:calls.append(url) or [])
        self.assertIn('skip=0',calls[0])
    def test_search_change_starts_its_own_cursor(self):
        def reader(url):return [self.lever(i) for i in range(55)]
        fetch_slice(self.service,'lever:example',self.rules,reader)
        acknowledge_slice(self.service,'lever:example',self.rules)
        calls=[]
        fetch_slice(self.service,'lever:example',{**self.rules,'title_keywords':['Developer']},lambda url:calls.append(url) or [])
        self.assertIn('skip=0',calls[0])

    def test_scan_persists_all_filters_and_deduplicates_replayed_slice(self):
        from agent.resume_worker import validate_profile
        raw={'profile_id':'test','verified':True,'resume_header':{'display_name':'Example','contact_lines':['example@example.com'],'links':[]},'facts':[{'id':'s','section':'Skills','text':'Java Python','verified':True}]}
        self.service.save_profile({'profile':raw,'revision':None})
        job={'url':'https://example.com/job','title':'Engineer','company':'Example','location':'US','description':'2 years of experience with Java and Python.',
             'source_posted_at':datetime.now(timezone.utc).isoformat()}
        calls=[]
        self.service.fetcher=lambda source:calls.append(source) or [job]
        body={'prompt':'Engineer','board_provider':'lever','sources':['greenhouse:a','lever:b'],
              'country_codes':['US'],'posted_within_hours':24,'max_required_years':2,'min_match_percent':70,'batch_size':55}
        self.service.start(body,background=False)
        self.service.scheduled_scan(background=False)
        self.assertEqual(calls,['lever:b','lever:b'])
        state=self.service.snapshot()
        self.assertEqual(len(state['queue']),1)
        self.assertEqual(state['jobs'][0]['match_evidence']['score'],100)
        self.assertEqual(self.service.last_manual_request()['max_required_years'],2)
        self.assertEqual(self.service.last_manual_request()['min_match_percent'],70)
    def test_region_names_work_without_mistaking_california_for_canada(self):
        from agent.intake import country_from_job
        for label,code in [('Toronto, Canada','CA'),('London, UK','GB'),('Hyderabad, India','IN'),('Sydney, Australia','AU'),('Berlin, Germany','DE'),('San Francisco, CA','US')]:
            self.assertEqual(country_from_job({'location':label}),code)

    def test_database_failure_replays_pending_page_without_skipping_jobs(self):
        from agent import discovery_pages
        body={'prompt':'Engineer','sources':['lever:example'],'batch_size':55}
        rows=[self.lever(1)]
        real_slice=discovery_pages.fetch_slice
        calls=[]
        def sliced(owner,source,rules):
            return real_slice(owner,source,rules,lambda url:calls.append(url) or rows)
        with self.service.connect() as db:
            db.execute("CREATE TRIGGER fail_queue BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with patch('agent.discovery_pages.fetch_slice',side_effect=sliced):
            self.service.start(body,background=False)
            self.assertEqual(self.service.snapshot()['queue'],[])
            with self.service.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM discovery_pending_slices').fetchone()[0],1)
                db.execute('DROP TRIGGER fail_queue')
            self.service.start(body,background=False)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(self.service.snapshot()['queue']),1)
    def test_current_board_selection_blocks_old_queue_jobs(self):
        from agent.handoff import current_decision
        self.service.fetcher=lambda source:[dict(url='https://example.com/a',title='Engineer',company='Example',location='US',description='Java',source=source)]
        self.service.start({'prompt':'Engineer','sources':['greenhouse:a']},background=False)
        old=json.loads(self.service.snapshot()['queue'][0]['snapshot'])
        self.service.fetcher=lambda source:[]
        self.service.start({'prompt':'Engineer','sources':['lever:b']},background=False)
        self.assertEqual(current_decision(self.service,old)['status'],'excluded')
