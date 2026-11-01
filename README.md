# Job Application Agent 2.0

Python 3.11+ modular application. Phase 1 provides a local HTML dashboard, public-board discovery, and durable intake. Discovery requires no third-party dependencies or API credentials. Agent 2 requires the DOCX dependency and, for GPT calls, local API configuration.

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
2. Replace every placeholder, including resume_header (name, contact lines, and links), with exact professional facts you have verified. Set `verified` to true for the profile and each fact only after review. Do not use the template as a real profile.
3. Configure `OPENAI_API_KEY` and `OPENAI_MODEL` in the worker's environment. The key is server-side only. Select a model your API account supports with Responses structured outputs. There is no assumed default model.
4. Install Agent 2 dependencies with `python -m pip install -r requirements.txt`. In a second terminal, from the same repository folder and using the same database:

```powershell
python -m agent.resume_worker --profile data/profile.json --limit 1
```

For polling while discovery continues:

```powershell
python -m agent.resume_worker --profile data/profile.json --watch --limit 10
```

The limit is a task-attempt budget, not a successful-draft count. Without --watch, the worker exits if no eligible tasks are available. With --watch, it waits until the task budget is used or you press Ctrl+C.

The independent worker consumes queued `resume.requested.v1` events, leases one task at a time, calls GPT through the OpenAI Responses API, validates returned fact IDs, and atomically records a draft, acknowledgement, and `resume.drafted.v1` event. The dashboard's **GPT drafts** tab shows the selection. Agent 2 then renders the DOCX and publishes it to the **Agent 3 queue** tab. Failures are visible, backed off, and retried at most three times. Expired leases can be recovered by another worker. If a process crashes after an API response but before saving, the API call may be repeated and billed again; only the local result/acknowledgement is deduplicated.

GPT selects/orders verified facts and reports unsupported job requirements. Agent 2 renders the exact supplied fact text into a Calibri DOCX with bold headings and configured links. There is no generated prose rewriting or PDF output yet. Runtime checks are structural/content checks; page count and candidate-specific visual layout are not automatically verified. No account creation or application submission is enabled. Keys never belong in the browser, Git, screenshots, or activity logs.

The browser and worker communicate through the local durable outbox/queue, not this ChatGPT conversation. A Kafka relay can later replace the transport behind this event contract. The profile hash is saved with every draft; only verified facts are sent to the model, not arbitrary profile/contact fields.


## Agent 2 artifact handoff

Agent 2 now includes two durable internal steps: GPT fact selection and DOCX packaging. A persisted draft is a checkpoint; packaging retries do not call GPT again. On success, the worker publishes `applications.ready.v1` with the full job snapshot, local resume URI, file SHA-256, profile version, event/correlation IDs, and structural validation report. The application queue is separate from the discovery/resume queue. Agent 3 is not running and no application is submitted.

Start Agent 2 after configuring the verified profile and API environment:

```powershell
python -m pip install -r requirements.txt
python -m agent.resume_worker --profile data/profile.json --watch --limit 10
```

To package already-saved drafts without API calls (requires the same profile version that generated those drafts):

```powershell
python -m agent.resume_worker --profile data/profile.json --render-only --limit 10
```

The resume header is kept local; only professional facts are sent to GPT. The profile fingerprint includes header data so the saved artifact remains tied to the exact input version. Adding a header to an old headerless profile changes its version; older drafts cannot be silently rebound to the new profile.

DOCX artifacts default to `artifacts/resumes`, configurable with `--artifact-dir`. This is a local-storage adapter: agents on this machine share the same database and artifact directory. For different hosts, replace it with shared artifact storage; `local://` is a logical locator, not a web-accessible URL. The dashboard offers checked DOCX downloads by artifact ID and rejects downloads if the stored checksum fails.

The built-in template uses Letter paper, Calibri 11-point body text, a 17-point centered name, bold section headings, and clickable verified links. A 2,500-word content budget prevents unbounded output; it does not prove a one-page layout. A representative one-page fixture was rendered and visually inspected during development. Runtime validation checks ZIP integrity, exact facts/header text, all link targets, and typography; `page_count_verified` remains false in the outgoing message.

Missing credentials/profile/header or dependencies stop before model use. Worker errors and render errors are recorded separately. A rendering failure, profile mismatch, changed fact, or failed outgoing-event transaction never creates an application-ready queue entry. Files are atomically saved under content hashes before the outgoing DB transaction. Failed transactions can leave harmless unreferenced files, reserved for future garbage collection.

Message fields:

| Field | Meaning |
| --- | --- |
| schema_version | Version of the JSON contract, not the model or candidate version |
| event_id | Unique ID of this message |
| correlation_id | Shared ID connecting one job workflow |
| job_id | Stable identity of the job |
| profile_version | Fingerprint of the verified candidate input |
| resume.artifact_id | Identity of the stored resume file |
| resume.sha256 | File fingerprint used to detect changes |

`ready_for_application` means the artifact is available for Agent 3 to inspect and begin its workflow. It does not mean legal/work-authorization eligibility has been assessed, missing requirements have been waived, or submission has been authorized by a new policy. Those decisions remain in the later application module.

### Create your profile in the browser

Start the dashboard with `python -m agent.dashboard`, then choose **Resume profile**
or open http://127.0.0.1:8765/profile. Enter your name, contact details, professional
links, and separate resume entries. Review the accuracy checkbox and click **Save
profile**. You can return to this page to edit your saved details; no JSON editing
is needed.

The default dashboard saves to `data/profile.json`, which is excluded from Git.
Run Agent 2 with `python -m agent.resume_worker --profile data/profile.json --limit 1`
in the terminal with your API key and model configured. Restart an already-running
worker after profile edits: it keeps the profile version loaded at startup.
Existing generated resumes are not regenerated automatically.

For a custom database location, the profile defaults to `profile.json` beside the
database. Override the dashboard save path with `--profile`, and give the worker
that same path. This form currently edits one local profile.

### Import your standard resume and preview its layout

On **Resume profile**, choose your existing paragraph-based DOCX (up to 2 MB)
and click **Import DOCX for review**. Import fills the form without saving,
verifying claims, or calling GPT. Review the entries, headline, dates, section
assignments, and links; confirm accuracy and save.

Imported experience and project entries retain their heading, metadata, bullets,
and hyperlink targets as a single selectable group. When editing an imported
entry, keep its paragraph count; to restructure it, remove it and add a fresh
entry. Import currently recognizes Summary, Technical Skills, Professional
Experience, Selected Engineering Projects, Education, and their common short
headings. Tables are rejected rather than silently skipped.

**Download saved profile resume** produces a full-profile DOCX with no model
call and no application-queue event. The compact reference template uses Calibri,
a centered name/headline/contact block, blue uppercase section headings and rules,
bold role/project headings, bullets, and right-aligned tabbed dates/links.
It supports multiple pages (2,500-word limit); page count is not enforced at runtime.
The preview includes every saved entry; job-tailored Agent 2 output selects relevant
groups from that same profile. Existing drafts and generated resumes retain their
original profile version and are not overwritten by an import.
