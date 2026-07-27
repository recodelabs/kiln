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
