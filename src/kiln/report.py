"""Collection and serialization of data-quality issues found during a run."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

import pandas as pd

# A systematic fault (every row using the (0, 0) default coordinate, a
# swapped lat/lon, ...) can raise one issue per row: at 100k rows a single
# kind has been observed to produce a report larger than the dataset it
# describes. Retaining only this many per kind keeps `_report.json` bounded
# regardless of dataset size, while `counts()` (see below) stays exact so
# the number users act on is never affected by the cap.
MAX_RETAINED_ISSUES_PER_KIND = 1000


@dataclass
class Issue:
    """A single data-quality problem, attributable to one Location."""

    kind: str
    location_id: str
    detail: str


@dataclass
class Report:
    """Accumulates issues across a run. Never raises; callers keep going.

    `issues` retains at most `MAX_RETAINED_ISSUES_PER_KIND` entries per
    kind, oldest first. `counts()` is tracked independently of the retained
    list and is always exact, no matter how much has been capped -- it is
    the number users act on, so it must never reflect the cap.
    """

    issues: list[Issue] = field(default_factory=list)
    _counts: Counter = field(default_factory=Counter, repr=False, compare=False)
    _retained: Counter = field(default_factory=Counter, repr=False, compare=False)

    def add(self, kind: str, location_id: str, detail: str) -> None:
        self._counts[kind] += 1
        if self._retained[kind] < MAX_RETAINED_ISSUES_PER_KIND:
            self.issues.append(Issue(kind=kind, location_id=location_id, detail=detail))
            self._retained[kind] += 1

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def truncated(self) -> dict[str, int]:
        """Kinds where more issues occurred than were retained, with the omitted count."""
        return {
            kind: total - self._retained[kind]
            for kind, total in self._counts.items()
            if total > self._retained[kind]
        }

    def to_dict(self) -> dict:
        return {
            "counts": self.counts(),
            "issues": [asdict(issue) for issue in self.issues],
            "truncated": self.truncated(),
        }

    def summary(self) -> str:
        counts = self.counts()
        if not counts:
            return "No issues found."
        lines = [f"  {kind}: {count}" for kind, count in sorted(counts.items())]
        result = "Issues found:\n" + "\n".join(lines)
        truncated = self.truncated()
        if truncated:
            omitted = ", ".join(f"{kind} (+{n} more)" for kind, n in sorted(truncated.items()))
            result += (
                f"\nissues list capped at {MAX_RETAINED_ISSUES_PER_KIND} per kind "
                f"(counts above are exact); omitted from the list: {omitted}"
            )
        return result


def check_duplicate_pcodes(frame, report: Report) -> None:
    """Flag pcodes claimed by more than one Location."""
    with_pcode = frame[frame["pcode"].notna()]
    duplicated = with_pcode[with_pcode.duplicated("pcode", keep=False)]
    for pcode, group in duplicated.groupby("pcode"):
        report.add(
            "duplicate_pcode",
            ", ".join(sorted(group["id"])),
            f"pcode {pcode} claimed by {len(group)} Locations",
        )


def check_points_within_parents(frame, report: Report) -> None:
    """Flag sites whose point falls outside their nearest admin ancestor.

    In microplanning this is nearly always a real data error.
    """
    import shapely

    polygons = frame[frame["geom_type"] == "polygon"]
    if polygons.empty:
        return
    by_id = dict(zip(polygons["id"], polygons.geometry, strict=True))

    for row in frame.itertuples():
        # lon is float64: a missing value surfaces as NaN, never None.
        if pd.isna(row.lon) or row.ancestor_ids is None:
            continue
        parent_polygon = next(
            (by_id[a] for a in reversed(list(row.ancestor_ids)) if a in by_id), None
        )
        if parent_polygon is None:
            continue
        if not parent_polygon.covers(shapely.Point(row.lon, row.lat)):
            report.add(
                "point_outside_parent",
                row.id,
                f"({row.lon}, {row.lat}) falls outside its nearest admin ancestor",
            )
