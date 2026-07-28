import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest

from kiln.cli import main
from kiln.write import GdalUnavailable, probe_gdal

FIXTURE = Path(__file__).parent / "fixtures" / "locations.ndjson"


@pytest.fixture(autouse=True)
def require_gdal():
    try:
        probe_gdal()
    except GdalUnavailable as exc:
        pytest.skip(str(exc))


@pytest.fixture
def transformed(tmp_path):
    out = tmp_path / "out"
    assert main(["transform", "--in", str(FIXTURE), "--out", str(out)]) == 0
    return out


def read_all(out: Path) -> gpd.GeoDataFrame:
    # Don't force crs="EPSG:4326" here: ogr2ogr writes the geographic CRS as
    # OGC:CRS84 (same datum, lon/lat axis order matching how the coordinates
    # are actually stored) rather than authority-strict EPSG:4326 (formally
    # lat/lon order). They're the same points; forcing the EPSG code trips
    # geopandas's CRS-mismatch guard for no benefit to the assertions below,
    # none of which inspect CRS.
    frames = [gpd.read_parquet(p) for p in sorted(out.rglob("*.parquet"))]
    return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True))


def test_transform_writes_partitioned_output(transformed):
    written = {p.relative_to(transformed).as_posix() for p in transformed.rglob("*.parquet")}

    assert written, "no parquet files written"
    assert all(part.startswith("locations/country=") for part in written)
    assert any(part.startswith("locations/country=NG/") for part in written)


def test_an_orphan_with_no_admin_ancestor_lands_under_unknown(transformed):
    frame = read_all(transformed).set_index("id")

    assert frame.loc["orphan", "country"] == "unknown"


def test_settlement_with_both_geometries_yields_exactly_one_row(transformed):
    frame = read_all(transformed)

    gama = frame[frame["id"] == "gama"]
    assert len(gama) == 1
    assert gama.iloc[0]["geom_type"] == "polygon"
    assert (gama.iloc[0]["lon"], gama.iloc[0]["lat"]) == (3.2, 6.2)


def test_facility_inherits_admin_ancestors(transformed):
    clinic = read_all(transformed).set_index("id").loc["clinic"]

    assert clinic["admin0_name"] == "Nigeria"
    assert clinic["admin2_name"] == "Nassarawa"
    assert pd.isna(clinic["admin_level"])
    assert clinic["tier"] == "site"


def test_cycle_members_are_absent_from_the_output(transformed):
    ids = set(read_all(transformed)["id"])

    assert "cyc-a" not in ids
    assert "cyc-b" not in ids


def test_location_without_geometry_is_absent(transformed):
    assert "ghost" not in set(read_all(transformed)["id"])


def test_report_records_every_expected_pathology(transformed):
    counts = json.loads((transformed / "_report.json").read_text())["counts"]

    assert counts["cycle"] == 2
    assert counts["orphan"] == 1
    assert counts["no_geometry"] >= 2  # ghost, plus the unresolved remote boundary
    assert counts["duplicate_pcode"] == 1
    assert counts["point_outside_parent"] == 1
    assert counts["no_country"] == 1  # the orphan, whose partOf dangles


def test_transform_is_offline_and_does_not_resolve_remote_boundaries(transformed):
    counts = json.loads((transformed / "_report.json").read_text())["counts"]

    assert counts["boundary_unresolved_url"] == 1


def test_country_override_is_applied(tmp_path):
    out = tmp_path / "out"
    main(["transform", "--in", str(FIXTURE), "--out", str(out), "--country", "ZZ"])

    assert any("country=ZZ" in p.as_posix() for p in out.rglob("*.parquet"))


def test_missing_input_file_exits_with_code_two(tmp_path, capsys):
    code = main(["transform", "--in", str(tmp_path / "nope.ndjson"), "--out", str(tmp_path)])

    assert code == 2
    assert "not found" in capsys.readouterr().err
