import geopandas as gpd
import numpy as np
import pytest
import shapely

from kiln.inspect import summarize
from kiln.report import Report
from kiln.write import GdalUnavailable, probe_gdal, write_dataset


@pytest.fixture(autouse=True)
def require_gdal():
    try:
        probe_gdal()
    except GdalUnavailable as exc:
        pytest.skip(str(exc))


@pytest.fixture
def dataset(tmp_path):
    rng = np.random.default_rng(0)
    points = [shapely.Point(x, y) for x, y in rng.random((300, 2)) * 10]
    frame = gpd.GeoDataFrame(
        [{"id": f"p{i}", "country": "NG", "geom_type": "point", "tier": "site"}
         for i in range(300)],
        geometry=points,
        crs="EPSG:4326",
    )
    write_dataset(frame, tmp_path, Report(), row_group_size=100)
    return tmp_path


def test_summarize_counts_rows_and_partitions(dataset):
    summary = summarize(dataset)

    assert summary["totals"]["rows"] == 300
    assert summary["totals"]["partitions"] == 1


def test_summarize_reports_row_group_layout(dataset):
    partition = summarize(dataset)["partitions"][0]

    assert partition["row_groups"] == 3
    assert partition["min_row_group_rows"] == 100
    assert partition["avg_row_group_rows"] == 100
    assert partition["max_row_group_rows"] == 100


def test_summarize_reports_geo_metadata(dataset):
    partition = summarize(dataset)["partitions"][0]

    assert partition["geo_version"] == "1.1.0"
    assert partition["has_covering"] is True
    assert partition["geometry_types"] == ["Point"]


def test_summarize_on_an_empty_directory_returns_zeroes(tmp_path):
    summary = summarize(tmp_path)

    assert summary["totals"] == {
        "rows": 0,
        "partitions": 0,
        "files": 0,
        "size_bytes": 0,
    }


def test_summarize_reports_uneven_row_group_distribution(tmp_path):
    """Test with row count not divisible by row_group_size to verify min/max."""
    rng = np.random.default_rng(0)
    # 250 rows with row_group_size=100 creates 3 groups: 100, 100, 50
    points = [shapely.Point(x, y) for x, y in rng.random((250, 2)) * 10]
    frame = gpd.GeoDataFrame(
        [{"id": f"p{i}", "country": "NG", "geom_type": "point", "tier": "site"}
         for i in range(250)],
        geometry=points,
        crs="EPSG:4326",
    )
    write_dataset(frame, tmp_path, Report(), row_group_size=100)

    partition = summarize(tmp_path)["partitions"][0]

    assert partition["rows"] == 250
    assert partition["row_groups"] == 3
    assert partition["min_row_group_rows"] == 50
    assert partition["avg_row_group_rows"] == 83  # round(250 / 3)
    assert partition["max_row_group_rows"] == 100


def test_summarize_reports_size_bytes(dataset):
    summary = summarize(dataset)
    partition = summary["partitions"][0]

    assert partition["size_bytes"] > 0
    assert summary["totals"]["size_bytes"] == partition["size_bytes"]


def test_summarize_size_totals_match_partition_sum(dataset):
    summary = summarize(dataset)
    partition_sizes_sum = sum(p["size_bytes"] for p in summary["partitions"])

    assert summary["totals"]["size_bytes"] == partition_sizes_sum
