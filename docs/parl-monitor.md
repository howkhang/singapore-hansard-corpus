# PARL discovery and acquisition

`singapore-hansard-corpus` owns parliamentary sitting discovery and Hansard
acquisition. `singapore-law-substrate` consumes the resulting validated export
through `EXPORT_MANIFEST.json`, report files, and section IDs. The producer does
not read the sibling's cache or import its monitoring code. The sibling may keep
its general SGPC monitoring for other purposes; it is not a prerequisite for this
pipeline.

The monitor is an operational queue, separate from the normalized export contract.
An announcement is evidence of a scheduled sitting. A dated, validated SPRS report
is required before a report can enter the corpus. Order Papers, Votes and
Proceedings, and Hansard remain distinct source types.

## Commands

Run from the repository root, using Python 3.9+ and `requirements.txt`:

```sh
python3 parl_monitor.py run --lookback-days 90 --votes --budget 5
python3 parl_monitor.py status --limit 20
```

`run` chains discovery, acquisition, and snapshot refresh under one process lock.
Valid reports already staged are included even when a new request fails; the run
still exits non-zero if any discovery or acquisition step is partial. Completeness
means all locally acquired source reports are included, not that every announced
or future sitting has an available report.

`discover` remains available as a discovery-only command. Both commands read a
publication-date window ending on today's Singapore date.
The overlapping 90-day window catches recent revisions; it does not establish
historical completeness. Use explicit dates for backfills:

```sh
python3 parl_monitor.py run --start-date 2026-07-08 --end-date 2026-09-20 --votes
```

`--max-pages` defaults to 40. A truncated inventory fails instead of becoming a
complete discovery. Large backfills should use smaller date windows or increase
that limit deliberately.

The acquisition step tries at most five due candidates, oldest first, and skips future
Singapore sitting dates. Failures retry after 24, 48, 96, then 168 hours. To retry
an already discovered date immediately:

```sh
python3 parl_monitor.py acquire --dates 2026-09-10 --retry-now --budget 1
python3 parl_monitor.py refresh
```

Explicit dates must have discovery evidence. A successful no-op means no eligible
work was due, not that all reports are available; inspect `status` and its counts.
Partial/failed runs exit 1. HTTP requests have bounded retries, timeouts, and a
short delay. Commands take an exclusive process lock to prevent overlapping writes.
Interrupted runs are identified when the next discovery/acquisition starts.

A daily scheduler can invoke `run` and retain its exit status. The separate
`discover`, `acquire`, and `refresh` commands support diagnosis and recovery.
This change does not install a scheduler.

## Sources and completeness

- [SGPC PARL listing](https://www.sgpc.gov.sg/?Page=1&Agency=PARL): read
  `/api/ApiMainListing/SubmitData` with `Agency=PARL`, `StartDate`, `EndDate`, and
  `page`. The observed API calls its row count `totalPagesCount`, with nine rows
  per page. Every page, row count, agency, publication date, and release ID is
  checked. Duplicate IDs or changing totals fail the window. `IsSuccess=false`
  also appears on valid results; structural checks determine success.
- Release detail: `/api/ApiMainListing/Detail?url=<release path>` supplies content
  and attachment references. The title must agree with the listing. Missing detail
  yields a partial run, while a fully checked listing can still nominate a date.
  A detail change creates a new release version even if its title is unchanged.
- [Votes and Proceedings](https://www.parliament.gov.sg/parliamentary-business/votes-and-proceedings):
  `--votes` captures visible sitting dates as independent positive evidence.
  This is a rolling page, not a complete inventory. Absence is never interpreted
  as cancellation or proof that a sitting did not occur.
- SPRS: POST `/search/getHansardReport/` with JSON
  `{"sittingDate":"DD-MM-YYYY"}` is checked only for eligible candidates, using
  the existing crawler's request method. A 200 response alone is insufficient: JSON structure and
  the requested sitting date must validate using `hansard_report.py`.

Only explicit "sitting on [date]" language in Order Paper or Parliament sitting
notice titles nominates a date. Publication dates are retained separately.
Ambiguous dates and cancellation/postponement language are flagged for review.
Unrelated committee announcements remain release records without becoming sittings.

All listing pages must pass before any of their releases are admitted. Previously
discovered dates survive failed or empty runs. A revised release retains its old
version and deactivates that version's evidence. If an old date loses all active
support, it becomes `needs_review`; a separate cancellation notice is flagged but
is not automatically linked to a prior release. Disappearance from a listing is
not treated as withdrawal. Attachment URLs are retained as `reference_only`;
PDFs are not captured or parsed, so changes confined to PDF bytes are not detected.

## Stored evidence and states

Default state directory: `var/monitoring/parl/` (gitignored). Override with the
global `--data-root` option; `--corpus-dir` controls reconciliation against an
existing raw corpus and defaults to `data/hansard/json/`.

| Path | Contents |
| --- | --- |
| `state.sqlite3` | Runs, versioned releases, evidence, sitting queue, acquisition attempts |
| `raw/<sha256>` | Content-addressed response bodies, including rejected responses |
| `runs/<run_id>/*.json` | Request URL/method/JSON, observation time, status, response hash, run summary |
| `reports/YYYY-MM-DD.json` | Validated staged Hansard response bytes |
| `reports/YYYY-MM-DD.meta.json` | Capture time, source URL, byte count, format, raw SHA-256 |
| `snapshot.build/build.log` | Normalization, index, validation, packing, and export log |
| `snapshot.previous/` | Previous local release, retained until the next refresh |

Network failures have an error record but no invented response body. The retained
HTTP body is `requests.Response.content` (after transport decoding). The final
response returned by the HTTP retry loop is archived; intermediate retry responses
are not. Back up this directory to retain monitoring history; it is not included
in a normalized export.

| State | Meaning |
| --- | --- |
| `candidate` | Active discovery evidence; no validated local report |
| `awaiting_report` | Empty response, HTTP 204 or 404; availability unconfirmed |
| `fetch_failed` | Transport error, other HTTP failure, invalid/error JSON, or wrong date |
| `acquired` | A validated report is in the staging directory |
| `present` | A validated report is already in the main raw corpus |
| `invalid_local` | An existing local report fails validation and needs attention |
| `needs_review` | A candidate lost all active supporting evidence |

`reconcile` revalidates local reports without making network requests. Existing
invalid files are surfaced for review. A missing staged or corpus file returns to
the candidate queue if it still has supporting evidence.

## Publishing and sibling consumption

Acquisition stages reports as an intermediate step. `run` then automatically calls
`refresh`, which validates capture sidecars, copies the current release to a build
workspace, and adds new reports with their sidecars. Conflicting bytes for an
existing date fail rather than overwrite the source. The candidate release is
normalized in full, indexed, validated, packed with shard verification, and
exported. Coverage statistics are regenerated. Only after every step succeeds
does it replace `data/hansard/` and reconcile the queue.

To finish an interrupted pipeline or publish already acquired reports:

```sh
python3 parl_monitor.py refresh
```

A failed build leaves the live release untouched and retains its build log. A
failed directory replacement restores the previous release; interruption between
the two renames is recovered at the next refresh. There is a brief path gap during
replacement, so consumers should import after `run`/`refresh` succeeds and always
verify the manifest. Do not run other corpus writers concurrently with the monitor.
An unchanged refresh is a no-op and keeps the existing manifest hash. The refresh
command operates on this repository's standard corpus path.

Give the sibling the resulting manifest SHA-256 and artifacts. Its worker verifies
the manifest bindings and report/section hashes under the existing
[consumer contract](CONSUMER_HANDOFF.md). It does not consume the producer's
SQLite database, implement legacy parsing, or treat a queue entry as a report.
The refresh summary records both the previous and new manifest hashes.

## Live verification, 20 September 2026 (Singapore)

The publication window 8 July–20 September returned eight PARL releases on one
complete listing page. All eight detail responses were captured. Seven releases
nominated five unique sitting dates; the remaining release was a committee report.
Votes and Proceedings showed ten visible dates, ending on 7 July, so it supplied
no corroboration for the five newer dates.

POST acquisition returned dated, valid modern reports for **4–5 August and
8–10 September 2026**. Snapshot refresh included all five with their capture
sidecars. The corpus now has **1,756 sittings** (423 modern, 1,333 legacy), and the
index has **2,549 Second Readings** with 303 pending First Readings. All six shards
verified. The published manifest SHA-256 is:

`3e643c42814cb44190fa54ee9018a6c7bebdde88719cef7d39934087a584c33b`

This supersedes `3bde1e1cbca412f3679036aaca9165e5803dab11bc89d9bd11b070dc29c14252`.

Local evidence under `var/monitoring/parl/runs/`:

- Discovery: `bf05b50fe4a44d0e8f5a914663c35c12/summary.json`.
- Successful acquisition: `abd24c81892d45dab85fa0cd7d8beeb3/summary.json`.
- Snapshot refresh: `a42df4b03503408b88a7225beafdf489/summary.json`.

Earlier sandbox DNS failures and GET request failures remain in the attempt
history. A known-date control established that SPRS required POST: GET returned
HTTP 500 for 7 July, while POST returned a valid report. The monitor now uses POST
and has a regression test for that request method. These earlier failures are
not evidence of unavailable Hansard reports.

`make check PYTHON=.venv/bin/python` passes 74 tests, including 19 monitor tests
and five snapshot refresh tests, and validates all 1,756 sittings.

## Snapshot update, 25 October 2026 (Singapore)

Discovery checked 12 PARL releases across two complete pages for the publication
window 27 July–25 October. All release details were captured. The 6 and 7 October
reports were acquired through SPRS POST requests and included with capture
sidecars in the normalized snapshot. The 3 November sitting is a future candidate.

The release contains **1,758 sittings** (425 modern, 1,333 legacy), **2,553 Second
Readings**, and 302 pending First Readings. All 74 tests pass, normalized report
validation passes, and all six shards verify. Seven capture sidecars are tracked
in `data/hansard/json/` so unpacking and normalizing a fresh checkout preserves
the capture metadata required to reproduce the report hashes.

Current export manifest SHA-256:

`f20f285462f4bd0e00e6de8ac3b536c970cb800f84429f96ef71f98602d76e4c`

This supersedes the September manifest above. Local discovery evidence is in
`runs/a5d8a1dfaf98457da06c5aff3de58d69/summary.json`, relative to the monitor
state directory. The final refresh summary is
`runs/78ef442e4b1340f09455e70adecfa68d/summary.json`.
