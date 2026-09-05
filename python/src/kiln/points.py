"""Offline: point-feature rows (CSV) -> ICRLocation site resources.

The site sibling of bake.py: facilities, schools, and other service
points become point Locations linked into an already-baked admin
hierarchy by name. Pure -- no network, no GDAL. FHIR-shape knowledge is
delegated to `kiln.profile.build_point_location`.

Parent resolution walks the admin registry level by level, matching each
row's admin-name columns against the registry's names *and aliases*,
slug-normalized (so "Tama/Daye" matches "Tama Daye" and NHFR alt-names
match GRID3 aliases). A row whose chain stops early links to the deepest
ancestor that did resolve and is reported as `parent_unresolved` -- a
facility under its LGA is more useful than one floating free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kiln.bake import BakeError, slugify
from kiln.profile import build_facility_organization, build_point_location
from kiln.report import Report

# FHIR id: https://hl7.org/fhir/datatypes.html#id
_FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


@dataclass(frozen=True)
class ParentSpec:
    """One `--parent level=column` mapping flag, parsed."""

    level: str
    column: str


def parse_parent_arg(arg: str) -> ParentSpec:
    """`state=state_name` -> ParentSpec("state", "state_name")."""
    level, sep, column = arg.partition("=")
    if not sep or not level or not column:
        raise BakeError(f"--parent must be LEVEL=COLUMN (e.g. 'ward=ward'), got {arg!r}")
    return ParentSpec(level=level, column=column)


def parse_identifier_arg(arg: str) -> tuple[str, str]:
    """`https://...system-uri=column` -> (system, column). Splits on the LAST '='."""
    system, sep, column = arg.rpartition("=")
    if not sep or not system or not column:
        raise BakeError(f"--identifier must be SYSTEM_URI=COLUMN, got {arg!r}")
    return system, column


def parse_org_type_arg(arg: str) -> tuple[str, str, str | None]:
    """`URI=CODE_COL[:TEXT_COL]` -> (system, code_column, text_column|None)."""
    system, sep, columns = arg.rpartition("=")
    if not sep or not system or not columns:
        raise BakeError(
            f"--org-type-coding must be SYSTEM_URI=CODE_COLUMN[:TEXT_COLUMN], got {arg!r}"
        )
    code_column, _, text_column = columns.partition(":")
    if not code_column:
        raise BakeError(f"--org-type-coding {arg!r}: empty code column")
    return system, code_column, text_column or None


def parse_where_arg(arg: str) -> tuple[str, str]:
    """`state=Bauchi` -> ("state", "Bauchi"). Exact match on the raw value."""
    column, sep, value = arg.partition("=")
    if not sep or not column or not value:
        raise BakeError(f"--where must be COLUMN=VALUE (e.g. 'state=Bauchi'), got {arg!r}")
    return column, value


def build_admin_index(admin_resources: list[dict]) -> dict[str, dict[str, str]]:
    """parent id -> {slugified name-or-alias: child id}, roots keyed under "".

    A resource whose partOf is absent -- or points outside the given set --
    counts as a root. First claim wins on a key collision (two children of
    one parent sharing a slugged name), matching bake's first-feature-wins
    convention for derived data.
    """
    ids = {r.get("id") for r in admin_resources}
    index: dict[str, dict[str, str]] = {}
    for resource in admin_resources:
        child_id = resource.get("id")
        name = resource.get("name")
        if not child_id or not name:
            continue
        reference = (resource.get("partOf") or {}).get("reference") or ""
        parent_id = reference.rsplit("/", 1)[-1]
        key = parent_id if parent_id in ids else ""
        bucket = index.setdefault(key, {})
        for label in [name, *resource.get("alias", [])]:
            slug = slugify(str(label))
            if slug:
                bucket.setdefault(slug, child_id)
    return index


def resolve_parent(
    index: dict[str, dict[str, str]],
    parents: list[ParentSpec],
    row: dict,
    row_label: str,
    report: Report,
) -> str | None:
    """Walk the row's admin-name chain; return the deepest resolved id.

    Reports `parent_unresolved` (with how far the walk got) whenever the
    chain stops short of the last level. Returns None when nothing at all
    resolved -- the resource is then emitted with no partOf.

    The walk starts *below* the registry's root when it has exactly one
    (the usual country root, which point rows never name in their admin
    columns); a multi-root registry starts at the roots themselves.
    """
    current = _walk_start(index)
    resolved: str | None = None
    for spec in parents:
        name = slugify(str(row.get(spec.column) or ""))
        child = index.get(current, {}).get(name) if name else None
        if child is None:
            deepest = "nothing" if resolved is None else f"only {current!r}"
            report.add(
                "parent_unresolved",
                row_label,
                f"{spec.level} {row.get(spec.column)!r} not found in the admin "
                f"registry; resolved {deepest}",
            )
            return resolved
        resolved = child
        current = child
    return resolved


def _walk_start(index: dict[str, dict[str, str]]) -> str:
    """The node whose children the first parent level is matched against."""
    roots = set(index.get("", {}).values())
    if len(roots) == 1:
        return next(iter(roots))
    return ""


def bake_points(
    rows: list[dict],
    admin_resources: list[dict],
    *,
    type_code: str,
    name_col: str,
    lat_col: str,
    lon_col: str,
    id_col: str,
    parents: list[ParentSpec],
    identifiers: list[tuple[str, str]],
    where: list[tuple[str, str]],
    report: Report,
    paired_org: bool = False,
    org_identifiers: list[tuple[str, str]] | None = None,
    org_type_codings: list[tuple[str, str, str | None]] | None = None,
    type_codings: list[tuple[str, str, str | None]] | None = None,
) -> list[dict]:
    """Turn point rows into site Location resources linked into the hierarchy.

    Same error taxonomy as bake(): per-row data problems (missing name,
    bad coordinates, malformed id) are reported and cost at most that row
    or its position; duplicate ids are fatal, because two rows claiming
    one resource id would silently overwrite each other on load.

    `paired_org=True` emits the mCSD facility pairing: per row, an
    Organization (id `org-<row id>`, the accountable entity carrying
    `org_identifiers` -- the registry codes -- and `org_type_codings`,
    each a (system, code_column, text_column|None) whose cell value is
    slugified into the coding code with the raw value as display) followed
    by the Location, which references it via managingOrganization and
    keeps only the place identifiers from `identifiers`.

    `type_codings` (same shape as `org_type_codings`) appends extra
    classification CodeableConcepts to Location.type after the generic
    functional code -- the mCSD-sanctioned duplication that lets
    Location-only consumers (GeoParquet exports, map layers) see the
    facility level and ownership without joining Organizations.
    """
    index = build_admin_index(admin_resources)
    resources: list[dict] = []
    seen_ids: set[str] = set()

    for number, row in enumerate(rows, start=1):
        if any(str(row.get(column) or "") != value for column, value in where):
            continue

        raw_id = str(row.get(id_col) or "").strip()
        name = str(row.get(name_col) or "").strip()
        row_label = raw_id or f"row {number}"

        if not name:
            report.add("missing_field", row_label, f"{name_col!r} is empty; row skipped")
            continue
        if not _FHIR_ID.match(raw_id):
            report.add(
                "invalid_id",
                f"row {number}",
                f"{id_col!r} value {raw_id!r} is not a valid FHIR id; row skipped",
            )
            continue
        if raw_id in seen_ids:
            raise BakeError(
                f"duplicate id: {id_col!r} value {raw_id!r} appears more than once"
            )
        seen_ids.add(raw_id)

        org_id = None
        if paired_org:
            org_id = f"org-{raw_id}"
            resources.append(
                build_facility_organization(
                    org_id,
                    name,
                    identifiers=_row_identifiers(row, org_identifiers or []),
                    type_concepts=_org_type_concepts(row, org_type_codings or []),
                )
            )

        resources.append(
            build_point_location(
                raw_id,
                name,
                type_code=type_code,
                parent_id=resolve_parent(index, parents, row, row_label, report),
                identifiers=_row_identifiers(row, identifiers),
                position=_read_position(row, lat_col, lon_col, row_label, report),
                managing_org_id=org_id,
                extra_type_concepts=_org_type_concepts(row, type_codings or []),
            )
        )

    return resources


def _row_identifiers(row: dict, specs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(system, value) pairs from the row; empty cells are dropped."""
    return [
        (system, value)
        for system, column in specs
        if (value := str(row.get(column) or "").strip())
    ]


def _org_type_concepts(
    row: dict, codings: list[tuple[str, str, str | None]]
) -> list[tuple[str, str, str, str | None]]:
    """(system, code, display, text) tuples for Organization.type.

    The cell value is slugified into the code (so "Primary" -> "primary",
    matching the IG CodeSystems' code style) with the raw value kept as
    display; the optional text column carries the country-specific kind
    ("Primary Health Center"). Empty cells contribute nothing.
    """
    concepts = []
    for system, code_column, text_column in codings:
        raw = str(row.get(code_column) or "").strip()
        code = slugify(raw)
        if not code:
            continue
        text = str(row.get(text_column) or "").strip() if text_column else ""
        concepts.append((system, code, raw, text or None))
    return concepts


def _read_position(
    row: dict, lat_col: str, lon_col: str, row_label: str, report: Report
) -> tuple[float, float] | None:
    """(longitude, latitude) from the row, or None (reported) if unusable."""
    try:
        latitude = float(str(row.get(lat_col) or ""))
        longitude = float(str(row.get(lon_col) or ""))
    except ValueError:
        report.add(
            "missing_position",
            row_label,
            f"coordinates not numeric: ({row.get(lat_col)!r}, {row.get(lon_col)!r})",
        )
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        report.add(
            "missing_position",
            row_label,
            f"coordinates out of range: ({latitude}, {longitude})",
        )
        return None
    return longitude, latitude
