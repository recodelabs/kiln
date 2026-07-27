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
