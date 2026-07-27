"""Turn Location.position and boundary GeoJSON into shapely geometry."""

from __future__ import annotations

import json
from dataclasses import dataclass

import shapely
import shapely.errors

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
            report.add(
                "boundary_unparseable", location_id, "FeatureCollection has no geometry"
            )
            return None
        if len(geometries) > 1:
            report.add(
                "boundary_multi_feature",
                location_id,
                f"{len(geometries)} features folded into one geometry",
            )
            parts = [_from_dict(g, location_id, report) for g in geometries]
            parts = [part for part in parts if part is not None]
            if not parts:
                return None
            # Repair each part before unioning
            parts = [
                shapely.make_valid(part) if not part.is_valid else part for part in parts
            ]
            try:
                return shapely.union_all(parts)
            except shapely.errors.GEOSException as exc:
                report.add("boundary_unparseable", location_id, f"union failed: {exc}")
                return None
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
    except Exception as exc:  # noqa: BLE001 shapely raises GEOSException subclasses
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
        # Check if geometry is empty or unexpected type after repair
        if polygon.is_empty:
            report.add(
                "geometry_unexpected_type", raw.id, "empty geometry after repair"
            )
            return GeometryResult()
        elif polygon.geom_type == "Point":
            if raw.position is not None:
                lon, lat = raw.position
            else:
                lon, lat = polygon.x, polygon.y
            return GeometryResult(geometry=polygon, geom_type="point", lon=lon, lat=lat)
        elif polygon.geom_type in POLYGON_TYPES:
            if raw.position is not None:
                lon, lat = raw.position
            else:
                representative = shapely.point_on_surface(polygon)
                lon, lat = representative.x, representative.y
            return GeometryResult(
                geometry=polygon, geom_type="polygon", lon=lon, lat=lat
            )
        else:
            report.add(
                "geometry_unexpected_type",
                raw.id,
                f"unsupported type {polygon.geom_type}",
            )
            return GeometryResult()

    if raw.position is not None:
        lon, lat = raw.position
        return GeometryResult(
            geometry=shapely.Point(lon, lat), geom_type="point", lon=lon, lat=lat
        )

    report.add("no_geometry", raw.id, "no position and no usable boundary")
    return GeometryResult()
