"""Two-pass GeoParquet write.

geopandas stages WKB parquet per partition; ogr2ogr rewrites each staging
file with native Parquet geometry types and a covering bbox. GDAL is the
only writer available that emits native geometry types, which is why the
second pass exists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

from kiln.report import Report

GEO_TYPE_FLAGS = {"both": "YES", "only": "ONLY", "legacy": "NO"}
DEFAULT_PARTITION_BY = ("country", "geom_type", "tier")
DEFAULT_ROW_GROUP_SIZE = 20000
MIN_PARTITION_ROWS = 100
DATASET_DIR = "locations"


class KilnWriteError(RuntimeError):
    """Base class for errors raised while producing the GeoParquet output.

    Lets a caller (the Task 8 CLI) catch one type and still tell environment
    problems (`GdalUnavailable`) apart from a specific partition failing
    (`PartitionWriteError`), without falling back to a bare `RuntimeError`
    that would also swallow unrelated bugs.
    """


class GdalUnavailable(KilnWriteError):
    """Raised when the system GDAL cannot produce the required output."""


class PartitionWriteError(KilnWriteError):
    """Raised when ogr2ogr fails to finalize a specific partition.

    An ogr2ogr failure is an environment or code problem, not a data
    problem, so it is never reported-and-continued like data-quality
    issues elsewhere in the pipeline: the run must fail loudly rather than
    silently produce a dataset that looks complete but is missing a slice.
    """


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


def _remove_if_empty_upward(start: Path, boundary: Path) -> None:
    """Remove `start` and then its ancestors while each is empty.

    Stops at the first non-empty directory, or at `boundary` (never removed
    itself — it is the caller-supplied out_dir). Used to undo the directory
    tree a failed partition would otherwise leave behind.
    """
    directory = start
    while directory != boundary:
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def _cleanup_failed_partition(scratch: Path, leaf_dir: Path, out_dir: Path) -> None:
    scratch.unlink(missing_ok=True)
    _remove_if_empty_upward(leaf_dir, out_dir)


def _partition_segment(key: str, value: object, report: Report) -> str:
    """Render one `--partition-by` value as a filesystem-safe directory name.

    Two failure modes land here, both from real data rather than a coding
    bug, so both are handled and reported rather than left to crash:

    - A null value (e.g. `admin_level`, nullable by design) used to be
      dropped entirely by pandas' default `groupby(dropna=True)`, silently
      losing every row in that group with no report entry at all. It is
      now its own group, rendered as the literal `"null"` segment.
    - A data-derived value (e.g. a `pcode` used as `--partition-by
      country`) can contain a `/`, which pyarrow/ogr2ogr would otherwise
      read as an extra path segment and fail with a raw FileNotFoundError.
    """
    if pd.isna(value):
        return "null"
    text = str(value)
    sanitized = text.replace("/", "_").replace("\\", "_").replace("\x00", "")
    sanitized = sanitized or "empty"
    if sanitized != text:
        report.add(
            "partition_value_sanitized",
            f"{key}={text}",
            f"contained a path separator or control character; "
            f"written to the directory {key}={sanitized!r} instead",
        )
    return sanitized


def _finalize(
    source: Path,
    destination: Path,
    row_group_size: int,
    geo_types: str,
    partition: str,
    out_dir: Path,
) -> None:
    """Run ogr2ogr into a sibling scratch file, then atomically publish it.

    The scratch file must live in `destination`'s own directory, not the
    caller's disposable staging directory: `os.replace` is only atomic
    within a single filesystem, and a temp directory has no guaranteed
    relationship to `out_dir` — a container with tmpfs `/tmp` and a
    bind-mounted output volume hits `Invalid cross-device link` on every
    run otherwise. Writing straight to `destination` would additionally let
    a reader observe a truncated-but-parseable file if ogr2ogr died
    partway through, which is worse than no file at all. Any failure here
    is cleaned up so no scratch file or empty partition directory survives.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = destination.with_name("." + destination.name + ".tmp")

    result = subprocess.run(
        [
            "ogr2ogr",
            "-f", "Parquet",
            str(scratch),
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
        check=False,
    )
    if result.returncode != 0:
        _cleanup_failed_partition(scratch, destination.parent, out_dir)
        raise PartitionWriteError(
            f"ogr2ogr failed writing partition {partition!r} "
            f"(exit status {result.returncode}):\n{result.stderr.strip()}"
        )

    try:
        os.replace(scratch, destination)
    except OSError as exc:
        _cleanup_failed_partition(scratch, destination.parent, out_dir)
        raise PartitionWriteError(
            f"could not publish partition {partition!r} to {destination}: {exc}"
        ) from exc


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

    out_dir = Path(out_dir)
    dataset_dir = out_dir / DATASET_DIR

    # kiln owns this directory exclusively (it is the only writer of
    # `out/locations/`). Re-running transform into the same --out is the
    # normal nightly-refresh workflow, and only partitions written *this*
    # run get replaced below -- anything from a prior run whose key set has
    # since changed (a --country change, an admin level disappearing from
    # source, all polygons failing to resolve) would otherwise survive
    # forever, silently doubling the dataset. Clearing it first makes each
    # run's output an exact reflection of this run's input, every time,
    # including the case where this run resolves zero rows.
    if dataset_dir.exists():
        shutil.rmtree(dataset_dir)

    if frame.empty:
        return []

    keys = list(partition_by)

    # Cluster spatially so a bbox query touches few row groups.
    ordered = frame.iloc[frame.hilbert_distance().argsort()].reset_index(drop=True)

    written: list[Path] = []
    with tempfile.TemporaryDirectory() as staging_root:
        staging = Path(staging_root)
        # dropna=False: a nullable partition key (admin_level is nullable
        # by design) must not silently drop every null-valued row from the
        # output -- see _partition_segment for how the null group's
        # directory name is rendered.
        for values, part in ordered.groupby(keys, sort=False, dropna=False):
            if not isinstance(values, tuple):
                values = (values,)
            segments = [
                f"{key}={_partition_segment(key, value, report)}"
                for key, value in zip(keys, values, strict=True)
            ]

            partition = "/".join(segments)
            if len(part) < MIN_PARTITION_ROWS:
                report.add(
                    "small_partition",
                    partition,
                    f"{len(part)} rows is below MIN_PARTITION_ROWS={MIN_PARTITION_ROWS}",
                )

            staged = staging / ("_".join(segments) + ".parquet")
            part.to_parquet(staged, index=False)

            destination = out_dir.joinpath(DATASET_DIR, *segments, "part-0.parquet")
            _finalize(staged, destination, row_group_size, geo_types, partition, out_dir)
            written.append(destination)

    return written
