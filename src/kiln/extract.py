"""All network work: paged FHIR search and boundary URL resolution.

This is the only module that touches the network. `transform` is offline,
which is what makes the geo half testable from fixtures.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Iterator
from pathlib import Path

import httpx

from kiln.profile import BOUNDARY_EXTENSION_URL, GEOJSON_CONTENT_TYPE
from kiln.report import Report
from kiln.shape import as_list

PAGE_SIZE = 1000
TIMEOUT = httpx.Timeout(60.0)


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


def resolve_boundary_urls(
    resources: list[dict],
    report: Report,
    token: str | None = None,
    client: httpx.Client | None = None,
) -> None:
    """Replace url-referenced boundary attachments with inline base64 data.

    Mutates `resources` in place. Failures are reported, never raised: one
    unreachable boundary must not lose the whole export.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    headers = _headers(token)

    try:
        for resource in resources:
            if not isinstance(resource, dict):
                report.add("boundary_fetch_failed", "<unknown>", "resource is not a dict")
                continue
            location_id = resource.get("id", "<unknown>")

            for extension in as_list(resource.get("extension")):
                if not isinstance(extension, dict):
                    report.add(
                        "boundary_fetch_failed", location_id, "extension entry is not a dict"
                    )
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

                payload = _fetch_boundary(url, client, headers, resource, report)
                if payload is None:
                    continue

                attachment["data"] = base64.b64encode(payload).decode()
                attachment.pop("url", None)
                attachment.setdefault("contentType", GEOJSON_CONTENT_TYPE)
    finally:
        if owns_client:
            client.close()


def _fetch_boundary(
    url: str,
    client: httpx.Client,
    headers: dict[str, str],
    resource: dict,
    report: Report,
) -> bytes | None:
    location_id = resource.get("id", "<unknown>")
    try:
        response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        report.add("boundary_fetch_failed", location_id, f"{url}: {exc}")
        return None

    if response.status_code != 200:
        report.add(
            "boundary_fetch_failed",
            location_id,
            f"{url}: HTTP {response.status_code}",
        )
        return None

    # The URL may point at a Binary resource rather than raw GeoJSON.
    try:
        parsed = response.json()
    except ValueError:
        return response.content

    if isinstance(parsed, dict) and parsed.get("resourceType") == "Binary":
        data = parsed.get("data")
        if not data:
            report.add("boundary_fetch_failed", location_id, f"{url}: Binary has no data")
            return None
        try:
            return base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            report.add(
                "boundary_fetch_failed",
                location_id,
                f"{url}: Binary data is not valid base64: {exc}",
            )
            return None

    return response.content


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
    """
    path = Path(path)

    with path.open("r", encoding="utf-8") as handle:
        first_resource_seen = False
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue

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
