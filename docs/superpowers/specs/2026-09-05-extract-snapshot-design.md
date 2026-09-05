# kiln extract and the snapshot — design (plan 2 of the Rust rewrite)

**Date:** 2026-09-05
**Status:** Approved design, ready for planning
**Builds on:** README.md (the overall design), plan 1 (`transform`, `inspect`, merged via PR #8)

## Problem

`kiln transform` reads a snapshot directory but nothing in the Rust binary
produces one. The Python `kiln extract` does, but it holds every resource in
memory, has no notion of a watermark, and rewrites the whole file every run.
The README promises incremental extract: fetch only what changed since the
last run, merge it into the snapshot, and advance a watermark, so a routine
update costs hundreds of fetches rather than hundreds of thousands.

## Goals

- `kiln extract` produces and maintains the snapshot the README describes:
  `locations.ndjson` with boundaries inlined, `state.json` with the
  watermark, and a boundary cache.
- Incremental by default; `--full` rebuilds from scratch.
- Bounded memory: never hold the registry in memory. One page, one line, one
  id set.
- Resumable: a run killed during the fetch phase repeats only what it did
  not finish.
- Generic FHIR R4: standard search, `_count`, `_lastUpdated`, `Bundle.link`
  paging, bearer token. Nothing vendor specific.
- Same retry, backoff, circuit breaker and cache semantics the Python has,
  so operators keep the behaviour they know.
- `kiln run` = extract then transform.

## Non-goals

- Deletion reconciliation. Incremental runs never remove a resource; `--full`
  is the only way. ICR practice is to retire with `status = inactive`.
- Any auth beyond a bearer token. Token acquisition stays outside kiln.
- Subscriptions, Bulk Data `$export`, `_history`.
- Fetching anything other than `Location`.

## Decisions taken during brainstorming

| Question | Decision |
|---|---|
| Auth | Bearer token only, `--token` or `$KILN_TOKEN` |
| Deletions | Full refresh only |
| Pipeline shape | Three phases through the cache: page, fetch, merge |
| Watermark comparison | `_lastUpdated=ge<watermark>` (greater or equal); refetched rows replace themselves |
| Watermark value | Greatest `meta.lastUpdated` seen in the snapshot, not the run start time |
| Cache location | `snapshot/boundaries/` by default, `--cache-dir` to share one |
| Cache format | Same as the Python: `<sha256(url)>.bin` + `.meta.json` |
| Python-made snapshot | Accepted; no `state.json` means full unless `--since` |
| Different server than `state.json` records | Usage error; `--full` repoints |

## CLI

```
kiln extract --server URL [--token T] --snapshot DIR
             [--full] [--since TS]
             [--concurrency 8] [--retries 3] [--max-consecutive-failures 50]
             [--no-cache] [--refresh] [--cache-dir DIR]
kiln run     <extract flags> <transform flags>
```

- `--server`: base URL of the FHIR server; `Location` is appended.
- `--token` falls back to `$KILN_TOKEN`. Sent as `Authorization: Bearer`.
- `--full`: ignore the existing snapshot and watermark, fetch everything, and
  replace the snapshot.
- `--since TS`: use `TS` (an instant, as in `meta.lastUpdated`) instead of
  the stored watermark for this run.
- `--concurrency`, `--retries`, `--max-consecutive-failures`: as in the
  Python; `0` disables the breaker.
- `--no-cache`: neither read nor write the cache. `--refresh`: skip reads,
  still write. `--cache-dir`: use another directory as the cache.
- `run`: extract, then transform with `--snapshot` as the handoff. If extract
  fails, transform does not run.

## The snapshot on disk

```
snapshot/
  locations.ndjson     one resource per line, compact JSON, boundaries inlined
  state.json           see below
  boundaries/          <sha256(url)>.bin, <sha256(url)>.meta.json
  .incoming.ndjson     present only during, or after an interrupted, run
```

`state.json`:

```json
{
  "server": "https://fhir.example/fhir",
  "watermark": "2026-09-05T10:22:31.123Z",
  "count": 200421,
  "kiln_version": "0.2.0",
  "completed_at": "2026-09-05T11:04:00Z"
}
```

- `watermark` is the greatest `meta.lastUpdated` across every resource in
  `locations.ndjson` after the merge. A resource with no `meta.lastUpdated`
  does not move it. If no resource has one, `watermark` is absent and the
  next run is full.
- `server` is compared, after trimming a trailing slash, on every
  incremental run.
- `count` is the number of lines in `locations.ndjson`.

## Phases

### 1. Page

- One blocking HTTP client (reqwest, rustls, 60 s timeout) with
  `Accept: application/fhir+json` and the bearer header when a token is set.
- First request: `<server>/Location?_count=1000`, plus
  `&_lastUpdated=ge<watermark>` on an incremental run. Later requests use
  `Bundle.link[relation=next].url` verbatim.
- Stop when there is no next link. A repeated next URL is an environment
  error ("cyclic pagination"). More than 10,000 pages is an environment
  error.
- A non-200 status after retries is an environment error carrying the
  status and the first 500 bytes of the body. Page requests use the same
  retry policy as boundaries (connection errors, 5xx and 429 retried with
  backoff; other 4xx not).
- Each `entry.resource` that is an object with a non-empty string `id` is
  written as one compact line to `.incoming.ndjson`. Any other entry is
  reported as `page_resource_skipped` with the entry index. Resources of any
  `resourceType` are written; transform reports non-Locations.
- While writing, the pager records per line: the id, the
  `meta.lastUpdated` (if any), and the boundary attachment URL when a
  boundary extension (either known URL) has `valueAttachment.url` and no
  `data`.
- `.incoming.ndjson` is truncated at the start of the page phase. Resuming
  an interrupted run repeats paging from the beginning; only the fetch
  phase resumes through the cache.

### 2. Fetch

- The work list is the distinct boundary URLs recorded by the pager.
- `--concurrency` scoped threads share the client. Each worker, for one URL:
  - unless `--no-cache` or `--refresh`, tries the cache; a hit returns
    `Cached`;
  - otherwise fetches with up to `--retries` attempts. Backoff between
    attempts is 0.5 s doubling, capped at 30 s and floored at 0; a numeric
    `Retry-After` on 429 or 5xx replaces the computed delay, subject to the
    same clamp. Any other 4xx is not retried. Connection and timeout errors
    are retried.
  - a 200 body that parses as JSON with `resourceType = "Binary"` is base64
    decoded from `data` (missing or invalid data is a failure); any other
    body is taken as the boundary bytes as received;
  - unless `--no-cache`, writes the bytes to the cache; a cache write error
    is reported as `cache_error` and does not fail the fetch.
- Outcomes are collected on the main thread in submission order (a channel
  indexed by work position, or futures joined in order). Only the main
  thread touches the report or the counters, so two runs over the same
  input produce identical reports.
- Progress: for 50 or more URLs, `resolved N/total (F failed)` to stderr
  about twenty times over the run, and once at the end if the last line was
  not a multiple.
- Circuit breaker: if `--max-consecutive-failures > 0` and that many
  consecutive outcomes have failed while no outcome in the whole run has
  succeeded, stop submitting, let running workers finish, and return an
  environment error naming the count, the last failure detail, and the
  flag to disable the check.
- Always prints `boundaries: C cached, F fetched, X failed` to stderr, even
  when there was no work.

### 3. Merge

- Read the id of every line in `.incoming.ndjson` into a `HashSet<String>`.
- Unless `--full`, stream the existing `locations.ndjson` and copy every
  line whose id is not in the set to `locations.ndjson.tmp`. A line that
  does not parse or has no id is copied unchanged and reported as
  `snapshot_line_unparsed` once.
- Stream `.incoming.ndjson`. For each line: if the pager recorded a boundary
  URL for it, look the bytes up in the cache (the fetch phase put them
  there); on a hit, set `valueAttachment.data` to the base64 of the bytes,
  remove `url`, default `contentType` to `application/geo+json`; on a miss
  (the fetch failed), leave the attachment as it is and report
  `boundary_fetch_failed` with the URL and the failure detail. If the same
  id appears more than once in the incoming file, the last occurrence wins
  and earlier ones are dropped silently (a resource updated between pages).
  Write the line.
- With `--no-cache`, fetched bytes are held in memory by URL for the merge
  instead of in the cache. That is the one mode with unbounded memory, and
  the help text says so.
- Rename `locations.ndjson.tmp` over `locations.ndjson`. Compute the
  watermark while writing (max `meta.lastUpdated` across all written
  lines). Write `state.json` atomically. Delete `.incoming.ndjson`.
- Print `snapshot: N resources, A new, U updated, watermark T`.

## Report kinds added

| kind | where | meaning |
|---|---|---|
| `page_resource_skipped` | page | a bundle entry that is not an object or has no string id |
| `boundary_fetch_failed` | merge | a boundary URL could not be fetched; attachment left as a URL |
| `cache_error` | fetch | a cache read or write failed; treated as a miss |
| `snapshot_line_unparsed` | merge | a line in the existing snapshot is not a JSON object with an id; copied unchanged |

Extract writes its report to `snapshot/_extract_report.json` (same shape as
transform's report) and prints the summary. Transform does not read it.

## Errors

Usage (exit 2): unreadable or unwritable snapshot directory, `--server`
differs from `state.json`, `--concurrency 0`, `--since` that is not a valid
instant, `--no-cache` together with `--refresh` or `--cache-dir`.

Environment (exit 1): connection failure after retries, non-200 page, cyclic
or runaway pagination, circuit breaker, disk errors while writing.

Reports (exit 0): everything per resource or per boundary.

A run that fails in any phase leaves the previous `locations.ndjson` and
`state.json` untouched. `.incoming.ndjson` may remain and is overwritten by
the next run.

## Testing

A fake FHIR server started inside the test process on a random port, using
`httptest`, serving canned responses:

- three pages linked by `next`, then no link;
- a boundary URL returning raw GeoJSON, one returning a `Binary`, one
  returning 429 with `Retry-After: 1` then 200, one returning 500 then 200,
  one returning 404, one that never responds (timeout with a short client
  timeout in tests).

Cases:

1. Full extract writes the snapshot, inlines boundaries, writes
   `state.json` with the greatest `lastUpdated`, and the boundary summary
   line reports the cached/fetched/failed split.
2. Second run without changes fetches nothing new, uses the cache, and
   leaves the file byte-identical.
3. Incremental run: the server returns one updated and one new resource
   for `_lastUpdated=ge<watermark>`; the merge replaces and appends, keeps
   the rest, and advances the watermark. Assert the request URL contained
   `ge` and the watermark.
4. `--full` after an incremental run replaces everything, including an id
   that the server no longer returns.
5. A snapshot without `state.json` is treated as full; with `--since` as
   incremental.
6. Server mismatch is a usage error; `--full` clears it.
7. Circuit breaker fires with `--retries 1` against a server returning 500
   for every boundary and no page has succeeded; does not fire when one
   boundary succeeded first.
8. Retry-After and backoff: assert the 429-then-200 URL succeeded and took
   at least one second.
9. Resumption: pre-populate the cache with one boundary and assert that
   URL is never requested on a fresh run (`cached` count is 1, the fake
   server saw no request for it).
10. Duplicate id within incoming: last wins.
11. `run` = extract then transform, and transform is skipped when extract
    fails.

Unit tests: `Retry-After` parsing, backoff clamp, cache key and sha256
verification, next-link extraction, Binary decoding, watermark max.

## Files

```
src/extract/
  mod.rs       run_extract: orchestrates the phases, prints summaries
  client.rs    FhirClient: headers, get_with_retry, Retry-After, backoff
  page.rs      paging into .incoming.ndjson, per-line notes
  boundary.rs  work list, worker pool, outcomes, breaker, progress
  cache.rs     content addressed cache: key, read with verify, atomic write
src/snapshot/
  mod.rs       Snapshot paths, State read/write, watermark helpers
  merge.rs     streaming upsert with boundary inlining
src/run.rs     kiln run
src/cli.rs     ExtractArgs, RunArgs
```

Dependencies added: `reqwest` (blocking, rustls-tls, no default features),
`sha2` (present), `base64` (present), `time` or `chrono` only if needed for
`completed_at` (prefer formatting `SystemTime` by hand to avoid a
dependency). Dev: `httptest`.

## Open items deferred

- Parallel transform passes (recorded in plan 1's follow-up).
- Deletion reconciliation options (`_history`, id listing), if a deployment
  needs them.
