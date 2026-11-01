"""Deterministic intake domain; no browser, LLM, or credentials required."""
import hashlib
from datetime import datetime, timezone
import re
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from .freshness import date_decision


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
                        ('greenhouse.io', 'greenhouse'), ('lever.co', 'lever'), ('ashbyhq.com', 'ashby')]:
        if host == suffix or host.endswith('.' + suffix):
            return ats
    return 'career_site'


US_STATE_CODES = set('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split())
US_STATE_NAMES = set(x.casefold() for x in 'Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia'.split('|'))


def country_from_job(job):
    """Only infer US from an explicit country code or recognized US location label."""
    explicit = job.get('country_code')
    if isinstance(explicit, str) and re.fullmatch(r'[A-Za-z]{2}', explicit):
        return explicit.upper()
    label = job.get('location', '')
    if not isinstance(label, str):
        return None
    if re.search(r'\b(?:United States(?: of America)?|USA|US)\b', label, re.I):
        return 'US'
    segments = [x.strip() for x in label.split(',')]
    if len(segments) >= 2:
        last = segments[-1]
        if last.upper() in US_STATE_CODES or last.casefold() in US_STATE_NAMES:
            return 'US'
    return None


def matches_location(label, wanted):
    label, wanted = label.strip().casefold(), wanted.strip().casefold()
    return label == wanted or any(label.startswith(wanted + sep)
                                  for sep in (',', ';', ' - ', ' ('))


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
    if locations and not any(matches_location(job['location'], wanted) for wanted in locations):
        reasons.append('Location is outside configured locations')
    allowed_countries = requirements.get('country_codes', [])
    if allowed_countries and country_from_job(job) not in allowed_countries:
        reasons.append('Country outside configured countries or not verifiable from posting')
    for term in requirements.get('jd_keywords', []):
        if term.casefold() not in job.get('description', '').casefold():
            reasons.append(f'JD missing required phrase: {term}')
    if job.get('description_completeness') == 'excerpt':
        return {'status': 'needs_full_jd', 'reasons': ['Search excerpt only; retrieve the complete employer JD before qualification'],
                'scope': 'Not qualified from a search excerpt'}
    if reasons:
        return {'status':'excluded', 'reasons':reasons,
                'scope':'configured title/location/JD rules only; full eligibility not assessed'}
    age = date_decision(job,requirements)
    if age:
        return age
    return {'status':'matched', 'reasons':[],
            'scope':'configured title/location/JD/time rules only; full eligibility not assessed'}


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
        # Additive migration: legacy jobs retain unknown first-seen history.
        columns = {row[1] for row in self.connection.execute('PRAGMA table_info(jobs)')}
        for name in ('first_seen_at', 'last_seen_at', 'last_run_id'):
            if name not in columns:
                self.connection.execute(f'ALTER TABLE jobs ADD COLUMN {name} TEXT')
        self.connection.commit()

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
        checked_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            prior = self.connection.execute('SELECT payload FROM jobs WHERE identity_key=?', (identity,)).fetchone()
            # A repeat market search must not replace a user-confirmed full JD
            # with a new excerpt. New search rules can still change qualification.
            if prior and job.get('description_completeness') == 'excerpt':
                saved = json.loads(prior[0])
                if (saved.get('description_completeness') == 'user_supplied_full'
                        and saved.get('description_provenance') == 'user_pasted_unverified'):
                    job = {**job, 'description': saved['description'],
                           'description_completeness': 'user_supplied_full',
                           'description_provenance': 'user_pasted_unverified'}
                    if saved.get('description_source_url'):
                        job['discovery_url'] = url
                        job['description_source_url'] = saved['description_source_url']
                        url = normalize_url(saved['url'])
            result = qualify(job, requirements)
            payload = {**job, 'url': url, 'qualification': result}
            self.connection.execute('''INSERT INTO jobs(id,identity_key,url,ats,payload,first_seen_at,last_seen_at,last_run_id)
              VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(identity_key) DO UPDATE SET
              url=excluded.url, ats=excluded.ats, payload=excluded.payload,
              last_seen_at=excluded.last_seen_at, last_run_id=excluded.last_run_id,
              updated_at=CURRENT_TIMESTAMP''',
              (job_id, identity, url, detect_ats(url), json.dumps(payload),checked_at,checked_at,
               job.get('intake_run_id')))
            self.connection.execute('INSERT INTO intake_attempts(job_id,result) VALUES(?,?)',
                                    (job_id, json.dumps(result)))
        return {'job_id': job_id, 'ats': detect_ats(url), 'is_new':prior is None, **payload}

    def close(self):
        self.connection.close()
