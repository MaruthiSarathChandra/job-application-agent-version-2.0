# Search filters, batches and submission history

## Search controls

| Control | Exact meaning |
|---|---|
| Prompt | Supported title search grammar; `|` separates alternative titles (OR), not an unrestricted natural-language interpreter |
| Job board | Selected provider only, or all configured providers in round-robin order; unavailable providers are disabled |
| Region | Explicit country code or recognized country/location; does not infer work authorization |
| Publication window | Employer publication timestamp within 7/24/72 hours; Any disables the date predicate |
| Maximum required years | Maximum of recognized numeric experience requirements in the JD must be at or below selection; preferred requirements excluded; unknown fails selected filter |
| Minimum skill match | Recognized JD skills also present in verified profile divided by recognized JD skills, times 100; unknown fails selected threshold |

Filter categories combine with AND. Within a selected title/location list alternatives combine with OR. Every must-have JD phrase must appear. The experience parser is conservative and does not resolve all degree/experience alternatives. Skill overlap uses a versioned explicit vocabulary; it does not claim semantic fit, proficiency, seniority, legal eligibility or ATS success. Each saved job exposes matched/missing skills and recognized years for inspection. Profile verification and nonempty recognized skills are required when enabling the score filter.

Default UI: all connected boards, US, last 24 hours, any experience, at least 70% technical-skill overlap. The profile must exist before that search can run. Hidden legacy browser settings do not silently apply; versioned settings restore the new form. A search must be submitted before its fields change the automatic scanner's saved filters.

## Provider-aware batching

Each source has a persistent cursor keyed by source and normalized search rules. One scan requests/consumes one slice from every selected company. Provider groups are interleaved; selecting a provider never falls back to another provider. A changed search has an independent cursor.

- Lever: one `limit=55&skip=offset` response per company per scan.
- SmartRecruiters: one 55-row public listing request; relevant rows may need additional detail requests.
- Greenhouse/Ashby: their full-board endpoints are cached locally; 55 postings are processed per scan. Cache refreshes after traversal or 24 hours. Strict Greenhouse publication filters fetch relevant job details to read `first_published`, not `updated_at`.
- Adzuna: 50 postings per request, rotating configured title queries over up to three pages. These are market results, not 55 per employer. Excerpts still need full-JD enrichment.

A pending slice is saved before ingestion. If interrupted, the same slice is replayed; queue/job deduplication prevents duplicates. The cursor advances only after processing the slice. A network failure does not skip a page. Individual malformed rows are reported in activity; good rows continue. Offset APIs can change underneath a scan, so they cannot guarantee an immutable view of every external posting; repeat traversals and identity deduplication reduce that limitation.

Provider documentation: [Lever pagination](https://github.com/lever/postings-api), [Greenhouse list/detail APIs](https://docs.greenhouse.io/job-board.html), [Ashby job postings](https://developers.ashbyhq.com/docs/public-job-posting-api). There is no implemented LinkedIn, Indeed or Workday discovery adapter. A disabled option is not a hidden working integration.

## Counts and views

- Jobs searched: postings examined across recorded scans, including repeated sightings.
- Saved jobs: deduplicated stored jobs, not all raw title-skipped API rows.
- Saved matches/exclusions/needs-JD: latest stored qualification per saved job; exclusions include unknown dates rejected by strict windows. Older jobs may reflect an earlier search.
- Resume pending: active/error resume tasks, excluding completed or filter-excluded tasks.

Technical source counts and prior scans are collapsed under diagnostics. Saved jobs is a discovery view, never evidence of applying. Completed resumes appear in Agent 3 queue. Agent 3's reported preparation moves the bundle to Agent 4 queue. Only a confirmed Agent 4 submission moves it to Applied history.

## Confirmed history transaction

`application_history.prepared` and `.submitted` are local integration functions, exposed through same-origin HTTP endpoints. There are no browser/account/submission adapters in this release. A future worker must verify the real external action before calling them.

Submission requires the current exact artifact ID, readable checksum-valid file, confirmation flag, receipt/reference, timezone-aware past/current timestamp and HTTPS confirmation URL. It requires the preceding prepared state. One transaction archives the entire bundle plus receipt, changes the application status, acknowledges the prepared event and inserts a unique submitted event. Failure rolls back all history/status/event changes. Duplicate identical receipts are no-ops; conflicting receipts fail. No “mark applied” shortcut exists in the UI.
