"""Collection and serialization of data-quality issues found during a run."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field


@dataclass
class Issue:
    """A single data-quality problem, attributable to one Location."""

    kind: str
    location_id: str
    detail: str


@dataclass
class Report:
    """Accumulates issues across a run. Never raises; callers keep going."""

    issues: list[Issue] = field(default_factory=list)

    def add(self, kind: str, location_id: str, detail: str) -> None:
        self.issues.append(Issue(kind=kind, location_id=location_id, detail=detail))

    def counts(self) -> dict[str, int]:
        return dict(Counter(issue.kind for issue in self.issues))

    def to_dict(self) -> dict:
        return {
            "counts": self.counts(),
            "issues": [asdict(issue) for issue in self.issues],
        }

    def summary(self) -> str:
        counts = self.counts()
        if not counts:
            return "No issues found."
        lines = [f"  {kind}: {count}" for kind, count in sorted(counts.items())]
        return "Issues found:\n" + "\n".join(lines)


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
        if row.lon is None or row.ancestor_ids is None:
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
