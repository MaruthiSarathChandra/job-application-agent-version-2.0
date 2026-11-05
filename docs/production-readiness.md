# Production readiness: what must be true before launch

**Current status: development preview, not production-ready.**

The project's user-facing definition of done is **one real, user-approved
application** submitted through an authorized employer ATS interface, followed
by an independently checked employer-issued receipt. See
[Issue #12](https://github.com/MaruthiSarathChandra/job-application-agent-version-2.0/issues/12).
A fully successful offline CI run, a saved resume, an application queue entry,
a guided browser prefill or an HTTP 200 never proves an application was sent.

## One start command; one audit command

Run from the repo root in your own private development environment:

```powershell
git switch main
git pull --ff-only origin main
python -m pip install -r requirements.txt
python -m agent.readiness
python -m agent.app
```

The readiness command is deliberately read-only and exits with status **2**
until mandatory conditions are met. `--json` prints a machine-readable report
without API keys, profile facts, OTP messages or local file paths. It does not
contact employers or the model.

The web interface displays a **Development preview** disclosure. Expanding it
and selecting *Check production blockers* calls `GET /api/readiness`, which
returns that same diagnostic. This is a status check, **not an install or
submission button**.

## Status by stage

| Stage | Current code | Launch acceptance |
|---|---|---|
| Discover | Public Greenhouse, Lever, Ashby, SmartRecruiters; optional Adzuna | Live board responses checked for target employers and stable pagination/dedup |
| Filter | Titles, region, employer-provided posting date, years, skill overlap | Run a real set of target jobs; verify rejects and false positives |
| Resume | GPT fact selection, verified source facts, checked DOCX | Check exact artifact contents and stable checksum with candidate |
| Handoff | Persistent application queue and Agent 3 preflight | Same job, profile revision, URL and DOCX verified before each action |
| Prepare | Guided Playwright contact prefill only | One employer-specific ATS adapter validates **all** mandatory questions, authorization and attachments |
| Submit | No enabled real write adapter | Explicit user permission, correct employer destination, safe retries, no CAPTCHA bypass |
| Reconcile | Unknown/sending state is retained | Read-only status check for ambiguous timeout/crash; never blindly resend |
| Applied history | Internal receipt contract | Employer-generated reference and confirmation URL, verified against the actual submission |
| Operations | Local SQLite and a foreground launcher | Private deployment, secrets management, recovery backups, metrics, retries, uptime and privacy review |

## Critical distinction: development adapter vs production adapter

The repository includes `application_execution.execute` and *fake adapters
for unit tests*. The executor requires an exact preflighted bundle, verified
answers and explicit permission. It creates a durable attempt reservation
**before** external actions and does not automatically retry unknown outcomes.

Neither `agent.application_browser` (guided contact prefill) nor
`agent.gmail_otp` (read-only OTP query) can submit an employer application.
Gmail's `gmail.readonly` scope can read an entire mailbox at the provider
level, even if this application filters to one expected OTP sender and label.
A separate mailbox is recommended if that capability is used.

The dashboard no longer exposes
`POST /api/applications/prepared` or `POST /api/applications/submitted`.
Previously those routes allowed arbitrary local JSON to claim a successful
submission. The internal persistence contracts remain available to a future
authorized submission worker, subject to review and employer-side evidence.

## Minimal employer pilot

1. Choose **one** exact employer job and ATS platform, and obtain permission
   to use its supported application path (authorized write API *or* a permitted
   browser-based workflow). Public **discovery** API access is not a write grant.
2. Review the exact job posting, profile claims, tailored DOCX and application
   answers. Never invent sponsorship, work authorization, work history, or legal
   declarations to satisfy an ATS form.
3. Build a platform-specific adapter that discovers mandatory questions, maps
   explicitly verified answers and uploads the checked DOCX in the supported
   format. Unknown questions or authentication/CAPTCHA challenges should
   request human intervention rather than being guessed or bypassed.
4. Test in a sandbox or employer-approved environment, then submit **one real
   application only with the candidate's explicit permission**. Prove
   employer-generated receipt and confirmation URL are stored exactly once.
5. Test cancellation, 400/401/403/429, timeouts and lost confirmations. Make
   the *uncertain* state reconcile with employer records instead of retrying.
6. Only after integration tests, security review, backups/recovery and a
   witnessed successful real submission should the readiness gate be expanded
   to certify a narrowly scoped launch. Do not add a "ready" override that
   merely changes a label.

## What will remain visible until that milestone is met

The site shows *Development preview — Employer submission NOT enabled* and
the readiness audit reports hard blockers. This prevents confusing generated
resumes, simulated receipts or merged PRs with working applications.

Previous documents and SQLite history are intentionally retained. A new
migration must be backup-tested before destructive changes.
