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
