"""Offline: one-level GeoJSON FeatureCollection -> ICRLocation NDJSON resources.

The import mirror of frame.py's transform: pure, no network, no GDAL.
All FHIR-shape knowledge is delegated to `kiln.profile.build_location`.
Spec: docs/superpowers/specs/2026-08-05-admin-import-design.md.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

import shapely
import shapely.geometry

from kiln.profile import (  # noqa: F401
    NATIONAL_ADMIN_CODE_SYSTEM,
    attach_boundary,
    build_location,
)
from kiln.report import Report


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
    shape = _shape_from_geojson(geometry, location_id, report)
    return None if shape is None else _geometry_bytes(shape)


def _shape_from_geojson(geometry: dict | None, location_id: str, report: Report):
    """Parse and validate one feature geometry into an oriented shapely shape.

    The shapely half of `normalize_geometry`, split out so the
    dissolve-parents pass can union child shapes without a serialize/parse
    round trip. None (reported as `geometry_invalid`) if unusable.
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
    except (ValueError, TypeError, KeyError) as exc:
        report.add("geometry_invalid", location_id, f"unparseable geometry: {exc}")
        return None

    if shape.is_empty or not shape.is_valid:
        reason = "empty geometry" if shape.is_empty else shapely.is_valid_reason(shape)
        report.add("geometry_invalid", location_id, reason)
        return None

    return _orient_rfc7946(shape)


def _geometry_bytes(shape) -> bytes:
    """Compact RFC 7946 GeoJSON bytes for an already-oriented shape."""
    return json.dumps(shapely.geometry.mapping(shape), separators=(",", ":")).encode()


def _orient_rfc7946(shape):
    """Exterior rings counterclockwise, holes clockwise, per RFC 7946."""
    from shapely.geometry import MultiPolygon
    from shapely.geometry.polygon import orient

    if isinstance(shape, MultiPolygon):
        return MultiPolygon([orient(polygon, sign=1.0) for polygon in shape.geoms])
    return orient(shape, sign=1.0)


def bake(
    collection: dict,
    country: tuple[str, str],
    levels: list[LevelSpec],
    aliases: dict[str, str],
    code_system: str,
    report: Report,
    dissolve_parents: bool = False,
) -> list[dict]:
    """Turn a one-level FeatureCollection into Location resources, parents first.

    Fatal problems (wrong shape, wrong CRS, a mapping typo, slug collisions)
    raise BakeError before anything is returned. Per-feature data problems
    (missing name, bad geometry) are reported and cost at most that feature's
    boundary or presence -- see the spec's error-handling section.

    `dissolve_parents=True` gives every minted ancestor (country, and each
    non-leaf level) a boundary too: the union of its children's geometries.
    These are *derived* boundaries -- exactly consistent with the child
    tiling, which is what makes rollups and containment checks line up,
    but not authoritative cartography; an authoritative file loaded later
    upserts over them. Invalid child geometries (already reported) simply
    don't contribute.
    """
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        raise BakeError("input is not a GeoJSON FeatureCollection")
    check_crs(collection)
    features = [f for f in collection.get("features", []) if isinstance(f, dict)]

    for level in levels:
        if not any(
            isinstance(f.get("properties"), dict)
            and f["properties"].get(level.name_prop)
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
    # parent slug -> child shapes to union, only populated when dissolving.
    pending_dissolve: dict[str, list] = {}

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
        # Ancestor slugs of the current feature, root first. Built during the
        # walk because they cannot be recovered by splitting the leaf slug --
        # "-" is both the segment separator and an ordinary in-name hyphen.
        lineage = [root_slug]
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
                shape = _shape_from_geojson(feature.get("geometry"), slug, report)
                if dissolve_parents and shape is not None:
                    for ancestor in lineage:
                        pending_dissolve.setdefault(ancestor, []).append(shape)
                per_level[index][slug] = build_location(
                    slug,
                    name,
                    parent_id=parent_slug,
                    identifiers=[(code_system, code or slug)],
                    aliases=_read_aliases(properties, aliases.get(level.name)),
                    boundary_geojson=None if shape is None else _geometry_bytes(shape),
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
            lineage.append(slug)

    if pending_dissolve:
        _dissolve_into_parents(minted, per_level[:-1], pending_dissolve, report)

    resources = list(minted.values())
    for level_resources in per_level:
        resources.extend(level_resources.values())
    return resources


def _dissolve_into_parents(
    minted: dict[str, dict],
    parent_levels: list[dict[str, dict]],
    pending_dissolve: dict[str, list],
    report: Report,
) -> None:
    """Attach the union of child geometries to every collected ancestor.

    GRID3-style inputs are clean coverages, so the union of a parent's
    children is its boundary. A union that comes out degenerate (not
    polygonal after make_valid) is reported and skipped -- the parent just
    stays boundary-less, same as when dissolving is off.
    """
    parents_by_slug: dict[str, dict] = dict(minted)
    for level_resources in parent_levels:
        parents_by_slug.update(level_resources)

    for slug, shapes in pending_dissolve.items():
        resource = parents_by_slug.get(slug)
        if resource is None:
            continue
        union = shapely.union_all(shapes)
        if not union.is_valid:
            union = shapely.make_valid(union)
        if union.is_empty or union.geom_type not in ("Polygon", "MultiPolygon"):
            report.add(
                "geometry_invalid",
                slug,
                f"dissolved boundary is {union.geom_type}, expected Polygon or "
                "MultiPolygon; parent left boundary-less",
            )
            continue
        attach_boundary(resource, _geometry_bytes(_orient_rfc7946(union)))


def _read_aliases(properties: dict, alias_prop: str | None) -> list[str]:
    if not alias_prop:
        return []
    raw = str(properties.get(alias_prop) or "")
    return [part.strip() for part in raw.split(";") if part.strip()]
