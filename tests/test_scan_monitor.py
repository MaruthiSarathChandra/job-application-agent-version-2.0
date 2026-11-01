"""Agent 1 acceptance checks: persisted scan evidence, dedupe, restart, scheduler."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent.dashboard import Service
from agent.intake import IntakeStore


def posting(identifier='j1', location='Kansas City, MO, US'):
    return {'url':f'https://employer.example/jobs/{identifier}',
            'title':'Backend Software Engineer','company':'Example',
            'location':location,'description':'Build Java and Spring Boot APIs',
            'employer_key':'example','requisition_id':identifier}


class ScanMonitorTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.db=str(Path(self.folder.name)/'agent.db')
        self.request={'prompt':'Backend Software Engineer','sources':['lever:example'],
                      'country_codes':['US'], 'locations':['Kansas City'],
                      'jd_keywords':['Spring Boot']}
    def tearDown(self):
        self.folder.cleanup()

    def test_repeated_scans_show_new_existing_and_exact_queue_counts(self):
        calls=[]
        def source(_):
            calls.append(True)
            return [posting('j1')] if len(calls)==1 else [posting('j1'),posting('j2')]
        service=Service(self.db,fetcher=source)
        service.start(self.request,background=False)
        first=service.snapshot()
        self.assertEqual(first['runs'][0]['status'],'completed')
        self.assertEqual(first['runs'][0]['trigger'],'manual')
        self.assertTrue(first['runs'][0]['finished_at'])
        metrics=first['source_scans'][0]
        self.assertEqual((metrics['fetched'],metrics['new_jobs'],metrics['existing_jobs'],
                          metrics['matched'],metrics['queued'],metrics['errors']), (1,1,0,1,1,0))
        self.assertTrue(first['jobs'][0]['first_seen_at'])
        self.assertEqual(first['jobs'][0]['last_run_id'], first['runs'][0]['id'])
        service.start(self.request,background=False)
        state=service.snapshot()
        stats=state['source_scans'][0]
        self.assertEqual((stats['fetched'],stats['new_jobs'],stats['existing_jobs'],
                          stats['matched'],stats['queued']), (2,1,1,2,1))
        self.assertEqual(len(state['queue']),2)
        self.assertEqual(len(state['jobs']),2)
        self.assertEqual(len({x['id'] for x in state['jobs']}),2)
        latest=[x for x in state['jobs'] if x['last_run_id']==state['runs'][0]['id']]
        self.assertEqual(len(latest),2)
        with service.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox WHERE topic=?',
                         ('resume.requested.v1',)).fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_scans').fetchone()[0],2)
        restarted=Service(self.db,fetcher=source)
        self.assertEqual(restarted.snapshot()['source_scans'][0]['queued'],1)
        self.assertEqual(restarted.snapshot()['runs'][0]['status'],'completed')

    def test_zero_result_source_is_reported_not_claimed_as_new_jobs(self):
        service=Service(self.db,fetcher=lambda _:[])
        service.start(self.request,background=False)
        state=service.snapshot()
        self.assertEqual(len(state['jobs']),0)
        self.assertEqual(len(state['queue']),0)
        self.assertEqual(state['runs'][0]['status'],'completed')
        self.assertIsNotNone(state['runs'][0]['finished_at'])
        metrics=state['source_scans'][0]
        self.assertEqual(metrics['status'],'completed')
        self.assertEqual(metrics['fetched'],0)
        self.assertEqual(metrics['queued'],0)
        self.assertIsNotNone(metrics['finished_at'])
        self.assertTrue(any('0 returned' in e['message'] for e in state['activity']))

    def test_failed_source_and_good_source_are_both_visible(self):
        def source(name):
            if name=='greenhouse:down':
                raise OSError('Secret API URL must not be logged')
            return [posting()]
        service=Service(self.db,fetcher=source)
        service.start({**self.request,'sources':['greenhouse:down','lever:example']},background=False)
        state=service.snapshot()
        scans={x['source']:x for x in state['source_scans']}
        self.assertEqual(scans['greenhouse:down']['status'],'failed')
        self.assertEqual(scans['greenhouse:down']['errors'],1)
        self.assertEqual(scans['lever:example']['queued'],1)
        self.assertEqual(state['runs'][0]['status'],'completed_with_errors')
        self.assertNotIn('Secret API',str(state['activity']))

    def test_excluded_us_rules_count_without_queueing(self):
        service=Service(self.db,fetcher=lambda _: [posting('j1','Argentina')])
        service.start(self.request,background=False)
        metrics=service.snapshot()['source_scans'][0]
        self.assertEqual(metrics['fetched'],1)
        self.assertEqual(metrics['excluded'],1)
        self.assertEqual(metrics['queued'],0)

    def test_manual_request_reused_by_scheduled_scan_without_guessing_filters(self):
        observed=[]
        def source(name):
            observed.append(name)
            return [posting()]
        service=Service(self.db,fetcher=source)
        self.assertIsNone(service.scheduled_scan(background=False))
        self.assertEqual(service.snapshot()['runs'],[])
        service.start(self.request,background=False)
        service.enable_schedule(60)
        try:
            self.assertEqual(service.snapshot()['scanner']['interval_minutes'],60)
            self.assertTrue(service.snapshot()['scanner']['next_scan_at'])
            saved_request=service.last_manual_request()
            self.assertEqual({key:saved_request[key] for key in self.request},self.request)
            service.scheduled_scan(background=False)
            state=service.snapshot()
            self.assertEqual(state['runs'][0]['trigger'],'scheduled')
            self.assertEqual(json.loads(state['runs'][0]['config'])['rules']['country_codes'],['US'])
            self.assertEqual(len(state['queue']),1)
            self.assertEqual(state['source_scans'][0]['existing_jobs'],1)
            self.assertEqual(observed,['lever:example','lever:example'])
        finally:
            service.stop_schedule()
        self.assertFalse(service.snapshot()['scanner']['enabled'])

    def test_legacy_database_migrates_without_fabricated_first_seen(self):
        con=sqlite3.connect(self.db)
        con.execute("""CREATE TABLE jobs(id TEXT PRIMARY KEY,identity_key TEXT UNIQUE NOT NULL,
            url TEXT NOT NULL,ats TEXT NOT NULL,payload TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("""CREATE TABLE discovery_runs(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,
            config TEXT NOT NULL,status TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("""INSERT INTO jobs(id,identity_key,url,ats,payload)
            VALUES('old','legacy','https://example.com','career_site','{}')""")
        con.commit()
        con.close()
        store=IntakeStore(self.db)
        legacy=store.connection.execute('SELECT first_seen_at,last_seen_at FROM jobs').fetchone()
        self.assertEqual(legacy,(None,None))
        store.close()
        service=Service(self.db)
        with service.connect() as db:
            self.assertIn('finished_at',{x['name'] for x in db.execute('PRAGMA table_info(discovery_runs)')})
            self.assertIn('trigger',{x['name'] for x in db.execute('PRAGMA table_info(discovery_runs)')})

    def test_interval_validation_rejects_unsafe_frequency(self):
        service=Service(self.db)
        for bad in (0,4,1441,True,'60'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    service.enable_schedule(bad)

if __name__=='__main__':
    unittest.main()
