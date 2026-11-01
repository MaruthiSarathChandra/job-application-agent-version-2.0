# Job Application Agent 2.0

Python 3.11+ modular application. Phase 1 increment 1 provides durable intake of captured job postings. No third-party dependencies or API credentials required.

## Run

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

1. Public posting fetch adapters, structured JD extraction, bounded timeouts and failure records.
2. Requirements enrichment, job-source connectors, and explicitly configured search expansion.
3. Verified profile and fixed-template resume generation.
4. Secure account access and Gmail verification.
5. ATS application workers and submission reconciliation.

See [architecture](docs/architecture.md). This increment does not fetch URLs, create accounts, generate resumes, or submit applications.
