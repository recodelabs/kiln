"""Assemble shredded records, hierarchy and geometry into one GeoDataFrame."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pyarrow as pa

from kiln.geometry import build_geometry
from kiln.hierarchy import ADMIN_COLUMNS, resolve_hierarchy
from kiln.profile import RawLocation
from kiln.report import Report

CRS = "EPSG:4326"
UNKNOWN_COUNTRY = "unknown"

# Column names for reuse in schema definition and validation
STRING_COLUMNS = {
    "id", "name", "status", "loc_type", "physical_type", "pcode", "gers_id",
    "parent_id", "path", "country", "settlement_type", "delivery_strategy",
    "facility_level", "ownership",
    "geom_type", "tier", "last_updated",
}
STRING_COLUMNS |= {f"admin{i}_name" for i in range(ADMIN_COLUMNS)}
STRING_COLUMNS |= {f"admin{i}_code" for i in range(ADMIN_COLUMNS)}

INTEGER_COLUMNS = {"depth", "admin_level"}
FLOAT_COLUMNS = {"lon", "lat"}
LIST_COLUMNS = {"identifiers", "ancestor_ids", "overlays_admin_unit_ids"}

# Define Arrow types for list columns to preserve through parquet/ogr2ogr
ARROW_LIST_TYPES = {
    "ancestor_ids": pa.list_(pa.string()),
    "overlays_admin_unit_ids": pa.list_(pa.string()),
    "identifiers": pa.list_(pa.struct([
        pa.field("system", pa.string()),
        pa.field("value", pa.string()),
    ])),
}

# Build dtype dict for all columns except geometry. Iterate each *_COLUMNS
# set in sorted order rather than raw set order: set iteration order for
# strings depends on the interpreter's hash seed, which is randomized per
# process, so building this from unsorted sets would make the empty
# frame's column order (see build_frame below) non-deterministic across
# separate runs of kiln even though it's stable within any single run.
DTYPE_MAP = {col: "string" for col in sorted(STRING_COLUMNS)}
DTYPE_MAP.update({col: "Int64" for col in sorted(INTEGER_COLUMNS)})
DTYPE_MAP.update({col: "float64" for col in sorted(FLOAT_COLUMNS)})
DTYPE_MAP.update({col: "object" for col in sorted(LIST_COLUMNS)})


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
            "facility_level": location.facility_level,
            "ownership": location.ownership,
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
        # Empty case: return frame with full schema but zero rows
        df_data = {}
        for col, dtype in DTYPE_MAP.items():
            if col in ARROW_LIST_TYPES:
                # Use Arrow dtype for list columns
                df_data[col] = pd.Series(dtype=pd.ArrowDtype(
                    ARROW_LIST_TYPES[col]))
            else:
                df_data[col] = pd.Series(dtype=dtype)
        df = pd.DataFrame(df_data)
        return gpd.GeoDataFrame(df, geometry=gpd.GeoSeries(dtype="geometry",
                                                            crs=CRS), crs=CRS)

    frame = gpd.GeoDataFrame(rows, geometry=geometries, crs=CRS)
    # Apply explicit dtypes to preserve all columns through parquet round-trip
    for col in STRING_COLUMNS:
        frame[col] = frame[col].astype("string")
    for col in INTEGER_COLUMNS:
        frame[col] = frame[col].astype("Int64")
    for col in FLOAT_COLUMNS:
        frame[col] = frame[col].astype("float64")
    # Apply Arrow types to list columns for round-trip preservation
    for col, arrow_type in ARROW_LIST_TYPES.items():
        frame[col] = frame[col].astype(pd.ArrowDtype(arrow_type))

    return frame
