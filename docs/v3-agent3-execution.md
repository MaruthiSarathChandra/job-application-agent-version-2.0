# V3 Agent 3: safe submission execution (initial slice)

## Definition of done

One real employer-confirmed application, with the exact qualified job description and generated DOCX linked to an employer-issued receipt. A saved job, generated resume, preflight plan or HTTP 200 response is **not** a submitted application.

This PR implements the **durable execution boundary**, not a live ATS API connector, general browser submitter, or unattended auto-apply.

## Pipeline

1. Frontend submits target job titles, location/region, posted time window, maximum required years and minimum technical-skill overlap.
2. Agent 1 retrieves candidate postings through configured supported feeds. Title-first discovery and strict eligibility filters produce qualified-only search results. The percentage is recognized technical-skill overlap, **not a hiring probability**.
3. Agent 2 consumes its queue, uses a verified profile and full JD to generate validated content, renders DOCX locally, and passes the exact job/resume checksum into the Agent 3 queue.
4. Agent 3 verifies the immutable handoff, then reserves an execution attempt before any authorized employer interaction.

## Employer adapter contract

An adapter exposes a method (api or browser), a nonempty name, supports(plan), and submit(plan, resume_path, verified_answers, attempt_key). It returns SubmissionOutcome with confirmed, rejected, or unknown. Only an employer-generated submission reference, valid timestamp and HTTPS confirmation URL can produce an applied-history entry.

Calling application_execution.execute requires an exact preflighted queue position, verified answers and explicit permission for submission. The adapter is responsible for employer-specific requirements, legal answers, authentication and receipt validation. **No adapter is enabled by default.** In particular, never blindly POST JSON to a job URL; discovery APIs typically do not grant public application write access.

## Durable attempt states

| State | Meaning | Automatic retry |
|---|---|---|
| No attempt | Resume preflighted; waiting for supported adapter | N/A |
| sending | Attempt reserved before external request | Never |
| confirmed | Receipt validated and stored in applied history | Never |
| rejected | Employer rejected the application | Never |
| unknown | Timeout, crash, ambiguous response or invalid receipt | Never; reconcile first |

An application queue position has at most one execution attempt in the initial slice. A timeout or lost response may still mean the employer received an application, so a retry requires checking the employer first.

Agent 3 status now exposes submission_attempt_status and submission_method. The existing Applied History remains the source of truth for confirmed application receipts.

## Next sprint: real employer adapter

- [ ] Add one employer-authorized ATS API adapter with documented form-question discovery and mandatory-field checks
- [ ] Map required applicant questions from verified answers, including location and authorization; stop for unknown questions
- [ ] Support the ATS-supported resume upload format and exact published job posting ID
- [ ] Handle real employer receipts and redirect confirmation accurately without guessing
- [ ] Add secure secrets, HTTPS target allowlisting and no sensitive logs
- [ ] Implement read-only reconciliation of unknown attempts; do not blindly retry
- [ ] Test one real consented submission end-to-end; no fake applied records

The current unit tests inject fake adapters and do not establish live API access or actual application submissions.
