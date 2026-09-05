"""Two-pass GeoParquet write.

geopandas stages WKB parquet per partition; ogr2ogr rewrites each staging
file with native Parquet geometry types and a covering bbox. GDAL is the
only writer available that emits native geometry types, which is why the
second pass exists.
"""

from __future__ import annotations

import hashlib
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
# Siblings of DATASET_DIR under out_dir, never nested under it, so every
# rename involved in the swap (see _swap_dataset_dir) stays on one
# filesystem and is atomic.
STAGING_DIR_NAME = ".locations.tmp"
SWAP_BACKUP_DIR_NAME = ".locations.old.tmp"


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
    itself — it is the caller-supplied dataset root). Used to undo the
    directory tree a failed partition would otherwise leave behind.
    """
    directory = start
    while directory != boundary:
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def _cleanup_failed_partition(scratch: Path, leaf_dir: Path, dataset_root: Path) -> None:
    scratch.unlink(missing_ok=True)
    _remove_if_empty_upward(leaf_dir, dataset_root)


def _reject_symlinked_dataset_dir(dataset_dir: Path) -> None:
    """Refuse to operate through a symlinked `out/locations`.

    Without this check, the rest of this module's rename/rmtree dance would
    eventually hit `shutil.rmtree`'s own guard ("Cannot call rmtree on a
    symbolic link") as a raw, uncaught OSError. Fail with a clear,
    typed error up front instead.
    """
    if dataset_dir.is_symlink():
        raise KilnWriteError(
            f"{dataset_dir} is a symlink; kiln refuses to write through a "
            "symlinked dataset directory. Replace it with a real directory "
            "(or remove the symlink) before running transform again."
        )


def _remove_dataset_dir(path: Path) -> None:
    """rmtree a finalized dataset directory, refusing to unlink through a symlink."""
    if path.is_symlink():
        raise KilnWriteError(
            f"{path} is a symlink; kiln refuses to delete through a symlinked "
            "path. Replace it with a real directory (or remove the symlink) "
            "before running transform again."
        )
    shutil.rmtree(path)


def _recover_incomplete_swap(dataset_dir: Path, backup_dir: Path, staging_dir: Path) -> None:
    """Heal leftover state from a process that died mid-swap on a previous run.

    `_swap_dataset_dir` publishes a new dataset with two renames, because
    POSIX `rename()` cannot atomically replace a non-empty directory in one
    step: first the live dataset is moved aside to `backup_dir`, then the
    fully-written new dataset is moved into its place. A crash can land in
    three spots, and this undoes each one before the next run starts:

    - Between the two renames: `dataset_dir` is briefly absent and
      `backup_dir` holds the last good dataset. Restore it.
    - After the swap but before `backup_dir` was cleaned up: `dataset_dir`
      is already the new data; `backup_dir` is just stale. Remove it.
    - Mid-write, before the swap ever started: only `staging_dir` has
      anything in it, and it is necessarily incomplete. Discard it.
    """
    if backup_dir.exists():
        if not dataset_dir.exists():
            os.replace(backup_dir, dataset_dir)
        else:
            _remove_dataset_dir(backup_dir)
    if staging_dir.exists():
        shutil.rmtree(staging_dir)


def _swap_dataset_dir(dataset_dir: Path, backup_dir: Path, new_dataset_dir: Path) -> None:
    """Publish `new_dataset_dir` as `dataset_dir`.

    Two renames, each atomic on its own (guaranteed single-filesystem since
    `backup_dir` and `new_dataset_dir` are siblings of `dataset_dir` under
    the same out_dir). The only non-atomic window is between them, where
    `dataset_dir` briefly does not exist; `_recover_incomplete_swap` (run at
    the top of every `write_dataset` call) detects and heals exactly that
    window on the next run by restoring `backup_dir`.
    """
    if dataset_dir.exists():
        os.replace(dataset_dir, backup_dir)
    os.replace(new_dataset_dir, dataset_dir)
    if backup_dir.exists():
        _remove_dataset_dir(backup_dir)


def _sanitize_partition_text(value: object) -> tuple[str, str, bool]:
    """Render one partition value as filesystem-safe text.

    Returns `(raw_text, candidate_segment, was_sanitized)`. `raw_text` is
    `str(value)` (or the literal `"null"` for a missing value) and is used
    only for reporting; `candidate_segment` is what actually gets used as
    the directory name, pending collision disambiguation.
    """
    if pd.isna(value):
        return "null", "null", False
    raw = str(value)
    candidate = raw.replace("/", "_").replace("\\", "_").replace("\x00", "")
    candidate = candidate or "empty"
    return raw, candidate, candidate != raw


def _value_identity(value: object) -> object:
    """A hashable identity distinguishing values that render identically.

    `None` and the literal string `"null"` both render as `"null"`; the
    integer `5` and the string `"5"` both render as `"5"`. They must still
    be treated as distinct source values for collision purposes, so the
    identity carries the original type, not just the rendered text.
    """
    if pd.isna(value):
        return "__null__"
    return (type(value).__name__, str(value))


def _partition_segment(
    key: str,
    value: object,
    report: Report,
    claims: dict[str, object],
) -> str:
    """Render one `--partition-by` value as a filesystem-safe, collision-free
    directory name segment (without the `key=` prefix), scoped to `claims`
    (one dict per parent-directory-and-key context — see `_write_all_partitions`).

    Three failure modes land here, all from real data rather than a coding
    bug, so all are handled and reported rather than left to crash or
    silently lose rows:

    - A null value (e.g. `admin_level`, nullable by design) used to be
      dropped entirely by pandas' default `groupby(dropna=True)`. It is its
      own group, rendered as the literal `"null"` segment.
    - A data-derived value (e.g. a `pcode` used as `--partition-by
      country`) can contain a `/`, which pyarrow/ogr2ogr would otherwise
      read as an extra path segment and fail with a raw FileNotFoundError.
      It is sanitized for the directory name.
    - Two *distinct* values can sanitize (or render) to the *same* segment
      — `"A/B"` and `"A_B"` both become `"A_B"`; `None` and the literal
      string `"null"` both become `"null"` — and the second one used to
      silently `os.replace` the first partition file, discarding every row
      in it with zero report entries. The second (and any further) value to
      claim an already-taken segment gets a short deterministic hash of its
      original value appended, so the two never collide on disk.
    """
    raw, candidate, was_sanitized = _sanitize_partition_text(value)
    identity = _value_identity(value)

    claimant = claims.get(candidate)
    if claimant is None:
        claims[candidate] = identity
        segment = candidate
    elif claimant == identity:
        segment = candidate
    else:
        segment = _disambiguate(candidate, value, identity, claims)
        report.add(
            "partition_value_collision",
            f"{key}={raw}",
            f"renders to the same directory segment {key}={candidate!r} as a "
            f"different value already written under this partition; "
            f"disambiguated to {key}={segment!r} instead",
        )
        return segment

    if was_sanitized:
        report.add(
            "partition_value_sanitized",
            f"{key}={raw}",
            f"contained a path separator or control character; "
            f"written to the directory {key}={candidate!r} instead",
        )
    return segment


def _disambiguate(
    candidate: str,
    value: object,
    identity: object,
    claims: dict[str, object],
) -> str:
    """Append a short deterministic hash of `value` to `candidate` until unique."""
    salt = ""
    while True:
        digest = hashlib.sha1(f"{candidate}\x00{value!r}{salt}".encode()).hexdigest()[:8]
        segment = f"{candidate}~{digest}"
        existing = claims.get(segment)
        if existing is None or existing == identity:
            claims[segment] = identity
            return segment
        salt += "#"


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


def _write_all_partitions(
    frame: gpd.GeoDataFrame,
    keys: list[str],
    dataset_dir: Path,
    report: Report,
    row_group_size: int,
    geo_types: str,
) -> list[Path]:
    """Write every partition of `frame` under `dataset_dir` (a fresh staging tree)."""
    # Cluster spatially so a bbox query touches few row groups.
    ordered = frame.iloc[frame.hilbert_distance().argsort()].reset_index(drop=True)

    written: list[Path] = []
    # One claims dict per (parent-path-so-far, key): a segment only needs to
    # be unique among siblings inside the same parent directory, not
    # globally, since a repeated value at a shallower level (e.g. the same
    # country under two different geom_types) is not a collision at all.
    claims_by_context: dict[tuple[tuple[str, ...], str], dict[str, object]] = {}

    with tempfile.TemporaryDirectory() as staging_root:
        staging = Path(staging_root)
        # dropna=False: a nullable partition key (admin_level is nullable
        # by design) must not silently drop every null-valued row from the
        # output -- see _partition_segment for how the null group's
        # directory name is rendered.
        for values, part in ordered.groupby(keys, sort=False, dropna=False):
            if not isinstance(values, tuple):
                values = (values,)

            segments: list[str] = []
            path_so_far: tuple[str, ...] = ()
            for key, value in zip(keys, values, strict=True):
                context = (path_so_far, key)
                claims = claims_by_context.setdefault(context, {})
                segment_value = _partition_segment(key, value, report, claims)
                segments.append(f"{key}={segment_value}")
                path_so_far = tuple(segments)

            partition = "/".join(segments)
            if len(part) < MIN_PARTITION_ROWS:
                report.add(
                    "small_partition",
                    partition,
                    f"{len(part)} rows is below MIN_PARTITION_ROWS={MIN_PARTITION_ROWS}",
                )

            staged = staging / ("_".join(segments) + ".parquet")
            part.to_parquet(staged, index=False)

            destination = dataset_dir.joinpath(*segments, "part-0.parquet")
            _finalize(staged, destination, row_group_size, geo_types, partition, dataset_dir)
            written.append(destination)

    return written


def write_dataset(
    frame: gpd.GeoDataFrame,
    out_dir: Path,
    report: Report,
    partition_by: tuple[str, ...] = DEFAULT_PARTITION_BY,
    row_group_size: int = DEFAULT_ROW_GROUP_SIZE,
    geo_types: str = "both",
) -> list[Path]:
    """Sort spatially, split into hive partitions, and write each one.

    `out_dir/locations` is replaced atomically: every partition is written
    into a sibling staging directory (`out_dir/.locations.tmp`) first, and
    only once every partition has finalized successfully is that staging
    directory swapped in for the live one (see `_swap_dataset_dir`). If any
    partition fails, the staging directory is discarded and the previous
    live dataset -- if any -- is left completely untouched, including its
    `--partition-by` key set: kiln owns `out/locations/` exclusively, and a
    re-run into the same `--out` (the normal nightly-refresh workflow) must
    make each successful run's output an exact reflection of that run's
    input, without ever leaving a torn mix of old and new partitions behind
    if the run fails partway through.
    """
    if geo_types not in GEO_TYPE_FLAGS:
        raise ValueError(f"geo_types must be one of {sorted(GEO_TYPE_FLAGS)}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir = out_dir / DATASET_DIR
    backup_dir = out_dir / SWAP_BACKUP_DIR_NAME
    new_dataset_dir = out_dir / STAGING_DIR_NAME

    _reject_symlinked_dataset_dir(dataset_dir)
    _recover_incomplete_swap(dataset_dir, backup_dir, new_dataset_dir)

    new_dataset_dir.mkdir()
    keys = list(partition_by)

    completed = False
    written: list[Path] = []
    try:
        if not frame.empty:
            written = _write_all_partitions(
                frame, keys, new_dataset_dir, report, row_group_size, geo_types
            )
        completed = True
    finally:
        if not completed:
            shutil.rmtree(new_dataset_dir, ignore_errors=True)

    _swap_dataset_dir(dataset_dir, backup_dir, new_dataset_dir)

    return [dataset_dir / p.relative_to(new_dataset_dir) for p in written]
