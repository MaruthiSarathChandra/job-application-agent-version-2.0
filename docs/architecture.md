# Durable discovery → resume → application handoff

## Responsibilities

| Component | Input | Output |
|---|---|---|
| Agent 1 | Saved manual search, mapped/provider sources | Qualified immutable JD snapshot in resume queue and `resume.requested.v1` outbox event |
| Agent 2 writer | Queue lease, current discovery filters, verified profile | Saved GPT response checkpoint and validated draft with immutable profile snapshot |
| Agent 2 packager | Saved draft and profile snapshot | Checked immutable DOCX, application bundle, `applications.ready.v1` |
| Agent 3 (future consumer) | Ready application bundle | Account/application processing, not implemented here |

The dashboard is an observer/controller. It does not call GPT itself. `agent.app` launches dashboard/scheduler and resume worker as separate processes. Queue order is an identifier, not an estimated waiting time. Schema versions are internal message-contract identifiers; users do not need to change them.

## Agent 1

A scheduled loop repeats the latest manual search (default every 15 minutes). It is iterative, not recursive, and skips overlapping runs in the single dashboard process. Sources are fetched sequentially, with provider pagination and per-source error isolation. Titles are filtered before ingestion; full JD, country/location, excluded titles and the chosen time rule are checked before enqueueing. One source failure does not cancel the remainder.

Deduplication uses employer+requisition where available, otherwise normalized URL. First-seen time remains stable across scans. Publication and first-seen windows are separate explicit choices; missing dates fail closed under a strict window. Search excerpts never qualify as complete JDs.

A unique queue job ID prevents repeated scans from generating repeated resumes. Tasks excluded by Agent 2 before a model call may be reset when a later search qualifies them; processing/completed tasks are not overwritten.

## Agent 2

`waiting → processing → draft_ready → rendering → ready` is the successful path. A worker claims with a lease/token, rechecks current filters, and makes one structured GPT request. Every supplied professional fact is accessible to the writer. The raw parsed response is persisted before validation. Validation failures retain the checkpoint for offline inspection, and render retries reuse the saved draft.

Jobs that fail current filters become `resume_excluded` before a model call. Provider/validation/render failures remain visible with bounded retry budgets. Expired leases can be reclaimed; stale tokens cannot finalize another worker's task. `--watch` runs continuously unless an explicit task limit is given. Worker heartbeat updates independently of slow provider calls.

The model writes content; local code owns document formatting, headers and file persistence. Source references and numeric/skill checks catch structural grounding errors but do not prove semantic truth. Automatic handoff records `review_status: not_reviewed`. `--require-resume-review` holds the bundle until explicit review instead.

## Atomic queue handoff

The immutable artifact is written and checksum-checked before the transaction. One SQLite transaction:

1. Fences the render task by lease token.
2. Records the artifact and application bundle.
3. Inserts the unique `applications.ready.v1` outbox event.
4. Records the packager receipt and completes the active resume task.

If publication fails, the transaction rolls back. An orphan immutable file is harmless; no ready event points to a partially written file. The active queue UI hides completed entries while history retains them. This is acknowledgment, not destructive deletion.

A bundle includes `schema_version`, event/correlation/job IDs, application queue order, full JD snapshot, `job_url`, profile version, artifact URI/checksum/media type/validation, unsupported requirements and eligibility status. Artifact URIs are local references: a future remote Agent 3 needs shared object storage or authenticated download, not access to a worker-local path.

Startup recovery can promote already checked `review_required` bundles without GPT, after current-filter and checksum checks. It is idempotent and retains failed/nonmatching artifacts for inspection. Regeneration keeps the previous artifact, atomically replaces an unpublished bundle only after success, and refuses replacement of a published event.

## Scaling boundary

Current deployment is one local SQLite workspace with durable logical queues, not Kafka. Consumer receipts and unique event keys provide idempotency in this database; external side effects are not guaranteed exactly once. A future Kafka relay must publish the outbox with at-least-once delivery, and Agent 3 must deduplicate by event/job revision and reconcile submissions. No Gmail access, account creation or submission worker is included in this change.

For multi-host scaling, move state/claims to a shared transactional database, artifacts to object storage, add an outbox relay, and retain the same consumer contracts. Do not run several independent dashboard schedulers against one SQLite file.

## Search and confirmed application history

See [search-and-history.md](search-and-history.md) for the six search controls, AND semantics, durable 55-posting slices, provider limitations, and Agent 3/4 prepared/submitted integration contracts. Saved discoveries are distinct from confirmed applied history.
