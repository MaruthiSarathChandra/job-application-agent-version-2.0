"""Strict, auditable job publication age filters (all timestamps UTC).

Provider response timestamps can be missing, malformed, or represent a job
CREATION rather than its public publication. Never replace a missing publication
date with the moment this agent first saw the job.
"""
from datetime import datetime, timezone, timedelta

WINDOW_HOURS = (0, 7, 24, 72)


def parse_published_at(value):
    """Return offset-aware UTC datetime only when source gave a precise instant."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        if 'T' not in value and ' ' not in value:
            return None  # Date-only values cannot reliably satisfy a 7-hour cutoff
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            return None  # Avoid assuming a provider-local timezone
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def freshness(job, within_hours=0, now=None):
    """One of unrestricted, in_window, too_old, unknown_posting_date."""
    if type(within_hours) is not int or within_hours not in WINDOW_HOURS:
        raise ValueError('Posting age window must be 0, 7, 24 or 72 hours')
    if within_hours == 0:
        return {'status': 'unrestricted', 'hours': 0}
    now = now or datetime.now(timezone.utc)
    posted = parse_published_at(job.get('source_posted_at'))
    if posted is None or posted > now + timedelta(minutes=10):
        return {'status': 'unknown_posting_date', 'hours': within_hours}
    if posted < now - timedelta(hours=within_hours):
        return {'status': 'too_old', 'hours': within_hours}
    return {'status': 'in_window', 'hours': within_hours}


def date_decision(job, rules, now=None):
    basis=rules.get('time_basis','published')
    if basis not in ('published','first_seen'):
        raise ValueError('Time basis must be published or first_seen')
    source=job if basis=='published' else {'source_posted_at':job.get('first_seen_at')}
    outcome = freshness(source, rules.get('posted_within_hours', 0), now)
    label='Employer publication' if basis=='published' else 'First discovery'
    if outcome['status'] == 'too_old':
        return {'status':'excluded','reasons':[f"{label} predates the {outcome['hours']}h window"],
                'scope':'Employer publication window' if basis=='published' else 'First discovery window; not employer posting age'}
    if outcome['status'] == 'unknown_posting_date':
        return {'status':'date_unknown',
                'reasons':[f"{label} time unavailable or unverified; cannot prove within {outcome['hours']}h"],
                'scope':'Review-only: never queue a job as newly posted without a verified publication instant'}
    return None
