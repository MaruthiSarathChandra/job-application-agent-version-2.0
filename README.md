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
2. Select sources (`directory:verified` uses mapped company feeds), countries, optional locations/JD phrases, and excluded title words. Empty exclusions permit senior titles.
3. Choose a time window and its meaning: **employer publication** requires a verified posting timestamp; **first discovered** uses when this workspace first found the job. First discovered does not mean newly posted. Older records with unknown first-seen time remain unknown.
4. Click **Scan now** to save and run the search. Future scheduled scans reuse these filters. Scans run sequentially across sources, isolate failures, and deduplicate jobs.
5. Agent 2 rechecks the queued JD against the latest manual search filters before spending an API call. It writes from the verified profile, validates the response, renders and checks the DOCX, and atomically sends resume + JD + job URL to Agent 3 queue.

The default repeat interval is 15 minutes. Change it on the website (5–1440 minutes), or use 0 to disable. The setting survives restarts. Discovery runs only while the app is running and after an initial search. The activity timeline reports source failures and counts. The Agent 2 indicator uses a heartbeat, independently of dashboard connectivity.

Successful tasks disappear from the **active Resume queue**. Their records and drafts remain available in history; the **Agent 3 queue** shows completed bundles. Readiness means the configured structural and source checks passed, not human factual approval or verified eligibility. Review generated resumes before real applications; semantic truth cannot be guaranteed by schema checks.

## Worker modes and recovery

For separate terminals:

```powershell
python -m agent.dashboard
python -m agent.resume_worker --watch
```

`--watch` without `--limit` runs continuously. `--limit 10` deliberately stops after ten task attempts. Without `--watch`, the default is one task. Failures are recorded with bounded retries; one failed regeneration does not stop other jobs.

Existing resumes held at `review_required` are recovered automatically at worker startup if their job still matches and their artifact checksum is intact. To do this without another GPT call:

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
