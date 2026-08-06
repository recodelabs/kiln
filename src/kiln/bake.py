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
