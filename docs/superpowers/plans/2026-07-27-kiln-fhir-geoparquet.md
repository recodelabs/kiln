# kiln — FHIR Locations to GeoParquet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export FHIR `Location` resources for one country to hive-partitioned GeoParquet with native Parquet geometry types, a per-row covering bbox, and Hilbert-sorted row groups.

**Architecture:** A three-command CLI (`extract`, `transform`, `run`, plus `inspect`) joined by an NDJSON seam. `extract` does all network work — paged FHIR search plus resolution of externally referenced boundary attachments — and writes raw Location resources to NDJSON. `transform` is pure and offline: it shreds FHIR JSON into flat records, walks the `partOf` hierarchy with a memoized dict walk, builds shapely geometry, assembles a GeoDataFrame, Hilbert-sorts it, groups it into partitions, and writes each through a two-pass write (geopandas stages WKB parquet, `ogr2ogr` finalizes it with native geometry types and a covering bbox).

**Tech Stack:** Python 3.11+, shapely 2.1, geopandas 1.1, pyarrow, httpx, system GDAL 3.13+ (`ogr2ogr`), pytest, ruff, uv.

## Global Constraints

- Python `>=3.11`. Target machine runs 3.14.6.
- System GDAL `>=3.13` with a Parquet driver exposing `USE_PARQUET_GEO_TYPES`. This is a hard runtime requirement, probed at startup.
- Dependencies are exactly: `shapely>=2.1`, `geopandas>=1.1`, `pyarrow>=17`, `httpx>=0.27`. Dev: `pytest>=8`, `ruff>=0.6`. **Do not add DuckDB** — the spec records why it was evaluated and dropped.
- All geometry is EPSG:4326.
- **FHIR `Location.position` is longitude-first.** `position.longitude` → x, `position.latitude` → y. Getting this backwards is silent and gets an explicit test.
- **Every `to_parquet` call must pass `index=False`.** geopandas otherwise emits a stray `__index_level_0__` column that survives into final output.
- `ogr2ogr` finalize always passes `SORT_BY_BBOX=NO` — the Hilbert order is applied upstream and must be preserved.
- Default row-group size is `20000`, not GDAL's 65536.
- Boundary extension URL is `https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson`. Identifier systems are `https://icr.healthcampaigns.org/identifiers/pcode` and `https://icr.healthcampaigns.org/identifiers/overture-gers`. All profile-specific constants live in `profile.py` and nowhere else.
- Run tests with `uv run pytest`. Lint with `uv run ruff check .`.
- One country per run. Everything fits in memory; do not build streaming or out-of-core machinery.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Deps, entry point, ruff and pytest config |
| `src/kiln/report.py` | `Issue`, `Report` — validation collection and serialization |
| `src/kiln/profile.py` | ICR profile constants; `RawLocation`, `BoundaryRef`; `shred()` |
| `src/kiln/hierarchy.py` | `HierarchyInfo`; memoized `partOf` walk, orphan and cycle detection |
| `src/kiln/geometry.py` | GeoJSON normalization, shapely geometry construction |
| `src/kiln/frame.py` | Assembles records + hierarchy + geometry into a `GeoDataFrame` |
| `src/kiln/write.py` | GDAL probe, Hilbert sort, partition loop, `ogr2ogr` finalize |
| `src/kiln/extract.py` | Paged FHIR search, boundary URL resolution, NDJSON output |
| `src/kiln/inspect.py` | Dataset summary for the `inspect` command |
| `src/kiln/cli.py` | Argument parsing, command dispatch, orchestration |

The spec's module layout listed geometry assembly inside `write.py`. This plan splits it into `frame.py` so `write.py` has one responsibility (getting bytes onto disk correctly) and frame assembly is testable without invoking GDAL.

`bake.py` from the spec is **not** created. PMTiles is a non-goal for v1 and an empty stub earns nothing.

---

### Task 1: Project scaffold and validation report

**Files:**
- Create: `pyproject.toml`
- Create: `src/kiln/__init__.py`
- Create: `src/kiln/report.py`
- Create: `.gitignore`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Issue(kind: str, location_id: str, detail: str)` dataclass. `Report` with `add(kind: str, location_id: str, detail: str) -> None`, `counts() -> dict[str, int]`, `to_dict() -> dict`, `summary() -> str`, and attribute `issues: list[Issue]`. Every later task takes a `Report` and calls `add`.

- [ ] **Step 1: Create the project scaffold**

`pyproject.toml`:

```toml
[project]
name = "kiln"
version = "0.1.0"
description = "Export FHIR Locations to partitioned GeoParquet"
requires-python = ">=3.11"
dependencies = [
    "shapely>=2.1",
    "geopandas>=1.1",
    "pyarrow>=17",
    "httpx>=0.27",
]

[project.scripts]
kiln = "kiln.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/kiln"]

[dependency-groups]
dev = ["pytest>=8", "ruff>=0.6"]

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.gitignore`:

```
__pycache__/
*.pyc
.venv/
.pytest_cache/
.ruff_cache/
dist/
out/
*.ndjson
```

`src/kiln/__init__.py`:

```python
__version__ = "0.1.0"
```

- [ ] **Step 2: Write the failing test**

`tests/test_report.py`:

```python
import json

from kiln.report import Report


def test_report_counts_issues_by_kind():
    report = Report()
    report.add("orphan", "loc-1", "partOf references missing id loc-99")
    report.add("orphan", "loc-2", "partOf references missing id loc-99")
    report.add("cycle", "loc-3", "loc-3 -> loc-4 -> loc-3")

    assert report.counts() == {"orphan": 2, "cycle": 1}


def test_report_retains_detail_for_each_issue():
    report = Report()
    report.add("orphan", "loc-1", "partOf references missing id loc-99")

    assert len(report.issues) == 1
    assert report.issues[0].kind == "orphan"
    assert report.issues[0].location_id == "loc-1"
    assert report.issues[0].detail == "partOf references missing id loc-99"


def test_report_to_dict_is_json_serializable():
    report = Report()
    report.add("cycle", "loc-3", "loc-3 -> loc-4 -> loc-3")

    payload = json.loads(json.dumps(report.to_dict()))

    assert payload["counts"] == {"cycle": 1}
    assert payload["issues"][0]["location_id"] == "loc-3"


def test_empty_report_summary_says_no_issues():
    assert Report().summary() == "No issues found."


def test_summary_lists_each_kind_with_a_count():
    report = Report()
    report.add("orphan", "loc-1", "x")
    report.add("orphan", "loc-2", "y")

    assert "orphan: 2" in Report.summary(report)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kiln.report'`

- [ ] **Step 4: Write the implementation**

`src/kiln/report.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_report.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore src/kiln/__init__.py src/kiln/report.py tests/test_report.py
git commit -m "Add project scaffold and validation report"
```

---

### Task 2: Shred FHIR Locations into flat records

**Files:**
- Create: `src/kiln/profile.py`
- Test: `tests/test_profile.py`

**Interfaces:**
- Consumes: `Report` from Task 1.
- Produces:
  - Constants `BOUNDARY_EXTENSION_URL`, `PCODE_SYSTEM`, `GERS_SYSTEM`, `OVERLAYS_EXTENSION_URL`, `SETTLEMENT_TYPE_EXTENSION_URL`, `DELIVERY_STRATEGY_EXTENSION_URL`.
  - `BoundaryRef(data: bytes | None, url: str | None)` dataclass.
  - `RawLocation` dataclass with fields: `id: str`, `name: str | None`, `status: str | None`, `loc_type: str | None`, `physical_type: str | None`, `pcode: str | None`, `gers_id: str | None`, `identifiers: list[dict]`, `parent_id: str | None`, `overlays_admin_unit_ids: list[str]`, `settlement_type: str | None`, `delivery_strategy: str | None`, `position: tuple[float, float] | None`, `boundary: BoundaryRef | None`, `last_updated: str | None`.
  - `shred(resource: dict, report: Report) -> RawLocation | None` — returns `None` and reports `missing_id` when the resource has no `id`.

- [ ] **Step 1: Write the failing test**

`tests/test_profile.py`:

```python
import base64

from kiln.profile import (
    BOUNDARY_EXTENSION_URL,
    GERS_SYSTEM,
    PCODE_SYSTEM,
    shred,
)
from kiln.report import Report

GEOJSON = b'{"type":"Polygon","coordinates":[[[3,6],[4,6],[4,7],[3,7],[3,6]]]}'


def a_location(**overrides) -> dict:
    resource = {
        "resourceType": "Location",
        "id": "loc-1",
        "name": "Nassarawa",
        "status": "active",
        "type": [{"coding": [{"code": "admin-unit"}]}],
        "physicalType": {"coding": [{"code": "jurisdiction"}]},
        "identifier": [
            {"system": PCODE_SYSTEM, "value": "NG001002"},
            {"system": GERS_SYSTEM, "value": "08f2a1b"},
            {"system": "http://example.org/other", "value": "keep-me"},
        ],
        "partOf": {"reference": "Location/loc-parent"},
        "meta": {"lastUpdated": "2026-07-01T00:00:00Z"},
    }
    resource.update(overrides)
    return resource


def test_shred_extracts_core_fields():
    raw = shred(a_location(), Report())

    assert raw.id == "loc-1"
    assert raw.name == "Nassarawa"
    assert raw.status == "active"
    assert raw.loc_type == "admin-unit"
    assert raw.physical_type == "jurisdiction"
    assert raw.last_updated == "2026-07-01T00:00:00Z"


def test_shred_promotes_pcode_and_gers_but_keeps_all_identifiers():
    raw = shred(a_location(), Report())

    assert raw.pcode == "NG001002"
    assert raw.gers_id == "08f2a1b"
    assert {i["value"] for i in raw.identifiers} == {"NG001002", "08f2a1b", "keep-me"}


def test_shred_strips_the_resource_type_prefix_from_partof():
    raw = shred(a_location(), Report())

    assert raw.parent_id == "loc-parent"


def test_shred_reads_position_longitude_first():
    resource = a_location(position={"longitude": 8.5, "latitude": 12.0})

    raw = shred(resource, Report())

    assert raw.position == (8.5, 12.0)


def test_shred_returns_no_position_when_absent():
    assert shred(a_location(), Report()).position is None


def test_shred_decodes_an_inline_base64_boundary():
    resource = a_location(
        extension=[
            {
                "url": BOUNDARY_EXTENSION_URL,
                "valueAttachment": {
                    "contentType": "application/geo+json",
                    "data": base64.b64encode(GEOJSON).decode(),
                },
            }
        ]
    )

    raw = shred(resource, Report())

    assert raw.boundary.data == GEOJSON
    assert raw.boundary.url is None


def test_shred_records_a_url_referenced_boundary_without_fetching():
    resource = a_location(
        extension=[
            {
                "url": BOUNDARY_EXTENSION_URL,
                "valueAttachment": {
                    "contentType": "application/geo+json",
                    "url": "https://example.org/Binary/abc",
                },
            }
        ]
    )

    raw = shred(resource, Report())

    assert raw.boundary.data is None
    assert raw.boundary.url == "https://example.org/Binary/abc"


def test_shred_rejects_a_boundary_with_the_wrong_content_type():
    report = Report()
    resource = a_location(
        extension=[
            {
                "url": BOUNDARY_EXTENSION_URL,
                "valueAttachment": {"contentType": "application/pdf", "data": "eyJ9"},
            }
        ]
    )

    raw = shred(resource, report)

    assert raw.boundary is None
    assert report.counts() == {"boundary_bad_content_type": 1}


def test_shred_collects_overlays_settlement_type_and_delivery_strategy():
    from kiln.profile import (
        DELIVERY_STRATEGY_EXTENSION_URL,
        OVERLAYS_EXTENSION_URL,
        SETTLEMENT_TYPE_EXTENSION_URL,
    )

    resource = a_location(
        extension=[
            {"url": OVERLAYS_EXTENSION_URL, "valueReference": {"reference": "Location/a"}},
            {"url": OVERLAYS_EXTENSION_URL, "valueReference": {"reference": "Location/b"}},
            {"url": SETTLEMENT_TYPE_EXTENSION_URL, "valueCode": "refugee-idp"},
            {"url": DELIVERY_STRATEGY_EXTENSION_URL, "valueCode": "door-to-door"},
        ]
    )

    raw = shred(resource, report=Report())

    assert raw.overlays_admin_unit_ids == ["a", "b"]
    assert raw.settlement_type == "refugee-idp"
    assert raw.delivery_strategy == "door-to-door"


def test_shred_reports_and_skips_a_resource_with_no_id():
    report = Report()
    resource = a_location()
    del resource["id"]

    assert shred(resource, report) is None
    assert report.counts() == {"missing_id": 1}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_profile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kiln.profile'`

- [ ] **Step 3: Write the implementation**

`src/kiln/profile.py`:

```python
"""ICR Location profile mapping.

Everything profile-specific lives here. Supporting a different Location
profile means editing this module and nothing else.

Profile: https://icr.healthcampaigns.org/StructureDefinition-ICRLocation.html
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field

BOUNDARY_EXTENSION_URL = (
    "https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson"
)
OVERLAYS_EXTENSION_URL = "https://icr.healthcampaigns.org/StructureDefinition/overlays-admin-unit"
SETTLEMENT_TYPE_EXTENSION_URL = (
    "https://icr.healthcampaigns.org/StructureDefinition/settlement-type"
)
DELIVERY_STRATEGY_EXTENSION_URL = (
    "https://icr.healthcampaigns.org/StructureDefinition/delivery-strategy"
)

PCODE_SYSTEM = "https://icr.healthcampaigns.org/identifiers/pcode"
GERS_SYSTEM = "https://icr.healthcampaigns.org/identifiers/overture-gers"

GEOJSON_CONTENT_TYPE = "application/geo+json"

ADMIN_UNIT_TYPE = "admin-unit"


@dataclass
class BoundaryRef:
    """A boundary attachment: either decoded inline bytes, or a URL to fetch."""

    data: bytes | None = None
    url: str | None = None


@dataclass
class RawLocation:
    """One FHIR Location flattened into the fields kiln cares about."""

    id: str
    name: str | None = None
    status: str | None = None
    loc_type: str | None = None
    physical_type: str | None = None
    pcode: str | None = None
    gers_id: str | None = None
    identifiers: list[dict] = field(default_factory=list)
    parent_id: str | None = None
    overlays_admin_unit_ids: list[str] = field(default_factory=list)
    settlement_type: str | None = None
    delivery_strategy: str | None = None
    position: tuple[float, float] | None = None
    boundary: BoundaryRef | None = None
    last_updated: str | None = None


def _first_coding_code(node: dict | list | None) -> str | None:
    """Pull the first coding.code out of a CodeableConcept or list of them."""
    if isinstance(node, list):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return None
    codings = node.get("coding") or []
    return codings[0].get("code") if codings else None


def _strip_reference(reference: str | None) -> str | None:
    """`Location/loc-1` -> `loc-1`. Bare ids pass through unchanged."""
    if not reference:
        return None
    return reference.rsplit("/", 1)[-1]


def _identifier_value(identifiers: list[dict], system: str) -> str | None:
    for identifier in identifiers:
        if identifier.get("system") == system:
            return identifier.get("value")
    return None


def _read_boundary(extension: dict, location_id: str, report) -> BoundaryRef | None:
    attachment = extension.get("valueAttachment") or {}
    content_type = attachment.get("contentType")
    if content_type != GEOJSON_CONTENT_TYPE:
        report.add(
            "boundary_bad_content_type",
            location_id,
            f"expected {GEOJSON_CONTENT_TYPE}, got {content_type!r}",
        )
        return None

    if attachment.get("data"):
        try:
            return BoundaryRef(data=base64.b64decode(attachment["data"], validate=True))
        except (binascii.Error, ValueError) as exc:
            report.add("boundary_bad_base64", location_id, str(exc))
            return None

    if attachment.get("url"):
        return BoundaryRef(url=attachment["url"])

    report.add("boundary_empty", location_id, "attachment has neither data nor url")
    return None


def shred(resource: dict, report) -> RawLocation | None:
    """Flatten one FHIR Location resource. Returns None if it has no id."""
    location_id = resource.get("id")
    if not location_id:
        report.add("missing_id", "<unknown>", "Location resource has no id")
        return None

    identifiers = resource.get("identifier") or []

    position = None
    if isinstance(resource.get("position"), dict):
        pos = resource["position"]
        longitude, latitude = pos.get("longitude"), pos.get("latitude")
        if longitude is not None and latitude is not None:
            # FHIR is longitude-first. x = longitude, y = latitude.
            position = (float(longitude), float(latitude))

    raw = RawLocation(
        id=location_id,
        name=resource.get("name"),
        status=resource.get("status"),
        loc_type=_first_coding_code(resource.get("type")),
        physical_type=_first_coding_code(resource.get("physicalType")),
        pcode=_identifier_value(identifiers, PCODE_SYSTEM),
        gers_id=_identifier_value(identifiers, GERS_SYSTEM),
        identifiers=[
            {"system": i.get("system"), "value": i.get("value")} for i in identifiers
        ],
        parent_id=_strip_reference((resource.get("partOf") or {}).get("reference")),
        position=position,
        last_updated=(resource.get("meta") or {}).get("lastUpdated"),
    )

    for extension in resource.get("extension") or []:
        url = extension.get("url")
        if url == BOUNDARY_EXTENSION_URL and raw.boundary is None:
            raw.boundary = _read_boundary(extension, location_id, report)
        elif url == OVERLAYS_EXTENSION_URL:
            target = _strip_reference((extension.get("valueReference") or {}).get("reference"))
            if target:
                raw.overlays_admin_unit_ids.append(target)
        elif url == SETTLEMENT_TYPE_EXTENSION_URL:
            raw.settlement_type = extension.get("valueCode")
        elif url == DELIVERY_STRATEGY_EXTENSION_URL:
            raw.delivery_strategy = extension.get("valueCode")

    return raw
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_profile.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/kiln/profile.py tests/test_profile.py
git commit -m "Add ICR profile mapping and FHIR Location shredding"
```

---

### Task 3: Resolve the partOf hierarchy

**Files:**
- Create: `src/kiln/hierarchy.py`
- Test: `tests/test_hierarchy.py`

**Interfaces:**
- Consumes: `RawLocation`, `ADMIN_UNIT_TYPE` from Task 2; `Report` from Task 1.
- Produces:
  - `MAX_DEPTH = 12` constant.
  - `HierarchyInfo` dataclass: `depth: int`, `admin_level: int | None`, `path: str`, `ancestor_ids: list[str]`, `admin_names: list[str | None]` (length 5), `admin_codes: list[str | None]` (length 5), `country: str | None`.
  - `resolve_hierarchy(locations: list[RawLocation], report: Report) -> dict[str, HierarchyInfo]` — keyed by location id. Ids involved in a cycle are **absent** from the returned dict.

**Semantics that the tests pin down:**
- `admin_level` counts only `loc_type == "admin-unit"` nodes in the root-to-self chain; non-admin nodes get `None`.
- `admin_names[i]` / `admin_codes[i]` come from the *i*-th admin-unit ancestor, so a facility hanging off a settlement still inherits `admin2`.
- `country` is `admin_codes[0]`.

- [ ] **Step 1: Write the failing test**

`tests/test_hierarchy.py`:

```python
import pytest

from kiln.hierarchy import resolve_hierarchy
from kiln.profile import RawLocation
from kiln.report import Report


def loc(id_, parent=None, loc_type="admin-unit", name=None, pcode=None) -> RawLocation:
    return RawLocation(
        id=id_,
        name=name or id_,
        loc_type=loc_type,
        parent_id=parent,
        pcode=pcode,
    )


@pytest.fixture
def ragged_tree() -> list[RawLocation]:
    """Nigeria > Kano > Nassarawa > Gama (settlement) > Clinic (facility)."""
    return [
        loc("ng", name="Nigeria", pcode="NG"),
        loc("kano", parent="ng", name="Kano", pcode="NG001"),
        loc("nassarawa", parent="kano", name="Nassarawa", pcode="NG001002"),
        loc("gama", parent="nassarawa", name="Gama", loc_type="settlement"),
        loc("clinic", parent="gama", name="Clinic", loc_type="facility"),
    ]


def test_admin_units_get_a_level_equal_to_their_admin_depth(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())

    assert info["ng"].admin_level == 0
    assert info["kano"].admin_level == 1
    assert info["nassarawa"].admin_level == 2


def test_non_admin_types_have_no_admin_level(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())

    assert info["gama"].admin_level is None
    assert info["clinic"].admin_level is None


def test_depth_counts_every_node_not_just_admin_units(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())

    assert info["clinic"].depth == 4
    assert info["ng"].depth == 0


def test_a_facility_inherits_the_nearest_admin_ancestors_not_hop_count(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())["clinic"]

    assert info.admin_names[:3] == ["Nigeria", "Kano", "Nassarawa"]
    assert info.admin_codes[:3] == ["NG", "NG001", "NG001002"]
    assert info.admin_names[3] is None


def test_country_comes_from_the_root_admin_code(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())

    assert info["clinic"].country == "NG"
    assert info["ng"].country == "NG"


def test_path_and_ancestors_run_root_first(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())["clinic"]

    assert info.path == "/ng/kano/nassarawa/gama/clinic"
    assert info.ancestor_ids == ["ng", "kano", "nassarawa", "gama"]


def test_a_root_node_has_no_ancestors(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())["ng"]

    assert info.ancestor_ids == []
    assert info.path == "/ng"


def test_a_dangling_parent_makes_the_node_a_root_and_is_reported():
    report = Report()
    locations = [loc("orphan", parent="missing", pcode="XX")]

    info = resolve_hierarchy(locations, report)

    assert info["orphan"].depth == 0
    assert info["orphan"].ancestor_ids == []
    assert report.counts() == {"orphan": 1}
    assert "missing" in report.issues[0].detail


def test_a_cycle_is_reported_with_its_path_and_excluded_from_output():
    report = Report()
    locations = [loc("a", parent="b"), loc("b", parent="a")]

    info = resolve_hierarchy(locations, report)

    assert "a" not in info
    assert "b" not in info
    assert report.counts()["cycle"] == 2
    assert "->" in report.issues[0].detail


def test_a_chain_deeper_than_max_depth_is_reported_as_too_deep():
    report = Report()
    locations = [loc("n0")] + [loc(f"n{i}", parent=f"n{i - 1}") for i in range(1, 20)]

    info = resolve_hierarchy(locations, report)

    assert "n19" not in info
    assert report.counts()["too_deep"] >= 1


def test_admin_columns_are_capped_at_five_levels():
    locations = [loc("a0")] + [loc(f"a{i}", parent=f"a{i - 1}") for i in range(1, 8)]

    info = resolve_hierarchy(locations, Report())["a7"]

    assert len(info.admin_names) == 5
    assert len(info.admin_codes) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hierarchy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kiln.hierarchy'`

- [ ] **Step 3: Write the implementation**

`src/kiln/hierarchy.py`:

```python
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
    def __init__(self, path: list[str]) -> None:
        super().__init__(" -> ".join(path))
        self.path = path


class _TooDeepError(Exception):
    def __init__(self, path: list[str]) -> None:
        super().__init__(" -> ".join(path))
        self.path = path


def _chain(location_id: str, by_id: dict[str, RawLocation], report: Report) -> list[str]:
    """Ids from root to `location_id` inclusive."""
    walked: list[str] = []
    seen: set[str] = set()
    current = location_id

    while True:
        if current in seen:
            raise _CycleError([*walked, current])
        seen.add(current)
        walked.append(current)

        if len(walked) > MAX_DEPTH:
            raise _TooDeepError(walked)

        parent_id = by_id[current].parent_id
        if parent_id is None:
            break
        if parent_id not in by_id:
            report.add(
                "orphan",
                current,
                f"partOf references missing id {parent_id}",
            )
            break
        current = parent_id

    walked.reverse()
    return walked


def resolve_hierarchy(
    locations: list[RawLocation], report: Report
) -> dict[str, HierarchyInfo]:
    """Build HierarchyInfo for every resolvable Location.

    Nodes in a cycle, or deeper than MAX_DEPTH, are reported and omitted.
    """
    by_id = {location.id: location for location in locations}
    resolved: dict[str, HierarchyInfo] = {}

    for location in locations:
        try:
            chain = _chain(location.id, by_id, report)
        except _CycleError as exc:
            report.add("cycle", location.id, " -> ".join(exc.path))
            continue
        except _TooDeepError as exc:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hierarchy.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/kiln/hierarchy.py tests/test_hierarchy.py
git commit -m "Add partOf hierarchy resolution with orphan and cycle detection"
```

---

### Task 4: Build geometry from position and boundary GeoJSON

**Files:**
- Create: `src/kiln/geometry.py`
- Test: `tests/test_geometry.py`

**Interfaces:**
- Consumes: `RawLocation`, `BoundaryRef` from Task 2; `Report` from Task 1.
- Produces:
  - `GeometryResult` dataclass: `geometry` (shapely geometry or `None`), `geom_type: str | None` (`"point"` or `"polygon"`), `lon: float | None`, `lat: float | None`.
  - `normalize_geojson(payload: bytes, location_id: str, report: Report)` → shapely geometry or `None`.
  - `build_geometry(raw: RawLocation, report: Report) -> GeometryResult`.

**Semantics the tests pin down:**
- A Location with both a position and a boundary yields **one** result: polygon geometry, point `lon`/`lat`.
- Polygon-only Locations get `lon`/`lat` from `point_on_surface`, never centroid.
- MultiPolygon folds into `geom_type == "polygon"`.

- [ ] **Step 1: Write the failing test**

`tests/test_geometry.py`:

```python
import shapely

from kiln.geometry import build_geometry, normalize_geojson
from kiln.profile import BoundaryRef, RawLocation
from kiln.report import Report

SQUARE = b'{"type":"Polygon","coordinates":[[[3,6],[4,6],[4,7],[3,7],[3,6]]]}'
# A C-shape whose centroid falls outside the polygon.
CRESCENT = (
    b'{"type":"Polygon","coordinates":[[[0,0],[3,0],[3,1],[1,1],[1,2],[3,2],'
    b"[3,3],[0,3],[0,0]]]}"
)


def test_normalize_accepts_a_bare_geometry():
    geom = normalize_geojson(SQUARE, "loc-1", Report())

    assert geom.geom_type == "Polygon"


def test_normalize_unwraps_a_feature():
    payload = b'{"type":"Feature","properties":{},"geometry":' + SQUARE + b"}"

    assert normalize_geojson(payload, "loc-1", Report()).geom_type == "Polygon"


def test_normalize_unwraps_a_single_feature_collection():
    payload = (
        b'{"type":"FeatureCollection","features":[{"type":"Feature",'
        b'"properties":{},"geometry":' + SQUARE + b"}]}"
    )

    assert normalize_geojson(payload, "loc-1", Report()).geom_type == "Polygon"


def test_normalize_folds_a_multi_feature_collection_and_warns():
    report = Report()
    payload = (
        b'{"type":"FeatureCollection","features":['
        b'{"type":"Feature","properties":{},"geometry":' + SQUARE + b"},"
        b'{"type":"Feature","properties":{},"geometry":' + SQUARE + b"}]}"
    )

    geom = normalize_geojson(payload, "loc-1", report)

    assert geom.geom_type in {"MultiPolygon", "Polygon"}
    assert report.counts() == {"boundary_multi_feature": 1}


def test_normalize_reports_unparseable_json():
    report = Report()

    assert normalize_geojson(b"{not json", "loc-1", report) is None
    assert report.counts() == {"boundary_unparseable": 1}


def test_position_only_location_becomes_a_point():
    raw = RawLocation(id="c", loc_type="facility", position=(8.5, 12.0))

    result = build_geometry(raw, Report())

    assert result.geom_type == "point"
    assert (result.lon, result.lat) == (8.5, 12.0)
    assert result.geometry.geom_type == "Point"


def test_point_uses_longitude_as_x_and_latitude_as_y():
    raw = RawLocation(id="c", position=(8.5, 12.0))

    geometry = build_geometry(raw, Report()).geometry

    assert (geometry.x, geometry.y) == (8.5, 12.0)


def test_boundary_only_location_becomes_a_polygon():
    raw = RawLocation(id="d", boundary=BoundaryRef(data=SQUARE))

    result = build_geometry(raw, Report())

    assert result.geom_type == "polygon"
    assert result.geometry.geom_type == "Polygon"


def test_polygon_lon_lat_come_from_point_on_surface_not_centroid():
    raw = RawLocation(id="d", boundary=BoundaryRef(data=CRESCENT))

    result = build_geometry(raw, Report())
    representative = shapely.Point(result.lon, result.lat)

    assert result.geometry.contains(representative)


def test_a_location_with_both_yields_one_row_polygon_geometry_point_coords():
    raw = RawLocation(id="e", position=(3.2, 6.2), boundary=BoundaryRef(data=SQUARE))

    result = build_geometry(raw, Report())

    assert result.geom_type == "polygon"
    assert result.geometry.geom_type == "Polygon"
    assert (result.lon, result.lat) == (3.2, 6.2)


def test_a_location_with_no_geometry_is_reported():
    report = Report()

    result = build_geometry(RawLocation(id="f"), report)

    assert result.geometry is None
    assert result.geom_type is None
    assert report.counts() == {"no_geometry": 1}


def test_an_invalid_ring_is_repaired_and_counted():
    report = Report()
    bowtie = b'{"type":"Polygon","coordinates":[[[0,0],[2,2],[2,0],[0,2],[0,0]]]}'

    result = build_geometry(RawLocation(id="g", boundary=BoundaryRef(data=bowtie)), report)

    assert result.geometry.is_valid
    assert report.counts() == {"geometry_repaired": 1}


def test_a_multipolygon_boundary_is_still_geom_type_polygon():
    payload = (
        b'{"type":"MultiPolygon","coordinates":[[[[0,0],[1,0],[1,1],[0,1],[0,0]]],'
        b"[[[2,2],[3,2],[3,3],[2,3],[2,2]]]]}"
    )

    result = build_geometry(RawLocation(id="h", boundary=BoundaryRef(data=payload)), Report())

    assert result.geom_type == "polygon"
    assert result.geometry.geom_type == "MultiPolygon"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_geometry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kiln.geometry'`

- [ ] **Step 3: Write the implementation**

`src/kiln/geometry.py`:

```python
"""Turn Location.position and boundary GeoJSON into shapely geometry."""

from __future__ import annotations

import json
from dataclasses import dataclass

import shapely

from kiln.profile import RawLocation
from kiln.report import Report

POLYGON_TYPES = {"Polygon", "MultiPolygon"}


@dataclass
class GeometryResult:
    """Geometry plus the representative point that always accompanies it."""

    geometry: object | None = None
    geom_type: str | None = None
    lon: float | None = None
    lat: float | None = None


def normalize_geojson(payload: bytes, location_id: str, report: Report):
    """Parse GeoJSON that may be a geometry, a Feature, or a FeatureCollection."""
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        report.add("boundary_unparseable", location_id, str(exc))
        return None

    if not isinstance(parsed, dict):
        report.add("boundary_unparseable", location_id, "GeoJSON root is not an object")
        return None

    kind = parsed.get("type")

    if kind == "FeatureCollection":
        geometries = [
            feature.get("geometry")
            for feature in parsed.get("features") or []
            if feature.get("geometry")
        ]
        if not geometries:
            report.add("boundary_unparseable", location_id, "FeatureCollection has no geometry")
            return None
        if len(geometries) > 1:
            report.add(
                "boundary_multi_feature",
                location_id,
                f"{len(geometries)} features folded into one geometry",
            )
            parts = [_from_dict(g, location_id, report) for g in geometries]
            parts = [part for part in parts if part is not None]
            return shapely.union_all(parts) if parts else None
        return _from_dict(geometries[0], location_id, report)

    if kind == "Feature":
        geometry = parsed.get("geometry")
        if not geometry:
            report.add("boundary_unparseable", location_id, "Feature has no geometry")
            return None
        return _from_dict(geometry, location_id, report)

    return _from_dict(parsed, location_id, report)


def _from_dict(geometry: dict, location_id: str, report: Report):
    try:
        return shapely.from_geojson(json.dumps(geometry))
    except Exception as exc:  # shapely raises GEOSException subclasses
        report.add("boundary_unparseable", location_id, str(exc))
        return None


def build_geometry(raw: RawLocation, report: Report) -> GeometryResult:
    """Resolve one Location's geometry.

    A Location carrying both a position and a boundary yields a single row:
    the polygon as geometry, the position as lon/lat.
    """
    polygon = None
    if raw.boundary is not None and raw.boundary.data:
        polygon = normalize_geojson(raw.boundary.data, raw.id, report)
        if polygon is not None and not polygon.is_valid:
            polygon = shapely.make_valid(polygon)
            report.add("geometry_repaired", raw.id, "invalid ring repaired by make_valid")

    if polygon is not None:
        if raw.position is not None:
            lon, lat = raw.position
        else:
            representative = shapely.point_on_surface(polygon)
            lon, lat = representative.x, representative.y
        geom_type = "polygon" if polygon.geom_type in POLYGON_TYPES else "point"
        return GeometryResult(geometry=polygon, geom_type=geom_type, lon=lon, lat=lat)

    if raw.position is not None:
        lon, lat = raw.position
        return GeometryResult(
            geometry=shapely.Point(lon, lat), geom_type="point", lon=lon, lat=lat
        )

    report.add("no_geometry", raw.id, "no position and no usable boundary")
    return GeometryResult()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_geometry.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add src/kiln/geometry.py tests/test_geometry.py
git commit -m "Add geometry construction from position and boundary GeoJSON"
```

---

### Task 5: Assemble the GeoDataFrame

**Files:**
- Create: `src/kiln/frame.py`
- Test: `tests/test_frame.py`

**Interfaces:**
- Consumes: `RawLocation` (Task 2), `resolve_hierarchy`/`HierarchyInfo` (Task 3), `build_geometry` (Task 4), `Report` (Task 1).
- Produces: `build_frame(locations: list[RawLocation], report: Report, country_override: str | None = None) -> geopandas.GeoDataFrame` with CRS `EPSG:4326`.

**Column contract** (later tasks depend on these exact names):
`id`, `name`, `status`, `loc_type`, `physical_type`, `pcode`, `gers_id`, `identifiers`, `parent_id`, `depth`, `admin_level`, `path`, `ancestor_ids`, `admin0_name`…`admin4_name`, `admin0_code`…`admin4_code`, `country`, `settlement_type`, `delivery_strategy`, `overlays_admin_unit_ids`, `geom_type`, `tier`, `lon`, `lat`, `last_updated`, `geometry`.

`tier` is `str(admin_level)` when `admin_level` is not None, else `"site"`. Rows with no geometry are dropped (already reported by Task 4).

`country` resolves as `country_override or info.country or "unknown"`. A row can legitimately have no derived country — an orphan whose dangling `partOf` left it with no admin ancestors — and such rows land in `country=unknown` with a `no_country` issue rather than being silently dropped. `--country` overrides everything, which is the normal single-country invocation.

- [ ] **Step 1: Write the failing test**

`tests/test_frame.py`:

```python
import geopandas as gpd

from kiln.frame import build_frame
from kiln.profile import BoundaryRef, RawLocation
from kiln.report import Report

SQUARE = b'{"type":"Polygon","coordinates":[[[3,6],[4,6],[4,7],[3,7],[3,6]]]}'


def tree() -> list[RawLocation]:
    return [
        RawLocation(id="ng", name="Nigeria", loc_type="admin-unit", pcode="NG"),
        RawLocation(
            id="kano",
            name="Kano",
            loc_type="admin-unit",
            pcode="NG001",
            parent_id="ng",
            boundary=BoundaryRef(data=SQUARE),
        ),
        RawLocation(
            id="clinic",
            name="Clinic",
            loc_type="facility",
            parent_id="kano",
            position=(3.2, 6.2),
        ),
    ]


def test_build_frame_returns_a_geodataframe_in_wgs84():
    frame = build_frame(tree(), Report())

    assert isinstance(frame, gpd.GeoDataFrame)
    assert frame.crs.to_string() == "EPSG:4326"


def test_rows_without_geometry_are_dropped():
    frame = build_frame(tree(), Report())

    assert set(frame["id"]) == {"kano", "clinic"}


def test_tier_is_the_admin_level_for_admin_units_and_site_otherwise():
    frame = build_frame(tree(), Report()).set_index("id")

    assert frame.loc["kano", "tier"] == "1"
    assert frame.loc["clinic", "tier"] == "site"


def test_geom_type_column_matches_the_geometry():
    frame = build_frame(tree(), Report()).set_index("id")

    assert frame.loc["kano", "geom_type"] == "polygon"
    assert frame.loc["clinic", "geom_type"] == "point"


def test_denormalized_admin_columns_are_present():
    frame = build_frame(tree(), Report()).set_index("id")

    assert frame.loc["clinic", "admin0_name"] == "Nigeria"
    assert frame.loc["clinic", "admin1_code"] == "NG001"
    assert frame.loc["clinic", "country"] == "NG"


def test_country_override_wins_over_the_derived_value():
    frame = build_frame(tree(), Report(), country_override="ZZ")

    assert set(frame["country"]) == {"ZZ"}


def test_frame_has_the_full_expected_column_set():
    frame = build_frame(tree(), Report())

    expected = {
        "id", "name", "status", "loc_type", "physical_type", "pcode", "gers_id",
        "identifiers", "parent_id", "depth", "admin_level", "path", "ancestor_ids",
        "country", "settlement_type", "delivery_strategy", "overlays_admin_unit_ids",
        "geom_type", "tier", "lon", "lat", "last_updated", "geometry",
    }
    expected |= {f"admin{i}_name" for i in range(5)}
    expected |= {f"admin{i}_code" for i in range(5)}

    assert set(frame.columns) == expected


def test_a_row_with_no_derivable_country_lands_in_unknown_and_is_reported():
    report = Report()
    locations = [
        RawLocation(id="orphan", loc_type="facility", parent_id="gone", position=(4.0, 7.0))
    ]

    frame = build_frame(locations, report)

    assert frame.iloc[0]["country"] == "unknown"
    assert report.counts()["no_country"] == 1


def test_nodes_excluded_by_the_hierarchy_are_absent_from_the_frame():
    locations = [
        RawLocation(id="a", parent_id="b", position=(1.0, 1.0)),
        RawLocation(id="b", parent_id="a", position=(2.0, 2.0)),
    ]

    frame = build_frame(locations, Report())

    assert frame.empty
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_frame.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kiln.frame'`

- [ ] **Step 3: Write the implementation**

`src/kiln/frame.py`:

```python
"""Assemble shredded records, hierarchy and geometry into one GeoDataFrame."""

from __future__ import annotations

import geopandas as gpd

from kiln.geometry import build_geometry
from kiln.hierarchy import ADMIN_COLUMNS, resolve_hierarchy
from kiln.profile import RawLocation
from kiln.report import Report

CRS = "EPSG:4326"
UNKNOWN_COUNTRY = "unknown"


def build_frame(
    locations: list[RawLocation],
    report: Report,
    country_override: str | None = None,
) -> gpd.GeoDataFrame:
    """Produce the single wide table that gets partitioned and written.

    Locations that the hierarchy could not resolve, or that have no
    geometry, are omitted. Both cases are already recorded in the report.
    """
    hierarchy = resolve_hierarchy(locations, report)

    rows: list[dict] = []
    geometries: list[object] = []

    for location in locations:
        info = hierarchy.get(location.id)
        if info is None:
            continue

        geometry = build_geometry(location, report)
        if geometry.geometry is None:
            continue

        country = country_override or info.country
        if country is None:
            report.add(
                "no_country",
                location.id,
                "no admin-unit ancestor carries a pcode; filed under 'unknown'",
            )
            country = UNKNOWN_COUNTRY

        row = {
            "id": location.id,
            "name": location.name,
            "status": location.status,
            "loc_type": location.loc_type,
            "physical_type": location.physical_type,
            "pcode": location.pcode,
            "gers_id": location.gers_id,
            "identifiers": location.identifiers,
            "parent_id": location.parent_id,
            "depth": info.depth,
            "admin_level": info.admin_level,
            "path": info.path,
            "ancestor_ids": info.ancestor_ids,
            "country": country,
            "settlement_type": location.settlement_type,
            "delivery_strategy": location.delivery_strategy,
            "overlays_admin_unit_ids": location.overlays_admin_unit_ids,
            "geom_type": geometry.geom_type,
            "tier": "site" if info.admin_level is None else str(info.admin_level),
            "lon": geometry.lon,
            "lat": geometry.lat,
            "last_updated": location.last_updated,
        }
        for index in range(ADMIN_COLUMNS):
            row[f"admin{index}_name"] = info.admin_names[index]
            row[f"admin{index}_code"] = info.admin_codes[index]

        rows.append(row)
        geometries.append(geometry.geometry)

    if not rows:
        return gpd.GeoDataFrame(geometry=[], crs=CRS)

    return gpd.GeoDataFrame(rows, geometry=geometries, crs=CRS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_frame.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/kiln/frame.py tests/test_frame.py
git commit -m "Assemble records, hierarchy and geometry into a GeoDataFrame"
```

---

### Task 6: Write partitioned GeoParquet

**Files:**
- Create: `src/kiln/write.py`
- Test: `tests/test_write.py`

**Interfaces:**
- Consumes: the frame from Task 5; `Report` from Task 1.
- Produces:
  - `GEO_TYPE_FLAGS = {"both": "YES", "only": "ONLY", "legacy": "NO"}`
  - `DEFAULT_PARTITION_BY = ("country", "geom_type", "tier")`
  - `DEFAULT_ROW_GROUP_SIZE = 20000`
  - `MIN_PARTITION_ROWS = 100`
  - `probe_gdal() -> None` — raises `GdalUnavailable` with an actionable message.
  - `GdalUnavailable(RuntimeError)`
  - `write_dataset(frame, out_dir: Path, report: Report, partition_by=DEFAULT_PARTITION_BY, row_group_size=DEFAULT_ROW_GROUP_SIZE, geo_types="both") -> list[Path]`

**These tests invoke the real `ogr2ogr`.** That is deliberate — GDAL is the load-bearing dependency and mocking it would test nothing.

- [ ] **Step 1: Write the failing test**

`tests/test_write.py`:

```python
import json

import geopandas as gpd
import numpy as np
import pyarrow.parquet as pq
import pytest
import shapely

from kiln.report import Report
from kiln.write import GdalUnavailable, probe_gdal, write_dataset


@pytest.fixture(autouse=True)
def require_gdal():
    try:
        probe_gdal()
    except GdalUnavailable as exc:
        pytest.skip(str(exc))


def a_frame(n_points=250, n_polygons=250) -> gpd.GeoDataFrame:
    rng = np.random.default_rng(0)
    points = [shapely.Point(x, y) for x, y in rng.random((n_points, 2)) * 10]
    polygons = [shapely.box(x, y, x + 0.1, y + 0.1) for x, y in rng.random((n_polygons, 2)) * 10]
    rows = (
        [{"id": f"p{i}", "country": "NG", "geom_type": "point", "tier": "site"}
         for i in range(n_points)]
        + [{"id": f"a{i}", "country": "NG", "geom_type": "polygon", "tier": "1"}
           for i in range(n_polygons)]
    )
    return gpd.GeoDataFrame(rows, geometry=points + polygons, crs="EPSG:4326")


def test_writes_the_expected_hive_partitions(tmp_path):
    write_dataset(a_frame(), tmp_path, Report())

    written = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.parquet")}

    assert written == {
        "locations/country=NG/geom_type=point/tier=site/part-0.parquet",
        "locations/country=NG/geom_type=polygon/tier=1/part-0.parquet",
    }


def test_geometry_column_uses_the_native_parquet_type(tmp_path):
    write_dataset(a_frame(), tmp_path, Report())
    target = next(tmp_path.rglob("*.parquet"))

    schema = pq.ParquetFile(target).schema

    assert "Geometry" in str(schema)


def test_a_covering_bbox_column_is_written_and_declared(tmp_path):
    write_dataset(a_frame(), tmp_path, Report())
    target = next(tmp_path.rglob("*.parquet"))

    meta = json.loads(pq.ParquetFile(target).metadata.metadata[b"geo"])

    assert "covering" in meta["columns"]["geometry"]


def test_each_partition_file_has_a_single_geometry_type(tmp_path):
    write_dataset(a_frame(), tmp_path, Report())

    for target in tmp_path.rglob("*.parquet"):
        meta = json.loads(pq.ParquetFile(target).metadata.metadata[b"geo"])
        assert len(meta["columns"]["geometry"]["geometry_types"]) == 1


def test_no_pandas_index_column_leaks_into_the_output(tmp_path):
    write_dataset(a_frame(), tmp_path, Report())
    target = next(tmp_path.rglob("*.parquet"))

    names = [pq.ParquetFile(target).schema.column(i).name
             for i in range(len(pq.ParquetFile(target).schema))]

    assert not any("__index_level_" in name for name in names)


def test_row_group_size_is_honoured(tmp_path):
    write_dataset(a_frame(n_points=500, n_polygons=0), tmp_path, Report(), row_group_size=100)
    target = next(tmp_path.rglob("*.parquet"))

    assert pq.ParquetFile(target).metadata.num_row_groups == 5


def test_hilbert_sorting_clusters_neighbours_together(tmp_path):
    write_dataset(a_frame(n_points=500, n_polygons=0), tmp_path, Report(), row_group_size=100)
    target = next(tmp_path.rglob("*.parquet"))
    frame = gpd.read_parquet(target)

    # Successive rows should be closer together than a random shuffle would be.
    ordered = frame.geometry
    step = np.mean([
        ordered.iloc[i].distance(ordered.iloc[i + 1]) for i in range(len(ordered) - 1)
    ])
    shuffled = frame.sample(frac=1, random_state=1).geometry
    shuffled_step = np.mean([
        shuffled.iloc[i].distance(shuffled.iloc[i + 1]) for i in range(len(shuffled) - 1)
    ])

    assert step < shuffled_step / 2


def test_small_partitions_are_reported(tmp_path):
    report = Report()

    write_dataset(a_frame(n_points=5, n_polygons=5), tmp_path, report)

    assert report.counts()["small_partition"] == 2


def test_legacy_geo_types_writes_wkb_without_the_native_type(tmp_path):
    write_dataset(a_frame(), tmp_path, Report(), geo_types="legacy")
    target = next(tmp_path.rglob("*.parquet"))

    assert "Geometry" not in str(pq.ParquetFile(target).schema)


def test_an_empty_frame_writes_nothing_and_does_not_raise(tmp_path):
    empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    assert write_dataset(empty, tmp_path, Report()) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_write.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kiln.write'`

- [ ] **Step 3: Write the implementation**

`src/kiln/write.py`:

```python
"""Two-pass GeoParquet write.

geopandas stages WKB parquet per partition; ogr2ogr rewrites each staging
file with native Parquet geometry types and a covering bbox. GDAL is the
only writer available that emits native geometry types, which is why the
second pass exists.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import geopandas as gpd

from kiln.report import Report

GEO_TYPE_FLAGS = {"both": "YES", "only": "ONLY", "legacy": "NO"}
DEFAULT_PARTITION_BY = ("country", "geom_type", "tier")
DEFAULT_ROW_GROUP_SIZE = 20000
MIN_PARTITION_ROWS = 100
DATASET_DIR = "locations"


class GdalUnavailable(RuntimeError):
    """Raised when the system GDAL cannot produce the required output."""


def probe_gdal() -> None:
    """Verify ogr2ogr exists and its Parquet driver supports native geo types."""
    if shutil.which("ogr2ogr") is None:
        raise GdalUnavailable(
            "ogr2ogr not found on PATH. kiln requires system GDAL >= 3.13 "
            "(brew install gdal / apt install gdal-bin)."
        )
    if shutil.which("ogrinfo") is None:
        raise GdalUnavailable("ogrinfo not found on PATH; install the full GDAL toolset.")

    result = subprocess.run(
        ["ogrinfo", "--format", "Parquet"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GdalUnavailable(
            "GDAL has no Parquet driver. Install a GDAL build with Arrow/Parquet support."
        )
    if "USE_PARQUET_GEO_TYPES" not in result.stdout:
        raise GdalUnavailable(
            "GDAL's Parquet driver does not support USE_PARQUET_GEO_TYPES. "
            "kiln requires GDAL >= 3.13 built against libarrow >= 21."
        )


def _finalize(source: Path, destination: Path, row_group_size: int, geo_types: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ogr2ogr",
            "-f", "Parquet",
            str(destination),
            str(source),
            "-lco", f"USE_PARQUET_GEO_TYPES={GEO_TYPE_FLAGS[geo_types]}",
            "-lco", "WRITE_COVERING_BBOX=YES",
            # The Hilbert order is applied upstream; do not let GDAL re-sort.
            "-lco", "SORT_BY_BBOX=NO",
            "-lco", "COMPRESSION=ZSTD",
            "-lco", f"ROW_GROUP_SIZE={row_group_size}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def write_dataset(
    frame: gpd.GeoDataFrame,
    out_dir: Path,
    report: Report,
    partition_by: tuple[str, ...] = DEFAULT_PARTITION_BY,
    row_group_size: int = DEFAULT_ROW_GROUP_SIZE,
    geo_types: str = "both",
) -> list[Path]:
    """Sort spatially, split into hive partitions, and write each one."""
    if geo_types not in GEO_TYPE_FLAGS:
        raise ValueError(f"geo_types must be one of {sorted(GEO_TYPE_FLAGS)}")
    if frame.empty:
        return []

    out_dir = Path(out_dir)
    keys = list(partition_by)

    # Cluster spatially so a bbox query touches few row groups.
    ordered = frame.iloc[frame.hilbert_distance().argsort()].reset_index(drop=True)

    written: list[Path] = []
    with tempfile.TemporaryDirectory() as staging_root:
        staging = Path(staging_root)
        for values, part in ordered.groupby(keys, sort=False):
            if not isinstance(values, tuple):
                values = (values,)
            segments = [f"{key}={value}" for key, value in zip(keys, values, strict=True)]

            if len(part) < MIN_PARTITION_ROWS:
                report.add(
                    "small_partition",
                    "/".join(segments),
                    f"{len(part)} rows is below MIN_PARTITION_ROWS={MIN_PARTITION_ROWS}",
                )

            staged = staging / ("_".join(segments) + ".parquet")
            part.to_parquet(staged, index=False)

            destination = out_dir.joinpath(DATASET_DIR, *segments, "part-0.parquet")
            _finalize(staged, destination, row_group_size, geo_types)
            written.append(destination)

    return written
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_write.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/kiln/write.py tests/test_write.py
git commit -m "Add two-pass partitioned GeoParquet write via geopandas and ogr2ogr"
```

---

### Task 7: Extract Locations from a FHIR server

**Files:**
- Create: `src/kiln/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `BOUNDARY_EXTENSION_URL`, `GEOJSON_CONTENT_TYPE` from Task 2; `Report` from Task 1.
- Produces:
  - `fetch_locations(server: str, token: str | None, since: str | None = None, client: httpx.Client | None = None) -> Iterator[dict]` — yields raw Location resources, following `Bundle.link[next]`.
  - `resolve_boundary_urls(resources: list[dict], report: Report, token: str | None = None, client: httpx.Client | None = None) -> None` — mutates resources in place, replacing a boundary attachment's `url` with inline `data`.
  - `write_ndjson(resources: Iterable[dict], path: Path) -> int` — returns the count written.
  - `read_ndjson(path: Path) -> Iterator[dict]` — also accepts a FHIR Bundle `.json` and explodes `entry[].resource`.

Tests use `httpx.MockTransport`; no network access.

- [ ] **Step 1: Write the failing test**

`tests/test_extract.py`:

```python
import base64
import json

import httpx
import pytest

from kiln.extract import (
    fetch_locations,
    read_ndjson,
    resolve_boundary_urls,
    write_ndjson,
)
from kiln.profile import BOUNDARY_EXTENSION_URL
from kiln.report import Report

GEOJSON = b'{"type":"Polygon","coordinates":[[[3,6],[4,6],[4,7],[3,7],[3,6]]]}'


def bundle(resources, next_url=None) -> dict:
    payload = {
        "resourceType": "Bundle",
        "entry": [{"resource": r} for r in resources],
    }
    if next_url:
        payload["link"] = [{"relation": "next", "url": next_url}]
    return payload


def test_fetch_follows_next_links_across_pages():
    page_two = "https://fhir.test/Location?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=bundle([{"id": "b"}]))
        return httpx.Response(200, json=bundle([{"id": "a"}], next_url=page_two))

    client = httpx.Client(transport=httpx.MockTransport(handler))

    ids = [r["id"] for r in fetch_locations("https://fhir.test", None, client=client)]

    assert ids == ["a", "b"]


def test_fetch_sends_the_bearer_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=bundle([]))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    list(fetch_locations("https://fhir.test", "tok-123", client=client))

    assert seen["auth"] == "Bearer tok-123"


def test_fetch_passes_since_when_given():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["since"] = request.url.params.get("_lastUpdated")
        return httpx.Response(200, json=bundle([]))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    list(fetch_locations("https://fhir.test", None, since="2026-01-01", client=client))

    assert seen["since"] == "gt2026-01-01"


def test_fetch_raises_with_the_server_body_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="403"):
        list(fetch_locations("https://fhir.test", None, client=client))


def a_resource_with_boundary_url(url: str) -> dict:
    return {
        "id": "loc-1",
        "extension": [
            {
                "url": BOUNDARY_EXTENSION_URL,
                "valueAttachment": {"contentType": "application/geo+json", "url": url},
            }
        ],
    }


def test_resolve_inlines_a_plain_geojson_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resources = [a_resource_with_boundary_url("https://files.test/a.geojson")]

    resolve_boundary_urls(resources, Report(), client=client)

    attachment = resources[0]["extension"][0]["valueAttachment"]
    assert base64.b64decode(attachment["data"]) == GEOJSON
    assert "url" not in attachment


def test_resolve_unwraps_a_binary_resource():
    payload = {
        "resourceType": "Binary",
        "contentType": "application/geo+json",
        "data": base64.b64encode(GEOJSON).decode(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resources = [a_resource_with_boundary_url("https://fhir.test/Binary/abc")]

    resolve_boundary_urls(resources, Report(), client=client)

    assert base64.b64decode(resources[0]["extension"][0]["valueAttachment"]["data"]) == GEOJSON


def test_a_failed_boundary_fetch_is_reported_and_does_not_abort_the_run():
    report = Report()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resources = [a_resource_with_boundary_url("https://files.test/missing.geojson")]

    resolve_boundary_urls(resources, report, client=client)

    assert report.counts() == {"boundary_fetch_failed": 1}
    assert "data" not in resources[0]["extension"][0]["valueAttachment"]


def test_ndjson_round_trips(tmp_path):
    path = tmp_path / "locations.ndjson"

    assert write_ndjson([{"id": "a"}, {"id": "b"}], path) == 2
    assert [r["id"] for r in read_ndjson(path)] == ["a", "b"]


def test_read_ndjson_also_accepts_a_bundle_json_file(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle([{"id": "a"}, {"id": "b"}])))

    assert [r["id"] for r in read_ndjson(path)] == ["a", "b"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_extract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kiln.extract'`

- [ ] **Step 3: Write the implementation**

`src/kiln/extract.py`:

```python
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
    """Read NDJSON, or a FHIR Bundle .json, as a stream of resources."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()

    if path.suffix == ".json" or stripped.startswith("{\"resourceType\": \"Bundle\""):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and payload.get("resourceType") == "Bundle":
            for entry in payload.get("entry") or []:
                if entry.get("resource"):
                    yield entry["resource"]
            return

    for line in text.splitlines():
        line = line.strip()
        if line:
            yield json.loads(line)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_extract.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/kiln/extract.py tests/test_extract.py
git commit -m "Add FHIR Location extraction and boundary URL resolution"
```

---

### Task 8: CLI and end-to-end fixture test

**Files:**
- Create: `src/kiln/cli.py`
- Create: `tests/fixtures/build_fixture.py`
- Create: `tests/fixtures/locations.ndjson`
- Modify: `src/kiln/report.py` (add `check_duplicate_pcodes`, `check_points_within_parents`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces: `main(argv: list[str] | None = None) -> int`, plus `cmd_extract`, `cmd_transform`, `cmd_run`. Exit code `0` on success, `2` on a usage or environment error.

The fixture carries one of every pathology named in the spec so the end-to-end test exercises all of them at once.

- [ ] **Step 1: Create the fixture**

`tests/fixtures/locations.ndjson` — one JSON object per line. The base64 below decodes to the polygons noted in the comments; generate the file with this script rather than hand-typing it:

```python
# Run once from the repo root to produce tests/fixtures/locations.ndjson
import base64, json, pathlib

B = "https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson"
P = "https://icr.healthcampaigns.org/identifiers/pcode"

def b64(geojson): return base64.b64encode(json.dumps(geojson).encode()).decode()
def box(x, y, s=1):
    return {"type": "Polygon", "coordinates": [[[x, y], [x + s, y], [x + s, y + s],
                                                [x, y + s], [x, y]]]}
def loc(id_, name, type_, parent=None, pcode=None, position=None,
        boundary=None, boundary_url=None):
    r = {"resourceType": "Location", "id": id_, "name": name, "status": "active",
         "type": [{"coding": [{"code": type_}]}]}
    if pcode: r["identifier"] = [{"system": P, "value": pcode}]
    if parent: r["partOf"] = {"reference": f"Location/{parent}"}
    if position: r["position"] = {"longitude": position[0], "latitude": position[1]}
    if boundary:
        r["extension"] = [{"url": B, "valueAttachment": {
            "contentType": "application/geo+json", "data": b64(boundary)}}]
    if boundary_url:
        r["extension"] = [{"url": B, "valueAttachment": {
            "contentType": "application/geo+json", "url": boundary_url}}]
    return r

resources = [
    loc("ng", "Nigeria", "admin-unit", pcode="NG", boundary=box(3, 6, 6)),
    loc("kano", "Kano", "admin-unit", parent="ng", pcode="NG001", boundary=box(3, 6, 3)),
    loc("nassarawa", "Nassarawa", "admin-unit", parent="kano", pcode="NG001002",
        boundary=box(3, 6, 1)),
    # settlement with BOTH a position and a boundary -> one row, polygon geometry
    loc("gama", "Gama", "settlement", parent="nassarawa", position=(3.2, 6.2),
        boundary=box(3.1, 6.1, 0.4)),
    loc("clinic", "Gama Clinic", "facility", parent="gama", position=(3.25, 6.25)),
    # point outside its nearest admin ancestor's polygon
    loc("stray", "Stray Post", "facility", parent="nassarawa", position=(50.0, 50.0)),
    # boundary by url -> transform alone cannot resolve it
    loc("remote", "Remote Area", "operational-area", parent="kano",
        boundary_url="https://files.test/remote.geojson"),
    # no geometry at all
    loc("ghost", "Ghost Ward", "admin-unit", parent="kano", pcode="NG001003"),
    # dangling partOf
    loc("orphan", "Orphan Site", "facility", parent="does-not-exist", position=(4.0, 7.0)),
    # duplicate pcode, clashing with nassarawa
    loc("dup", "Duplicate", "admin-unit", parent="kano", pcode="NG001002",
        boundary=box(4, 7, 1)),
    # cycle
    loc("cyc-a", "Cycle A", "admin-unit", parent="cyc-b", position=(5.0, 8.0)),
    loc("cyc-b", "Cycle B", "admin-unit", parent="cyc-a", position=(5.1, 8.1)),
]

out = pathlib.Path("tests/fixtures/locations.ndjson")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in resources) + "\n")
print(f"wrote {len(resources)} resources")
```

Run: `uv run python -` with that script pasted, or save it as `tests/fixtures/build_fixture.py` and run `uv run python tests/fixtures/build_fixture.py`. Commit both the script and the generated `.ndjson`.

- [ ] **Step 2: Write the failing test**

`tests/test_cli.py`:

```python
import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest

from kiln.cli import main
from kiln.write import GdalUnavailable, probe_gdal

FIXTURE = Path(__file__).parent / "fixtures" / "locations.ndjson"


@pytest.fixture(autouse=True)
def require_gdal():
    try:
        probe_gdal()
    except GdalUnavailable as exc:
        pytest.skip(str(exc))


@pytest.fixture
def transformed(tmp_path):
    out = tmp_path / "out"
    assert main(["transform", "--in", str(FIXTURE), "--out", str(out)]) == 0
    return out


def read_all(out: Path) -> gpd.GeoDataFrame:
    frames = [gpd.read_parquet(p) for p in sorted(out.rglob("*.parquet"))]
    return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs="EPSG:4326")


def test_transform_writes_partitioned_output(transformed):
    written = {p.relative_to(transformed).as_posix() for p in transformed.rglob("*.parquet")}

    assert written, "no parquet files written"
    assert all(part.startswith("locations/country=") for part in written)
    assert any(part.startswith("locations/country=NG/") for part in written)


def test_an_orphan_with_no_admin_ancestor_lands_under_unknown(transformed):
    frame = read_all(transformed).set_index("id")

    assert frame.loc["orphan", "country"] == "unknown"


def test_settlement_with_both_geometries_yields_exactly_one_row(transformed):
    frame = read_all(transformed)

    gama = frame[frame["id"] == "gama"]
    assert len(gama) == 1
    assert gama.iloc[0]["geom_type"] == "polygon"
    assert (gama.iloc[0]["lon"], gama.iloc[0]["lat"]) == (3.2, 6.2)


def test_facility_inherits_admin_ancestors(transformed):
    clinic = read_all(transformed).set_index("id").loc["clinic"]

    assert clinic["admin0_name"] == "Nigeria"
    assert clinic["admin2_name"] == "Nassarawa"
    assert pd.isna(clinic["admin_level"])
    assert clinic["tier"] == "site"


def test_cycle_members_are_absent_from_the_output(transformed):
    ids = set(read_all(transformed)["id"])

    assert "cyc-a" not in ids
    assert "cyc-b" not in ids


def test_location_without_geometry_is_absent(transformed):
    assert "ghost" not in set(read_all(transformed)["id"])


def test_report_records_every_expected_pathology(transformed):
    counts = json.loads((transformed / "_report.json").read_text())["counts"]

    assert counts["cycle"] == 2
    assert counts["orphan"] == 1
    assert counts["no_geometry"] >= 2  # ghost, plus the unresolved remote boundary
    assert counts["duplicate_pcode"] == 1
    assert counts["point_outside_parent"] == 1
    assert counts["no_country"] == 1  # the orphan, whose partOf dangles


def test_transform_is_offline_and_does_not_resolve_remote_boundaries(transformed):
    counts = json.loads((transformed / "_report.json").read_text())["counts"]

    assert counts["boundary_unresolved_url"] == 1


def test_country_override_is_applied(tmp_path):
    out = tmp_path / "out"
    main(["transform", "--in", str(FIXTURE), "--out", str(out), "--country", "ZZ"])

    assert any("country=ZZ" in p.as_posix() for p in out.rglob("*.parquet"))


def test_missing_input_file_exits_with_code_two(tmp_path, capsys):
    code = main(["transform", "--in", str(tmp_path / "nope.ndjson"), "--out", str(tmp_path)])

    assert code == 2
    assert "not found" in capsys.readouterr().err
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kiln.cli'`

- [ ] **Step 4: Add the two remaining report checks**

These two checks are computed at transform time and have no home yet. Add them to `src/kiln/report.py`:

```python
def check_duplicate_pcodes(frame, report) -> None:
    """Flag pcodes claimed by more than one Location."""
    with_pcode = frame[frame["pcode"].notna()]
    duplicated = with_pcode[with_pcode.duplicated("pcode", keep=False)]
    for pcode, group in duplicated.groupby("pcode"):
        report.add(
            "duplicate_pcode",
            ", ".join(sorted(group["id"])),
            f"pcode {pcode} claimed by {len(group)} Locations",
        )


def check_points_within_parents(frame, report) -> None:
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
```

Add a matching import guard at the top of `report.py` — these two functions take a GeoDataFrame, so keep `shapely` imported locally as shown to avoid a hard import at module load.

- [ ] **Step 5: Write the CLI**

`src/kiln/cli.py`:

```python
"""kiln command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kiln import __version__
from kiln.extract import (
    fetch_locations,
    read_ndjson,
    resolve_boundary_urls,
    write_ndjson,
)
from kiln.frame import build_frame
from kiln.profile import BOUNDARY_EXTENSION_URL, shred
from kiln.report import Report, check_duplicate_pcodes, check_points_within_parents
from kiln.write import (
    DEFAULT_PARTITION_BY,
    DEFAULT_ROW_GROUP_SIZE,
    GdalUnavailable,
    probe_gdal,
    write_dataset,
)

USAGE_ERROR = 2


def _note_unresolved_boundary_urls(resources: list[dict], report: Report) -> None:
    """transform is offline; a url-only boundary cannot be fetched here."""
    for resource in resources:
        for extension in resource.get("extension") or []:
            if extension.get("url") != BOUNDARY_EXTENSION_URL:
                continue
            attachment = extension.get("valueAttachment") or {}
            if attachment.get("url") and not attachment.get("data"):
                report.add(
                    "boundary_unresolved_url",
                    resource.get("id", "<unknown>"),
                    f"{attachment['url']} — run `kiln extract` to inline it",
                )


def cmd_extract(args: argparse.Namespace) -> int:
    report = Report()
    resources = list(fetch_locations(args.server, args.token, since=args.since))
    resolve_boundary_urls(resources, report, token=args.token)
    count = write_ndjson(resources, Path(args.out))
    print(f"Wrote {count} Locations to {args.out}")
    print(report.summary())
    return 0


def cmd_transform(args: argparse.Namespace) -> int:
    source = Path(args.input)
    if not source.exists():
        print(f"Input file not found: {source}", file=sys.stderr)
        return USAGE_ERROR

    try:
        probe_gdal()
    except GdalUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return USAGE_ERROR

    report = Report()
    resources = list(read_ndjson(source))
    _note_unresolved_boundary_urls(resources, report)

    locations = [loc for loc in (shred(r, report) for r in resources) if loc is not None]
    frame = build_frame(locations, report, country_override=args.country)

    if not frame.empty:
        check_duplicate_pcodes(frame, report)
        check_points_within_parents(frame, report)

    out_dir = Path(args.out)
    written = write_dataset(
        frame,
        out_dir,
        report,
        partition_by=tuple(args.partition_by.split(",")),
        row_group_size=args.row_group_size,
        geo_types=args.geo_types,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_report.json").write_text(json.dumps(report.to_dict(), indent=2))

    print(f"Wrote {len(frame)} rows across {len(written)} partitions to {out_dir}")
    print(report.summary())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ndjson = Path(args.out) / "locations.ndjson"
    extract_args = argparse.Namespace(
        server=args.server, token=args.token, since=args.since, out=str(ndjson)
    )
    code = cmd_extract(extract_args)
    if code != 0:
        return code
    args.input = str(ndjson)
    return cmd_transform(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kiln", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_transform_options(sub):
        sub.add_argument("--out", required=True, help="Output directory")
        sub.add_argument("--country", default=None, help="Override the derived country code")
        sub.add_argument(
            "--geo-types",
            dest="geo_types",
            choices=["both", "only", "legacy"],
            default="both",
        )
        sub.add_argument("--partition-by", default=",".join(DEFAULT_PARTITION_BY))
        sub.add_argument("--row-group-size", type=int, default=DEFAULT_ROW_GROUP_SIZE)

    extract = subparsers.add_parser("extract", help="Fetch Locations from a FHIR server")
    extract.add_argument("--server", required=True)
    extract.add_argument("--token", default=None)
    extract.add_argument("--since", default=None)
    extract.add_argument("--out", required=True)
    extract.set_defaults(func=cmd_extract)

    transform = subparsers.add_parser("transform", help="Convert NDJSON to GeoParquet")
    transform.add_argument("--in", dest="input", required=True)
    add_transform_options(transform)
    transform.set_defaults(func=cmd_transform)

    run = subparsers.add_parser("run", help="Extract then transform")
    run.add_argument("--server", required=True)
    run.add_argument("--token", default=None)
    run.add_argument("--since", default=None)
    add_transform_options(run)
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    import os

    if getattr(args, "token", None) is None:
        args.token = os.environ.get("KILN_TOKEN")

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -v`
Expected: all tests pass

- [ ] **Step 7: Lint**

Run: `uv run ruff check .`
Expected: no findings. Fix any that appear.

- [ ] **Step 8: Commit**

```bash
git add src/kiln/cli.py src/kiln/report.py tests/fixtures tests/test_cli.py
git commit -m "Add CLI with end-to-end fixture coverage"
```

---

### Task 9: Inspect command

**Files:**
- Create: `src/kiln/inspect.py`
- Modify: `src/kiln/cli.py` (register the `inspect` subcommand)
- Test: `tests/test_inspect.py`

**Interfaces:**
- Consumes: output of Task 6.
- Produces: `summarize(out_dir: Path) -> dict` with keys `partitions` (list of `{path, rows, row_groups, avg_row_group_rows, geometry_types, geo_version, has_covering}`) and `totals` (`{rows, partitions, files}`). Plus `cmd_inspect(args) -> int`.

- [ ] **Step 1: Write the failing test**

`tests/test_inspect.py`:

```python
import geopandas as gpd
import numpy as np
import pytest
import shapely

from kiln.inspect import summarize
from kiln.report import Report
from kiln.write import GdalUnavailable, probe_gdal, write_dataset


@pytest.fixture(autouse=True)
def require_gdal():
    try:
        probe_gdal()
    except GdalUnavailable as exc:
        pytest.skip(str(exc))


@pytest.fixture
def dataset(tmp_path):
    rng = np.random.default_rng(0)
    points = [shapely.Point(x, y) for x, y in rng.random((300, 2)) * 10]
    frame = gpd.GeoDataFrame(
        [{"id": f"p{i}", "country": "NG", "geom_type": "point", "tier": "site"}
         for i in range(300)],
        geometry=points,
        crs="EPSG:4326",
    )
    write_dataset(frame, tmp_path, Report(), row_group_size=100)
    return tmp_path


def test_summarize_counts_rows_and_partitions(dataset):
    summary = summarize(dataset)

    assert summary["totals"]["rows"] == 300
    assert summary["totals"]["partitions"] == 1


def test_summarize_reports_row_group_layout(dataset):
    partition = summarize(dataset)["partitions"][0]

    assert partition["row_groups"] == 3
    assert partition["avg_row_group_rows"] == 100


def test_summarize_reports_geo_metadata(dataset):
    partition = summarize(dataset)["partitions"][0]

    assert partition["geo_version"] == "1.1.0"
    assert partition["has_covering"] is True
    assert partition["geometry_types"] == ["Point"]


def test_summarize_on_an_empty_directory_returns_zeroes(tmp_path):
    summary = summarize(tmp_path)

    assert summary["totals"] == {"rows": 0, "partitions": 0, "files": 0}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_inspect.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kiln.inspect'`

- [ ] **Step 3: Write the implementation**

`src/kiln/inspect.py`:

```python
"""Summarize a written dataset: partitions, row groups and geo metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq


def summarize(out_dir: Path) -> dict:
    """Describe every parquet file under `out_dir`."""
    out_dir = Path(out_dir)
    partitions: list[dict] = []
    total_rows = 0

    for path in sorted(out_dir.rglob("*.parquet")):
        parquet = pq.ParquetFile(path)
        metadata = parquet.metadata
        geo = {}
        raw = (metadata.metadata or {}).get(b"geo")
        if raw:
            geo = json.loads(raw)
        primary = geo.get("primary_column", "geometry")
        column_meta = geo.get("columns", {}).get(primary, {})

        rows = metadata.num_rows
        groups = metadata.num_row_groups
        total_rows += rows

        partitions.append(
            {
                "path": path.relative_to(out_dir).as_posix(),
                "rows": rows,
                "row_groups": groups,
                "avg_row_group_rows": round(rows / groups) if groups else 0,
                "geometry_types": column_meta.get("geometry_types", []),
                "geo_version": geo.get("version"),
                "has_covering": "covering" in column_meta,
            }
        )

    return {
        "partitions": partitions,
        "totals": {
            "rows": total_rows,
            "partitions": len(partitions),
            "files": len(partitions),
        },
    }


def format_summary(summary: dict) -> str:
    if not summary["partitions"]:
        return "No parquet files found."
    lines = [
        f"{summary['totals']['rows']} rows across "
        f"{summary['totals']['partitions']} partitions",
        "",
    ]
    for partition in summary["partitions"]:
        lines.append(
            f"  {partition['path']}\n"
            f"    rows={partition['rows']} "
            f"row_groups={partition['row_groups']} "
            f"avg={partition['avg_row_group_rows']}\n"
            f"    geo={partition['geo_version']} "
            f"covering={partition['has_covering']} "
            f"types={','.join(partition['geometry_types'])}"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Register the subcommand**

In `src/kiln/cli.py`, add the import:

```python
from kiln.inspect import format_summary, summarize
```

Add the command function above `build_parser`:

```python
def cmd_inspect(args: argparse.Namespace) -> int:
    target = Path(args.out)
    if not target.exists():
        print(f"Directory not found: {target}", file=sys.stderr)
        return USAGE_ERROR
    print(format_summary(summarize(target)))
    return 0
```

And register it inside `build_parser`, just before `return parser`:

```python
    inspect_cmd = subparsers.add_parser("inspect", help="Summarize a written dataset")
    inspect_cmd.add_argument("--out", required=True, help="Dataset directory")
    inspect_cmd.set_defaults(func=cmd_inspect)
```

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all tests pass, no lint findings

- [ ] **Step 6: Commit**

```bash
git add src/kiln/inspect.py src/kiln/cli.py tests/test_inspect.py
git commit -m "Add inspect command for dataset summaries"
```

---

### Task 10: README

**Files:**
- Create: `README.md`
- Test: manual — the commands in the README must run as written.

**Interfaces:**
- Consumes: the finished CLI.
- Produces: nothing consumed by other tasks.

- [ ] **Step 1: Write the README**

`README.md`:

```markdown
# kiln

Export FHIR `Location` resources to partitioned GeoParquet — native Parquet
geometry types, per-row covering bbox, Hilbert-sorted row groups.

## Requirements

- Python 3.11+
- **System GDAL 3.13+** with a Parquet driver (`brew install gdal`). kiln probes
  for it at startup and fails with a clear message if it is missing or too old.
  GDAL is the only writer that emits native Parquet geometry types.

## Install

```bash
uv sync
```

## Usage

One country per run.

```bash
# Fetch Locations, resolving any url-referenced boundary attachments
kiln extract \
  --server https://healthcare.googleapis.com/v1/projects/.../fhir \
  --token "$(gcloud auth print-access-token)" \
  --out locations.ndjson

# Convert to partitioned GeoParquet (offline — no network)
kiln transform --in locations.ndjson --out out/

# Or both at once
kiln run --server ... --token ... --out out/

# Check what was written
kiln inspect --out out/
```

`--token` falls back to `$KILN_TOKEN`.

## Output layout

```
out/locations/country=NG/geom_type=polygon/tier=0/part-0.parquet
out/locations/country=NG/geom_type=polygon/tier=1/part-0.parquet
out/locations/country=NG/geom_type=point/tier=site/part-0.parquet
out/_report.json
```

`tier` is the admin level for admin-units, `site` for everything else. Every file
holds a single geometry type.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--country` | derived from the root admin-unit's pcode | Override the country partition value |
| `--geo-types` | `both` | `both` = native types + 1.1 metadata; `only` = native only; `legacy` = WKB |
| `--partition-by` | `country,geom_type,tier` | Hive partition keys |
| `--row-group-size` | `20000` | Rows per Parquet row group |

## Data quality

`out/_report.json` records orphans, cycles, duplicate pcodes, Locations with no
geometry, unresolvable boundaries, repaired geometries, and sites whose point
falls outside their nearest admin ancestor's polygon.

## Checking the output

[geopq-workbench](https://github.com/gsueur/geopq-workbench) opens the output
directory as a single layer and has a quality gate for exactly the properties
kiln targets — spatial index present, row-group clustering, sensible row-group
sizes.
```

- [ ] **Step 2: Verify the commands run**

Run: `uv run kiln --help` and `uv run kiln inspect --out out/` against a real transform output.
Expected: help text matches the README; inspect prints a summary.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Add README"
```

---

## Deferred

Recorded here so they are not silently dropped:

- **PMTiles baking.** Non-goal for v1. `tier` maps to tile zoom ranges, so baking is a selection over existing partitions.
- **Bulk `$export`.** A third front-end behind the NDJSON seam.
- **pcode-derived or config-driven admin levels.** `resolve_hierarchy` derives level from tree depth, which is not comparable across countries. Only matters if multi-country rollups arrive.
- **Multi-country runs.** The `country=` partition key already accommodates merging separate runs into one tree.
