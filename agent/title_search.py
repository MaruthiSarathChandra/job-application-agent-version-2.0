"""Layer 1: title-only candidate discovery, before job-description qualification.

Provider-specific searching belongs in sources/discovery_pages. This module
only checks whether a public posting's *title* matches the user's role query.
No profile, seniority, years, location or JD filters run at this stage.
"""
import re


_SENIORITY = frozenset(('senior','sr','staff','principal','lead','manager',
                         'director','head','architect','chief','vp','vice'))


def _words(value):
    return [word.rstrip('.') for word in re.findall(r'[a-z0-9+#.]+', value.casefold())]


def requested_title(job_title, phrases):
    """Match requested role titles without silently expanding junior roles.

    A broad title such as 'Software Engineer' does not include Senior, Staff,
    Principal or Lead variants. The user may request one of those explicitly.
    This is a title-level safety constraint, not a résumé/profile decision.
    """
    if not isinstance(job_title, str):
        return None
    words = _words(job_title)
    seniority = _SENIORITY.intersection(words)
    for phrase in phrases:
        if not isinstance(phrase, str) or not phrase.strip():
            continue
        wanted = _words(phrase)
        if not wanted:
            continue
        if seniority and not seniority.intersection(wanted):
            # Never burn model credits on senior/staff roles merely because
            # they contain a generic 'Software Engineer' phrase.
            continue
        if any(words[i:i+len(wanted)] == wanted
               for i in range(len(words)-len(wanted)+1)):
            return phrase
    return None

def title_candidate(posting, rules):
    """Convert only title-matching API results into layer-2 input."""
    phrase = requested_title(posting.get('title'), rules['title_keywords'])
    if phrase is None:
        return None
    return {**posting,
            'discovery_layer': 'title_candidate_v1',
            'requested_role': phrase,
            'title_match_method': 'provider_keyword_plus_local_title_check'
                                  if posting.get('source_title_query') else 'local_title_check'}
