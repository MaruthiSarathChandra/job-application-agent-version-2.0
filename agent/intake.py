"""Deterministic intake domain; no browser, LLM, or credentials required."""
import hashlib
import re
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_url(value):
    value = value.strip()
    match = re.fullmatch(r'\[[^\]]*\]\((https?://[^\s]+)\)', value)
    if match:
        value = match[1]
    value = value.removeprefix('<').removesuffix('>')
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Expected an HTTP(S) job URL without credentials')
    if any(c.isspace() for c in value):
        raise ValueError('URL cannot contain whitespace')
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'source', 'src', 'trackingid'}]
    path = parts.path.rstrip('/')
    if parts.hostname.endswith('.myworkdayjobs.com') and path.endswith('/apply'):
        path = path[:-6]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ''))


def detect_ats(url):
    host = urlsplit(url).hostname
    for suffix, ats in [('myworkdayjobs.com', 'workday'), ('ats.rippling.com', 'rippling'),
                        ('greenhouse.io', 'greenhouse'), ('lever.co', 'lever')]:
        if host == suffix or host.endswith('.' + suffix):
            return ats
    return 'career_site'


def qualify(job, requirements):
    """Only explicit title/location rules; never infer legal eligibility from prose."""
    reasons = []
    title = job['title'].casefold()
    wanted = requirements.get('title_keywords', [])
    if wanted and not any(word.casefold() in title for word in wanted):
        reasons.append('Title does not match configured roles')
    if any(re.search(r'\b' + re.escape(word.casefold()) + r'\b', title)
           for word in requirements.get('excluded_title_keywords', [])):
        reasons.append('Title matches an excluded keyword')
    locations = requirements.get('locations', [])
    if locations and job['location'].casefold() not in [x.casefold() for x in locations]:
        reasons.append('Location is outside configured locations')
    for term in requirements.get('jd_keywords', []):
        if term.casefold() not in job.get('description', '').casefold():
            reasons.append(f'JD missing required phrase: {term}')
    return {'status': 'excluded' if reasons else 'matched', 'reasons': reasons,
            'scope': 'configured title/location/JD rules only; full eligibility not assessed'}


class IntakeStore:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript('''
          CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, identity_key TEXT UNIQUE NOT NULL,
            url TEXT NOT NULL, ats TEXT NOT NULL, payload TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
          CREATE TABLE IF NOT EXISTS intake_attempts (
            id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
            result TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
          PRAGMA user_version=1;
        ''')

    def import_job(self, job, requirements):
        import json
        for key in ('url', 'title', 'company', 'location', 'description'):
            if not isinstance(job.get(key), str) or not job[key].strip():
                raise ValueError(f'Missing or invalid job field: {key}')
        url = normalize_url(job['url'])
        # Explicit employer key + requisition survives URL aliases and email changes.
        employer = job.get('employer_key', '').strip().casefold()
        requisition = job.get('requisition_id', '').strip()
        identity = json.dumps(['requisition', employer, requisition] if employer and requisition
                              else ['url', url], separators=(',', ':'))
        job_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        result = qualify(job, requirements)
        payload = {**job, 'url': url, 'qualification': result}
        with self.connection:
            self.connection.execute('''INSERT INTO jobs(id,identity_key,url,ats,payload)
              VALUES(?,?,?,?,?) ON CONFLICT(identity_key) DO UPDATE SET
              url=excluded.url, ats=excluded.ats, payload=excluded.payload,
              updated_at=CURRENT_TIMESTAMP''',
              (job_id, identity, url, detect_ats(url), json.dumps(payload)))
            self.connection.execute('INSERT INTO intake_attempts(job_id,result) VALUES(?,?)',
                                    (job_id, json.dumps(result)))
        return {'job_id': job_id, 'ats': detect_ats(url), **payload}

    def close(self):
        self.connection.close()
