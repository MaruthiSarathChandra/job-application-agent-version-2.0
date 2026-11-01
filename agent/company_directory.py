"""Curated employer directory; URLs are leads, never automatically guessed APIs."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DIRECTORY = ROOT / 'catalog' / 'company_careers_directory.json'
MAPPINGS = ROOT / 'catalog' / 'verified_public_boards.json'


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_directory(path=DIRECTORY):
    data = _json(path)
    companies = data.get('companies')
    if not isinstance(companies, list) or len(companies) > 1000:
        raise ValueError('Company directory is missing or oversized')
    found = set()
    for company in companies:
        if not isinstance(company, dict) or not all(
                isinstance(company.get(k), str) and company[k].strip()
                for k in ('id', 'name', 'category', 'career_url')):
            raise ValueError('Company directory contains invalid records')
        if company['id'] in found:
            raise ValueError('Company directory contains duplicate IDs')
        found.add(company['id'])
        url = urlsplit(company['career_url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('Every career link must be an HTTPS public URL')
    return companies


def verified_boards(path=MAPPINGS, directory_path=DIRECTORY):
    """Mappings must cite a real board URL; no deriving a token from company name."""
    companies = {row['id']:row for row in load_directory(directory_path)}
    entries = _json(path).get('boards')
    if not isinstance(entries, list) or len(entries) > 50:
        raise ValueError('Verified board mapping must contain up to 50 entries')
    mapped, used_sources = {}, set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Invalid board mapping')
        company_id, source = entry.get('company_id'), entry.get('source')
        if company_id not in companies or company_id in mapped:
            raise ValueError('Unknown or duplicated company board mapping')
        if not isinstance(source, str):
            raise ValueError('Mapping source must be a string')
        # Kept as a local import to avoid circular imports from agent.sources.
        from .sources import validate_sources
        validate_sources([source])
        if source.split(':')[0] not in ('greenhouse', 'lever', 'ashby', 'smartrecruiters'):
            raise ValueError('Only public employer ATS boards belong in this mapping')
        if source in used_sources:
            raise ValueError('A board source is mapped to multiple companies')
        board_url = entry.get('board_url')
        proof = entry.get('proof')
        if not isinstance(board_url, str) or not isinstance(proof, str):
            raise ValueError('Verified board mappings need URL evidence')
        parts = urlsplit(board_url)
        evidence = urlsplit(proof)
        if parts.scheme != 'https' or evidence.scheme != 'https' or not parts.hostname or not evidence.hostname:
            raise ValueError('Board evidence must use HTTPS')
        if parts.hostname != evidence.hostname:
            raise ValueError('Board evidence must originate on the board host')
        verified_hosts = {
            'greenhouse': ('job-boards.greenhouse.io','boards.greenhouse.io'),
            'lever': ('jobs.lever.co',),
            'ashby': ('jobs.ashbyhq.com',),
            'smartrecruiters': ('careers.smartrecruiters.com','jobs.smartrecruiters.com'),
        }
        provider, token = source.split(':')
        if parts.hostname not in verified_hosts[provider] or parts.path.strip('/').split('/')[0].casefold() != token.casefold():
            raise ValueError('Mapping source must match the verified public board URL')
        mapped[company_id] = {**entry, 'company':companies[company_id]['name']}
        used_sources.add(source)
    return mapped


def list_companies(query='', category='', limit=100, directory_path=DIRECTORY, mapping_path=MAPPINGS):
    if not isinstance(query, str) or len(query) > 100 or not isinstance(category, str) or len(category) > 80:
        raise ValueError('Company search filters are too long')
    if type(limit) is not int or not 1 <= limit <= 299:
        raise ValueError('Company search limit must be 1–299')
    verified = verified_boards(mapping_path, directory_path)
    rows = load_directory(directory_path)
    filtered = [dict(row, board_source=verified.get(row['id'],{}).get('source'),
                     board_verified=row['id'] in verified)
                for row in rows if (not query or query.casefold() in row['name'].casefold())
                and (not category or category.casefold() in row['category'].casefold())]
    return {'directory_total':len(rows), 'verified_boards_total':len(verified),
            'matched_companies':len(filtered), 'companies':filtered[:limit],
            'truncated':len(filtered)>limit}


def discovery_sources(mapping_path=MAPPINGS, directory_path=DIRECTORY):
    return [x['source'] for x in verified_boards(mapping_path, directory_path).values()]


def main():
    parser = argparse.ArgumentParser(description='Search employer careers directory; does not fetch jobs')
    parser.add_argument('--search', default='')
    parser.add_argument('--category', default='')
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--sources', action='store_true',help='Print verified public ATS source strings only')
    args=parser.parse_args()
    try:
        if args.sources:
            print(json.dumps(discovery_sources(), indent=2))
        else:
            print(json.dumps(list_companies(args.search,args.category,args.limit),indent=2))
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f'Company directory error: {exc}\n')


if __name__ == '__main__':
    main()
