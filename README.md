# Job Application Agent 2.0

A local Python 3.11+ dashboard with independent job discovery and resume workers. SQLite queues and transactional outbox records connect the workers. Agent 3 receives application bundles; account creation and application submission are not implemented.

## Start both agents

From the repository root, stop old dashboard/worker processes, install dependencies, then run:

```powershell
python -m pip install -r requirements.txt
python -m agent.app
```

Set `OPENAI_API_KEY` and `OPENAI_MODEL` in this terminal's environment before starting. Keep credentials private; never paste them into the profile or commit them. The launcher opens the server at http://127.0.0.1:8765 and waits for a verified profile before starting Agent 2. Create/import/edit your profile on the website. No manual JSON editing is required. The launcher stays running until Ctrl+C.

Use one launcher/dashboard per database. Relative paths resolve from your current directory; always run from the same repository root. Defaults are `data/agent.db`, `data/profile.json`, and `artifacts/resumes`. Back up these together before upgrading. Do not delete your database to refresh the page.

## Discovery and automatic processing

1. Enter title alternatives separated by `|`, such as `Software Engineer | Java Developer`.
2. Choose a **job board** or **All connected boards**, **region**, and **publication window** (7 hours, 24 hours, 3 days, or Any).
3. Optionally choose the maximum years explicitly required in the JD, and minimum technical-skill match (70–100%, or no filter). Match compares recognized JD technical skills against your verified profile; unknown scores/years fail a selected filter. It is not an ATS or hiring probability.
4. All selected filter categories use **AND**. Alternative titles within the prompt use **OR**. Optional company tokens, city phrases, required JD phrases and excluded title words are under **Advanced**.
5. Click **Scan now**. Each scan processes up to 55 raw postings per company, then continues from the next durable slice on the next scan. Different providers rotate in order. One provider selected means only that provider; unavailable connectors cannot be selected.
6. Agent 2 rechecks the JD before GPT, writes from the verified profile, validates and renders the DOCX, then atomically sends resume + JD + job URL to Agent 3 queue.

Greenhouse/Ashby return full-board responses: the app caches them and processes 55 at a time. Lever requests `limit=55`; SmartRecruiters requests 55 listing rows and fetches details only for relevant titles. Adzuna uses 50 per request, its supported page size. These are not universally 55-job network responses. Publication filtering uses employer timestamps; Greenhouse detail lookups obtain `first_published` when needed. Unknown publication times are never invented. See [search and history contracts](docs/search-and-history.md).

The default repeat interval is 15 minutes. Change it on the website (5–1440 minutes), or use 0 to disable. The setting survives restarts. Discovery runs only while the app is running and after an initial search. The activity timeline reports source failures and counts. The Agent 2 indicator uses a heartbeat, independently of dashboard connectivity.

Successful tasks disappear from the **active Resume queue**. **Applied history** is separate and remains empty until Agent 4 reports a confirmed submission with the exact resume and confirmation reference. Their records remain under **Saved jobs** and drafts under **GPT drafts**; the **Agent 3 queue** shows completed bundles. Readiness means the configured structural and source checks passed, not human factual approval or verified eligibility. Review generated resumes before real applications; semantic truth cannot be guaranteed by schema checks.

## Worker modes and recovery

For separate terminals:

```powershell
python -m agent.dashboard
python -m agent.resume_worker --watch
```

`--watch` without `--limit` runs continuously. `--limit 10` deliberately stops after ten task attempts. Without `--watch`, the default is one task. Failures are recorded with bounded retries; one failed regeneration does not stop other jobs.

Existing resumes held at `review_required` are recovered automatically at worker startup if their job still matches and their artifact checksum is intact. Use **Move completed resumes to Agent 3** on the dashboard, or run this without another GPT call:

```powershell
python -m agent.resume_worker --handoff-only
```

This retains the verified-profile requirement but does not require an API key. Nonmatching jobs are marked for requalification, corrupt/missing files are blocked, and neither is silently published. Existing already-ready bundles are not rewritten.

To retain an explicit manual review gate instead of automatic handoff:

```powershell
python -m agent.app --require-resume-review
```

To package saved drafts without calling GPT:

```powershell
python -m agent.resume_worker --render-only --limit 100
```

Use **Regenerate resume** only when changing the desired content/profile; it creates a new request. The previous file is retained until the replacement succeeds. Published downstream events cannot be silently replaced. **View resume** shows the saved DOCX's reading preview; exact Word pagination is not guaranteed.

Diagnostics:

```powershell
python -m agent.resume_worker --status
python -m agent.resume_checkpoint --database data/agent.db
```

A normal successful resume requires one GPT request. Returned responses are checkpointed before validation, so retrying validation/rendering does not repeatedly pay for the same saved response. An explicit regeneration is a new request. API/network failures before a response is saved can require another request.

## Resume evidence and formatting

Import a standard DOCX on `/profile`, review its parsed experience/projects/education, and save verified facts and header fields. Include all actual experience, skills, dates and projects. The model receives all saved professional facts, their layout blocks, and the full queued JD; contact data stays in the local renderer. It cannot access your regular ChatGPT conversations or infer missing resume facts.

The writer returns structured JSON with source references. Validation rejects unknown or duplicate sources, unsupported skill names, unsupported numbers and omitted required sections. It allows 1–5 evidence-supported bullets; it must not invent extra achievements to fill space. The deterministic DOCX renderer preserves headings, dates, education and links. A saved draft carries its original profile snapshot, so editing your profile while jobs are processing does not invalidate that draft.

## Actual source coverage

Live adapters: Greenhouse, Lever, Ashby and SmartRecruiters. `directory:verified` expands only mapped feeds, not all 299 links in the company directory. Explicit board tokens can add employers using supported adapters. Adzuna market search requires its credentials/configuration; excerpts need full-JD enrichment before enqueueing.

There is no universal “every job board” connector. LinkedIn, Indeed, Workday and other unimplemented providers are shown as unavailable. Detecting an ATS from a URL does not mean its discovery connector exists. Source failures are visible; missing publication timestamps are never replaced with employer update timestamps.

## Validation

```powershell
python -m unittest discover -s tests -v
```

Tests cover source parsing/pagination, title/country/date filters, repeated scans, queue deduplication, worker leases, response checkpoints, DOCX validation, regeneration, HTTP endpoints and atomic handoff recovery. GPT results in tests are fixtures; this does not establish live-provider uptime or factual accuracy of arbitrary generated resumes. CI runs on Windows and Linux across Python 3.11–3.13.

See [architecture](docs/architecture.md) for queue contracts and failure behavior.

## Agent 3 and Agent 4 status

This repository has queue contracts, not working account-creation or submission adapters. After actual preparation, Agent 3 can POST `/api/applications/prepared` with `job_id`, `artifact_id`, and `prepared: true`. After actual confirmed submission, Agent 4 can POST `/api/applications/submitted` with the same IDs, `submitted: true`, `confirmed: true`, `submission_id`, timezone-aware `submitted_at`, and an HTTPS `confirmation_url`. These endpoints record trusted local worker reports; they do not submit or independently confirm with employers. Do not call them to simulate real applications.

Applied history retains the exact JD, job link, resume artifact and receipt. Failed/unconfirmed outcomes never create an applied record. Repeated identical receipts are idempotent; conflicting receipts are rejected. Once prepared/submitted, regeneration cannot replace the resume used for that application.
