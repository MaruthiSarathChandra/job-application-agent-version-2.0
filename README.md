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

The independent worker consumes queued `resume.requested.v1` events, leases one task at a time, calls GPT through the Responses API, validates the returned writing and source references, and persists a `resume.drafted.v1` checkpoint. DOCX packaging does not repeat that API call. New written resumes are held as **Review required**, with no `applications.ready.v1` event until the user approves the exact artifact in **View resume**. Older fact-selection drafts remain readable for compatibility.

GPT writes a summary and 2–5 bullets per selected experience/project entry. The response schema restricts source IDs to the saved profile. Validation rejects unknown/duplicate entries, new numbers absent from cited sources, unsupported skill names, omitted employment, and omission of every project when projects exist. Education and structured heading/date/link data are copied locally. These checks do not prove semantic truth: reviewing the generated claims remains mandatory. No account creation or application submission is enabled.

Failures remain visible. If a process crashes after an API response but before saving, an API call may be repeated and billed again; local acknowledgements do not guarantee exactly-once billing.

The browser and worker communicate through the local durable outbox/queue, not this ChatGPT conversation. A Kafka relay can later replace the transport behind this event contract. The profile hash is saved with every draft; only verified facts are sent to the model, not arbitrary profile/contact fields.


## Agent 2 artifact handoff

Agent 2 now includes two durable internal steps: grounded GPT writing and DOCX packaging. A persisted draft is a checkpoint; packaging retries do not call GPT again. After successful packaging and user approval, the system publishes `applications.ready.v1` with the full job snapshot, local resume URI, file SHA-256, profile version, event/correlation IDs, and structural validation report. The application queue is separate from the discovery/resume queue. Agent 3 is not running and no application is submitted.

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

The built-in template uses Letter paper, Calibri 8-point body text, a 23-point centered name, bold section headings, and clickable verified links. A 2,500-word content budget prevents unbounded output; it does not prove a one-page layout. A representative one-page fixture was rendered and visually inspected during development. Runtime validation checks ZIP integrity, exact validated draft/header text, all link targets, and typography; `page_count_verified` remains false in the outgoing message.

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
The preview includes every saved entry; job-tailored Agent 2 output rewrites relevant
experience/project groups from that same profile, with a review gate. Existing drafts and generated resumes retain their
original profile version and are not overwritten by an import.

### Regenerate and view an existing resume

Each completed resume now has **View resume**, **Download checked DOCX**, and
**Regenerate resume** actions in the pipeline. View opens an escaped HTML reading
preview of the actual stored DOCX inside the dashboard; Word pagination and exact
spacing can differ.

Save/import your full profile first. Regenerate queues a fresh GPT writing request using
a snapshot of that saved profile and the job's original JD. Keep Agent 2 running:

```powershell
python -m agent.resume_worker --profile data/profile.json --watch --limit 10
```

The limit covers all worker tasks, including ordinary queued jobs; generation uses
your configured API key/model and incurs API usage. Render-only mode does not
process regeneration requests. The dashboard shows queued/processing/completed
or failure states. Repeated clicks cannot enqueue concurrent replacements for the
same resume. Failed replacements retain the previous artifact, and successful
ones archive its draft, application payload, and unpublished ready event in
`resume_revision_history`. Old artifact downloads remain available.

Replacement is supported only before downstream handoff. An already published
application-ready event is rejected. The current local outbox is replaced
atomically with the revised artifact; a future broker relay must coordinate with
application_queue.status and exclude regenerating applications before publication.

### First controlled writer run

Stop older workers and keep the dashboard on the same database. The two already
queued regeneration requests do not need another button click. Process only one:

```powershell
python -m agent.resume_worker --profile data/profile.json --limit 1
```

Open **Resume queue → View resume**, compare the draft against **View draft and
source evidence**, then approve only if the claims are accurate. The Agent 3 tab
shows approved entries only. Approval checks the exact artifact ID and checksum
so a stale preview cannot approve a newer revision.

Offline validation covers profile → writer response validation → DOCX → HTML
preview → approval → application-ready event and regeneration. It uses fixed
model-response fixtures, not a live model evaluation. Live quality still requires
one controlled API call with the user's profile and JD.

### Resume generation checkpoints and bullet errors

Agent 2 now uses one consistent 1–5 bullet contract. A short source can produce one
supported bullet instead of padding the resume. Errors identify the entry/source
and actual bullet count. Saved v1 drafts remain readable.

Completed JSON responses are saved locally **before validation**. Both initial and
replacement processing reuse the saved response when the same request is retried;
a validation failure does not cause another model call for that request. Clicking
Regenerate creates a new request and may incur a new API charge.

Inspect saved responses without credentials or a running worker:

```powershell
python -m agent.resume_checkpoint --database data/agent.db
python -m agent.resume_checkpoint --database data/agent.db --id <checkpoint-id>
```

The second command reports entry bullet counts and runs current validation against
the saved evidence. It does not change queues, approve a resume or call GPT.
`--status` and `/api/diagnostics` also include recent checkpoint metadata.
Failures from versions before checkpoint storage cannot be replayed.

After pulling the bundled update, restart the dashboard and worker. Review the
saved profile first. For an old failed regeneration, create one new regeneration
request and process one task; inspect any checkpoint error before requesting more.
Structural checks are not a guarantee of factual truth: compare rewritten claims
with their cited evidence and approve the exact resume before downstream use.
See `docs/architecture.md` for implemented boundaries and remaining work.


### Complete an excerpt-only market lead (no model call)

If an Adzuna lead shows **needs full JD** in **Fetched jobs**, use **Paste complete JD**.
Copy the complete employer description from its original posting (300–80,000 characters),
optionally enter the employer's direct HTTP(S) posting URL, and explicitly confirm
that it is the full description. The dashboard re-runs the *stored search rules*
against the pasted text and, only if it matches, transactionally creates one
resume-queue request and one durable event. Nonmatches remain excluded.

The pasted description is **user supplied and not independently verified**.
Review job provenance, eligibility, and resume claims before any downstream
application. This workflow does not scrape a website, spend GPT tokens, or submit
an application. Existing excerpt records without stored rules need one new
discovery run before they can be enriched. Re-running a market search preserves
an already-supplied full JD instead of reverting it to a shorter excerpt.

Test locally: `python -m unittest discover -s tests -v`.


## Agent 3, increment 1: approved queue preflight and restricted-behavior Gmail OTP read

The **Agent 3 queue** tab now shows *all packaged resumes*, including
**review required** items previously hidden by the UI filter. If the tab is empty,
Agent 2 has not successfully packaged a resume in the **same local SQLite database**.
Run `python -m agent.resume_worker --profile data/profile.json --limit 1`
with your already-configured API key/model, or use `--render-only` to package
an existing saved draft without another model request. Review the actual DOCX
and **approve the exact artifact** before any Agent 3 task can be created.

Agent 3 preflight can be started by clicking **Check approved jobs** on the
dashboard, or from a second terminal:

```powershell
python -m agent.application_worker --database data/agent.db --limit 10
python -m agent.application_worker --database data/agent.db --status
```

It reads only approved `applications.ready.v1` handoffs and verifies the job
snapshot, profile version, approval ID, stored resume bytes and SHA-256 before
preparing a durable application plan. Concurrency claims and bounded retries
prevent duplicate preparations. Current completed preflight status is
`awaiting_browser_adapter`, **not submitted**. No employer form is filled,
no account created, no application is submitted and no job is marked applied.
Provider-specific ATS browser adapters, answer mappings, submission confirmation,
CAPTCHA/manual blockers and uncertain-submit reconciliation are next increments.
This is a safe foundation, not a finished unattended application bot.

### Optional Gmail authorization for OTP checking (separate local consent)

Gmail is **not required** to run Agent 3 preflight. It should only be used
after an employer's sign-in/verification flow requests an OTP.

```powershell
python -m pip install -r requirements-gmail.txt
python -m agent.gmail_otp --connect --client-secrets credentials/gmail-client.json
python -m agent.gmail_otp --sender otp@employer.example --label JobAgentOTP --requested-at <unix-seconds>
```

Create a Google Cloud **OAuth Desktop app** client, enable Gmail API, and
download client JSON to the gitignored local `credentials/` folder. Google
user consent is required and OAuth might require a configured testing user.
Do **not** upload client secrets, OAuth token files, or OTPs to GitHub/chat.
The last command reports only whether an eligible code was found, not its value.
For an actual employer flow, supply its exact expected sender and request timestamp;
the sender above is only a placeholder. A dedicated Gmail filter should label
appropriate incoming verification emails with `JobAgentOTP`.

**Important Gmail permission boundary:** Google's `gmail.readonly` OAuth
scope grants read access to the mailbox, not only OTP messages. Our code
intentionally limits reads to exact sender + dedicated label + 15-minute window
and never sends, modifies or deletes mail, but that is an **application-side
restriction**, not a provider-enforced OTP-only permission. Use a dedicated
verification mailbox if you require provider-level isolation. Linking Gmail
inside ChatGPT does not automatically authorize this separate local Python app.
Gmail OTP retrieval is not yet wired to an ATS browser adapter; it is an
isolated, offline-tested reader module.

Run `python -m unittest discover -s tests -v` to execute the regression suite.


### Optional guided ATS form prefill (no automatic submission)

Once an approved job reaches `awaiting_browser_adapter`, Agent 3 can open a
**local Chromium window** and fill common contact fields plus an approved DOCX
on a visible employer form. This helper does not bypass login, navigate a
multi-page ATS workflow by itself, choose sensitive answers, or click Submit.

Create **data/application-answers.json** locally (gitignored), for example:

```json
{
  "verified": true,
  "first_name": "Your verified first name",
  "last_name": "Your verified last name",
  "email": "your-application-email@example.com",
  "phone": "+1 555 010 0000",
  "linkedin": "https://www.linkedin.com/in/your-profile/"
}
```

Never claim false work authorization, employment or degree information;
unrecognized employer questions remain for you to answer.

```powershell
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
python -m agent.application_browser --application-order 1 --dry-run
python -m agent.application_browser --application-order 1
```

The command opens the approved job in isolated local Chromium. If an
application form is not visible yet, navigate to it manually, return to the
terminal and press Enter. The helper fills *only* recognized fields, uploads
the approved DOCX if the page accepts that format, reports skipped fields and
then leaves the browser open for your review. Press Enter to close it.
Employer redirects to a different host stop the helper unless you explicitly
add `--allow-host trusted-employer.example`. No automation is attempted
for unknown questions, authentication or final submission.

This is a bounded first browser adapter, not a universal Workday/Greenhouse/
Lever solution. The Gmail OTP reader exists separately but is not yet
automatically wired to this browser. No code or employer login details are
stored in logs, and this helper does not mark an application as submitted.
