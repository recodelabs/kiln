import errno
import json
import os

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
import shapely

from kiln.frame import ARROW_LIST_TYPES, INTEGER_COLUMNS, STRING_COLUMNS
from kiln.report import Report
from kiln.write import GdalUnavailable, PartitionWriteError, _finalize, probe_gdal, write_dataset


@pytest.fixture(autouse=True)
def require_gdal():
    try:
        probe_gdal()
    except GdalUnavailable as exc:
        pytest.skip(str(exc))


def a_frame(n_points=250, n_polygons=250) -> gpd.GeoDataFrame:
    rng = np.random.default_rng(0)
    points = [shapely.Point(x, y) for x, y in rng.random((n_points, 2)) * 10]
    polygons = [shapely.box(x, y, x + 0.1, y + 0.1) for x, y in rng.random((n_polygons, 2)) * 10]
    rows = (
        [{"id": f"p{i}", "country": "NG", "geom_type": "point", "tier": "site"}
         for i in range(n_points)]
        + [{"id": f"a{i}", "country": "NG", "geom_type": "polygon", "tier": "1"}
           for i in range(n_polygons)]
    )
    return gpd.GeoDataFrame(rows, geometry=points + polygons, crs="EPSG:4326")


def test_writes_the_expected_hive_partitions(tmp_path):
    write_dataset(a_frame(), tmp_path, Report())

    written = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.parquet")}

    assert written == {
        "locations/country=NG/geom_type=point/tier=site/part-0.parquet",
        "locations/country=NG/geom_type=polygon/tier=1/part-0.parquet",
    }


def test_geometry_column_uses_the_native_parquet_type(tmp_path):
    write_dataset(a_frame(), tmp_path, Report())
    target = next(tmp_path.rglob("*.parquet"))

    schema = pq.ParquetFile(target).schema

    assert "Geometry" in str(schema)


def test_a_covering_bbox_column_is_written_and_declared(tmp_path):
    write_dataset(a_frame(), tmp_path, Report())
    target = next(tmp_path.rglob("*.parquet"))

    meta = json.loads(pq.ParquetFile(target).metadata.metadata[b"geo"])

    assert "covering" in meta["columns"]["geometry"]


def test_each_partition_file_has_a_single_geometry_type(tmp_path):
    write_dataset(a_frame(), tmp_path, Report())

    for target in tmp_path.rglob("*.parquet"):
        meta = json.loads(pq.ParquetFile(target).metadata.metadata[b"geo"])
        assert len(meta["columns"]["geometry"]["geometry_types"]) == 1


def test_no_pandas_index_column_leaks_into_the_output(tmp_path):
    write_dataset(a_frame(), tmp_path, Report())
    target = next(tmp_path.rglob("*.parquet"))

    names = [pq.ParquetFile(target).schema.column(i).name
             for i in range(len(pq.ParquetFile(target).schema))]

    assert not any("__index_level_" in name for name in names)


def test_row_group_size_is_honoured(tmp_path):
    write_dataset(a_frame(n_points=500, n_polygons=0), tmp_path, Report(), row_group_size=100)
    target = next(tmp_path.rglob("*.parquet"))

    assert pq.ParquetFile(target).metadata.num_row_groups == 5


def test_hilbert_sorting_clusters_neighbours_together(tmp_path):
    write_dataset(a_frame(n_points=500, n_polygons=0), tmp_path, Report(), row_group_size=100)
    target = next(tmp_path.rglob("*.parquet"))
    frame = gpd.read_parquet(target)

    # Successive rows should be closer together than a random shuffle would be.
    ordered = frame.geometry
    step = np.mean([
        ordered.iloc[i].distance(ordered.iloc[i + 1]) for i in range(len(ordered) - 1)
    ])
    shuffled = frame.sample(frac=1, random_state=1).geometry
    shuffled_step = np.mean([
        shuffled.iloc[i].distance(shuffled.iloc[i + 1]) for i in range(len(shuffled) - 1)
    ])

    assert step < shuffled_step / 2


def test_small_partitions_are_reported(tmp_path):
    report = Report()

    write_dataset(a_frame(n_points=5, n_polygons=5), tmp_path, report)

    assert report.counts()["small_partition"] == 2


def test_legacy_geo_types_writes_wkb_without_the_native_type(tmp_path):
    write_dataset(a_frame(), tmp_path, Report(), geo_types="legacy")
    target = next(tmp_path.rglob("*.parquet"))

    assert "Geometry" not in str(pq.ParquetFile(target).schema)


def test_an_empty_frame_writes_nothing_and_does_not_raise(tmp_path):
    empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    assert write_dataset(empty, tmp_path, Report()) == []


def test_a_failed_partition_raises_with_partition_and_stderr_and_leaves_no_file(tmp_path):
    # A source file that does not exist forces ogr2ogr to fail before it
    # writes anything, which is an easy, deterministic way to exercise the
    # failure path without depending on any particular data shape.
    out_dir = tmp_path / "out"
    destination = out_dir / "country=NG" / "geom_type=point" / "tier=site" / "part-0.parquet"
    bogus_source = tmp_path / "does-not-exist.parquet"

    with pytest.raises(PartitionWriteError) as excinfo:
        _finalize(
            bogus_source,
            destination,
            row_group_size=1000,
            geo_types="both",
            partition="country=NG/geom_type=point/tier=site",
            out_dir=out_dir,
        )

    message = str(excinfo.value)
    assert "country=NG/geom_type=point/tier=site" in message
    assert "does-not-exist.parquet" in message  # GDAL's stderr names the missing input
    assert not destination.exists()
    assert not destination.parent.exists()  # tier=site
    assert not (out_dir / "country=NG").exists()  # whole empty tree, not just the leaf


def test_a_cross_filesystem_replace_is_reported_and_cleaned_up(tmp_path, monkeypatch):
    # os.replace/os.rename never fall back to a copy across filesystems --
    # only shutil.move does that -- so a cross-device link is an outright
    # crash unless _finalize guards it explicitly. Simulate that guard
    # firing by forcing os.replace to raise the same OSError a tmpfs-vs-
    # bind-mount container setup would produce.
    def fake_replace(_source, _destination):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "replace", fake_replace)

    with pytest.raises(PartitionWriteError) as excinfo:
        write_dataset(a_frame(n_points=200, n_polygons=0), tmp_path, Report())

    assert "country=NG/geom_type=point/tier=site" in str(excinfo.value)
    assert list(tmp_path.rglob("*.parquet")) == []
    assert not (tmp_path / "locations").exists()


def test_a_nullable_partition_key_does_not_drop_rows(tmp_path):
    """pandas' groupby default (dropna=True) used to drop every row whose
    partition key was null with no report entry at all. admin_level is
    nullable by design, and --partition-by is a documented option, so this
    is one flag away from a user losing data silently.
    """
    frame = a_frame(n_points=5, n_polygons=0)
    frame["admin_level"] = pd.array([None] * len(frame), dtype="Int64")
    report = Report()

    written = write_dataset(frame, tmp_path, report, partition_by=("admin_level",))

    assert len(written) == 1
    result = gpd.read_parquet(written[0])
    assert len(result) == 5
    assert "admin_level=null" in written[0].as_posix()


def test_a_slash_in_a_partition_value_is_sanitized_not_crashed(tmp_path):
    """A data-derived value (e.g. a pcode used as --partition-by country)
    can contain a "/", which would otherwise be read as an extra path
    segment and crash pyarrow/ogr2ogr with a raw FileNotFoundError.
    """
    frame = a_frame(n_points=5, n_polygons=0)
    frame["country"] = "NG/01"
    report = Report()

    written = write_dataset(frame, tmp_path, report, partition_by=("country",))

    assert len(written) == 1
    assert "country=NG_01" in written[0].as_posix()
    assert "NG/01" not in written[0].as_posix()
    assert report.counts()["partition_value_sanitized"] == 1


def test_rerunning_into_the_same_out_dir_replaces_the_dataset_not_doubles_it(tmp_path):
    write_dataset(a_frame(n_points=150, n_polygons=0), tmp_path, Report())
    first = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.parquet")}
    assert first == {"locations/country=NG/geom_type=point/tier=site/part-0.parquet"}

    second_frame = a_frame(n_points=150, n_polygons=0)
    second_frame["country"] = "ZZ"
    write_dataset(second_frame, tmp_path, Report())

    second = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.parquet")}
    assert second == {"locations/country=ZZ/geom_type=point/tier=site/part-0.parquet"}


def test_rerunning_with_an_empty_frame_clears_the_previous_dataset(tmp_path):
    write_dataset(a_frame(n_points=150, n_polygons=0), tmp_path, Report())
    assert list(tmp_path.rglob("*.parquet"))

    empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    written = write_dataset(empty, tmp_path, Report())

    assert written == []
    assert list(tmp_path.rglob("*.parquet")) == []


def test_null_optional_columns_survive_the_full_write_path(tmp_path):
    """A uniformly-null column must not be silently dropped by ogr2ogr.

    GDAL's Parquet driver drops any column that Arrow infers as the `null`
    type, which happens whenever every value in a column is null. Because
    partitions are narrow, an individual partition file is exactly where an
    optional column goes all-null. frame.py's explicit "string" / "Int64" /
    pd.ArrowDtype(...) dtypes -- not plain object dtype -- are what keep
    GDAL from dropping it; this guards that the two-pass geopandas/ogr2ogr
    write actually preserves that end to end.
    """
    n = 150
    rng = np.random.default_rng(0)
    points = [shapely.Point(x, y) for x, y in rng.random((n, 2)) * 10]

    rows = [
        {
            "id": f"p{i}",
            "country": "NG",
            "geom_type": "point",
            "tier": "site",
            "gers_id": None,
            "settlement_type": None,
            "admin_level": None,
            "identifiers": None,
            "ancestor_ids": None,
            "overlays_admin_unit_ids": None,
        }
        for i in range(n)
    ]
    frame = gpd.GeoDataFrame(rows, geometry=points, crs="EPSG:4326")

    # Apply exactly the dtype scheme frame.py applies to a real frame, not a
    # lookalike, so this exercises the actual invariant Task 5 established.
    for col in ("id", "country", "geom_type", "tier", "gers_id", "settlement_type"):
        assert col in STRING_COLUMNS
        frame[col] = frame[col].astype("string")
    assert "admin_level" in INTEGER_COLUMNS
    frame["admin_level"] = frame["admin_level"].astype("Int64")
    for col, arrow_type in ARROW_LIST_TYPES.items():
        frame[col] = frame[col].astype(pd.ArrowDtype(arrow_type))

    write_dataset(frame, tmp_path, Report())
    target = next(tmp_path.rglob("*.parquet"))

    # schema_arrow, not schema: ParquetSchema.names flattens nested
    # list/struct leaves and would mislead on the list columns here.
    written_columns = set(pq.ParquetFile(target).schema_arrow.names)
    assert set(frame.columns) <= written_columns
