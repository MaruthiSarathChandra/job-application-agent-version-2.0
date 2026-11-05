"""Read-only, fail-closed deployment readiness report.

An offline test suite or a completed resume is not proof of a real employer
submission. This command never phones an employer, reads Gmail, runs GPT,
mutates SQLite, or prints credentials/PII.
"""
import argparse
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote

# Deliberately empty until a *real* supported ATS write path has been built,
# validated with its owner, and enabled with strict employer restrictions.
# Guided contact-field browser prefill does not qualify as a write adapter.
LIVE_EMPLOYER_ADAPTERS = ()


def _check(name, passed, explanation, *, mandatory=True):
    return {'name': name, 'passed': bool(passed), 'mandatory': mandatory,
            'message': explanation}


def _database_status(database):
    path = Path(database).expanduser().absolute()
    if not path.is_file():
        return False, 'Local database does not exist. Start the dashboard first.'
    try:
        # Read-only mode prevents the readiness report from creating or
        # mutating a database. Work with paths containing spaces on Windows.
        uri = 'file:' + quote(str(path).replace('\\', '/'), safe='/:') + '?mode=ro'
        # sqlite3.Connection.__exit__ does NOT close the connection;
        # explicitly closing is essential on Windows for cleanup/backups.
        with closing(sqlite3.connect(uri, uri=True, timeout=3)) as conn:
            conn.execute('PRAGMA query_only=ON')
            if conn.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
                return False, 'Database quick-check failed; restore from backup.'
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        needed = {'jobs', 'discovery_runs', 'resume_queue', 'application_queue',
                  'application_attempts', 'applied_history'}
        missing = sorted(needed - names)
        if missing:
            return False, 'Database is missing required tables: ' + ', '.join(missing)
    except (sqlite3.Error, OSError, ValueError):
        return False, 'Database read/quick-check failed. Check the local database.'
    return True, 'SQLite quick-check passed; application tables exist (not a durability certification).'


def _profile_status(profile):
    if not Path(profile).is_file():
        return False, 'Create and save a verified profile first.'
    try:
        from .resume_worker import load_profile
        safe, _ = load_profile(profile)
        if not safe.get('_resume_header'):
            return False, 'Profile needs verified resume contact details.'
    except (ValueError, KeyError, TypeError, OSError, ImportError):
        return False, 'Profile validation failed. Review verified facts and resume header.'
    return True, 'Verified profile and resume header validated.'


def assess(database='data/agent.db', profile='data/profile.json', environ=None):
    """Return structured checks, no sensitive paths/values or side effects."""
    env = os.environ if environ is None else environ
    checks = []
    checks.append(_check(
        'Python runtime', sys.version_info >= (3, 11),
        'Python 3.11+ required.'))
    db_ok, db_note = _database_status(database)
    checks.append(_check('Local database', db_ok, db_note))
    profile_ok, profile_note = _profile_status(profile)
    checks.append(_check('Resume profile', profile_ok, profile_note))
    configured = bool(env.get('OPENAI_API_KEY')) and bool(env.get('OPENAI_MODEL'))
    checks.append(_check(
        'Resume model configuration', configured,
        'OPENAI_API_KEY and OPENAI_MODEL both configured.' if configured
        else 'Set OPENAI_API_KEY and OPENAI_MODEL locally; do not commit secrets.'))
    checks.append(_check(
        'Browser guided prefill', importlib.util.find_spec('playwright') is not None,
        'Playwright package detected (browser installation and real ATS behavior are NOT verified).',
        mandatory=False))
    checks.append(_check(
        'Employer-authorized submission adapter', bool(LIVE_EMPLOYER_ADAPTERS),
        'BLOCKED: no real employer-authorized ATS submit adapter is shipped. '
        'Guided prefill, mocked adapters and public job-discovery APIs cannot submit.',
        mandatory=True))
    checks.append(_check(
        'Live end-to-end receipt acceptance', False,
        'BLOCKED: no witnessed user-approved submission with a real employer-issued '
        'receipt has been validated for this release.',
        mandatory=True))
    checks.append(_check(
        'Deployment security', False,
        'BLOCKED: localhost development workspace has not passed production '
        'authentication, secrets, personal-data protection, backup and recovery review.',
        mandatory=True))
    return {
        'production_ready': all(c['passed'] for c in checks if c['mandatory']),
        'stage': 'development / not production ready',
        'checks': checks,
        'next_milestone': 'Complete Issue #12: authorize one employer ATS adapter; '
                          'validate form questions and resume upload; perform one '
                          'user-approved real submission and independently reconcile its receipt.',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Read-only production readiness audit. Exits 2 when blocked.')
    parser.add_argument('--database', default='data/agent.db')
    parser.add_argument('--profile', default='data/profile.json')
    parser.add_argument('--json', action='store_true', help='Machine-readable output')
    args = parser.parse_args(argv)
    result = assess(args.database, args.profile)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print('PRODUCTION READINESS: ' + ('READY' if result['production_ready']
                                          else 'NOT READY'))
        for check in result['checks']:
            marker = 'PASS' if check['passed'] else ('BLOCKED' if check['mandatory'] else 'OPTIONAL')
            print(f"  [{marker}] {check['name']}: {check['message']}")
        print('\nNEXT: ' + result['next_milestone'])
    return 0 if result['production_ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
