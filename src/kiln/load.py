"""Network: NDJSON Location resources -> a FHIR store, as idempotent PUTs.

The import mirror of extract.py, and with it one of the only two modules
that touch the network. Spec: docs/superpowers/specs/2026-08-05-admin-import-design.md.
"""

from __future__ import annotations

import sys
import time

import httpx

from kiln.extract import (
    DEFAULT_RETRIES,
    TIMEOUT,
    _backoff_delay,
    _headers,
    _retry_after_seconds,
)

DEFAULT_BATCH_SIZE = 100


class LoadError(RuntimeError):
    """Systematic load failure (bad store config, cycle, failed bundle).

    load never skips individual resources: a bundle-level failure is an
    environment or data-structure problem, and PUT-by-id makes re-running
    the whole load safe, so aborting loses nothing.
    """


def order_parents_first(resources: list[dict]) -> list[dict]:
    """Sort so every resource comes after its partOf parent (when loaded too).

    Depth is the length of the partOf chain *within this set*: references
    to resources not being loaded (already in the store) count as roots.
    The sort is stable, so siblings keep their input order.
    """
    by_id = {r.get("id"): r for r in resources if r.get("id")}
    depths: dict[str, int] = {}

    def depth(location_id: str) -> int:
        if location_id in depths:
            return depths[location_id]
        chain: list[str] = []
        current: str | None = location_id
        while (
            current is not None
            and current in by_id
            and current not in depths
        ):
            if current in chain:
                raise LoadError(
                    f"partOf cycle detected involving Location/{current} -- "
                    "fix the hierarchy before loading"
                )
            chain.append(current)
            part_of = by_id[current].get("partOf") or {}
            reference = part_of.get("reference") or ""
            parent = reference.rsplit("/", 1)[-1] or None
            current = parent if parent in by_id else None
        base = depths.get(current, -1) if current else -1
        for offset, chain_id in enumerate(reversed(chain), start=1):
            depths[chain_id] = base + offset
        return depths[location_id]

    return sorted(
        resources, key=lambda r: depth(r["id"]) if r.get("id") else 0
    )


def build_bundles(
    resources: list[dict], batch_size: int = DEFAULT_BATCH_SIZE
) -> list[dict]:
    """Chunk resources (parents first) into transaction Bundles of PUT entries."""
    ordered = order_parents_first(resources)
    bundles = []
    for start in range(0, len(ordered), batch_size):
        chunk = ordered[start : start + batch_size]
        bundles.append(
            {
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": [
                    {
                        "resource": resource,
                        "request": {
                            "method": "PUT",
                            "url": f"Location/{resource['id']}",
                        },
                    }
                    for resource in chunk
                ],
            }
        )
    return bundles


def check_update_create(server: str, token: str | None, client: httpx.Client) -> None:
    """Verify the store does update-as-create for Location, or refuse to load.

    Without it, `PUT Location/<new-id>` 404s (or 400s) on every resource:
    better one clear preflight error naming the store setting than 345
    identical failures.
    """
    url = f"{server.rstrip('/')}/metadata"
    response = client.get(url, headers=_headers(token))
    if response.status_code != 200:
        raise LoadError(
            f"capability preflight failed: {response.status_code} for {url} -- "
            "check the server URL and token"
        )
    try:
        capability = response.json()
    except ValueError as exc:
        raise LoadError(
            f"capability preflight: {url} returned non-JSON: {exc}"
        ) from exc

    for rest in capability.get("rest", []) or []:
        for resource in (rest or {}).get("resource", []) or []:
            if isinstance(resource, dict) and resource.get("type") == "Location":
                if resource.get("updateCreate") is True:
                    return
    raise LoadError(
        "this FHIR store does not advertise update-as-create for Location "
        "(CapabilityStatement rest.resource.updateCreate). kiln load PUTs "
        "resources by id, which needs it -- on Google Healthcare API, set "
        "enableUpdateCreate=true on the FHIR store."
    )


def load(
    resources: list[dict],
    server: str,
    token: str | None,
    client: httpx.Client | None = None,
    retries: int = DEFAULT_RETRIES,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Upsert every resource via transaction bundles. Returns the count.

    All-or-abort: a bundle that still fails after retries raises LoadError
    with the server's OperationOutcome text. Re-running is always safe --
    PUT by id upserts, so already-loaded bundles just write the same bytes
    again.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    base_url = server.rstrip("/")
    headers = _headers(token) | {"Content-Type": "application/fhir+json"}

    try:
        check_update_create(server, token, client)
        bundles = build_bundles(resources, batch_size=batch_size)
        count = 0
        for index, bundle in enumerate(bundles, start=1):
            _post_bundle_with_retry(client, base_url, headers, bundle, index, retries)
            count += len(bundle["entry"])
            print(f"bundle {index}/{len(bundles)} committed", file=sys.stderr)
        return count
    finally:
        if owns_client:
            client.close()


def _post_bundle_with_retry(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    bundle: dict,
    index: int,
    retries: int,
) -> None:
    attempts = max(1, retries)
    detail = "no attempts made"
    for attempt in range(1, attempts + 1):
        try:
            response = client.post(base_url, json=bundle, headers=headers)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, OSError) as exc:
            detail = str(exc)
            if attempt < attempts:
                time.sleep(_backoff_delay(attempt))
                continue
            break

        if response.status_code == 429 or response.status_code >= 500:
            detail = f"HTTP {response.status_code}"
            if attempt < attempts:
                time.sleep(_backoff_delay(attempt, _retry_after_seconds(response)))
                continue
            break

        if 200 <= response.status_code < 300:
            return

        # Non-retryable 4xx: surface the OperationOutcome and abort.
        raise LoadError(
            f"bundle {index} rejected with HTTP {response.status_code}: {response.text}"
        )

    raise LoadError(f"bundle {index} failed after {attempts} attempts: {detail}")
