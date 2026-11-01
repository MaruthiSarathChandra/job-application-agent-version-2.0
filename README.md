# Job Application Agent 2.0

Python 3.11+ modular application. Phase 1 provides a local HTML dashboard, public-board discovery, and durable intake. No third-party dependencies or API credentials required.

## Interactive dashboard

```powershell
git switch phase-1/job-intake
python -m agent.dashboard
```

Open http://127.0.0.1:8765 in your browser. Keep the terminal running. No pip installation is needed.

Enter a title prompt such as `Find software engineer jobs` or `software engineer | backend developer`. Add company boards as `greenhouse:board-token, lever:site-name`; the token is the company segment of its public board URL. These are configured boards, not an internet-wide search. Greenhouse and Lever discovery use their public read-only APIs.

Optional exact locations use semicolons (location labels can contain commas). Required JD phrases and excluded title words use commas. Full natural-language requirement extraction and automatic fallback expansion are not implemented; the page describes the supported grammar. No model calls occur in this phase.

Watch the activity feed, open a saved JD, inspect exclusion reasons, or select Resume queue to see stable order numbers, job links, and `resume_waiting` status. Data survives a server restart. The table currently shows the latest 500 saved jobs; activity shows the latest 200 events. Older records remain in SQLite. Queue order numbers are stable identifiers, not recalculated positions, and can have gaps.

Only one discovery run is active per server process. Use one server process per database. A restart marks unfinished runs interrupted; rerunning the same boards is safe against duplicate queue entries.

## CLI import

```powershell
python -m agent --job-file examples/job.json --requirements examples/requirements.json
python -m unittest discover -s tests -v
```

Repeat the first command: one job remains in `data/agent.db`, with a separate intake attempt per run. The example is fictional. Replace it with a captured posting to import real data.

## Current behavior

- Normalize raw, Markdown, and angle-bracket URLs; remove tracking and Workday `/apply` aliases.
- Detect Workday, Rippling, Greenhouse, Lever, or generic career sites.
- Require title, company, location, and JD before saving.
- Deduplicate by explicit employer key + requisition when available; otherwise canonical URL. URL-only identity cannot catch every employer alias.
- Record explicit title/location matching reasons. `matched` does not establish work authorization, experience eligibility, or fitness for submission.
- Persist jobs and intake attempt results transactionally in SQLite. Restarting does not erase progress.
- Reject unsupported configuration keys rather than silently ignoring them.

## Next increments

1. Broader job-source connectors, natural-language query planning, and richer eligibility rules.
2. Requirements enrichment, job-source connectors, and explicitly configured search expansion.
3. Verified profile and fixed-template resume generation.
4. Secure account access and Gmail verification.
5. ATS application workers and submission reconciliation.

See [architecture](docs/architecture.md). This increment fetches Greenhouse/Lever boards; it does not scrape arbitrary job URLs, create accounts, generate resumes, or submit applications.
