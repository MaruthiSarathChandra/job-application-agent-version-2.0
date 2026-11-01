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
