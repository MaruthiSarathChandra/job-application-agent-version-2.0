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

## Qualified-only home view (current behavior)

The discovery home page intentionally shows **only confirmed matches for the
latest scan**, with the recognized technical-skill overlap score in the match
column and the employer publication date in its own column.

- The four main counters describe the **current scan only**: candidate rows
  checked, unique title candidates stored, qualified matches, and new resume
  requests queued. They do **not** sum unrelated historical jobs or failed
  resume attempts.
- Full employer postings that fail the selected country, publication window,
  required-years or profile-skill conditions are not returned in
  `qualified_jobs`. They remain available in persisted history and
  collapsed scan diagnostics for investigation.
- The endpoint's `qualified_jobs_count` reports all exact matches for
  the latest run; `qualified_jobs` is a bounded list of the top 250,
  ordered by descending recognized technical-skill overlap.
- A broad title phrase such as `Software Engineer` **does not** include
  `Senior`, `Sr`, `Staff`, `Principal`, `Lead`, `Director`,
  `Manager` or `Architect` variants. The user can request an explicit
  senior-level title by typing that complete title in the search prompt.
  No user-maintained exclusion control is needed.
- Saved search settings migrate from the previous browser form, **ignoring
  obsolete manual excluded-title lists**, preserving chosen 7/24/72-hour,
  experience and overlap filters. Changes become effective when the user
  submits a fresh search, and scheduled scans replay the latest submitted
  request.
- Historical resumes, failed GPT attempts and existing Agent 3 preflight
  records are not deleted or reported as applied. The Agent 2 worker's
  existing pre-GPT requalification check uses the newly required role/level
  match before making a paid generation call. Unclaimable exhausted older
  tasks may remain in audit data.

A displayed 85% score means the percentage of *recognized JD technical
skills* found in verified profile facts, **not an application acceptance
probability or proof of overall eligibility**. Requirements whose evidence
is missing do not pass a selected strict filter. The initial employer-site
submission problem is a separate future milestone.

## Counts and views

- Jobs searched: postings examined across recorded scans, including repeated sightings.
- Saved jobs: deduplicated stored jobs, not all raw title-skipped API rows.
- Saved matches/exclusions/needs-JD: latest stored qualification per saved job; exclusions include unknown dates rejected by strict windows. Older jobs may reflect an earlier search.
- Resume pending: active/error resume tasks, excluding completed or filter-excluded tasks.

Technical source counts and prior scans are collapsed under diagnostics. Saved jobs is a discovery view, never evidence of applying. Completed resumes appear in Agent 3 queue. Agent 3's reported preparation moves the bundle to Agent 4 queue. Only a confirmed Agent 4 submission moves it to Applied history.

## Confirmed history transaction

`application_history.prepared` and `.submitted` are local integration functions, exposed through same-origin HTTP endpoints. There are no browser/account/submission adapters in this release. A future worker must verify the real external action before calling them.

Submission requires the current exact artifact ID, readable checksum-valid file, confirmation flag, receipt/reference, timezone-aware past/current timestamp and HTTPS confirmation URL. It requires the preceding prepared state. One transaction archives the entire bundle plus receipt, changes the application status, acknowledges the prepared event and inserts a unique submitted event. Failure rolls back all history/status/event changes. Duplicate identical receipts are no-ops; conflicting receipts fail. No “mark applied” shortcut exists in the UI.


## Title-first discovery (updated pipeline)

The dashboard now sends `title_first: true`, and stores that choice with
the scheduled search. The same existing Layer 2 match/eligibility rules are
still applied before *any new resume GPT request*:

```text
User requested titles (OR)
  → Layer 1: public employer GET / title candidate screen
    → Match section (title-matched candidates, regardless of eligibility)
      → Layer 2: AND of selected filters
        → matched: resume.requested.v1 → Agent 2
        → excluded: remains visible with reasons, never queues a new resume
        → needs_full_jd/date_unknown: hold until enough information exists
```

Provider honesty matters:

- **SmartRecruiters public Posting API:** actual `GET /postings?q=<job title>`;
  one title per company/scan slice, independently paginated in 55-row chunks.
  The response gets a second local title check because its q parameter can
  match other text such as location.
- **Adzuna:** actual market search `what=<job title>` plus local title check;
  supports 50 records per provider page and only returns JD excerpts.
- **Greenhouse / Ashby:** public list GET has no general role-title query;
  fetch the board index once, locally select relevant titles, then process
  title candidates in up-to-55 slices. There is no claim of provider-side
  keyword search.
- **Lever:** public Postings API supports pagination but not generic full-text
  title search; each 55-row GET is title-screened locally. Its cursor advances
  by the *raw fetched page count*, never by the number of candidates.
- **Indeed / LinkedIn:** not connected, never silently substituted with an
  unrelated provider.

The title stage deliberately does **not** apply years, years' experience,
sponsorship, region, skill overlap or JD keyword heuristics. Those live in
the separate second filtering layer. Role alternatives are OR; the selected
Layer 2 filters are AND. Generated resumes never pass through title
screening alone.

The new web form defaults to excluding Senior/Sr/Staff/Principal/Lead/
Manager/Director/Architect titles. The user can explicitly edit exclusions.
A fresh v4 browser form is used to avoid old empty seniority exclusions
being silently restored, while the previous saved title prompt is retained.

Old job records, DOCX files and Agent 3 queue plans remain untouched;
new exclusions apply only to future scans and current eligibility
checks at handoff. Existing queued/generated applications should be
reviewed separately instead of being automatically deleted.
