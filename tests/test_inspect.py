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
    assert partition["avg_row_group_rows"] == 100


def test_summarize_reports_geo_metadata(dataset):
    partition = summarize(dataset)["partitions"][0]

    assert partition["geo_version"] == "1.1.0"
    assert partition["has_covering"] is True
    assert partition["geometry_types"] == ["Point"]


def test_summarize_on_an_empty_directory_returns_zeroes(tmp_path):
    summary = summarize(tmp_path)

    assert summary["totals"] == {"rows": 0, "partitions": 0, "files": 0}
