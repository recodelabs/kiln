import json
from pathlib import Path

import geopandas as gpd
import httpx
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


def test_partition_by_an_unknown_column_exits_with_code_two_not_a_traceback(tmp_path, capsys):
    code = main(
        ["transform", "--in", str(FIXTURE), "--out", str(tmp_path), "--partition-by", "nosuchcol"]
    )

    assert code == 2
    assert "nosuchcol" in capsys.readouterr().err


def test_rerunning_transform_into_the_same_out_replaces_stale_partitions(tmp_path):
    """A nightly refresh -- the normal way this tool is used -- re-runs
    transform into the same --out. Nothing used to clear out/locations/
    first, so a changed key set (here, --country) left the old partitions
    behind and the dataset silently doubled.
    """
    out = tmp_path / "out"
    main(["transform", "--in", str(FIXTURE), "--out", str(out)])
    first_rows = len(read_all(out))

    main(["transform", "--in", str(FIXTURE), "--out", str(out), "--country", "ZZ"])
    second = read_all(out)

    assert len(second) == first_rows
    assert set(second["country"]) == {"ZZ"}


def _fhir_bundle_with_one_location() -> dict:
    return {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "resourceType": "Location",
                    "id": "loc-1",
                    "position": {"longitude": 3.0, "latitude": 6.0},
                }
            }
        ],
    }


def _mock_transport_client(handler):
    """A drop-in httpx.Client that always talks to `handler` instead of the
    network, for monkeypatching over kiln.extract's `httpx.Client(...)`
    calls (cmd_extract/cmd_run construct their own client and don't accept
    one as an argument, unlike the lower-level extract.py functions).
    """

    class _FakeClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return _FakeClient


def test_cmd_run_extracts_then_transforms_end_to_end(tmp_path, monkeypatch):
    """cmd_run has zero coverage even though it's the headline command: it
    wires cmd_extract's output ndjson straight into cmd_transform's input.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fhir_bundle_with_one_location())

    monkeypatch.setattr(httpx, "Client", _mock_transport_client(handler))

    out = tmp_path / "out"
    code = main(["run", "--server", "https://fhir.test", "--out", str(out)])

    assert code == 0
    assert (out / "locations.ndjson").exists()
    assert list(out.rglob("*.parquet"))
    assert read_all(out).iloc[0]["id"] == "loc-1"


def test_cmd_run_propagates_a_nonzero_exit_code_from_transform(tmp_path, monkeypatch):
    """extract succeeding must not mask transform failing: run's exit code
    is the one the caller (and any nightly-job monitoring) checks.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fhir_bundle_with_one_location())

    monkeypatch.setattr(httpx, "Client", _mock_transport_client(handler))

    out = tmp_path / "out"
    code = main(
        [
            "run",
            "--server", "https://fhir.test",
            "--out", str(out),
            "--partition-by", "nosuchcolumn",
        ]
    )

    assert code == 2
    assert (out / "locations.ndjson").exists()  # extract's half did complete
    assert not list(out.rglob("*.parquet"))  # transform's half did not
