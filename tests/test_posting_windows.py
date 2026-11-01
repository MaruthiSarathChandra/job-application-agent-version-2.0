"""Falsifiable acceptance tests for strict publication windows and job relevance."""
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import unittest

from agent.dashboard import Service, rules_from_request
from agent.freshness import date_decision, freshness, parse_published_at
from agent.intake import qualify


class PublicationWindows(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026, 11, 1, 18, 0, 0, tzinfo=timezone.utc)

    def test_three_requested_windows_boundary_and_timezone(self):
        for hours in (7,24,72):
            inside=(self.now-timedelta(hours=hours)+timedelta(seconds=1)).isoformat()
            outside=(self.now-timedelta(hours=hours)-timedelta(seconds=1)).isoformat()
            with self.subTest(hours=hours):
                self.assertEqual(freshness({'source_posted_at':inside},hours,self.now)['status'],'in_window')
                self.assertEqual(freshness({'source_posted_at':outside},hours,self.now)['status'],'too_old')
                self.assertEqual(freshness({'source_posted_at':inside.replace('+00:00','Z')},hours,self.now)['status'],'in_window')
                self.assertEqual(date_decision({'source_posted_at':outside},{'posted_within_hours':hours},
                                                self.now)['status'],'excluded')

    def test_unknown_publication_date_cannot_be_fabricated_from_first_seen(self):
        for posted in (None,'','2026-11-01','2026-11-01T17:00:00','garbage','2077-01-01T00:00:00Z'):
            job={'source_posted_at':posted,'first_seen_at':self.now.isoformat()}
            self.assertEqual(date_decision(job,{'posted_within_hours':24},self.now)['status'],'date_unknown')
        self.assertIsNone(parse_published_at('2026-11-01'))
        self.assertIsNone(parse_published_at('2026-11-01T15:03:00'))
        self.assertIsNone(date_decision({}, {'posted_within_hours':0}, self.now))
        self.assertEqual(freshness({},0,self.now)['status'],'unrestricted')

    def test_exclusions_and_job_description_before_recency(self):
        base={'title':'Backend Software Engineer','location':'Kansas City, MO, US',
              'description':'Build Java Spring Boot APIs'}
        rules={'title_keywords':['Backend Software Engineer'],'locations':['Kansas City'],
               'country_codes':['US'],'jd_keywords':['Java'],'posted_within_hours':24}
        self.assertEqual(qualify(base,rules)['status'],'date_unknown')
        self.assertEqual(qualify({**base,'title':'Senior Backend Software Engineer'},
                                 {**rules,'excluded_title_keywords':['Senior']})['status'],'excluded')
        self.assertEqual(qualify({**base,'location':'Argentina'},rules)['status'],'excluded')
        self.assertEqual(qualify({**base,'source_posted_at':'2020-01-01T00:00:00Z'},rules)['status'],'excluded')
        self.assertEqual(qualify({**base,'description_completeness':'excerpt'},rules)['status'],'needs_full_jd')

    def test_country_in_exact_locations_is_canonicalized(self):
        for country in ('united states','United States of America','USA','US'):
            request={'prompt':'Software Engineer','sources':['lever:example'],
                     'locations':[country],'country_codes':['US'],'posted_within_hours':7}
            _,rules,_=rules_from_request(request)
            self.assertEqual(rules['locations'],[])
            self.assertEqual(rules['country_codes'],['US'])
        _,rules,_=rules_from_request({'prompt':'Software Engineer','sources':['lever:example'],
                                      'locations':['United States'],'country_codes':[]})
        self.assertEqual(rules['country_codes'],['US'])
        for invalid in (True, '24', -1, 1, 12, 73):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    rules_from_request({'prompt':'Software Engineer','sources':['lever:example'],
                                        'posted_within_hours':invalid})


class SequentialScanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.database=str(Path(self.temp.name)/'agent.db')
        self.now=datetime.now(timezone.utc)
        self.req={'prompt':'Software Engineer','sources':['lever:first','lever:second','lever:third'],
                  'locations':['United States'],'country_codes':['US'],'posted_within_hours':24}
    def tearDown(self):
        self.temp.cleanup()
    def job(self,identity,title='Software Engineer',date=None):
        result={'url':f'https://jobs.example.com/{identity}','title':title,
                'company':'Example','location':'San Francisco, California',
                'description':'Build services','employer_key':'test','requisition_id':identity}
        if date is not None:
            result['source_posted_at']=date
        return result

    def test_failure_then_continues_one_source_at_a_time_and_gates_ages(self):
        called=[]
        recent=(self.now-timedelta(hours=3)).isoformat()
        old=(self.now-timedelta(days=4)).isoformat()
        def fetch(source):
            called.append(source)
            if source=='lever:first':
                raise OSError('Network unavailable')
            if source=='lever:second':
                return [self.job('j1',date=recent),self.job('j2',date=old),
                        self.job('j3'),self.job('j4',title='Office Manager',date=recent)]
            return [self.job('j5',date=recent)]
        service=Service(self.database,fetcher=fetch)
        service.start(self.req,background=False)
        state=service.snapshot()
        self.assertEqual(called,self.req['sources'])
        self.assertEqual(state['runs'][0]['status'],'completed_with_errors')
        self.assertEqual(len(state['queue']),2)
        self.assertEqual(state['saved_stats']['total'],3) # only relevant titles are stored
        second=next(x for x in state['source_scans'] if x['source']=='lever:second')
        self.assertEqual((second['fetched'],second['processed'],second['title_filtered']), (4,4,1))
        self.assertEqual((second['matched'],second['date_unknown'],second['too_old'],second['queued']),
                         (1,1,1,1))
        self.assertEqual(len(state['latest_jobs']),3)
        self.assertTrue(any(j['qualification']['status']=='date_unknown'
                            for j in state['latest_jobs']))
        self.assertTrue(any(j['qualification']['status']=='excluded'
                            for j in state['latest_jobs']))
        self.assertEqual(state['source_scans'][0]['status'],'failed')
        third=next(x for x in state['source_scans'] if x['source']=='lever:third')
        self.assertEqual(third['queued'],1)
        with service.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE topic='resume.requested.v1'").fetchone()[0],2)

    def test_second_scan_dedupes_and_retains_window(self):
        recent=(self.now-timedelta(hours=2)).isoformat()
        service=Service(self.database,fetcher=lambda _: [self.job('j1',date=recent)])
        service.start({**self.req,'sources':['lever:first']},background=False)
        service.scheduled_scan(background=False)
        state=service.snapshot()
        self.assertEqual(state['runs'][0]['trigger'],'scheduled')
        self.assertEqual(state['source_scans'][0]['existing_jobs'],1)
        self.assertEqual(state['source_scans'][0]['queued'],0)
        self.assertEqual(len(state['queue']),1)
        self.assertEqual(state['saved_stats']['total'],1)


if __name__ == '__main__':
    unittest.main()
