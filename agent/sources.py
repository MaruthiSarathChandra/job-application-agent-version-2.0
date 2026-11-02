"""Read-only public board adapters. No arbitrary URL fetching."""
import html
import json
import os
from urllib.parse import urlencode
import re
from html.parser import HTMLParser
from urllib.request import Request, build_opener, HTTPRedirectHandler
from datetime import datetime, timezone


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
    if not isinstance(sources, list) or not 1 <= len(sources) <= 50:
        raise ValueError('Configure 1–50 boards')
    result = []
    for source in sources:
        if not isinstance(source, str) or not re.fullmatch(r'(greenhouse|lever|ashby|smartrecruiters):[A-Za-z0-9_-]{1,80}|adzuna:[a-z]{2}|directory:verified', source):
            raise ValueError('Use greenhouse:token, lever:site, ashby:board, smartrecruiters:company, adzuna:country, or directory:verified')
        if source not in result:
            result.append(source)
    return result


def fetch_board(source, reader=read_json, page_size=None, offset=0):
    validate_sources([source])
    provider, company = source.split(':')
    if provider in ('smartrecruiters','directory'):
        raise ValueError('Use fetch_source for company directory or SmartRecruiters')
    if provider == 'greenhouse':
        payload = reader(f'https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true')
        rows = payload['jobs']
    elif provider == 'ashby':
        rows = reader(f'https://api.ashbyhq.com/posting-api/job-board/{company}')['jobs']
    elif provider == 'adzuna':
        raise ValueError('Adzuna requires a search query; use fetch_source')
    else:
        rows = reader(f'https://api.lever.co/v0/postings/{company}?mode=json&limit={page_size or 100}&skip={offset}')
        # Bounded pagination avoids silently treating page one as the whole board.
        page = rows
        while page_size is None and len(page) == 100:
            if len(rows) >= 2000:
                raise ValueError('Lever board exceeds 2,000-job run limit')
            page = reader(f'https://api.lever.co/v0/postings/{company}?mode=json&limit=100&skip={len(rows)}')
            rows = rows + page
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError('Invalid or oversized board')
    if page_size is not None and len(rows)>page_size:
        raise ValueError('Provider ignored the requested page size; cursor not advanced')
    jobs = []
    for row in rows:
        try:
            if provider == 'greenhouse':
                job = dict(url=row['absolute_url'], title=row['title'],
                           location=row.get('location', {}).get('name') or 'Unspecified',
                           description=plain(row.get('content')), requisition_id=str(row['id']))
                if isinstance(row.get('first_published'),str):
                    job['source_posted_at']=row['first_published']
                if isinstance(row.get('updated_at'),str):
                    job['source_updated_at'] = row['updated_at']  # NOT a posted-at timestamp
            elif provider == 'ashby':
                if row.get('isListed') is False:
                    continue
                job = dict(url=row['jobUrl'], title=row['title'],
                           location=row.get('location') or 'Unspecified',
                           description=plain(row.get('descriptionPlain') or row.get('descriptionHtml')),
                           requisition_id=row['jobUrl'].rstrip('/').split('/')[-1])
                if isinstance(row.get('publishedAt'), str):
                    job['source_posted_at'] = row['publishedAt']
            else:
                description = row.get('descriptionPlain') or plain(row.get('description'))
                sections = ' '.join(plain(x.get('text', '') + ' ' + x.get('content', '')) for x in row.get('lists', []))
                job = dict(url=row['hostedUrl'], title=row['text'],
                           location=row.get('categories', {}).get('location') or 'Unspecified',
                           description=plain(description + ' ' + sections + ' ' + (row.get('additionalPlain') or '')),
                           requisition_id=str(row['id']))
            if provider == 'lever':
                # Lever createdAt/updatedAt are creation/update, NOT publishing.
                for field,target in (('createdAt','source_created_at'),('updatedAt','source_updated_at')):
                    value = row.get(field)
                    if type(value) is int and 0 <= value <= 9999999999999:
                        try:
                            job[target] = datetime.fromtimestamp(
                                value/1000, timezone.utc).isoformat(timespec='seconds')
                        except (ValueError,OverflowError,OSError):
                            pass
            jobs.append({**job, 'company': company, 'employer_key': source, 'source': source})
        except (KeyError, TypeError, AttributeError) as exc:
            jobs.append({'source_error': f'Malformed posting: {type(exc).__name__}'})
    return jobs


def fetch_smartrecruiters(source, rules, reader=read_json, page_size=None, start_offset=0, title_query=None):
    """Read publicly published postings + full details via official Posting API."""
    validate_sources([source])
    provider, board = source.split(':', 1)
    if provider != 'smartrecruiters':
        raise ValueError('Expected a SmartRecruiters source')
    base = f'https://api.smartrecruiters.com/v1/companies/{board}/postings'
    rows = []
    offset = start_offset
    # Bounded traversal, public listings ONLY. No third-party career URL fetch.
    while True:
        # Public SmartRecruiters Posting API supports server-side q=job-title.
        # Exact title matching is STILL checked locally: q may search location too.
        query = (f'&q={urlencode({"q":title_query})[2:]}'
                 if isinstance(title_query,str) and title_query.strip() else '')
        payload = reader(f'{base}?limit={page_size or 100}&offset={offset}&destination=PUBLIC{query}')
        page = payload.get('content') if isinstance(payload, dict) else None
        if not isinstance(page, list) or len(page) > (page_size or 100):
            raise ValueError('SmartRecruiters returned an invalid postings page')
        rows.extend(page)
        offset += len(page)
        if page_size is None and offset > 2000:
            raise ValueError('SmartRecruiters board exceeds 2,000 postings per run')
        if page_size is not None or len(page) < 100:
            break
    wanted = rules.get('title_keywords', [])
    selected = [row for row in rows
                if isinstance(row, dict) and isinstance(row.get('name'), str)
                and (not wanted or any(term.casefold() in row['name'].casefold() for term in wanted))]
    if len(selected) > 200:
        raise ValueError('SmartRecruiters matched over 200 postings; narrow the job titles')
    # Preserve title-skipped listing rows in sliced mode so search counters reflect
    # the entire provider page, without fetching unnecessary full descriptions.
    jobs = ([{'title':row.get('name',''), 'source':source} for row in rows
             if isinstance(row,dict) and row not in selected] if page_size is not None else [])
    for row in selected:
        identifier = str(row.get('id', ''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identifier):
            jobs.append({'source':source, 'source_error':'Malformed public posting identifier'})
            continue
        try:
            detail = reader(f'{base}/{identifier}')
            sections = ((detail.get('jobAd') or {}).get('sections') or {})
            content = ' '.join(plain(sections.get(field, {}).get('text'))
                               for field in ('jobDescription','qualifications','additionalInformation')).strip()
            location = detail.get('location') or row.get('location') or {}
            if not isinstance(location, dict):
                raise ValueError('Invalid location record')
            country = str(location.get('country') or '').upper()
            location_label = ', '.join(str(location[x]) for x in ('city','region','country')
                                       if isinstance(location.get(x), str) and location[x].strip()) or 'Unspecified'
            employer = detail.get('company') or row.get('company') or {}
            apply_url = detail.get('applyUrl')
            if not isinstance(apply_url, str) or not apply_url.startswith('https://'):
                raise ValueError('Posting lacks its official HTTPS apply URL')
            jobs.append({'source':source, 'employer_key':source, 'requisition_id':identifier,
                         'company':str(employer.get('name') or board), 'title':detail.get('name') or row['name'],
                         'url':apply_url, 'location':location_label, 'country_code':country,
                         'description':content or 'Full JD unavailable',
                         'description_completeness':'full' if content.strip() else 'excerpt',
                         'description_provenance':'official_smartrecruiters_public_api',
                         **({'source_title_query':title_query} if title_query else {}),
                         **({'source_posted_at':detail['releasedDate']}
                            if isinstance(detail.get('releasedDate'),str) else {})})
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            jobs.append({'source':source,'source_error':f'Invalid public posting detail ({type(exc).__name__})'})
    if page_size is not None:
        return jobs,len(rows)
    return jobs


def fetch_source(source, rules, reader=read_json, market_page=None, market_title=None):
    """Market search and direct boards implement the same producer contract."""
    validate_sources([source])
    if source == 'directory:verified':
        from .company_directory import discovery_sources, verified_boards
        boards = discovery_sources()
        proper_names = {mapping['source']:mapping['company'] for mapping in verified_boards().values()}
        if not boards:
            raise ValueError('No verified company ATS boards registered')
        if len(boards) > 20:
            raise ValueError('Verified directory exceeds the per-run limit of 20 boards; use smaller source batches')
        jobs = []
        for board in boards:
            try:
                fetched = fetch_source(board, rules, reader)
                wanted = rules.get('title_keywords', [])
                # Keep the 299-company directory practical: it is a job
                # candidate search, not an all-roles indexing crawler.
                jobs.extend({**row, 'company':proper_names.get(board,row.get('company'))}
                            if 'source_error' not in row else row
                            for row in fetched if 'source_error' in row
                            or not wanted or any(term.casefold() in row.get('title','').casefold()
                                                 for term in wanted))
            except Exception as exc:
                # Do not discard other companies' valid results on a single API failure.
                jobs.append({'source':board, 'source_error':f'Public board unavailable ({type(exc).__name__})'})
        return jobs
    if source.startswith('smartrecruiters:'):
        # Direct (non-sliced) calls also search by the actual requested role
        # rather than retrieving all functions from a company's career board.
        # A keyword result is a candidate, never an automatic qualification.
        if rules.get('title_first') and rules.get('title_keywords'):
            seen = set()
            result = []
            for phrase in rules['title_keywords']:
                for job in fetch_smartrecruiters(source,rules,reader,title_query=phrase):
                    key = job.get('requisition_id') or job.get('url') or repr(job)
                    if key not in seen:
                        seen.add(key)
                        result.append(job)
            return result
        return fetch_smartrecruiters(source, rules, reader)
    if not source.startswith('adzuna:'):
        return fetch_board(source, reader)
    app_id, app_key = os.getenv('ADZUNA_APP_ID'), os.getenv('ADZUNA_APP_KEY')
    if not app_id or not app_key:
        raise ValueError('Adzuna market search needs ADZUNA_APP_ID and ADZUNA_APP_KEY on the server')
    country = source.split(':')[1]
    jobs = []
    seen = set()
    # Explicit per-run cap: 3 pages per title, 5 alternative titles.
    for title in ([market_title] if market_title else rules['title_keywords'][:5]):
        for page in ([market_page] if market_page else range(1, 4)):
            query = urlencode({'app_id': app_id, 'app_key': app_key, 'what': title, 'results_per_page': 50})
            try:
                payload = reader(f'https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?{query}')
            except Exception:
                # HTTPError URLs can contain credential query parameters: never log them.
                raise ValueError('Adzuna request failed; check credentials, country, quota, and network') from None
            rows = payload.get('results', [])
            for row in rows:
                identifier = str(row['id'])
                if identifier in seen:
                    continue
                seen.add(identifier)
                jobs.append(dict(url=row['redirect_url'], title=plain(row['title']),
                    company=row.get('company', {}).get('display_name') or 'Unspecified',
                    location=row.get('location', {}).get('display_name') or 'Unspecified',
                    description=plain(row.get('description')) or 'Full JD unavailable',
                    requisition_id=identifier, employer_key=source, source=source,
                    description_completeness='excerpt',
                    # Adzuna created is when the aggregator listed it; that
                    # does not prove the employer's publication instant.
                    **({'source_listed_at':row['created']}
                       if isinstance(row.get('created'),str) else {})))
            if len(rows) < 50:
                break
    return jobs


def source_catalog():
    try:
        from .company_directory import load_directory, verified_boards
        catalog_count, verified_count = len(load_directory()), len(verified_boards())
    except (ValueError, OSError, KeyError):
        catalog_count, verified_count = 0, 0
    return [
        {'name':'Greenhouse', 'kind':'Company boards', 'configured':True, 'syntax':'greenhouse:token'},
        {'name':'Lever', 'kind':'Company boards', 'configured':True, 'syntax':'lever:site'},
        {'name':'Ashby', 'kind':'Company boards', 'configured':True, 'syntax':'ashby:board'},
        {'name':'SmartRecruiters', 'kind':'Company boards · full detail', 'configured':True, 'syntax':'smartrecruiters:company'},
        {'name':f'Company watchlist · {catalog_count} career links; {verified_count} verified ATS feeds',
         'kind':'Directory; unverified links are NOT fetched',
         'configured':verified_count>0, 'syntax':'directory:verified'},
        {'name':'Adzuna', 'kind':'Market search · excerpts require full-JD enrichment',
         'configured':bool(os.getenv('ADZUNA_APP_ID') and os.getenv('ADZUNA_APP_KEY')), 'syntax':'adzuna:us'},
        {'name':'LinkedIn / Indeed / Workday / other sources', 'kind':'Dedicated discovery connectors not implemented',
         'configured':False, 'syntax':''},
    ]
