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

## Expanded sources and the independent GPT worker

Supported connectors now include `ashby:board` and optional market search `adzuna:us`, in addition to Greenhouse and Lever. These are separate source adapters, not universal job-board coverage. LinkedIn, Indeed, Workday discovery and other sources do not have dedicated connectors yet. The dashboard reports supported sources and setup state.

Adzuna requires `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` in the dashboard process environment. Set them locally using your normal secret manager; do not commit or send credentials in chat. With both configured, an empty Sources field searches Adzuna US using the title prompt. Search is bounded to 3 pages per title and 5 alternative titles. Adzuna descriptions are treated as excerpts: they appear as **needs full JD**, and do not enter the resume queue. Complete employer-JD enrichment is the next discovery milestone.

To avoid entering company boards on every run, put your board sources in a local JSON array and start:

```powershell
python -m agent.dashboard --sources-file examples/sources.json
```

The included file contains only the Greenhouse demonstration board. Replace it with the boards you want; leaving the Sources field empty uses those defaults. Explicit Sources overrides server defaults. Sources and requirements are visible in the persisted run config.

### Start the second process

1. Copy `examples/profile.template.json` to `data/profile.json`.
2. Replace every placeholder with exact professional facts you have verified. Set `verified` to true for the profile and each fact only after review. Do not use the template as a real profile.
3. Configure `OPENAI_API_KEY` and `OPENAI_MODEL` in the worker's environment. The key is server-side only. Select a model your API account supports with Responses structured outputs. There is no assumed default model.
4. In a second terminal, from the same repository folder and using the same database:

```powershell
python -m agent.resume_worker --profile data/profile.json --limit 1
```

For polling while discovery continues:

```powershell
python -m agent.resume_worker --profile data/profile.json --watch --limit 10
```

The limit is a task-attempt budget, not a successful-draft count. Without --watch, the worker exits if no eligible tasks are available. With --watch, it waits until the task budget is used or you press Ctrl+C.

The independent worker consumes queued `resume.requested.v1` events, leases one task at a time, calls GPT through the OpenAI Responses API, validates returned fact IDs, and atomically records a draft, acknowledgement, and `resume.drafted.v1` event. The dashboard's **GPT drafts** tab shows the result. Failures are visible, backed off, and retried at most three times. Expired leases can be recovered by another worker. If a process crashes after an API response but before saving, the API call may be repeated and billed again; only the local result/acknowledgement is deduplicated.

This is the first stage-2 increment: GPT selects/orders verified facts and reports unsupported job requirements. The application renders the exact supplied fact text, preventing generated replacements of dates, titles, or experience claims. It is **not yet a finished tailored DOCX/PDF resume**; the approved format and document renderer are still needed. No account creation or application submission is enabled. Keys never belong in the browser, Git, screenshots, or activity logs.

The browser and worker communicate through the local durable outbox/queue, not this ChatGPT conversation. A Kafka relay can later replace the transport behind this event contract. The profile hash is saved with every draft; only verified facts are sent to the model, not arbitrary profile/contact fields.
