"""All network work: paged FHIR search and boundary URL resolution.

This is the only module that touches the network. `transform` is offline,
which is what makes the geo half testable from fixtures.
"""

from __future__ import annotations

import base64
import binascii
import json
import sys
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

import httpx

from kiln.cache import cache_read, cache_write
from kiln.profile import BOUNDARY_EXTENSION_URL, GEOJSON_CONTENT_TYPE
from kiln.report import Report
from kiln.shape import as_list

PAGE_SIZE = 1000
TIMEOUT = httpx.Timeout(60.0)

# resolve_boundary_urls: concurrency and retry defaults, and the CLI flags
# that override them.
DEFAULT_CONCURRENCY = 8
DEFAULT_RETRIES = 3
# Circuit breaker: abort if this many *consecutive* boundary fetches fail
# with zero successes anywhere in the run so far. 0 disables the breaker.
# See resolve_boundary_urls' docstring and BoundaryFetchAborted.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 50

# Exponential backoff between retry attempts: 0.5s, 1s, 2s, 4s, ...
_RETRY_BASE_DELAY = 0.5
# Cap on any single sleep -- including a server-supplied Retry-After -- so a
# pathological server (a five-digit Retry-After, or just enough attempts)
# cannot stall a run indefinitely.
_RETRY_MAX_DELAY = 30.0

# Below this many url-referenced boundaries, progress output is just noise --
# a handful resolve near-instantly. Above it, print periodic progress to
# stderr so a large run isn't silent for the whole time it's fetching.
_PROGRESS_MIN_ITEMS = 50

# The classic first line of a pretty-printed JSON object or array
# (`json.dumps(..., indent=2)`): nothing else on the line. Used by
# read_ndjson to detect a multi-line document up front, cheaply, without
# reading the rest of the file.
_PRETTY_PRINTED_JSON_MARKERS = ("{", "[")


class MalformedNdjsonError(ValueError):
    """Raised when the input file is not NDJSON at all (e.g. pretty-printed JSON).

    This is a format/code problem, not a per-row data-quality issue: unlike
    a single malformed line (reported and skipped), a pretty-printed
    multi-line document breaks *every* line, so read_ndjson would otherwise
    yield zero resources and a pile of `malformed_field` issues -- an empty,
    silently "successful" run that (combined with `write_dataset` owning
    `out/locations/`) would replace a user's previous good export with
    nothing, for an input kiln simply cannot read.
    """


class BoundaryFetchAborted(RuntimeError):
    """Raised by resolve_boundary_urls' circuit breaker: systematic failure,
    not a scattering of individually bad boundary URLs.

    `resolve_boundary_urls` otherwise never raises -- a single unreachable
    boundary is a data problem, reported and skipped. This is different: it
    only fires when *zero* boundaries have succeeded anywhere in the run and
    a long consecutive run of failures has piled up, which is the signature
    of an environment problem (server unreachable, wrong token, bad base
    URL) rather than a few dead URLs in an otherwise-healthy registry. That
    is deliberately an abort, not a report: continuing would just retry the
    same broken connection thousands more times for no benefit (see
    `--max-consecutive-failures`, 0 to disable).
    """


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/fhir+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_locations(
    server: str,
    token: str | None,
    since: str | None = None,
    client: httpx.Client | None = None,
) -> Iterator[dict]:
    """Yield every Location resource, following Bundle.link[next].

    Closes the client when the generator is exhausted or garbage-collected.
    To avoid resource leaks, either exhaust the generator or pass your own
    client and manage its lifecycle.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    headers = _headers(token)

    params = {"_count": str(PAGE_SIZE)}
    if since:
        params["_lastUpdated"] = f"gt{since}"
    url = f"{server.rstrip('/')}/Location"
    visited_urls = set()
    max_pages = 10000

    try:
        page_count = 0
        while url:
            if url in visited_urls:
                raise RuntimeError(
                    f"Cyclic pagination detected: server returned "
                    f"duplicate next link: {url}"
                )
            if page_count >= max_pages:
                raise RuntimeError(
                    f"Exceeded maximum pagination limit ({max_pages} pages). "
                    f"Server may have misconfigured pagination."
                )
            visited_urls.add(url)
            page_count += 1

            response = client.get(url, params=params, headers=headers)
            if response.status_code != 200:
                raise RuntimeError(
                    f"FHIR request failed: {response.status_code} {response.text}"
                )
            payload = response.json()
            if not isinstance(payload, dict):
                # A malformed page breaks pagination for every subsequent
                # page too: this is a server/protocol fault, not a single
                # bad record, so it aborts (like the sibling RuntimeErrors
                # in this function) rather than being reported.
                raise RuntimeError(  # noqa: TRY004 protocol fault, not a caller type error
                    f"FHIR response was not a JSON object: {url}"
                )

            for entry in as_list(payload.get("entry")):
                if isinstance(entry, dict) and entry.get("resource"):
                    yield entry["resource"]

            url, params = _next_page_url(payload), None
    finally:
        if owns_client:
            client.close()


def _next_page_url(payload: dict) -> str | None:
    """Pull `Bundle.link[relation=next].url`, tolerating a malformed link."""
    for link in as_list(payload.get("link")):
        if not isinstance(link, dict) or link.get("relation") != "next":
            continue
        url = link.get("url")
        if isinstance(url, str) and url:
            return url
    return None


class _BoundaryFetch(NamedTuple):
    """One url-referenced attachment queued for fetching.

    `attachment` is the dict to mutate in place on success -- distinct per
    item, so it can be written to from a worker thread with no risk of two
    threads touching the same dict.
    """

    attachment: dict
    location_id: str
    url: str


def resolve_boundary_urls(
    resources: list[dict],
    report: Report,
    token: str | None = None,
    client: httpx.Client | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    retries: int = DEFAULT_RETRIES,
    cache_dir: Path | str | None = None,
    refresh: bool = False,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
) -> None:
    """Replace url-referenced boundary attachments with inline base64 data.

    Mutates `resources` in place. Per-boundary failures are reported, never
    raised: one unreachable boundary must not lose the whole export. The
    one exception is the circuit breaker (`max_consecutive_failures`,
    `BoundaryFetchAborted`) below, which is deliberately an abort -- it
    only trips on a systematic, environment-level failure, not a data
    problem, so it does not weaken that guarantee for ordinary bad data.

    The tree walk (finding which attachments need fetching) runs
    single-threaded and reports its own shape problems (a non-dict
    resource/extension/attachment) immediately, in resource order, exactly
    as before. The fetches themselves run on up to `concurrency` worker
    threads sharing one `httpx.Client` (thread-safe by design), each with up
    to `retries` attempts and exponential backoff. Results are collected
    back on this thread and applied to `report` in submission order, so two
    runs over the same input produce identical `report.issues` regardless of
    which thread happened to finish first.

    `cache_dir` turns on the on-disk boundary cache (see `kiln.cache`);
    `None` (the default) means no cache at all, matching the behavior
    before this parameter existed -- existing callers that don't pass it
    keep making a network request for every url-referenced boundary, every
    time. Passing a directory checks it for a cached copy of each boundary
    before fetching, and writes every successful fetch back into it
    (never a failure -- see `kiln.cache.cache_write`). `refresh=True` skips
    the read (always re-fetches over the network) but still writes the
    result back, refreshing the entry. Cache problems (an unwritable
    directory, a corrupt entry) never raise or abort the run; they're
    reported as `cache_error` and treated as a miss.

    `max_consecutive_failures` (default `DEFAULT_MAX_CONSECUTIVE_FAILURES`,
    `--max-consecutive-failures` at the CLI, 0 to disable) guards against a
    fully unreachable server or a bad token: with a large registry and the
    default 60s timeout and 3 retries, thousands of boundaries that all
    fail can take hours to grind through for zero useful output. If this
    many fetches in a row fail *and nothing has succeeded yet in this run*,
    resolution stops and raises `BoundaryFetchAborted` instead of grinding
    through the rest at the same failure rate. Requiring zero successes
    (not just a recent streak) means a registry with a genuine scattering
    of dead URLs -- a data problem, not an environment one -- can never
    trip it, no matter how the failures happen to be ordered.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    headers = _headers(token)
    resolved_cache_dir = Path(cache_dir) if cache_dir is not None else None

    try:
        work = _collect_boundary_work(resources, report)
        if work:
            _resolve_boundary_work(
                work,
                client,
                headers,
                report,
                concurrency,
                retries,
                resolved_cache_dir,
                refresh,
                max_consecutive_failures,
            )
    finally:
        if owns_client:
            client.close()


def _collect_boundary_work(resources: list[dict], report: Report) -> list[_BoundaryFetch]:
    """Walk resources -> extension -> valueAttachment, collecting fetch work.

    Single-threaded. Shape problems found along the way (a non-dict
    resource/extension/attachment) are reported here, immediately, exactly
    as they were when this walk and the fetch were interleaved.
    """
    work: list[_BoundaryFetch] = []

    for resource in resources:
        if not isinstance(resource, dict):
            report.add("boundary_fetch_failed", "<unknown>", "resource is not a dict")
            continue
        location_id = resource.get("id", "<unknown>")

        for extension in as_list(resource.get("extension")):
            if not isinstance(extension, dict):
                report.add("boundary_fetch_failed", location_id, "extension entry is not a dict")
                continue
            if extension.get("url") != BOUNDARY_EXTENSION_URL:
                continue

            value_attachment = extension.get("valueAttachment")
            if not isinstance(value_attachment, dict):
                if value_attachment is not None:
                    report.add(
                        "boundary_fetch_failed",
                        location_id,
                        "extension valueAttachment is not a dict",
                    )
                continue
            attachment = value_attachment
            url = attachment.get("url")
            if not isinstance(url, str) or not url or attachment.get("data"):
                continue

            work.append(_BoundaryFetch(attachment=attachment, location_id=location_id, url=url))

    return work


class _FetchOutcome(NamedTuple):
    """What happened resolving one `_BoundaryFetch`, reported back to the
    main thread so `report` (and the cache-summary counters) are only ever
    touched there -- see `resolve_boundary_urls`."""

    cached: bool
    failure: tuple[str, str, str] | None
    cache_errors: tuple[tuple[str, str, str], ...]


def _resolve_boundary_work(
    work: list[_BoundaryFetch],
    client: httpx.Client,
    headers: dict[str, str],
    report: Report,
    concurrency: int,
    retries: int,
    cache_dir: Path | None,
    refresh: bool,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
) -> None:
    """Fetch every queued attachment concurrently and apply reports in order."""
    max_workers = max(1, concurrency)
    total = len(work)
    progress = _ProgressReporter(total) if total >= _PROGRESS_MIN_ITEMS else None
    breaker_enabled = max_consecutive_failures > 0

    resolved = 0
    failed = 0
    cached = 0
    fetched = 0
    consecutive_failures = 0
    had_any_success = False
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submitting all of them up front (rather than as_completed) is what
        # makes the report order deterministic: futures are resolved below
        # in submission order, not completion order, regardless of which
        # worker thread finishes first.
        futures = [
            executor.submit(_resolve_one, item, client, headers, retries, cache_dir, refresh)
            for item in work
        ]
        for future in futures:
            resolved += 1
            # future.result() re-raises whatever escaped _resolve_one on its
            # worker thread. Nothing catches that here -- so it propagates
            # straight out of resolve_boundary_urls, destroying the export,
            # exactly as a negative Retry-After (ValueError from time.sleep)
            # or a malformed boundary URL (httpx.InvalidURL) used to before
            # both were fixed at the source. _resolve_one's job is to turn
            # every failure mode it can anticipate into a `_FetchOutcome`
            # instead of letting it raise; if a future change adds a new way
            # for it to fail, it must do the same, or it will be invisible
            # right here, same as those two were.
            outcome = future.result()
            if outcome.cached:
                cached += 1
                had_any_success = True
                consecutive_failures = 0
            elif outcome.failure is not None:
                failed += 1
                consecutive_failures += 1
            else:
                fetched += 1
                had_any_success = True
                consecutive_failures = 0
            if outcome.failure is not None:
                report.add(*outcome.failure)
            for cache_error in outcome.cache_errors:
                report.add(*cache_error)
            if progress:
                progress.maybe_emit(resolved, failed)

            if (
                breaker_enabled
                and not had_any_success
                and consecutive_failures >= max_consecutive_failures
            ):
                # Systematic failure, not a data problem: cancel whatever
                # hasn't started yet so the thousands of remaining items
                # don't all still run out their own retries/timeouts before
                # this can return. Already-running attempts still finish
                # (a thread mid-`client.get()` can't be interrupted), so the
                # actual wall-clock cost of aborting is bounded by roughly
                # one worker's worth of attempts, not the whole queue.
                executor.shutdown(wait=False, cancel_futures=True)
                last_detail = outcome.failure[2] if outcome.failure else "unknown"
                raise BoundaryFetchAborted(
                    f"{consecutive_failures} consecutive boundary fetches failed with "
                    "zero successes -- this looks like a systematic problem (server "
                    "unreachable, wrong token, or a bad base URL), not a handful of "
                    f"bad boundary URLs. Aborting instead of grinding through the "
                    f"remaining {total - resolved} at the same failure rate. Last "
                    f"failure: {last_detail}. Pass --max-consecutive-failures 0 to "
                    "disable this check."
                )

    if progress:
        progress.emit_final(resolved, failed)

    # Printed unconditionally (unlike the periodic progress above, which is
    # gated behind _PROGRESS_MIN_ITEMS): a user needs to be able to tell a
    # run that was fast because the cache was warm from one that was fast
    # because there was nothing to do, and that distinction matters just as
    # much on a small run as a huge one.
    print(f"boundaries: {cached} cached, {fetched} fetched, {failed} failed", file=sys.stderr)


def _apply_boundary_payload(item: _BoundaryFetch, payload: bytes) -> None:
    """Inline `payload` into `item.attachment` as base64 `data`.

    Safe to call from a worker thread: no other thread ever touches this
    particular attachment dict.
    """
    item.attachment["data"] = base64.b64encode(payload).decode()
    item.attachment.pop("url", None)
    item.attachment.setdefault("contentType", GEOJSON_CONTENT_TYPE)


def _resolve_one(
    item: _BoundaryFetch,
    client: httpx.Client,
    headers: dict[str, str],
    retries: int,
    cache_dir: Path | None,
    refresh: bool,
) -> _FetchOutcome:
    """Fetch one attachment; runs on a worker thread.

    Checks the cache first (unless `cache_dir` is None or `refresh` forces
    a re-fetch); on a hit, `item.attachment` is filled in from the cached
    bytes with no network call. On a miss -- or when caching is off --
    fetches over the network as before, then writes the result back to the
    cache on success only: a failed fetch is never cached, since a 404
    today may be a working URL tomorrow, and caching failures would make a
    transient outage permanent.

    Cache problems (a corrupt entry, an unwritable directory) never raise:
    `kiln.cache` returns an error string instead, which comes back here as
    an extra `cache_error` report tuple alongside whatever this fetch's own
    outcome is, and resolution proceeds exactly as if the cache entry
    hadn't been there.
    """
    cache_errors: list[tuple[str, str, str]] = []

    if cache_dir is not None and not refresh:
        payload, error = cache_read(cache_dir, item.url)
        if error is not None:
            cache_errors.append(("cache_error", item.location_id, error))
        if payload is not None:
            _apply_boundary_payload(item, payload)
            return _FetchOutcome(cached=True, failure=None, cache_errors=tuple(cache_errors))

    payload, detail = _fetch_boundary_with_retry(item.url, client, headers, retries)
    if payload is None:
        return _FetchOutcome(
            cached=False,
            failure=("boundary_fetch_failed", item.location_id, detail),
            cache_errors=tuple(cache_errors),
        )

    _apply_boundary_payload(item, payload)

    if cache_dir is not None:
        write_error = cache_write(cache_dir, item.url, payload)
        if write_error is not None:
            cache_errors.append(("cache_error", item.location_id, write_error))

    return _FetchOutcome(cached=False, failure=None, cache_errors=tuple(cache_errors))


def _fetch_boundary_with_retry(
    url: str,
    client: httpx.Client,
    headers: dict[str, str],
    retries: int,
) -> tuple[bytes | None, str | None]:
    """Fetch `url`, retrying connection errors, 5xx and 429 with backoff.

    A 404 (or any other 4xx) is not retried: it will still be missing on
    the next attempt, and retrying it only multiplies the wait. Content-
    level failures (an unparseable Binary, bad base64) are likewise not
    retried -- the bytes came back fine, they just weren't usable.

    A `Retry-After` header is honoured on *any* retryable status (429 or
    5xx), not just 429 -- a 503 with `Retry-After` is just as real a signal
    from the server as a 429 with one, and this is what the README already
    documents. Only the numeric-seconds form is parsed; the HTTP-date form
    (e.g. `Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`) is not recognized
    and falls back to exponential backoff instead (see
    `_retry_after_seconds`).
    """
    attempts = max(1, retries)
    detail: str | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url, headers=headers)
        # httpx.InvalidURL (e.g. a malformed port in the URL) is a plain
        # Exception subclass, not an httpx.HTTPError, so it slipped past
        # this catch before -- as would a stray ValueError/OSError from a
        # misbehaving transport. Any of these, left uncaught, propagates out
        # of _resolve_one and destroys the whole export (see the comment at
        # the future.result() call site in _resolve_boundary_work) for what
        # should be one reported, skippable boundary failure.
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, OSError) as exc:
            detail = f"{url}: {exc}"
            if attempt < attempts:
                time.sleep(_backoff_delay(attempt))
                continue
            return None, detail

        if response.status_code == 429 or response.status_code >= 500:
            detail = f"{url}: HTTP {response.status_code}"
            if attempt < attempts:
                retry_after = _retry_after_seconds(response)
                time.sleep(_backoff_delay(attempt, retry_after=retry_after))
                continue
            return None, detail

        if response.status_code != 200:
            # Any other 4xx: not retryable.
            return None, f"{url}: HTTP {response.status_code}"

        return _extract_boundary_payload(response, url)

    return None, detail


def _extract_boundary_payload(
    response: httpx.Response, url: str
) -> tuple[bytes | None, str | None]:
    """Pull the boundary bytes out of a 200 response.

    The URL may point at a Binary resource rather than raw GeoJSON.
    """
    try:
        parsed = response.json()
    except ValueError:
        return response.content, None

    if isinstance(parsed, dict) and parsed.get("resourceType") == "Binary":
        data = parsed.get("data")
        if not data:
            return None, f"{url}: Binary has no data"
        try:
            return base64.b64decode(data, validate=True), None
        except (binascii.Error, ValueError, TypeError) as exc:
            return None, f"{url}: Binary data is not valid base64: {exc}"

    return response.content, None


def _backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    """Delay before the next attempt, clamped to `[0, _RETRY_MAX_DELAY]`.

    The upper bound guards against a pathological server (or a huge
    Retry-After, e.g. `Retry-After: 99999`) stalling a run indefinitely. The
    lower bound guards against the opposite: a server sending a *negative*
    Retry-After (`Retry-After: -5`) would otherwise reach `time.sleep()`
    with a negative argument, which raises `ValueError` -- escaping this
    function, the worker, and (before the fix now catching it too)
    `resolve_boundary_urls` itself, turning one hostile response into a
    dead run instead of one reported boundary failure.
    """
    delay = retry_after if retry_after is not None else _RETRY_BASE_DELAY * (2 ** (attempt - 1))
    return max(0.0, min(delay, _RETRY_MAX_DELAY))


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a numeric `Retry-After` header, in seconds. None if absent or
    not a plain number (the HTTP-date form falls back to exponential backoff
    rather than being parsed here)."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class _ProgressReporter:
    """Periodic `resolved N/total (F failed)` lines to stderr for large runs."""

    def __init__(self, total: int) -> None:
        self.total = total
        # ~20 updates across the run, regardless of size.
        self.interval = max(1, total // 20)

    def maybe_emit(self, resolved: int, failed: int) -> None:
        if resolved % self.interval == 0:
            self._emit(resolved, failed)

    def emit_final(self, resolved: int, failed: int) -> None:
        if resolved % self.interval != 0:
            self._emit(resolved, failed)

    def _emit(self, resolved: int, failed: int) -> None:
        print(f"resolved {resolved}/{self.total} boundaries ({failed} failed)", file=sys.stderr)


def write_ndjson(resources: Iterable[dict], path: Path) -> int:
    """Write resources one-per-line. Returns the number written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for resource in resources:
            handle.write(json.dumps(resource, separators=(",", ":")) + "\n")
            count += 1
    return count


def read_ndjson(path: Path, report: Report | None = None) -> Iterator[dict]:
    """Read NDJSON, or a single-line FHIR Bundle .json, as a stream of resources.

    Reads the file line by line rather than loading it whole: a boundary
    attachment inlines several KB-MB of base64 GeoJSON per line, so a
    file of a million Locations can be gigabytes on disk, and the previous
    `read_text()` + `splitlines()` implementation held two full in-memory
    copies of that before parsing a single row.

    The first non-blank line is checked for the Bundle case: if it parses
    on its own as a complete JSON document and is a Bundle, its
    `entry[].resource` list is exploded and the rest of the file is not
    read at all. (This only recognizes a Bundle written on one line, true
    of every Bundle `kiln extract` or `write_ndjson` itself produces --
    not one pretty-printed across several lines, which is no longer
    supported now that the file is read as a stream rather than parsed
    whole up front.) Otherwise that line, and every line after it, are
    parsed as one resource each.

    The NDJSON boundary is the documented hand-off point with third-party
    export tools, so unvalidated input is expected here: a line that isn't
    valid JSON is reported to `report` (kind `malformed_field`, with the
    1-based line number) and skipped rather than aborting the whole
    transform on one bad line. `report` is optional so existing callers
    that don't need the detail keep working.

    A pretty-printed, multi-line JSON document (e.g. a formatted FHIR
    Bundle from `json.dumps(..., indent=2)`) is a different failure mode
    entirely: every line fails to parse on its own, so nothing above would
    catch it as a single bad line -- the whole file would be reported as a
    pile of `malformed_field` issues and yield zero resources, exiting 0 as
    if the run had simply resolved no data. That is loud enough to notice
    in isolation, but not in the normal nightly-refresh flow, where
    `write_dataset` owns `out/locations/` and treats "zero rows resolved"
    as a legitimate, intentional result to replace the previous dataset
    with. So this case is detected explicitly and raises `MalformedNdjsonError`
    instead of being folded into the per-line reporting path.
    """
    path = Path(path)

    with path.open("r", encoding="utf-8") as handle:
        first_resource_seen = False
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue

            if not first_resource_seen and line in _PRETTY_PRINTED_JSON_MARKERS:
                raise MalformedNdjsonError(
                    f"{path}: line {line_number} is just {line!r} -- this looks "
                    "like a pretty-printed, multi-line JSON document, not NDJSON "
                    "or a single-line FHIR Bundle. read_ndjson reads one JSON "
                    "value per line and cannot parse a document split across "
                    "lines this way. Re-export as newline-delimited JSON, or "
                    "compact it first, e.g.: `jq -c . input.json > input.ndjson`."
                )

            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                if report is not None:
                    report.add("malformed_field", "<unknown>", f"line {line_number}: {exc}")
                continue

            if not first_resource_seen:
                first_resource_seen = True
                if isinstance(payload, dict) and payload.get("resourceType") == "Bundle":
                    for entry in as_list(payload.get("entry")):
                        if isinstance(entry, dict) and entry.get("resource"):
                            yield entry["resource"]
                    return

            yield payload
