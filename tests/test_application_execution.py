"""Agent 3 execution tests: confirmations, deduplication and uncertainty.

These tests inject a fake authorized employer adapter; they do NOT claim
real-world application/API connectivity.
"""
from datetime import datetime, timezone
import unittest

from agent.application_execution import SubmissionOutcome, attempt_status, execute
from agent.application_worker import Agent3
from test_agent3_pipeline import StageThreeTests as Fixture


class FakeAuthorizedAdapter:
    method = 'api'
    name = 'fixture-employer-api'

    def __init__(self, outcome=None, *, supported=True, fail=False,
                 independent_receipt=True):
        self.calls = []
        self.verifications = []
        self.outcome = outcome
        self.supported = supported
        self.fail = fail
        self.independent_receipt = independent_receipt

    def supports(self, plan):
        return self.supported

    def submit(self, plan, resume_path, answers, attempt_key):
        self.calls.append((plan, resume_path, answers, attempt_key))
        if self.fail:
            raise TimeoutError('employer did not respond')
        return self.outcome

    def verify_confirmation(self, plan, outcome, attempt_key):
        # Fake acceptance test: a real adapter must query employer-side
        # evidence using the immutable receipt and posting/attempt identity.
        self.verifications.append((plan,outcome,attempt_key))
        return self.independent_receipt


class ExecutionContractTests(unittest.TestCase):
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    ready_bundle = Fixture.ready_bundle

    def ready(self):
        self.ready_bundle()
        self.worker = Agent3(self.database)
        self.assertTrue(self.worker.process_one())
        self.assertEqual(self.worker.status()[0]['status'],
                         'awaiting_browser_adapter')
        return self.worker

    def receipt(self):
        return SubmissionOutcome(
            status='confirmed', submission_id='real-employer-reference-123',
            submitted_at=datetime.now(timezone.utc).isoformat(),
            confirmation_url='https://careers.example.com/confirmation/123',
        )

    def test_verified_employer_receipt_records_exact_job_resume_once(self):
        worker = self.ready()
        adapter = FakeAuthorizedAdapter(self.receipt())
        result = execute(worker, 1, adapter,
                         {'verified': True, 'email':'candidate@example.com'},
                         allow_submission=True)
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0][0]['resume_artifact_id'], self.sha)
        self.assertEqual(adapter.calls[0][0]['job_id'], self.queue['job_id'])
        self.assertTrue(adapter.calls[0][3])
        self.assertEqual(attempt_status(worker, 1)['status'], 'confirmed')
        self.assertEqual(worker.status()[0]['submission_attempt_status'],
                         'confirmed')
        self.assertEqual(len(self.service.snapshot()['applied_history']), 1)
        self.assertEqual(self.service.snapshot()['applied_history'][0]['artifact_id'],
                         self.sha)
        with self.assertRaises(ValueError):
            execute(worker, 1, adapter, {'verified':True},
                    allow_submission=True)
        self.assertEqual(len(adapter.calls), 1)

    def test_explicit_authorization_and_verified_answers_required(self):
        worker = self.ready()
        adapter = FakeAuthorizedAdapter(self.receipt())
        for answers, enabled in (({'verified':True},False),
                                 ({'verified':False},True), ({},True)):
            with self.assertRaises(ValueError):
                execute(worker, 1, adapter, answers,
                        allow_submission=enabled)
        self.assertIsNone(attempt_status(worker, 1))
        self.assertEqual(adapter.calls, [])

    def test_unsupported_adapter_is_not_allowed_to_call_employer(self):
        worker = self.ready()
        adapter = FakeAuthorizedAdapter(self.receipt(), supported=False)
        with self.assertRaises(ValueError):
            execute(worker, 1, adapter, {'verified':True},
                    allow_submission=True)
        self.assertIsNone(attempt_status(worker, 1))
        self.assertEqual(adapter.calls, [])

    def test_unverified_receipt_is_unknown_even_when_submit_says_confirmed(self):
        worker = self.ready()
        adapter = FakeAuthorizedAdapter(self.receipt(), independent_receipt=False)
        result = execute(worker, 1, adapter, {'verified':True},
                         allow_submission=True)
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(len(adapter.verifications),1)
        self.assertEqual(attempt_status(worker,1)['status'],'unknown')
        self.assertEqual(self.service.snapshot()['applied_history'],[])
        with self.assertRaises(ValueError):
            execute(worker,1,adapter,{'verified':True},allow_submission=True)
        self.assertEqual(len(adapter.calls),1)

    def test_missing_independent_verifier_is_not_a_valid_submission(self):
        worker = self.ready()
        adapter = FakeAuthorizedAdapter(self.receipt())
        adapter.verify_confirmation = None
        result = execute(worker,1,adapter,{'verified':True},allow_submission=True)
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(self.service.snapshot()['applied_history'],[])

    def test_timeout_is_uncertain_not_submitted_or_retried(self):
        worker = self.ready()
        adapter = FakeAuthorizedAdapter(fail=True)
        result = execute(worker, 1, adapter, {'verified':True},
                         allow_submission=True)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(attempt_status(worker, 1)['status'], 'unknown')
        self.assertEqual(self.service.snapshot()['applied_history'], [])
        with self.assertRaises(ValueError):
            execute(worker, 1, adapter, {'verified':True},
                    allow_submission=True)
        self.assertEqual(len(adapter.calls), 1)

    def test_employer_rejection_never_becomes_applied(self):
        worker = self.ready()
        result = execute(worker, 1,
                         FakeAuthorizedAdapter(SubmissionOutcome('rejected')),
                         {'verified':True}, allow_submission=True)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(attempt_status(worker, 1)['status'], 'rejected')
        self.assertEqual(self.service.snapshot()['applied_history'], [])

    def test_incomplete_confirmation_is_unknown_not_success(self):
        worker = self.ready()
        result = execute(worker, 1,
                         FakeAuthorizedAdapter(SubmissionOutcome(
                             'confirmed', submission_id='missing-url')),
                         {'verified':True}, allow_submission=True)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.service.snapshot()['applied_history'], [])
        self.assertEqual(attempt_status(worker, 1)['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
