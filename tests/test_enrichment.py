"""Offline regression tests for user-confirmed full-JD enrichment."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent.dashboard import Service
from agent.enrichment import enrich_job


class FullJDEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temporary.name) / 'agent.db')
        self.source_job = {
            'url': 'https://www.adzuna.com/details/123',
            'title': 'Software Engineer',
            'company': 'Example',
            'location': 'US',
            'description': 'Java roles available',
            'description_completeness': 'excerpt',
            'source': 'adzuna:us',
            'employer_key': 'adzuna:us',
            'requisition_id': '123',
        }
        self.service = Service(self.db, fetcher=lambda _: [self.source_job])
        self.search = {'prompt': 'software engineer', 'sources': ['adzuna:us'],
                       'jd_keywords': ['Spring Boot']}
        self.service.start(self.search, background=False)
        self.job_id = self.service.snapshot()['jobs'][0]['id']

    def tearDown(self):
        self.temporary.cleanup()

    def enriched(self, **changes):
        body = {'job_id': self.job_id,
                'description': 'Responsibilities: Build services using Java and Spring Boot. ' * 8,
                'confirmed_complete': True,
                'source_url': 'https://careers.example.org/jobs/123?utm_source=market'}
        body.update(changes)
        return enrich_job(self.service, body)

    def test_full_jd_is_requalified_and_queued_transactionally(self):
        before = self.service.snapshot()
        self.assertEqual(before['jobs'][0]['qualification']['status'], 'needs_full_jd')
        self.assertEqual(before['queue'], [])
        result = self.enriched()
        self.assertTrue(result['queued'])
        self.assertEqual(result['queue_order'], 1)
        state = self.service.snapshot()
        self.assertEqual(state['jobs'][0]['qualification']['status'], 'matched')
        self.assertEqual(state['jobs'][0]['description_completeness'], 'user_supplied_full')
        self.assertEqual(state['jobs'][0]['description_provenance'], 'user_pasted_unverified')
        self.assertEqual(state['jobs'][0]['url'], 'https://careers.example.org/jobs/123')
        self.assertEqual(len(state['queue']), 1)
        payload = json.loads(state['queue'][0]['snapshot'])
        self.assertEqual(payload['description'], state['jobs'][0]['description'])
        with self.service.connect() as db:
            event = db.execute("SELECT payload FROM outbox WHERE topic='resume.requested.v1'").fetchone()
            self.assertEqual(json.loads(event['payload'])['job']['qualification']['status'], 'matched')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intake_attempts').fetchone()[0], 2)

    def test_repeated_market_discovery_does_not_downgrade_full_jd(self):
        self.enriched()
        self.service.start(self.search, background=False)
        state = self.service.snapshot()
        self.assertEqual(len(state['queue']), 1)
        self.assertEqual(state['jobs'][0]['description_completeness'], 'user_supplied_full')
        self.assertEqual(state['jobs'][0]['qualification']['status'], 'matched')
        self.assertIn('Spring Boot', state['jobs'][0]['description'])
        self.assertEqual(state['jobs'][0]['url'], 'https://careers.example.org/jobs/123')
        with self.service.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE topic='resume.requested.v1'").fetchone()[0], 1)

    def test_full_jd_can_still_fail_requirements(self):
        result = self.enriched(description='Build reliable REST APIs with Java. ' * 12)
        self.assertFalse(result['queued'])
        self.assertEqual(result['qualification']['status'], 'excluded')
        self.assertEqual(self.service.snapshot()['queue'], [])
        self.assertEqual(self.service.snapshot()['jobs'][0]['qualification']['reasons'],
                         ['JD missing required phrase: Spring Boot'])

    def test_requires_explicit_confirmation_and_long_description(self):
        for changes in ({'confirmed_complete': False}, {'description': 'short'},
                        {'source_url': 'javascript:alert(1)'}, {'job_id': 'invalid'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.enriched(**changes)
        self.assertEqual(self.service.snapshot()['jobs'][0]['qualification']['status'], 'needs_full_jd')
        self.assertEqual(self.service.snapshot()['queue'], [])

    def test_collision_rolls_back_job_and_queue(self):
        with self.service.connect() as db:
            db.execute("INSERT INTO outbox(event_key,topic,payload) VALUES(?,?,?)",
                       ('resume.requested:' + self.job_id, 'resume.requested.v1', '{}'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.enriched()
        self.assertEqual(self.service.snapshot()['queue'], [])
        self.assertEqual(self.service.snapshot()['jobs'][0]['qualification']['status'], 'needs_full_jd')
        with self.service.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intake_attempts').fetchone()[0], 1)

    def test_older_lead_without_saved_rules_must_be_rediscovered(self):
        with self.service.connect() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (self.job_id,)).fetchone()
            payload = json.loads(row['payload'])
            payload.pop('qualification_rules')
            db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(payload), self.job_id))
        with self.assertRaisesRegex(ValueError, 'Re-run discovery'):
            self.enriched()
        self.assertEqual(self.service.snapshot()['queue'], [])


if __name__ == '__main__':
    unittest.main()
