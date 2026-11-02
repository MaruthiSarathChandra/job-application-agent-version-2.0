"""Conservative local browser pre-fill for Agent 3.

Only fills a narrow whitelist of known contact fields and one DOCX upload on
the currently visible page. Never clicks Apply/Submit, bypasses CAPTCHA,
answers legal questions, or marks an application submitted.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from .application_worker import Agent3

FIELDS = {
    'first_name': ('input[autocomplete="given-name"]', 'input[name="first_name"]',
                   'input[name="firstName"]', 'input[id="first_name"]'),
    'last_name': ('input[autocomplete="family-name"]', 'input[name="last_name"]',
                  'input[name="lastName"]', 'input[id="last_name"]'),
    'email': ('input[type="email"]', 'input[autocomplete="email"]',
              'input[name="email"]', 'input[id="email"]'),
    'phone': ('input[autocomplete="tel"]', 'input[type="tel"]',
              'input[name="phone"]', 'input[name="phone_number"]'),
    'linkedin': ('input[name="linkedin"]', 'input[name="linkedin_url"]'),
}
ALLOWED = {'verified', *FIELDS}


def verified_answers(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict) or set(value) - ALLOWED or value.get('verified') is not True:
        raise ValueError('Application answers must be an explicitly verified object with only supported fields')
    for required in ('first_name', 'last_name', 'email'):
        if not isinstance(value.get(required), str) or not 1 <= len(value[required].strip()) <= 120:
            raise ValueError(f'Provide a verified {required}')
    for key in FIELDS:
        if key in value and (not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 300):
            raise ValueError(f'Invalid {key}')
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', value['email']):
        raise ValueError('Invalid application email')
    if 'linkedin' in value:
        url = urlsplit(value['linkedin'])
        if url.scheme != 'https' or not url.hostname or not (
                url.hostname == 'linkedin.com' or url.hostname.endswith('.linkedin.com')):
            raise ValueError('Only a verified HTTPS LinkedIn URL is supported')
    return {key:value[key].strip() for key in FIELDS if key in value}


def load_handoff(database, order):
    worker = Agent3(database)
    stages = {task['application_order']:task for task in worker.status()}
    stage = stages.get(order)
    if not stage or stage['status'] != 'awaiting_browser_adapter':
        raise ValueError('Run Agent 3 preflight for an approved resume before opening a browser')
    plan = json.loads(stage['plan'])
    with worker.connect() as db:
        row = db.execute('''SELECT a.status,a.resume_artifact_id,r.path,r.sha256
            FROM application_queue a JOIN resume_artifacts r ON r.id=a.resume_artifact_id
            WHERE a.sequence=?''',(order,)).fetchone()
    if not row or row['status'] != 'ready_for_application' or plan['resume_artifact_id'] != row['resume_artifact_id']:
        raise ValueError('Application handoff is no longer approved')
    file = Path(row['path'])
    if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != row['sha256']:
        raise ValueError('Resume was replaced or is not readable')
    return plan, file


def fill_visible_fields(page, answers, resume):
    """Return local non-sensitive results. No clicking, submitting or navigation."""
    filled = []
    skipped = []
    for key, selectors in FIELDS.items():
        if key not in answers:
            continue
        for selector in selectors:
            locator = page.locator(selector)
            if locator.count() == 1 and locator.first.is_visible() and locator.first.is_enabled():
                locator.first.fill(answers[key])
                filled.append(key)
                break
        else:
            skipped.append(key)
    files = page.locator('input[type="file"]')
    uploaded = False
    if files.count() == 1 and files.first.is_enabled():
        accept = (files.first.get_attribute('accept') or '').lower()
        # Never force DOCX onto a PDF-only or image-only input.
        if not accept or 'docx' in accept or 'wordprocessingml' in accept or 'msword' in accept:
            files.first.set_input_files(str(resume))
            uploaded = True
    return {'filled_fields':filled,'unmapped_fields':skipped,'resume_uploaded':uploaded,
            'submitted':False, 'manual_review_required':True}


def open_application(database, order, answers_path, allow_hosts=(), dry_run=False):
    plan, resume = load_handoff(database, order)
    answers = verified_answers(answers_path)
    original_host = urlsplit(plan['job_url']).hostname
    allowed = {original_host}
    for host in allow_hosts:
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', host):
            raise ValueError('Only exact hostname allowlist entries are supported')
        allowed.add(host.lower())
    if dry_run:
        return {'application_order':order,'job_url':plan['job_url'],'allowed_hosts':sorted(allowed),
                'available_fields':sorted(answers),'submitted':False,'dry_run':True}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ValueError('Install browser dependencies using requirements-browser.txt, then playwright install chromium') from None
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=False)
        try:
            context = browser.new_context(accept_downloads=False)
            page = context.new_page()
            page.goto(plan['job_url'], wait_until='domcontentloaded', timeout=30000)
            destination = urlsplit(page.url)
            if destination.scheme != 'https' or destination.hostname not in allowed:
                raise ValueError('Cross-host redirect blocked: add the trusted hostname explicitly if intended')
            print('Browser open. Navigate to the employer application form, then return here.')
            input('Press Enter when the form is visible to fill known contact fields: ')
            destination = urlsplit(page.url)
            if destination.scheme != 'https' or destination.hostname not in allowed:
                raise ValueError('Current form host is not explicitly allowed')
            report = fill_visible_fields(page,answers,resume)
            print(json.dumps(report,indent=2))
            print('Review ALL answers and required questions in the browser. Nothing is submitted automatically.')
            input('Press Enter to close the browser after your review: ')
            return report
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description='Agent 3 guided browser pre-fill; never submits')
    parser.add_argument('--database',default='data/agent.db')
    parser.add_argument('--application-order',type=int,required=True)
    parser.add_argument('--answers',default='data/application-answers.json')
    parser.add_argument('--allow-host',action='append',default=[])
    parser.add_argument('--dry-run',action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(open_application(args.database,args.application_order,args.answers,
                                          args.allow_host,args.dry_run),indent=2))
    except (ValueError,OSError,sqlite3.Error) as exc:
        parser.exit(1,'Agent 3 browser error: '+str(exc)+'\n')


if __name__ == '__main__':
    main()
