"""All network work: paged FHIR search and boundary URL resolution.

This is the only module that touches the network. `transform` is offline,
which is what makes the geo half testable from fixtures.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Iterator
from pathlib import Path

import httpx

from kiln.profile import BOUNDARY_EXTENSION_URL, GEOJSON_CONTENT_TYPE
from kiln.report import Report

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
    """Yield every Location resource, following Bundle.link[next]."""
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    headers = _headers(token)

    params = {"_count": str(PAGE_SIZE)}
    if since:
        params["_lastUpdated"] = f"gt{since}"
    url = f"{server.rstrip('/')}/Location"

    try:
        while url:
            response = client.get(url, params=params, headers=headers)
            if response.status_code != 200:
                raise RuntimeError(
                    f"FHIR request failed: {response.status_code} {response.text}"
                )
            payload = response.json()
            for entry in payload.get("entry") or []:
                if entry.get("resource"):
                    yield entry["resource"]

            next_link = next(
                (
                    link["url"]
                    for link in payload.get("link") or []
                    if link.get("relation") == "next"
                ),
                None,
            )
            url, params = next_link, None
    finally:
        if owns_client:
            client.close()


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
            for extension in resource.get("extension") or []:
                if extension.get("url") != BOUNDARY_EXTENSION_URL:
                    continue
                attachment = extension.get("valueAttachment") or {}
                url = attachment.get("url")
                if not url or attachment.get("data"):
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
        return base64.b64decode(data)

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


def read_ndjson(path: Path) -> Iterator[dict]:
    """Read NDJSON, or a FHIR Bundle .json, as a stream of resources.

    Attempts to parse the file as JSON first. If it's a dict with
    resourceType == "Bundle", explodes entry[].resource. Otherwise falls
    back to line-by-line NDJSON parsing.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    # Try to parse the entire file as JSON first
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None

    # If it's a Bundle, explode the entries
    if isinstance(payload, dict) and payload.get("resourceType") == "Bundle":
        for entry in payload.get("entry") or []:
            if entry.get("resource"):
                yield entry["resource"]
        return

    # Otherwise, parse as NDJSON line-by-line
    for line in text.splitlines():
        line = line.strip()
        if line:
            yield json.loads(line)
