"""Layer 1: title-only candidate discovery, before job-description qualification.

Provider-specific searching belongs in sources/discovery_pages. This module
only checks whether a public posting's *title* matches the user's role query.
No profile, seniority, years, location or JD filters run at this stage.
"""
import re


def requested_title(job_title, phrases):
    """Return the matching user-supplied title, or None.

    Treat punctuation between words as spaces (e.g. 'Backend-Software Engineer')
    and require word boundaries. Do not accept a substring inside a longer
    unrelated word such as 'Engineer' inside 'Bioengineering'.
    """
    if not isinstance(job_title, str):
        return None
    title_words = re.findall(r'[a-z0-9+#.]+', job_title.casefold())
    for phrase in phrases:
        if not isinstance(phrase, str) or not phrase.strip():
            continue
        wanted = re.findall(r'[a-z0-9+#.]+', phrase.casefold())
        if wanted and any(title_words[i:i+len(wanted)] == wanted
                          for i in range(len(title_words)-len(wanted)+1)):
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
