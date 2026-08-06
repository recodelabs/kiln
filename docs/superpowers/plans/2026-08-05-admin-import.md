# kiln bake + load (GeoJSON admin boundaries into FHIR) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two new kiln subcommands — `bake` (offline: one-level GeoJSON → ICRLocation-profiled NDJSON, minting ancestor admin units) and `load` (network: NDJSON → FHIR store via idempotent PUT transaction bundles).

**Architecture:** Mirrors the existing export pipeline's offline/network split. `bake.py` is pure (no network), delegating all FHIR-shape knowledge to `profile.py`'s new `build_location()` (the write-side twin of `shred()`). `load.py` is the only new networked module, reusing `extract.py`'s header/backoff helpers. Spec: `docs/superpowers/specs/2026-08-05-admin-import-design.md`.

**Tech Stack:** Python 3.11+, httpx (+ `httpx.MockTransport` in tests), shapely 2.x, argparse, pytest. No new dependencies.

## Global Constraints

- No new dependencies in `pyproject.toml` (shapely, httpx, pytest already present).
- Ruff: line length 100, rules `E,F,B`. Run `uv run ruff check src tests` before each commit.
- Only `extract.py` and `load.py` may touch the network. `bake.py` must be importable and testable with no network and no GDAL.
- All profile-specific knowledge (URLs, systems, resource shapes) lives in `profile.py` — `bake.py` and `load.py` never hard-code a FHIR URL or system.
- Data problems → `Report` (run continues); systematic/mapping problems → typed exceptions (`BakeError`, `LoadError`) that the CLI turns into exit code 2 (`USAGE_ERROR`), matching `BoundaryFetchAborted` handling.
- Boundary extension: **write** the ICR URL (`https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson`), **read** both it and the HL7 canonical (`http://hl7.org/fhir/StructureDefinition/location-boundary-geojson`).
- Tests: plain pytest functions named `test_<behavior>`, httpx mocked with `MockTransport` handlers, `tmp_path` for files — the style of `tests/test_extract.py`.
- Run the full suite with `uv run pytest -q`; it must stay green at every commit.

---

### Task 1: `profile.build_location()` — the write-side twin of `shred()`

**Files:**
- Modify: `src/kiln/profile.py` (add constants + one function; do not touch `shred`)
- Test: `tests/test_profile.py` (append)

**Interfaces:**
- Produces (used by Tasks 4, 5, 9):
  ```python
  ICR_LOCATION_PROFILE_URL = "https://icr.healthcampaigns.org/StructureDefinition/ICRLocation"
  LOCATION_TYPE_SYSTEM = "https://icr.healthcampaigns.org/CodeSystem/icr-location-type"
  PHYSICAL_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/location-physical-type"
  NATIONAL_ADMIN_CODE_SYSTEM = "https://icr.healthcampaigns.org/identifiers/national-admin-code"

  def build_location(
      location_id: str,
      name: str,
      *,
      parent_id: str | None = None,
      identifiers: list[tuple[str, str]],   # (system, value), must be non-empty
      aliases: list[str] | None = None,
      boundary_geojson: bytes | None = None,  # compact GeoJSON geometry bytes
  ) -> dict: ...
  ```

- [ ] **Step 1: Write the failing tests** (append to `tests/test_profile.py`)

```python
import base64

from kiln.profile import (
    BOUNDARY_EXTENSION_URL,
    GEOJSON_CONTENT_TYPE,
    ICR_LOCATION_PROFILE_URL,
    LOCATION_TYPE_SYSTEM,
    NATIONAL_ADMIN_CODE_SYSTEM,
    PHYSICAL_TYPE_SYSTEM,
    build_location,
)

WARD_GEOJSON = b'{"type":"Polygon","coordinates":[[[3,6],[4,6],[4,7],[3,7],[3,6]]]}'


def test_build_location_produces_a_profiled_admin_unit():
    resource = build_location(
        "nga-ba",
        "Bauchi",
        parent_id="nga",
        identifiers=[(NATIONAL_ADMIN_CODE_SYSTEM, "BA")],
    )

    assert resource["resourceType"] == "Location"
    assert resource["id"] == "nga-ba"
    assert resource["meta"]["profile"] == [ICR_LOCATION_PROFILE_URL]
    assert resource["name"] == "Bauchi"
    assert resource["status"] == "active"
    assert resource["type"][0]["coding"][0] == {
        "system": LOCATION_TYPE_SYSTEM,
        "code": "admin-unit",
    }
    assert resource["physicalType"]["coding"][0]["system"] == PHYSICAL_TYPE_SYSTEM
    assert resource["physicalType"]["coding"][0]["code"] == "jdn"
    assert resource["partOf"] == {"reference": "Location/nga"}
    assert resource["identifier"] == [
        {"system": NATIONAL_ADMIN_CODE_SYSTEM, "value": "BA"}
    ]
    assert "alias" not in resource
    assert "extension" not in resource


def test_build_location_root_has_no_partof():
    resource = build_location(
        "nga", "Nigeria", identifiers=[(NATIONAL_ADMIN_CODE_SYSTEM, "NGA")]
    )
    assert "partOf" not in resource


def test_build_location_inlines_the_boundary_as_base64():
    resource = build_location(
        "nga-ba-alkaleri-alkaleri-east",
        "Alkaleri East",
        parent_id="nga-ba-alkaleri",
        identifiers=[(NATIONAL_ADMIN_CODE_SYSTEM, "nga-ba-alkaleri-alkaleri-east")],
        aliases=["Alkaleri E."],
        boundary_geojson=WARD_GEOJSON,
    )

    assert resource["alias"] == ["Alkaleri E."]
    (extension,) = resource["extension"]
    assert extension["url"] == BOUNDARY_EXTENSION_URL
    attachment = extension["valueAttachment"]
    assert attachment["contentType"] == GEOJSON_CONTENT_TYPE
    assert base64.b64decode(attachment["data"]) == WARD_GEOJSON


def test_build_location_requires_at_least_one_identifier():
    import pytest

    with pytest.raises(ValueError):
        build_location("nga", "Nigeria", identifiers=[])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_profile.py -q`
Expected: FAIL — `ImportError: cannot import name 'ICR_LOCATION_PROFILE_URL'`

- [ ] **Step 3: Implement in `src/kiln/profile.py`**

Add below the existing constants:

```python
ICR_LOCATION_PROFILE_URL = "https://icr.healthcampaigns.org/StructureDefinition/ICRLocation"
LOCATION_TYPE_SYSTEM = "https://icr.healthcampaigns.org/CodeSystem/icr-location-type"
PHYSICAL_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/location-physical-type"
NATIONAL_ADMIN_CODE_SYSTEM = "https://icr.healthcampaigns.org/identifiers/national-admin-code"
```

Add at the end of the module:

```python
def build_location(
    location_id: str,
    name: str,
    *,
    parent_id: str | None = None,
    identifiers: list[tuple[str, str]],
    aliases: list[str] | None = None,
    boundary_geojson: bytes | None = None,
) -> dict:
    """Build one admin-unit Location conforming to ICRLocation.

    The write-side twin of `shred`: bake constructs resources only through
    this function, so the emitted shape lives next to the parsed one. Every
    admin unit must carry >=1 identifier (IG rule icr-loc-admin-id), so an
    empty `identifiers` is a programming error, not a data problem.
    """
    if not identifiers:
        raise ValueError(f"admin unit {location_id!r} must carry at least one identifier")

    resource: dict = {
        "resourceType": "Location",
        "id": location_id,
        "meta": {"profile": [ICR_LOCATION_PROFILE_URL]},
        "name": name,
        "status": "active",
        "type": [
            {
                "coding": [
                    {"system": LOCATION_TYPE_SYSTEM, "code": ADMIN_UNIT_TYPE}
                ]
            }
        ],
        "physicalType": {
            "coding": [
                {
                    "system": PHYSICAL_TYPE_SYSTEM,
                    "code": "jdn",
                    "display": "Jurisdiction",
                }
            ]
        },
        "identifier": [
            {"system": system, "value": value} for system, value in identifiers
        ],
    }
    if parent_id:
        resource["partOf"] = {"reference": f"Location/{parent_id}"}
    if aliases:
        resource["alias"] = list(aliases)
    if boundary_geojson is not None:
        resource["extension"] = [
            {
                "url": BOUNDARY_EXTENSION_URL,
                "valueAttachment": {
                    "contentType": GEOJSON_CONTENT_TYPE,
                    "data": base64.b64encode(boundary_geojson).decode(),
                },
            }
        ]
    return resource
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_profile.py -q`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests && uv run pytest -q
git add src/kiln/profile.py tests/test_profile.py
git commit -m "feat: profile.build_location, the write-side twin of shred"
```

---

### Task 2: Read both boundary-extension URLs (ICR + HL7 canonical)

**Files:**
- Modify: `src/kiln/profile.py` (constant + `shred`'s extension loop)
- Modify: `src/kiln/extract.py` (`_collect_boundary_work`)
- Modify: `src/kiln/cli.py` (`_note_unresolved_boundary_urls`)
- Test: `tests/test_profile.py`, `tests/test_extract.py` (append)

**Interfaces:**
- Produces (used by Task 9's round-trip test):
  ```python
  HL7_BOUNDARY_EXTENSION_URL = "http://hl7.org/fhir/StructureDefinition/location-boundary-geojson"
  BOUNDARY_EXTENSION_URLS: tuple[str, str]  # (ICR, HL7) — the read set
  ```
  `BOUNDARY_EXTENSION_URL` (singular) stays and remains the **write** URL.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_profile.py`:

```python
from kiln.profile import HL7_BOUNDARY_EXTENSION_URL, shred
from kiln.report import Report


def test_shred_reads_a_boundary_under_the_hl7_canonical_url():
    resource = {
        "resourceType": "Location",
        "id": "w1",
        "extension": [
            {
                "url": HL7_BOUNDARY_EXTENSION_URL,
                "valueAttachment": {
                    "contentType": "application/geo+json",
                    "data": base64.b64encode(WARD_GEOJSON).decode(),
                },
            }
        ],
    }
    raw = shred(resource, Report())
    assert raw.boundary is not None
    assert raw.boundary.data == WARD_GEOJSON
```

Append to `tests/test_extract.py`:

```python
def test_resolve_fetches_a_boundary_under_the_hl7_canonical_url():
    from kiln.profile import HL7_BOUNDARY_EXTENSION_URL

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resource = {
        "id": "w1",
        "extension": [
            {
                "url": HL7_BOUNDARY_EXTENSION_URL,
                "valueAttachment": {"url": "https://geo.test/w1.geojson"},
            }
        ],
    }
    resolve_boundary_urls([resource], Report(), client=client)

    attachment = resource["extension"][0]["valueAttachment"]
    assert base64.b64decode(attachment["data"]) == GEOJSON
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_profile.py tests/test_extract.py -q`
Expected: FAIL — `ImportError: cannot import name 'HL7_BOUNDARY_EXTENSION_URL'`

- [ ] **Step 3: Implement**

`src/kiln/profile.py` — below `BOUNDARY_EXTENSION_URL`:

```python
HL7_BOUNDARY_EXTENSION_URL = "http://hl7.org/fhir/StructureDefinition/location-boundary-geojson"
# Read set: kiln writes the ICR URL but reads both, so a later IG switch to
# the HL7 canonical strands no data (spec 2026-08-05, extension-URL policy).
BOUNDARY_EXTENSION_URLS = (BOUNDARY_EXTENSION_URL, HL7_BOUNDARY_EXTENSION_URL)
```

In `shred`'s extension loop change `if url == BOUNDARY_EXTENSION_URL and raw.boundary is None:` to `if url in BOUNDARY_EXTENSION_URLS and raw.boundary is None:`.

`src/kiln/extract.py` — import `BOUNDARY_EXTENSION_URLS` instead of `BOUNDARY_EXTENSION_URL`; in `_collect_boundary_work` change `if extension.get("url") != BOUNDARY_EXTENSION_URL:` to `if extension.get("url") not in BOUNDARY_EXTENSION_URLS:`.

`src/kiln/cli.py` — import `BOUNDARY_EXTENSION_URLS` instead of `BOUNDARY_EXTENSION_URL`; in `_note_unresolved_boundary_urls` change the URL test to `extension.get("url") not in BOUNDARY_EXTENSION_URLS`.

- [ ] **Step 4: Run the full suite** (guards against the import changes breaking extract/cli)

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/kiln/profile.py src/kiln/extract.py src/kiln/cli.py tests/
git commit -m "feat: read boundary extension under both ICR and HL7 canonical URLs"
```

---

### Task 3: `bake.py` — slugify and CLI-mapping parsers

**Files:**
- Create: `src/kiln/bake.py`
- Create: `tests/test_bake.py`

**Interfaces:**
- Produces (used by Tasks 4, 5, 8):
  ```python
  class BakeError(ValueError): ...   # fatal mapping/input problem -> CLI exit 2

  @dataclass(frozen=True)
  class LevelSpec:
      name: str        # level label, e.g. "state"
      name_prop: str   # feature property holding the unit name
      code_prop: str | None  # optional property holding the unit code

  def slugify(text: str) -> str
  def parse_country_arg(arg: str) -> tuple[str, str]     # "Nigeria=NGA" -> ("Nigeria", "NGA")
  def parse_level_arg(arg: str) -> LevelSpec             # "state=state:statecode"
  def parse_alias_args(args: list[str]) -> dict[str, str]  # ["lga=lga_alt_names"] -> {"lga": ...}
  ```

- [ ] **Step 1: Write the failing tests** (`tests/test_bake.py`)

```python
import pytest

from kiln.bake import (
    BakeError,
    LevelSpec,
    parse_alias_args,
    parse_country_arg,
    parse_level_arg,
    slugify,
)


def test_slugify_lowercases_folds_and_hyphenates():
    assert slugify("Alkaleri East") == "alkaleri-east"
    assert slugify("Grand-Popo / Centre") == "grand-popo-centre"
    assert slugify("  N'Djaména ") == "n-djamena"


def test_slugify_collapses_runs_and_strips_edges():
    assert slugify("A  --  B") == "a-b"


def test_parse_country_arg():
    assert parse_country_arg("Nigeria=NGA") == ("Nigeria", "NGA")


def test_parse_country_arg_rejects_missing_code():
    with pytest.raises(BakeError):
        parse_country_arg("Nigeria")


def test_parse_level_arg_with_and_without_code():
    assert parse_level_arg("state=state:statecode") == LevelSpec(
        name="state", name_prop="state", code_prop="statecode"
    )
    assert parse_level_arg("ward=ward") == LevelSpec(
        name="ward", name_prop="ward", code_prop=None
    )


def test_parse_level_arg_rejects_bad_shapes():
    for bad in ("ward", "ward=", "=ward", "ward=a:b:c"):
        with pytest.raises(BakeError):
            parse_level_arg(bad)


def test_parse_alias_args():
    assert parse_alias_args(["lga=lga_alt_names", "ward=ward_alt_names"]) == {
        "lga": "lga_alt_names",
        "ward": "ward_alt_names",
    }
    with pytest.raises(BakeError):
        parse_alias_args(["lga"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bake.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kiln.bake'`

- [ ] **Step 3: Create `src/kiln/bake.py`**

```python
"""Offline: one-level GeoJSON FeatureCollection -> ICRLocation NDJSON resources.

The import mirror of frame.py's transform: pure, no network, no GDAL.
All FHIR-shape knowledge is delegated to `kiln.profile.build_location`.
Spec: docs/superpowers/specs/2026-08-05-admin-import-design.md.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


class BakeError(ValueError):
    """Fatal input/mapping problem: nothing is written, CLI exits 2.

    Distinct from per-feature data issues, which go to `Report` and skip
    only the affected feature. A BakeError means the *run* is wrong -- a
    mapping typo, the wrong CRS, a slug collision that would silently
    merge two admin units -- and proceeding would bake bad structure into
    every emitted resource.
    """


@dataclass(frozen=True)
class LevelSpec:
    """One `--level name=name_prop[:code_prop]` mapping flag, parsed."""

    name: str
    name_prop: str
    code_prop: str | None


_HYPHEN_RUNS = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase, ASCII-fold, non-alphanumerics -> '-', collapsed and stripped."""
    folded = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    return _HYPHEN_RUNS.sub("-", folded.lower()).strip("-")


def parse_country_arg(arg: str) -> tuple[str, str]:
    """`Nigeria=NGA` -> (name, code)."""
    name, sep, code = arg.partition("=")
    if not sep or not name or not code:
        raise BakeError(f"--country must be NAME=CODE (e.g. 'Nigeria=NGA'), got {arg!r}")
    return name, code


def parse_level_arg(arg: str) -> LevelSpec:
    """`state=state:statecode` or `ward=ward` -> LevelSpec."""
    name, sep, props = arg.partition("=")
    if not sep or not name or not props:
        raise BakeError(
            f"--level must be LEVEL=NAME_PROP[:CODE_PROP] (e.g. 'state=state:statecode'), "
            f"got {arg!r}"
        )
    parts = props.split(":")
    if len(parts) == 1:
        return LevelSpec(name=name, name_prop=parts[0], code_prop=None)
    if len(parts) == 2 and parts[0] and parts[1]:
        return LevelSpec(name=name, name_prop=parts[0], code_prop=parts[1])
    raise BakeError(f"--level {arg!r}: expected at most one ':' separating NAME_PROP:CODE_PROP")


def parse_alias_args(args: list[str]) -> dict[str, str]:
    """`['lga=lga_alt_names']` -> {'lga': 'lga_alt_names'}."""
    aliases: dict[str, str] = {}
    for arg in args:
        level, sep, prop = arg.partition("=")
        if not sep or not level or not prop:
            raise BakeError(f"--alias must be LEVEL=PROPERTY, got {arg!r}")
        aliases[level] = prop
    return aliases
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_bake.py -q`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests && uv run pytest -q
git add src/kiln/bake.py tests/test_bake.py
git commit -m "feat: bake slugify and CLI level-mapping parsers"
```

---

### Task 4: `bake.py` — CRS check and geometry normalization

**Files:**
- Modify: `src/kiln/bake.py`
- Test: `tests/test_bake.py` (append)

**Interfaces:**
- Consumes: `BakeError` (Task 3), `Report` (`kiln.report`).
- Produces (used by Task 5):
  ```python
  def check_crs(collection: dict) -> None       # raises BakeError on a non-4326 CRS
  def normalize_geometry(geometry: dict | None, location_id: str, report: Report) -> bytes | None
  ```
  `normalize_geometry` returns compact RFC 7946 GeoJSON bytes (rings rewound:
  exterior CCW, holes CW) for a valid Polygon/MultiPolygon, else reports
  `geometry_invalid` and returns None.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_bake.py`)

```python
import json

from kiln.bake import check_crs, normalize_geometry
from kiln.report import Report

# Exterior ring wound clockwise on purpose: RFC 7946 requires counterclockwise,
# so normalize_geometry must rewind it.
CW_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[3.0, 6.0], [3.0, 7.0], [4.0, 7.0], [4.0, 6.0], [3.0, 6.0]]],
}


def test_check_crs_accepts_missing_and_4326_spellings():
    check_crs({"type": "FeatureCollection", "features": []})  # absent = default
    for name in (
        "EPSG:4326",
        "urn:ogc:def:crs:EPSG::4326",
        "urn:ogc:def:crs:OGC:1.3:CRS84",
        "CRS84",
    ):
        check_crs(
            {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": name}},
                "features": [],
            }
        )


def test_check_crs_rejects_anything_else():
    import pytest
    from kiln.bake import BakeError

    with pytest.raises(BakeError):
        check_crs(
            {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
                "features": [],
            }
        )


def test_normalize_geometry_rewinds_rings_to_rfc7946():
    report = Report()
    payload = normalize_geometry(CW_SQUARE, "w1", report)

    assert payload is not None
    ring = json.loads(payload)["coordinates"][0]
    # Shoelace: positive area = counterclockwise exterior.
    area2 = sum(
        (x1 * y2 - x2 * y1)
        for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False)
    )
    assert area2 > 0
    assert report.counts() == {}


def test_normalize_geometry_reports_invalid_and_returns_none():
    report = Report()
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]],
    }
    assert normalize_geometry(bowtie, "w1", report) is None
    assert report.counts() == {"geometry_invalid": 1}


def test_normalize_geometry_reports_non_polygon_and_missing():
    report = Report()
    point = {"type": "Point", "coordinates": [3.0, 6.0]}
    assert normalize_geometry(point, "w1", report) is None
    assert normalize_geometry(None, "w2", report) is None
    assert report.counts() == {"geometry_invalid": 2}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bake.py -q`
Expected: FAIL — `ImportError: cannot import name 'check_crs'`

- [ ] **Step 3: Implement** (append to `src/kiln/bake.py`)

```python
import json

import shapely
import shapely.geometry

from kiln.report import Report

# GeoJSON's only legal CRS is WGS84 lon/lat (RFC 7946 killed the `crs`
# member); these are the spellings of it seen in the wild. Anything else
# means the coordinates are in a projected system and every geometry would
# be garbage -- fatal, not per-feature.
_ACCEPTED_CRS_NAMES = frozenset(
    {
        "EPSG:4326",
        "urn:ogc:def:crs:EPSG::4326",
        "urn:ogc:def:crs:OGC:1.3:CRS84",
        "CRS84",
        "OGC:CRS84",
    }
)


def check_crs(collection: dict) -> None:
    """Raise BakeError unless the collection is (explicitly or by default) WGS84."""
    crs = collection.get("crs")
    if crs is None:
        return
    name = ""
    if isinstance(crs, dict):
        properties = crs.get("properties")
        if isinstance(properties, dict):
            name = properties.get("name", "")
    if name not in _ACCEPTED_CRS_NAMES:
        raise BakeError(
            f"input CRS {name!r} is not WGS84 (EPSG:4326); reproject the file first, "
            "e.g.: ogr2ogr -t_srs EPSG:4326 out.geojson in.geojson"
        )


def normalize_geometry(
    geometry: dict | None, location_id: str, report: Report
) -> bytes | None:
    """Validate and rewind one feature geometry; None (reported) if unusable.

    Only Polygon/MultiPolygon are boundaries. Output is compact RFC 7946
    GeoJSON bytes with exterior rings counterclockwise and holes clockwise
    (shapely's `orient(sign=1.0)` convention, applied per polygon).
    """
    if not isinstance(geometry, dict) or geometry.get("type") not in (
        "Polygon",
        "MultiPolygon",
    ):
        kind = geometry.get("type") if isinstance(geometry, dict) else None
        report.add(
            "geometry_invalid",
            location_id,
            f"geometry is {kind!r}, expected Polygon or MultiPolygon",
        )
        return None

    try:
        shape = shapely.geometry.shape(geometry)
    except (ValueError, TypeError) as exc:
        report.add("geometry_invalid", location_id, f"unparseable geometry: {exc}")
        return None

    if shape.is_empty or not shape.is_valid:
        reason = "empty geometry" if shape.is_empty else shapely.is_valid_reason(shape)
        report.add("geometry_invalid", location_id, reason)
        return None

    oriented = _orient_rfc7946(shape)
    return json.dumps(
        shapely.geometry.mapping(oriented), separators=(",", ":")
    ).encode()


def _orient_rfc7946(shape):
    """Exterior rings counterclockwise, holes clockwise, per RFC 7946."""
    from shapely.geometry import MultiPolygon
    from shapely.geometry.polygon import orient

    if isinstance(shape, MultiPolygon):
        return MultiPolygon([orient(polygon, sign=1.0) for polygon in shape.geoms])
    return orient(shape, sign=1.0)
```

Move the `import json`, `import shapely`-related lines to the top of the module with the other imports (ruff `E402` will flag them otherwise).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_bake.py -q`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests && uv run pytest -q
git add src/kiln/bake.py tests/test_bake.py
git commit -m "feat: bake CRS check and RFC 7946 geometry normalization"
```

---

### Task 5: `bake.py` — `bake()`: hierarchy minting and resource emission

**Files:**
- Modify: `src/kiln/bake.py`
- Test: `tests/test_bake.py` (append)

**Interfaces:**
- Consumes: `build_location`, `NATIONAL_ADMIN_CODE_SYSTEM` (Task 1); `LevelSpec`, `slugify`, `check_crs`, `normalize_geometry`, `BakeError` (Tasks 3–4).
- Produces (used by Tasks 8, 9):
  ```python
  def bake(
      collection: dict,                 # parsed GeoJSON FeatureCollection
      country: tuple[str, str],         # (name, code) from parse_country_arg
      levels: list[LevelSpec],          # ordered; last level carries geometry
      aliases: dict[str, str],          # level name -> alias property
      code_system: str,                 # identifier system URI
      report: Report,
  ) -> list[dict]                       # Location resources, parents first
  ```
  Guarantees: output order is country, then each level top-down, features last;
  every resource has ≥1 identifier; IDs are slug paths.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_bake.py`)

```python
import pytest

from kiln.bake import bake
from kiln.profile import NATIONAL_ADMIN_CODE_SYSTEM

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[3.0, 6.0], [4.0, 6.0], [4.0, 7.0], [3.0, 7.0], [3.0, 6.0]]],
}

LEVELS = [
    LevelSpec(name="state", name_prop="state", code_prop="statecode"),
    LevelSpec(name="lga", name_prop="lga", code_prop=None),
    LevelSpec(name="ward", name_prop="ward", code_prop=None),
]


def feature(state="Bauchi", statecode="BA", lga="Alkaleri", ward="Alkaleri East",
            geometry=SQUARE, **extra):
    properties = {"state": state, "statecode": statecode, "lga": lga, "ward": ward}
    properties.update(extra)
    return {"type": "Feature", "properties": properties, "geometry": geometry}


def collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def test_bake_mints_the_full_hierarchy_parents_first():
    report = Report()
    resources = bake(
        collection(feature(), feature(ward="Alkaleri West")),
        ("Nigeria", "NGA"),
        LEVELS,
        {},
        NATIONAL_ADMIN_CODE_SYSTEM,
        report,
    )

    ids = [r["id"] for r in resources]
    assert ids == [
        "nga",
        "nga-ba",
        "nga-ba-alkaleri",
        "nga-ba-alkaleri-alkaleri-east",
        "nga-ba-alkaleri-alkaleri-west",
    ]
    by_id = {r["id"]: r for r in resources}
    assert by_id["nga-ba"]["partOf"] == {"reference": "Location/nga"}
    assert by_id["nga-ba"]["name"] == "Bauchi"
    # State code comes from statecode; codeless levels get the slug path.
    assert by_id["nga-ba"]["identifier"] == [
        {"system": NATIONAL_ADMIN_CODE_SYSTEM, "value": "BA"}
    ]
    assert by_id["nga-ba-alkaleri"]["identifier"] == [
        {"system": NATIONAL_ADMIN_CODE_SYSTEM, "value": "nga-ba-alkaleri"}
    ]
    # Only the feature level carries a boundary.
    assert "extension" not in by_id["nga-ba-alkaleri"]
    assert "extension" in by_id["nga-ba-alkaleri-alkaleri-east"]
    assert report.counts() == {}


def test_bake_slug_uses_code_when_the_level_has_one():
    resources = bake(
        collection(feature()),
        ("Nigeria", "NGA"),
        LEVELS,
        {},
        NATIONAL_ADMIN_CODE_SYSTEM,
        Report(),
    )
    assert resources[1]["id"] == "nga-ba"  # from statecode BA, not "bauchi"


def test_bake_splits_alias_properties_on_semicolons():
    resources = bake(
        collection(feature(ward_alt_names="Alkaleri E.; The East")),
        ("Nigeria", "NGA"),
        LEVELS,
        {"ward": "ward_alt_names"},
        NATIONAL_ADMIN_CODE_SYSTEM,
        Report(),
    )
    ward = resources[-1]
    assert ward["alias"] == ["Alkaleri E.", "The East"]


def test_bake_skips_a_feature_missing_a_level_name_and_reports_it():
    report = Report()
    resources = bake(
        collection(feature(), feature(ward="")),
        ("Nigeria", "NGA"),
        LEVELS,
        {},
        NATIONAL_ADMIN_CODE_SYSTEM,
        report,
    )
    assert len([r for r in resources if "extension" in r]) == 1
    assert report.counts() == {"missing_field": 1}


def test_bake_emits_a_boundaryless_unit_for_invalid_geometry():
    report = Report()
    resources = bake(
        collection(feature(geometry=None)),
        ("Nigeria", "NGA"),
        LEVELS,
        {},
        NATIONAL_ADMIN_CODE_SYSTEM,
        report,
    )
    ward = resources[-1]
    assert ward["id"] == "nga-ba-alkaleri-alkaleri-east"
    assert "extension" not in ward
    assert report.counts() == {"geometry_invalid": 1}


def test_bake_rejects_two_distinct_names_colliding_on_one_slug():
    with pytest.raises(BakeError, match="collision"):
        bake(
            collection(feature(ward="Alkaleri East"), feature(ward="Alkaleri  East!")),
            ("Nigeria", "NGA"),
            LEVELS,
            {},
            NATIONAL_ADMIN_CODE_SYSTEM,
            Report(),
        )


def test_bake_rejects_a_duplicate_leaf_feature():
    with pytest.raises(BakeError, match="duplicate"):
        bake(
            collection(feature(), feature()),
            ("Nigeria", "NGA"),
            LEVELS,
            {},
            NATIONAL_ADMIN_CODE_SYSTEM,
            Report(),
        )


def test_bake_rejects_a_level_property_present_on_no_feature():
    with pytest.raises(BakeError, match="wardd"):
        bake(
            collection(feature()),
            ("Nigeria", "NGA"),
            [LEVELS[0], LEVELS[1], LevelSpec("ward", "wardd", None)],
            {},
            NATIONAL_ADMIN_CODE_SYSTEM,
            Report(),
        )


def test_bake_rejects_a_non_feature_collection():
    with pytest.raises(BakeError):
        bake({"type": "Feature"}, ("Nigeria", "NGA"), LEVELS, {},
             NATIONAL_ADMIN_CODE_SYSTEM, Report())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bake.py -q`
Expected: FAIL — `ImportError: cannot import name 'bake'`

- [ ] **Step 3: Implement** (append to `src/kiln/bake.py`; add `from kiln.profile import NATIONAL_ADMIN_CODE_SYSTEM, build_location` to the top imports — `NATIONAL_ADMIN_CODE_SYSTEM` is re-exported for the CLI default in Task 8)

```python
def bake(
    collection: dict,
    country: tuple[str, str],
    levels: list[LevelSpec],
    aliases: dict[str, str],
    code_system: str,
    report: Report,
) -> list[dict]:
    """Turn a one-level FeatureCollection into Location resources, parents first.

    Fatal problems (wrong shape, wrong CRS, a mapping typo, slug collisions)
    raise BakeError before anything is returned. Per-feature data problems
    (missing name, bad geometry) are reported and cost at most that feature's
    boundary or presence -- see the spec's error-handling section.
    """
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        raise BakeError("input is not a GeoJSON FeatureCollection")
    check_crs(collection)
    features = [f for f in collection.get("features", []) if isinstance(f, dict)]

    for level in levels:
        if not any(
            isinstance(f.get("properties"), dict) and f["properties"].get(level.name_prop)
            for f in features
        ):
            raise BakeError(
                f"--level {level.name}: property {level.name_prop!r} is empty on every "
                "feature -- likely a mapping typo"
            )

    country_name, country_code = country
    root_slug = slugify(country_code)
    # slug -> resource, insertion-ordered per level so output is parents-first.
    minted: dict[str, dict] = {
        root_slug: build_location(
            root_slug, country_name, identifiers=[(code_system, country_code)]
        )
    }
    # (parent_slug, child_slug) -> name that claimed it, for collision checks.
    claimed: dict[str, str] = {}
    # Levels are minted top-down into one dict per level, concatenated at the end.
    per_level: list[dict[str, dict]] = [dict() for _ in levels]
    leaf_index = len(levels) - 1

    for feature in features:
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            report.add("missing_field", "<unknown>", "feature has no properties object")
            continue

        names = [str(properties.get(level.name_prop) or "").strip() for level in levels]
        missing = next((levels[i].name for i, n in enumerate(names) if not n), None)
        if missing is not None:
            report.add(
                "missing_field",
                "<unknown>",
                f"feature is missing {missing!r}; skipped: {names!r}",
            )
            continue

        parent_slug = root_slug
        for index, (level, name) in enumerate(zip(levels, names, strict=True)):
            code = None
            if level.code_prop:
                code = str(properties.get(level.code_prop) or "").strip() or None
            segment = slugify(code or name)
            if not segment:
                report.add("missing_field", "<unknown>", f"{level.name} name slugs to nothing")
                break
            slug = f"{parent_slug}-{segment}"

            already = claimed.get(slug)
            if already is not None and already != name:
                raise BakeError(
                    f"slug collision: {level.name} {name!r} and {already!r} both "
                    f"slug to {slug!r} -- disambiguate the source data"
                )
            claimed[slug] = name

            if index == leaf_index:
                if slug in per_level[index]:
                    raise BakeError(
                        f"duplicate feature: {level.name} {name!r} ({slug!r}) appears "
                        "more than once"
                    )
                boundary = normalize_geometry(feature.get("geometry"), slug, report)
                per_level[index][slug] = build_location(
                    slug,
                    name,
                    parent_id=parent_slug,
                    identifiers=[(code_system, code or slug)],
                    aliases=_read_aliases(properties, aliases.get(level.name)),
                    boundary_geojson=boundary,
                )
            elif slug not in per_level[index]:
                per_level[index][slug] = build_location(
                    slug,
                    name,
                    parent_id=parent_slug,
                    identifiers=[(code_system, code or slug)],
                    aliases=_read_aliases(properties, aliases.get(level.name)),
                )
            parent_slug = slug

    resources = list(minted.values())
    for level_resources in per_level:
        resources.extend(level_resources.values())
    return resources


def _read_aliases(properties: dict, alias_prop: str | None) -> list[str]:
    if not alias_prop:
        return []
    raw = str(properties.get(alias_prop) or "")
    return [part.strip() for part in raw.split(";") if part.strip()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_bake.py -q`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests && uv run pytest -q
git add src/kiln/bake.py tests/test_bake.py
git commit -m "feat: bake() mints the admin hierarchy from a one-level GeoJSON"
```

---

### Task 6: `load.py` — parent-first ordering and bundle assembly

**Files:**
- Create: `src/kiln/load.py`
- Create: `tests/test_load.py`

**Interfaces:**
- Consumes: nothing kiln-specific yet (pure functions over resource dicts).
- Produces (used by Tasks 7, 8):
  ```python
  DEFAULT_BATCH_SIZE = 100

  class LoadError(RuntimeError): ...  # systematic load failure -> CLI exit 2

  def order_parents_first(resources: list[dict]) -> list[dict]
  def build_bundles(resources: list[dict], batch_size: int = DEFAULT_BATCH_SIZE) -> list[dict]
  ```
  `build_bundles` orders parents-first, then chunks into FHIR `transaction`
  Bundles whose entries are `PUT Location/<id>`.

- [ ] **Step 1: Write the failing tests** (`tests/test_load.py`)

```python
import pytest

from kiln.load import LoadError, build_bundles, order_parents_first


def location(location_id, parent_id=None):
    resource = {"resourceType": "Location", "id": location_id}
    if parent_id:
        resource["partOf"] = {"reference": f"Location/{parent_id}"}
    return resource


def test_order_parents_first_sorts_children_after_ancestors():
    resources = [
        location("ward", "lga"),
        location("country"),
        location("lga", "state"),
        location("state", "country"),
    ]
    ordered = [r["id"] for r in order_parents_first(resources)]
    assert ordered == ["country", "state", "lga", "ward"]


def test_order_parents_first_treats_external_parents_as_roots():
    # A partOf pointing outside the loaded set (already in the store) is fine.
    resources = [location("ward", "elsewhere"), location("country")]
    ordered = [r["id"] for r in order_parents_first(resources)]
    assert set(ordered) == {"ward", "country"}


def test_order_parents_first_rejects_a_cycle():
    with pytest.raises(LoadError, match="cycle"):
        order_parents_first([location("a", "b"), location("b", "a")])


def test_build_bundles_chunks_put_entries_parents_first():
    resources = [location("ward", "country"), location("country")]
    bundles = build_bundles(resources, batch_size=1)

    assert [b["resourceType"] for b in bundles] == ["Bundle", "Bundle"]
    assert all(b["type"] == "transaction" for b in bundles)
    first_entry = bundles[0]["entry"][0]
    assert first_entry["resource"]["id"] == "country"
    assert first_entry["request"] == {"method": "PUT", "url": "Location/country"}
    assert bundles[1]["entry"][0]["request"]["url"] == "Location/ward"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_load.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kiln.load'`

- [ ] **Step 3: Create `src/kiln/load.py`**

```python
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
        while current is not None and current in by_id and current not in depths:
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

    return sorted(resources, key=lambda r: depth(r["id"]) if r.get("id") else 0)


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_load.py -q`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests && uv run pytest -q
git add src/kiln/load.py tests/test_load.py
git commit -m "feat: load bundle assembly with parent-first ordering"
```

---

### Task 7: `load.py` — capability preflight and the network loop

**Files:**
- Modify: `src/kiln/load.py`
- Test: `tests/test_load.py` (append)

**Interfaces:**
- Consumes: `_headers`, `_backoff_delay`, `_retry_after_seconds`, `TIMEOUT`, `DEFAULT_RETRIES` from `kiln.extract` (same package; documented reuse, not an API).
- Produces (used by Task 8):
  ```python
  def check_update_create(server: str, token: str | None, client: httpx.Client) -> None
  def load(
      resources: list[dict],
      server: str,
      token: str | None,
      client: httpx.Client | None = None,
      retries: int = DEFAULT_RETRIES,
      batch_size: int = DEFAULT_BATCH_SIZE,
  ) -> int   # number of resources upserted; raises LoadError on any failure
  ```

- [ ] **Step 1: Write the failing tests** (append to `tests/test_load.py`)

```python
import httpx

from kiln.load import check_update_create, load

CAPABILITY_OK = {
    "resourceType": "CapabilityStatement",
    "rest": [
        {
            "mode": "server",
            "resource": [
                {"type": "Patient", "updateCreate": False},
                {"type": "Location", "updateCreate": True},
            ],
        }
    ],
}


def capability(update_create):
    payload = {
        "resourceType": "CapabilityStatement",
        "rest": [{"mode": "server", "resource": [{"type": "Location"}]}],
    }
    if update_create is not None:
        payload["rest"][0]["resource"][0]["updateCreate"] = update_create
    return payload


def test_check_update_create_passes_when_the_store_supports_it():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/metadata")
        return httpx.Response(200, json=CAPABILITY_OK)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    check_update_create("https://fhir.test/store/fhir", None, client)


def test_check_update_create_rejects_a_store_without_it():
    for payload in (capability(False), capability(None)):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
        )
        with pytest.raises(LoadError, match="enableUpdateCreate"):
            check_update_create("https://fhir.test/store/fhir", None, client)


def test_load_puts_every_resource_and_returns_the_count():
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json=CAPABILITY_OK)
        assert request.method == "POST"
        import json as json_module

        posted.append(json_module.loads(request.content))
        return httpx.Response(
            200, json={"resourceType": "Bundle", "type": "transaction-response"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resources = [
        {"resourceType": "Location", "id": "country"},
        {"resourceType": "Location", "id": "state",
         "partOf": {"reference": "Location/country"}},
    ]

    count = load(resources, "https://fhir.test/store/fhir", "tok", client=client,
                 batch_size=1)

    assert count == 2
    assert len(posted) == 2
    assert posted[0]["entry"][0]["request"]["url"] == "Location/country"


def test_load_retries_a_503_then_succeeds():
    calls = {"bundle": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json=CAPABILITY_OK)
        calls["bundle"] += 1
        if calls["bundle"] == 1:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"resourceType": "Bundle", "type": "transaction-response"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    count = load(
        [{"resourceType": "Location", "id": "x"}],
        "https://fhir.test/store/fhir",
        None,
        client=client,
    )
    assert count == 1
    assert calls["bundle"] == 2


def test_load_raises_with_the_server_body_after_exhausting_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json=CAPABILITY_OK)
        return httpx.Response(
            400,
            json={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "details": {"text": "bad partOf"}}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(LoadError, match="bad partOf"):
        load(
            [{"resourceType": "Location", "id": "x"}],
            "https://fhir.test/store/fhir",
            None,
            client=client,
        )
```

Also patch the retry sleep so tests stay fast — add at the top of `tests/test_load.py`:

```python
import kiln.load as load_module


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(load_module.time, "sleep", lambda seconds: None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_load.py -q`
Expected: FAIL — `ImportError: cannot import name 'check_update_create'`

- [ ] **Step 3: Implement** (append to `src/kiln/load.py`; extend the top imports)

```python
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
        raise LoadError(f"capability preflight: {url} returned non-JSON: {exc}") from exc

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_load.py -q`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests && uv run pytest -q
git add src/kiln/load.py tests/test_load.py
git commit -m "feat: load() with capability preflight, retry, and all-or-abort bundles"
```

---

### Task 8: CLI wiring — `kiln bake` and `kiln load`

**Files:**
- Modify: `src/kiln/cli.py`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `bake`, `parse_country_arg`, `parse_level_arg`, `parse_alias_args`, `BakeError` (Tasks 3, 5); `load`, `LoadError`, `DEFAULT_BATCH_SIZE` (Tasks 6–7); `NATIONAL_ADMIN_CODE_SYSTEM` (Task 1); existing `write_ndjson`, `read_ndjson`, `Report`, `USAGE_ERROR`, `DEFAULT_RETRIES`.
- Produces: `kiln bake --in F --country N=C --level ... [--alias ...] [--code-system URI] --out F` and `kiln load --server URL [--token T] --in F [--retries N] [--batch-size N]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli.py`, following its existing style — check the file's imports first and reuse its helpers if any)

```python
import json

from kiln.cli import build_parser, main


BAKE_COLLECTION = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"state": "Bauchi", "statecode": "BA",
                           "lga": "Alkaleri", "ward": "Alkaleri East"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[3.0, 6.0], [4.0, 6.0], [4.0, 7.0],
                                 [3.0, 7.0], [3.0, 6.0]]],
            },
        }
    ],
}


def test_bake_writes_profiled_ndjson(tmp_path, capsys):
    source = tmp_path / "wards.geojson"
    source.write_text(json.dumps(BAKE_COLLECTION))
    out = tmp_path / "locations.ndjson"

    code = main([
        "bake", "--in", str(source),
        "--country", "Nigeria=NGA",
        "--level", "state=state:statecode",
        "--level", "lga=lga",
        "--level", "ward=ward",
        "--out", str(out),
    ])

    assert code == 0
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["id"] for r in lines] == [
        "nga", "nga-ba", "nga-ba-alkaleri", "nga-ba-alkaleri-alkaleri-east",
    ]
    assert "Wrote 4 Locations" in capsys.readouterr().out


def test_bake_exits_2_on_a_mapping_typo(tmp_path, capsys):
    source = tmp_path / "wards.geojson"
    source.write_text(json.dumps(BAKE_COLLECTION))

    code = main([
        "bake", "--in", str(source),
        "--country", "Nigeria=NGA",
        "--level", "ward=wardd",
        "--out", str(tmp_path / "out.ndjson"),
    ])

    assert code == 2
    assert "wardd" in capsys.readouterr().err
    assert not (tmp_path / "out.ndjson").exists()


def test_load_reads_ndjson_and_calls_load(tmp_path, monkeypatch, capsys):
    ndjson = tmp_path / "locations.ndjson"
    ndjson.write_text('{"resourceType":"Location","id":"nga"}\n')
    seen = {}

    def fake_load(resources, server, token, retries, batch_size):
        seen.update(resources=list(resources), server=server, token=token,
                    retries=retries, batch_size=batch_size)
        return len(seen["resources"])

    import kiln.cli as cli_module
    monkeypatch.setattr(cli_module, "load", fake_load)

    code = main([
        "load", "--server", "https://fhir.test/fhir",
        "--token", "tok", "--in", str(ndjson),
    ])

    assert code == 0
    assert seen["server"] == "https://fhir.test/fhir"
    assert seen["resources"][0]["id"] == "nga"
    assert "loaded: 1 upserted" in capsys.readouterr().out


def test_load_exits_2_on_load_error(tmp_path, monkeypatch, capsys):
    from kiln.load import LoadError

    ndjson = tmp_path / "locations.ndjson"
    ndjson.write_text('{"resourceType":"Location","id":"nga"}\n')

    import kiln.cli as cli_module

    def failing_load(*args, **kwargs):
        raise LoadError("store does not advertise update-as-create")

    monkeypatch.setattr(cli_module, "load", failing_load)

    code = main(["load", "--server", "https://fhir.test/fhir", "--in", str(ndjson)])

    assert code == 2
    assert "update-as-create" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q`
Expected: FAIL — argparse error `invalid choice: 'bake'`

- [ ] **Step 3: Implement in `src/kiln/cli.py`**

Add imports:

```python
from kiln.bake import (
    BakeError,
    bake,
    parse_alias_args,
    parse_country_arg,
    parse_level_arg,
)
from kiln.load import DEFAULT_BATCH_SIZE, LoadError, load
from kiln.profile import NATIONAL_ADMIN_CODE_SYSTEM
```

Add the command functions (after `cmd_inspect`):

```python
def cmd_bake(args: argparse.Namespace) -> int:
    source = Path(args.input)
    if not source.exists():
        print(f"Input file not found: {source}", file=sys.stderr)
        return USAGE_ERROR

    report = Report()
    try:
        collection = json.loads(source.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"kiln bake: {source} is not valid JSON: {exc}", file=sys.stderr)
        return USAGE_ERROR

    try:
        resources = bake(
            collection,
            parse_country_arg(args.country),
            [parse_level_arg(level) for level in args.level],
            parse_alias_args(args.alias),
            args.code_system,
            report,
        )
    except BakeError as exc:
        # Fatal mapping/input problem: nothing written (same contract as
        # BoundaryFetchAborted in cmd_extract).
        print(f"kiln bake: {exc}", file=sys.stderr)
        return USAGE_ERROR

    count = write_ndjson(resources, Path(args.out))
    print(f"Wrote {count} Locations to {args.out}")
    print(report.summary())
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    source = Path(args.input)
    if not source.exists():
        print(f"Input file not found: {source}", file=sys.stderr)
        return USAGE_ERROR

    report = Report()
    try:
        resources = list(read_ndjson(source, report))
    except MalformedNdjsonError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE_ERROR

    try:
        count = load(
            resources,
            args.server,
            args.token,
            retries=args.retries,
            batch_size=args.batch_size,
        )
    except LoadError as exc:
        print(f"kiln load: {exc}", file=sys.stderr)
        return USAGE_ERROR

    print(f"loaded: {count} upserted")
    print(report.summary())
    return 0
```

Wire the subparsers in `build_parser()` (after the `inspect` block):

```python
    bake_cmd = subparsers.add_parser(
        "bake", help="Convert one-level admin GeoJSON to Location NDJSON"
    )
    bake_cmd.add_argument("--in", dest="input", required=True, help="GeoJSON file")
    bake_cmd.add_argument(
        "--country", required=True, help="Admin0 root as NAME=CODE, e.g. 'Nigeria=NGA'"
    )
    bake_cmd.add_argument(
        "--level",
        action="append",
        required=True,
        help=(
            "LEVEL=NAME_PROP[:CODE_PROP], repeatable and ordered; the last "
            "--level is the feature level and carries the geometry"
        ),
    )
    bake_cmd.add_argument(
        "--alias",
        action="append",
        default=[],
        help="LEVEL=PROPERTY holding ';'-separated alternate names",
    )
    bake_cmd.add_argument(
        "--code-system",
        dest="code_system",
        default=NATIONAL_ADMIN_CODE_SYSTEM,
        help=f"Identifier system URI for admin codes (default: {NATIONAL_ADMIN_CODE_SYSTEM})",
    )
    bake_cmd.add_argument("--out", required=True, help="Output NDJSON file")
    bake_cmd.set_defaults(func=cmd_bake)

    load_cmd = subparsers.add_parser(
        "load", help="Upsert Location NDJSON into a FHIR store"
    )
    load_cmd.add_argument("--server", required=True)
    load_cmd.add_argument("--token", default=None)
    load_cmd.add_argument("--in", dest="input", required=True, help="NDJSON file")
    load_cmd.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Attempts per bundle before giving up (with backoff)",
    )
    load_cmd.add_argument(
        "--batch-size",
        dest="batch_size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Resources per transaction bundle (default: {DEFAULT_BATCH_SIZE})",
    )
    load_cmd.set_defaults(func=cmd_load)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q` then `uv run pytest -q`
Expected: PASS (both)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/kiln/cli.py tests/test_cli.py
git commit -m "feat: kiln bake and kiln load subcommands"
```

---

### Task 9: Round-trip integration test (bake → shred → build_frame)

**Files:**
- Create: `tests/test_roundtrip.py`

**Interfaces:**
- Consumes: `bake`, `LevelSpec` (Tasks 3, 5); existing `kiln.profile.shred`, `kiln.frame.build_frame`.

- [ ] **Step 1: Write the test** (`tests/test_roundtrip.py`)

```python
"""The two directions must agree: what bake writes, transform understands.

bake -> shred -> build_frame exercises the whole offline pipeline with no
server and no GDAL (build_frame is pure; only write_dataset needs ogr2ogr).
"""

import json

from kiln.bake import LevelSpec, bake
from kiln.frame import build_frame
from kiln.profile import NATIONAL_ADMIN_CODE_SYSTEM, shred
from kiln.report import Report

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[3.0, 6.0], [4.0, 6.0], [4.0, 7.0], [3.0, 7.0], [3.0, 6.0]]],
}

COLLECTION = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"state": "Bauchi", "statecode": "BA",
                           "lga": "Alkaleri", "ward": ward},
            "geometry": SQUARE,
        }
        for ward in ("Alkaleri East", "Alkaleri West")
    ],
}


def test_baked_resources_round_trip_through_the_export_pipeline():
    report = Report()
    resources = bake(
        COLLECTION,
        ("Nigeria", "NGA"),
        [
            LevelSpec("state", "state", "statecode"),
            LevelSpec("lga", "lga", None),
            LevelSpec("ward", "ward", None),
        ],
        {},
        NATIONAL_ADMIN_CODE_SYSTEM,
        report,
    )

    locations = [shred(r, report) for r in resources]
    assert all(location is not None for location in locations)

    frame = build_frame([loc for loc in locations if loc], report)

    assert len(frame) == 5  # country + state + lga + 2 wards
    ward = frame[frame["id"] == "nga-ba-alkaleri-alkaleri-east"].iloc[0]
    assert ward["admin0_name"] == "Nigeria"
    assert ward["admin1_name"] == "Bauchi"
    assert ward["admin2_name"] == "Alkaleri"
    assert ward["geom_type"] == "polygon"
    assert ward.geometry.is_valid and not ward.geometry.is_empty
    assert report.counts() == {}
```

Before finalizing, check `build_frame`'s actual signature and the frame's column names in `src/kiln/frame.py` (e.g. whether the country column is `admin0_name` and whether `build_frame` takes `country_override`) and adjust the assertions to the real columns — the assertions above follow `ABOUT.md`, but the source is authoritative.

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_roundtrip.py -q`
Expected: PASS. If an assertion fails, the mismatch is a real finding — the two directions disagree; fix `bake`/`build_location` (never the export side) until the frame comes out right.

- [ ] **Step 3: Run the full suite, lint, and commit**

```bash
uv run ruff check src tests && uv run pytest -q
git add tests/test_roundtrip.py
git commit -m "test: round-trip bake output through the export pipeline"
```

---

### Task 10: README and worked example

**Files:**
- Modify: `README.md` (add an "Importing admin boundaries" section after the Usage section)

- [ ] **Step 1: Write the docs**

Add to `README.md` after the existing usage block:

````markdown
## Importing admin boundaries

The reverse direction: a one-level admin GeoJSON file (all features at the
same level, e.g. GRID3 wards) becomes ICRLocation-profiled resources in the
store, ancestors minted from feature properties.

```bash
# Offline: GeoJSON -> Location NDJSON (inspect/validate before loading)
uv run kiln bake \
  --in data/GRID3_NGA_operational_wards_v3_0.geojson \
  --country "Nigeria=NGA" \
  --level state=state:statecode \
  --level lga=lga \
  --level ward=ward \
  --alias lga=lga_alt_names --alias ward=ward_alt_names \
  --out wards.ndjson

# Network: NDJSON -> FHIR store (idempotent -- re-runs upsert in place)
uv run kiln load \
  --server https://healthcare.googleapis.com/v1/projects/.../fhir \
  --token "$(gcloud auth print-access-token)" \
  --in wards.ndjson
```

`--level` flags are ordered: the sequence is the hierarchy below the
country, and the last one is the feature level that carries the geometry.
Each flag is `LEVEL=NAME_PROP[:CODE_PROP]` — which property holds the
unit's name, and (optionally) its code. Units get slug-path ids
(`nga-ba-alkaleri-alkaleri-east`) built from codes where the source has
them, names otherwise, so loading is `PUT`-idempotent.

`kiln load` requires the store to support update-as-create (on Google
Healthcare API: `enableUpdateCreate=true`); it checks the store's
CapabilityStatement up front and refuses to half-load. A bundle failure
aborts the run — re-running the whole load is always safe.

Data problems (a feature missing its ward name, an invalid polygon) are
reported and skipped; mapping problems (a `--level` property that matches
nothing, two units slugging to the same id, a non-WGS84 CRS) abort before
anything is written.
````

- [ ] **Step 2: Verify the documented commands parse**

Run: `uv run kiln bake --help && uv run kiln load --help`
Expected: both print help including every flag the README names.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document kiln bake and kiln load"
```

---

## Self-review notes

- Spec coverage: CLI syntax (T3, T8), resource construction table (T1, T5), extension-URL policy (T2), geometry/CRS rules (T4), hierarchy minting + error taxonomy (T5), load semantics/preflight/bundles/retry (T6, T7), round-trip test (T9), docs (T10). Live smoke test is manual by design (spec, Testing).
- The spec's `loaded: 345 upserted, 0 failed` summary line is emitted as `loaded: 345 upserted`: bundle failures abort, so a non-zero failed count is unreachable — the simpler line is honest.
- Task 9 deliberately instructs verifying `build_frame`'s real signature/columns rather than trusting this plan; that boundary was written from ABOUT.md, not the source.
