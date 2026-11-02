"""Durable per-company cursors. One source slice per scan, fair provider ordering."""
from collections import defaultdict, deque
import hashlib
import json
import os
import time
from .sources import fetch_board, fetch_smartrecruiters, fetch_source, read_json, validate_sources

SCHEMA='''CREATE TABLE IF NOT EXISTS discovery_cursors(
 source TEXT NOT NULL, search_key TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
 cache TEXT, fetched_at REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(source,search_key));
CREATE TABLE IF NOT EXISTS discovery_pending_slices(
 source TEXT NOT NULL,search_key TEXT NOT NULL,jobs TEXT NOT NULL,next_offset INTEGER NOT NULL,
 PRIMARY KEY(source,search_key));'''
PROVIDERS=('greenhouse','lever','ashby','smartrecruiters','adzuna')


def resolve_sources(provider, explicit=None, defaults=None):
    if provider not in ('', 'all', *PROVIDERS):
        raise ValueError('This job-board connector is not available; choose an enabled board')
    from .company_directory import discovery_sources
    configured=explicit or defaults or discovery_sources()
    expanded=[]
    for source in configured:
        expanded.extend(discovery_sources() if source=='directory:verified' else [source])
    if not explicit and os.getenv('ADZUNA_APP_ID') and os.getenv('ADZUNA_APP_KEY'):
        expanded.append('adzuna:us')
    selected=list(dict.fromkeys(x for x in expanded if provider in ('','all') or x.split(':')[0]==provider))
    if not selected:
        raise ValueError('No company feeds configured for this board. Add an exact company source under Advanced.')
    # Each source is individually validated; directory expansion can exceed 10.
    for source in selected:validate_sources([source])
    return selected


def board_options(defaults=None):
    from .company_directory import discovery_sources
    configured=defaults or discovery_sources()
    expanded=[]
    for source in configured:expanded.extend(discovery_sources() if source=='directory:verified' else [source])
    names={'greenhouse':'Greenhouse','lever':'Lever','ashby':'Ashby','smartrecruiters':'SmartRecruiters','adzuna':'Adzuna'}
    options=[]
    for provider in PROVIDERS:
        count=sum(s.startswith(provider+':') for s in expanded)
        enabled=bool(count) if provider!='adzuna' else bool(os.getenv('ADZUNA_APP_ID') and os.getenv('ADZUNA_APP_KEY'))
        options.append({'id':provider,'name':names[provider],'available':enabled,'company_feeds':count,
                        'reason':'' if enabled else 'Add company feeds in Advanced' if provider!='adzuna' else 'API credentials required'})
    return options+[{'id':x.lower(),'name':x,'available':False,'reason':'Connector not implemented','company_feeds':0} for x in ('LinkedIn','Indeed','Workday')]


def round_robin_sources(sources):
    groups=defaultdict(deque)
    for source in sources:groups[source.split(':')[0]].append(source)
    result=[]
    while any(groups.values()):
        for queue in groups.values():
            if queue:result.append(queue.popleft())
    return result


def fetch_slice(owner,source,rules,reader=read_json):
    key=hashlib.sha256(json.dumps(rules,sort_keys=True).encode()).hexdigest()
    with owner.connect() as db:
        db.executescript(SCHEMA)
        pending=db.execute('SELECT jobs FROM discovery_pending_slices WHERE source=? AND search_key=?',(source,key)).fetchone()
        if pending:return json.loads(pending['jobs'])
        row=db.execute('SELECT * FROM discovery_cursors WHERE source=? AND search_key=?',(source,key)).fetchone()
    offset=row['offset'] if row else 0
    cache=json.loads(row['cache']) if row and row['cache'] else None
    fetched_at=row['fetched_at'] if row else 0
    provider=source.split(':')[0]
    size=55
    if provider=='lever':
        jobs=fetch_board(source,reader,page_size=size,offset=offset)
        next_offset=offset+len(jobs) if len(jobs)==size else 0
        # Lever's public Postings API has no arbitrary full-text job-title
        # search parameter; screen these 55 titles locally before stage 2.
        # Cursor still advances over provider raw pages without dropping jobs.
        if rules.get('title_first'):
            from .title_search import requested_title
            jobs=[item for item in jobs if 'source_error' in item or
                  requested_title(item.get('title'),rules['title_keywords'])]
    elif provider=='smartrecruiters':
        if rules.get('title_first'):
            # q=title is supported by this employer's public Posting API.
            # Rotate alternative role titles; each keeps an independent page
            # cursor, so we do not scan every role at the company.
            titles=rules['title_keywords'][:10]
            state=cache if isinstance(cache,dict) and cache.get('mode')=='title_queries_v1' else {}
            title_offsets=state.get('offsets')
            if not isinstance(title_offsets,list) or len(title_offsets)!=len(titles):
                title_offsets=[0]*len(titles)
            index=offset%len(titles)
            jobs,count=fetch_smartrecruiters(
                source,rules,reader,page_size=size,
                start_offset=title_offsets[index],title_query=titles[index])
            title_offsets[index]=title_offsets[index]+count if count==size else 0
            cache={'mode':'title_queries_v1','offsets':title_offsets}
            next_offset=(index+1)%len(titles)
        else:
            jobs,count=fetch_smartrecruiters(source,rules,reader,page_size=size,start_offset=offset)
            next_offset=offset+count if count==size else 0
    elif provider=='adzuna':
        titles=rules['title_keywords'][:5]
        title_index=offset%len(titles)
        page=offset//len(titles)+1
        jobs=fetch_source(source,rules,reader,market_page=page,market_title=titles[title_index])
        # Adzuna supports 50 per request; do not promise 55 or lose the final page.
        next_offset=offset+1 if page<3 or title_index<len(titles)-1 else 0
        # Adzuna what=title is a real server query, but may return semantic
        # near matches. Enforce job-title scope before further filtering.
        if rules.get('title_first'):
            from .title_search import requested_title
            jobs=[item for item in jobs if 'source_error' in item or
                  requested_title(item.get('title'),rules['title_keywords'])]
    else:
        # Greenhouse/Ashby list APIs return full boards. Cache one response, then
        # consume at most 55 raw postings per company per scan, without fake pagination.
        if cache is None or offset==0 or time.time()-fetched_at>86400:
            cache=fetch_board(source,reader)
            offset=0;fetched_at=time.time()
            if rules.get('title_first'):
                from .title_search import requested_title
                # Greenhouse and Ashby public boards lack a title-query GET.
                # Filter the fetched listing once and page only relevant
                # title candidates; never claim provider-side keyword search.
                cache=[item for item in cache if 'source_error' in item or
                       requested_title(item.get('title'),rules['title_keywords'])]
        jobs=cache[offset:offset+size]
        next_offset=offset+len(jobs) if offset+len(jobs)<len(cache) else 0
    if provider=='greenhouse' and rules.get('posted_within_hours') and rules.get('time_basis','published')=='published':
        company=source.split(':')[1]
        for job in jobs:
            if job.get('source_error') or job.get('source_posted_at'):continue
            from .title_search import requested_title
            if not requested_title(job.get('title'),rules.get('title_keywords',[])):continue
            identifier=job.get('requisition_id','')
            if not str(identifier).isdigit():continue
            try:
                detail=reader(f'https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{identifier}')
                if isinstance(detail.get('first_published'),str):job['source_posted_at']=detail['first_published']
            except (OSError,ValueError,TypeError):
                job['date_enrichment_error']='Publication date unavailable from job detail'
    # Save the response before ingestion. Restart replays this same slice;
    # acknowledgement advances the cursor only after ingestion finishes.
    with owner.connect() as db:
        db.execute('INSERT OR REPLACE INTO discovery_cursors VALUES(?,?,?,?,?)',
                   (source,key,offset,json.dumps(cache) if cache is not None else None,fetched_at))
        db.execute('INSERT OR REPLACE INTO discovery_pending_slices VALUES(?,?,?,?)',
                   (source,key,json.dumps(jobs),next_offset))
    return jobs


def acknowledge_slice(owner,source,rules):
    key=hashlib.sha256(json.dumps(rules,sort_keys=True).encode()).hexdigest()
    with owner.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT next_offset FROM discovery_pending_slices WHERE source=? AND search_key=?',(source,key)).fetchone()
        if row:
            db.execute('UPDATE discovery_cursors SET offset=? WHERE source=? AND search_key=?',(row['next_offset'],source,key))
            db.execute('DELETE FROM discovery_pending_slices WHERE source=? AND search_key=?',(source,key))
