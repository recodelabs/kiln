"""Resolve Location.partOf chains into levels, paths and ancestor columns.

A plain dict walk rather than a recursive SQL CTE: at single-country scale
it is faster to write, easier to test, and it can report *which* id dangles
and *what* the cycle path was.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kiln.profile import ADMIN_UNIT_TYPE, RawLocation
from kiln.report import Report

MAX_DEPTH = 12
ADMIN_COLUMNS = 5


@dataclass
class HierarchyInfo:
    """Everything derivable from one node's position in the tree."""

    depth: int
    admin_level: int | None
    path: str
    ancestor_ids: list[str] = field(default_factory=list)
    admin_names: list[str | None] = field(default_factory=list)
    admin_codes: list[str | None] = field(default_factory=list)
    country: str | None = None


class _CycleError(Exception):
    def __init__(self, path: list[str], cycle_node: str) -> None:
        super().__init__(" -> ".join(path))
        self.path = path
        self.cycle_node = cycle_node


class _TooDeepError(Exception):
    def __init__(self, path: list[str]) -> None:
        super().__init__(" -> ".join(path))
        self.path = path


def _chain(
    location_id: str,
    by_id: dict[str, RawLocation],
    cache: dict[str, list[str]],
) -> list[str]:
    """Ids from root to `location_id` inclusive."""
    if location_id in cache:
        return cache[location_id]

    walked: list[str] = []
    seen: set[str] = set()
    current = location_id

    while True:
        if current in seen:
            raise _CycleError([*walked, current], current)
        seen.add(current)
        walked.append(current)

        if len(walked) > MAX_DEPTH:
            raise _TooDeepError(walked)

        parent_id = by_id[current].parent_id
        if parent_id is None:
            break
        if parent_id not in by_id:
            # Dangling parent; treat as root (already reported in pre-walk)
            break
        current = parent_id

    walked.reverse()
    cache[location_id] = walked
    return walked


def resolve_hierarchy(
    locations: list[RawLocation], report: Report
) -> dict[str, HierarchyInfo]:
    """Build HierarchyInfo for every resolvable Location.

    Nodes in a cycle, or deeper than MAX_DEPTH, are reported and omitted.
    """
    by_id = {location.id: location for location in locations}
    resolved: dict[str, HierarchyInfo] = {}
    chain_cache: dict[str, list[str]] = {}

    # Pre-walk: Report all dangling parents exactly once
    for location in locations:
        if location.parent_id and location.parent_id not in by_id:
            report.add(
                "orphan",
                location.id,
                f"partOf references missing id {location.parent_id}",
            )

    for location in locations:
        try:
            chain = _chain(location.id, by_id, chain_cache)
        except _CycleError as exc:
            if exc.cycle_node == location.id:
                report.add("cycle", location.id, " -> ".join(exc.path))
            else:
                report.add(
                    "unreachable_ancestor",
                    location.id,
                    f"cycle involving {exc.cycle_node}",
                )
            continue
        except _TooDeepError as exc:
            # Unlike cycles, depth is a property of the node itself. In a
            # chain n0..n19 with MAX_DEPTH=12, every node from n12 onward
            # has an over-long chain from itself to the root — each is
            # independently too deep. There are no collateral victims
            # distinguished from guilty parties, so no unreachable_ancestor
            # distinction is needed.
            report.add(
                "too_deep",
                location.id,
                f"chain exceeds MAX_DEPTH={MAX_DEPTH}: " + " -> ".join(exc.path),
            )
            continue

        admin_chain = [
            by_id[node_id]
            for node_id in chain
            if by_id[node_id].loc_type == ADMIN_UNIT_TYPE
        ]

        admin_level = None
        if location.loc_type == ADMIN_UNIT_TYPE:
            admin_level = len(admin_chain) - 1

        names: list[str | None] = [None] * ADMIN_COLUMNS
        codes: list[str | None] = [None] * ADMIN_COLUMNS
        for index, ancestor in enumerate(admin_chain[:ADMIN_COLUMNS]):
            names[index] = ancestor.name
            codes[index] = ancestor.pcode

        resolved[location.id] = HierarchyInfo(
            depth=len(chain) - 1,
            admin_level=admin_level,
            path="/" + "/".join(chain),
            ancestor_ids=chain[:-1],
            admin_names=names,
            admin_codes=codes,
            country=codes[0],
        )

    return resolved
