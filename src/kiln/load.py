"""Network: NDJSON Location resources -> a FHIR store, as idempotent PUTs.

The import mirror of extract.py, and with it one of the only two modules
that touch the network. Spec: docs/superpowers/specs/2026-08-05-admin-import-design.md.
"""

from __future__ import annotations

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
