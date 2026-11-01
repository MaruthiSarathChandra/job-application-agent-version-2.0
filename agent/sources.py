"""Read-only public board adapters. No arbitrary URL fetching."""
import html
import json
import re
from html.parser import HTMLParser
from urllib.request import Request, build_opener, HTTPRedirectHandler


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)


def plain(value):
    parser = Text()
    parser.feed(html.unescape(value or ''))
    return ' '.join(' '.join(parser.parts).split())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Unexpected board API redirect')


def read_json(url):
    request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'JobIntake/0.2'})
    with build_opener(NoRedirect).open(request, timeout=20) as response:
        data = response.read(8_000_001)
    if len(data) > 8_000_000:
        raise ValueError('Board response exceeds 8 MB limit')
    return json.loads(data)


def validate_sources(sources):
    if not isinstance(sources, list) or not 1 <= len(sources) <= 10:
        raise ValueError('Configure 1–10 boards')
    result = []
    for source in sources:
        if not isinstance(source, str) or not re.fullmatch(r'(greenhouse|lever):[A-Za-z0-9_-]{1,80}', source):
            raise ValueError('Use greenhouse:board-token or lever:site-name')
        if source not in result:
            result.append(source)
    return result


def fetch_board(source, reader=read_json):
    validate_sources([source])
    provider, company = source.split(':')
    if provider == 'greenhouse':
        payload = reader(f'https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true')
        rows = payload['jobs']
    else:
        rows = reader(f'https://api.lever.co/v0/postings/{company}?mode=json&limit=100&skip=0')
        # Bounded pagination avoids silently treating page one as the whole board.
        page = rows
        while len(page) == 100:
            if len(rows) >= 2000:
                raise ValueError('Lever board exceeds 2,000-job run limit')
            page = reader(f'https://api.lever.co/v0/postings/{company}?mode=json&limit=100&skip={len(rows)}')
            rows = rows + page
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError('Invalid or oversized board')
    jobs = []
    for row in rows:
        try:
            if provider == 'greenhouse':
                job = dict(url=row['absolute_url'], title=row['title'],
                           location=row.get('location', {}).get('name') or 'Unspecified',
                           description=plain(row.get('content')), requisition_id=str(row['id']))
            else:
                description = row.get('descriptionPlain') or plain(row.get('description'))
                sections = ' '.join(plain(x.get('text', '') + ' ' + x.get('content', '')) for x in row.get('lists', []))
                job = dict(url=row['hostedUrl'], title=row['text'],
                           location=row.get('categories', {}).get('location') or 'Unspecified',
                           description=plain(description + ' ' + sections + ' ' + (row.get('additionalPlain') or '')),
                           requisition_id=str(row['id']))
            jobs.append({**job, 'company': company, 'employer_key': source, 'source': source})
        except (KeyError, TypeError, AttributeError) as exc:
            jobs.append({'source_error': f'Malformed posting: {type(exc).__name__}'})
    return jobs
