"""Agent 3 execution boundary for explicitly authorized employer submissions.

The repository does not ship a universal ATS submission endpoint. An adapter
must implement a real, authorized employer interaction and return evidence
from that interaction; HTTP 200 alone is not a submission receipt.

Reservations are written BEFORE the external side effect. A crash/timeout
leaves an uncertain attempt requiring reconciliation, never a blind retry.
"""
from dataclasses import dataclass
import json
import sqlite3
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS application_attempts (
    application_order INTEGER PRIMARY KEY REFERENCES application_queue(sequence),
    job_id TEXT NOT NULL,
    resume_artifact_id TEXT NOT NULL,
    method TEXT NOT NULL CHECK(method IN ('api','browser')),
    adapter_name TEXT NOT NULL,
    attempt_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('sending','unknown','rejected','confirmed')),
    last_error TEXT,
    employer_receipt TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


@dataclass(frozen=True)
class SubmissionOutcome:
    """Only an adapter's verified employer response can confirm submission."""
    status: str  # confirmed, rejected, unknown
    submission_id: str = ""
    submitted_at: str = ""
    confirmation_url: str = ""


def attempt_status(owner, application_order):
    with owner.connect() as db:
        db.executescript(SCHEMA)
        row = db.execute(
            """SELECT application_order,method,adapter_name,status,last_error,
                      employer_receipt,started_at,updated_at
               FROM application_attempts WHERE application_order=?""",
            (application_order,),
        ).fetchone()
    return dict(row) if row else None


def _finish(owner, order, state, error=None, receipt=None):
    with owner.connect() as db:
        db.execute(
            """UPDATE application_attempts
               SET status=?,last_error=?,employer_receipt=?,updated_at=CURRENT_TIMESTAMP
               WHERE application_order=? AND status='sending'""",
            (state, error, json.dumps(receipt) if receipt else None, order),
        )


def execute(owner, application_order, adapter, answers, *, allow_submission=False):
    """Execute ONE preflighted bundle with explicit authorization.

    adapter.method: 'api' or 'browser'
    adapter.name: stable adapter identifier
    adapter.supports(plan): True only if this exact employer is supported
    adapter.submit(plan, resume_path, answers, attempt_key): SubmissionOutcome

    There is intentionally no generic POST-to-job-URL fallback. The adapter
    owns auth, form discovery, required fields and employer response parsing.
    """
    if allow_submission is not True:
        raise ValueError('Explicit permission to submit this application is required')
    if not isinstance(application_order, int) or application_order < 1:
        raise ValueError('Provide a valid application queue order')
    if not isinstance(answers, dict) or answers.get('verified') is not True:
        raise ValueError('Review and explicitly verify application answers first')
    method = getattr(adapter, 'method', None)
    name = getattr(adapter, 'name', None)
    if method not in ('api', 'browser') or not isinstance(name, str) or not name.strip():
        raise ValueError('A named authorized API or browser adapter is required')

    # This checks the persisted preflight, exact resume bytes and queue revision.
    from .application_browser import load_handoff
    plan, resume_path = load_handoff(owner.database, application_order)
    if adapter.supports(plan) is not True:
        raise ValueError('This adapter is not authorized for the target employer')
    if plan['resume_artifact_id'] is None or plan['job_id'] is None:
        raise ValueError('An exact job/resume handoff is required')

    # Reserve once before talking to an employer; even an ambiguous timeout
    # must not silently send the same personal information a second time.
    attempt_key = str(uuid.uuid4())
    with owner.connect() as db:
        db.executescript(SCHEMA)
        db.execute('BEGIN IMMEDIATE')
        app = db.execute(
            """SELECT job_id,resume_artifact_id,status FROM application_queue
               WHERE sequence=?""", (application_order,)
        ).fetchone()
        if not app or app['status'] != 'ready_for_application':
            raise ValueError('Application queue state changed; rerun preflight')
        if (app['job_id'], app['resume_artifact_id']) != (
                plan['job_id'], plan['resume_artifact_id']):
            raise ValueError('Application job/resume revision changed')
        existing = db.execute(
            'SELECT status FROM application_attempts WHERE application_order=?',
            (application_order,),
        ).fetchone()
        if existing:
            raise ValueError(
                f"An application attempt already exists ({existing['status']}); "
                "reconcile with the employer before any new submission")
        db.execute(
            """INSERT INTO application_attempts
                (application_order,job_id,resume_artifact_id,method,adapter_name,
                 attempt_key,status) VALUES(?,?,?,?,?,?,'sending')""",
            (application_order,app['job_id'],app['resume_artifact_id'],
             method,name,attempt_key),
        )

    from .application_history import prepared, submitted
    try:
        prepared(owner, {
            'job_id':plan['job_id'], 'artifact_id':plan['resume_artifact_id'],
            'prepared':True,
        })
    except Exception:
        _finish(owner, application_order, 'unknown',
                'Preparation state could not be recorded; inspect before retry')
        raise

    try:
        result = adapter.submit(plan, resume_path, answers, attempt_key)
    except Exception:
        _finish(owner, application_order, 'unknown',
                'Adapter failed or timed out; check employer status before retry')
        return {'status':'unknown', 'application_order':application_order}

    if not isinstance(result, SubmissionOutcome) or result.status not in (
            'confirmed', 'rejected', 'unknown'):
        _finish(owner, application_order, 'unknown',
                'Unrecognized adapter response; employer status must be checked')
        return {'status':'unknown', 'application_order':application_order}

    if result.status == 'confirmed':
        receipt = {
            'job_id':plan['job_id'], 'artifact_id':plan['resume_artifact_id'],
            'submitted':True, 'confirmed':True,
            'submission_id':result.submission_id,
            'submitted_at':result.submitted_at,
            'confirmation_url':result.confirmation_url,
        }
        try:
            # Retains the existing exact-artifact and real-receipt contract.
            submitted(owner, receipt)
        except (ValueError, OSError, sqlite3.Error):
            _finish(owner, application_order, 'unknown',
                    'Reported employer confirmation is incomplete or inconsistent')
            return {'status':'unknown', 'application_order':application_order}
        _finish(owner, application_order, 'confirmed', receipt=receipt)
        return {'status':'confirmed', 'application_order':application_order,
                'submission_id':result.submission_id}

    _finish(owner, application_order, result.status,
            'Employer rejected the application' if result.status=='rejected'
            else 'Employer submission state is uncertain')
    return {'status':result.status, 'application_order':application_order}
