# Architecture decision: modular application with durable stages

Status: initial engineering baseline; business fallback boundaries and additional profile facts remain unconfigured.

## Four modules

| Module | Input | Output |
| --- | --- | --- |
| Discovery/intake | Requirements, source or URL | Normalized job, captured JD, provenance, qualification |
| Resume preparation | Job + immutable verified profile version | Validated content + fixed-template artifact |
| Account access | Employer tenant + account reference | Session or blocker |
| Application | Job + artifact + session + approved answers | Confirmation, blocker, or submission uncertainty |

A coordinator will persist transitions and claim bounded work. Modules depend on domain contracts; browser, model API, mailbox, and storage are adapters. Start with SQLite for local use. Multiple hosts will require a database with suitable job-claim concurrency and migrations; do not treat SQLite as a distributed queue.

## Implemented boundary

The first increment covers captured JSON -> validation -> normalization -> title/location qualification -> transactional upsert + attempt history. It establishes durable intake only. No browser or worker coordinator exists yet. SQLite schema version is 1; introduce explicit migrations before changing deployed schemas.

## Planned workflow

Discovered -> qualified/excluded -> resume_ready -> account_ready -> applying -> submission_pending -> submitted.

Any stage can record a blocker. Account access may be requested midway through application navigation. Retry counts and timeouts must be bounded. Persist submission_pending before attempting Submit; an interrupted attempt becomes submission_unknown and must reconcile with employer confirmation before any retry.

## Identity and facts

Job identity is shared across email accounts. Explicit employer keys and requisition IDs are preferable to URLs. Resume/profile versions are immutable inputs to later stages. Two email accounts never justify different invented employment histories. Sensitive factual answers come from explicitly verified values. Secrets, mailbox tokens, sessions, and generated candidate documents must stay outside Git and model prompts unless specifically required and appropriately scoped.

## Acceptance and testing

Current: importing the same job twice, including a requisition URL alias, retains one job and two attempt records across database reopening. Invalid data is not imported. URL tracking cleanup preserves meaningful requisition parameters. ATS detection checks host boundaries.

Next: fixture-based fetch tests, extraction completeness, explicit failure persistence, transition/lease recovery, account isolation, and post-submit timeout reconciliation. Live applications are not the regression suite.

## Discovery dashboard and resume handoff (increment 2)

The local HTTP server serves an interactive HTML client. `POST /api/runs` creates an asynchronous discovery run; `GET /api/state` reads persisted runs, jobs, activities, and queue entries. The browser polls every two seconds. No model is called: a documented title-prompt grammar plus explicit location, exclusion, and JD-phrase fields produces deterministic rules. A board fetch or individual posting failure is recorded while other sources continue.

Public read adapters: Greenhouse Job Board API (`content=true`) and Lever Postings API (bounded pagination). Network calls have a 20-second timeout and an 8 MB response cap. Board identifiers are validated; arbitrary hosts and redirects are not accepted. Company labels currently use board tokens. References: https://docs.greenhouse.io/job-board.html and https://github.com/lever/postings-api .

Matches produce an immutable `resume_queue` snapshot. Queue insertion and the corresponding `outbox` insert share one transaction. A unique job ID prevents duplicate enqueues across searches. Intake records and activity are separate from this transaction; a crash before enqueue is recoverable by rerunning discovery. An existing queued snapshot is not replaced when the job is fetched again with different criteria. Stage 2 must revalidate job availability and the current candidate profile before generation/application.

Event topic: `resume.requested.v1`. Payload: schema_version, queue_order, job_id, job snapshot (including URL, full JD, source, and qualification), and status. Event key: `resume.requested:<job_id>`.

This is a SQLite transactional outbox, not an installed Kafka broker. A future relay can publish outbox rows and mark published_at after broker acknowledgement; consumers must deduplicate by event_key because relay retries can redeliver. Consumer leases, acknowledgements, retries, dead-letter handling, and resume generation are deferred to stage 2. There is no consumer in this increment.

The default server listens on loopback only and rejects cross-origin POST requests. It is a local development server, not an internet-facing deployment. Each discovery run has persisted config and activity; restarts mark old running records interrupted. One run per process prevents overlapping producers; multi-process ownership/leases are future work.
