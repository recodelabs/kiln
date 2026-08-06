import json

import pytest

from kiln.bake import (
    BakeError,
    LevelSpec,
    check_crs,
    normalize_geometry,
    parse_alias_args,
    parse_country_arg,
    parse_level_arg,
    slugify,
)
from kiln.report import Report


def test_slugify_lowercases_folds_and_hyphenates():
    assert slugify("Alkaleri East") == "alkaleri-east"
    assert slugify("Grand-Popo / Centre") == "grand-popo-centre"
    assert slugify("  N'Djaména ") == "n-djamena"


def test_slugify_collapses_runs_and_strips_edges():
    assert slugify("A  --  B") == "a-b"


def test_parse_country_arg():
    assert parse_country_arg("Nigeria=NGA") == ("Nigeria", "NGA")


def test_parse_country_arg_rejects_missing_code():
    with pytest.raises(BakeError):
        parse_country_arg("Nigeria")


def test_parse_level_arg_with_and_without_code():
    assert parse_level_arg("state=state:statecode") == LevelSpec(
        name="state", name_prop="state", code_prop="statecode"
    )
    assert parse_level_arg("ward=ward") == LevelSpec(
        name="ward", name_prop="ward", code_prop=None
    )


def test_parse_level_arg_rejects_bad_shapes():
    for bad in ("ward", "ward=", "=ward", "ward=a:b:c"):
        with pytest.raises(BakeError):
            parse_level_arg(bad)


def test_parse_alias_args():
    assert parse_alias_args(["lga=lga_alt_names", "ward=ward_alt_names"]) == {
        "lga": "lga_alt_names",
        "ward": "ward_alt_names",
    }
    with pytest.raises(BakeError):
        parse_alias_args(["lga"])


# Exterior ring wound clockwise on purpose: RFC 7946 requires counterclockwise,
# so normalize_geometry must rewind it.
CW_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[3.0, 6.0], [3.0, 7.0], [4.0, 7.0], [4.0, 6.0], [3.0, 6.0]]],
}


def test_check_crs_accepts_missing_and_4326_spellings():
    check_crs({"type": "FeatureCollection", "features": []})  # absent = default
    for name in (
        "EPSG:4326",
        "urn:ogc:def:crs:EPSG::4326",
        "urn:ogc:def:crs:OGC:1.3:CRS84",
        "CRS84",
    ):
        check_crs(
            {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": name}},
                "features": [],
            }
        )


def test_check_crs_rejects_anything_else():
    with pytest.raises(BakeError):
        check_crs(
            {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
                "features": [],
            }
        )


def test_normalize_geometry_rewinds_rings_to_rfc7946():
    report = Report()
    payload = normalize_geometry(CW_SQUARE, "w1", report)

    assert payload is not None
    ring = json.loads(payload)["coordinates"][0]
    # Shoelace: positive area = counterclockwise exterior.
    area2 = sum(
        (x1 * y2 - x2 * y1)
        for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False)
    )
    assert area2 > 0
    assert report.counts() == {}


def test_normalize_geometry_reports_invalid_and_returns_none():
    report = Report()
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]],
    }
    assert normalize_geometry(bowtie, "w1", report) is None
    assert report.counts() == {"geometry_invalid": 1}


def test_normalize_geometry_reports_non_polygon_and_missing():
    report = Report()
    point = {"type": "Point", "coordinates": [3.0, 6.0]}
    assert normalize_geometry(point, "w1", report) is None
    assert normalize_geometry(None, "w2", report) is None
    assert report.counts() == {"geometry_invalid": 2}


def test_normalize_geometry_reports_incomplete_geometry():
    report = Report()
    incomplete = {"type": "Polygon"}  # missing coordinates key
    assert normalize_geometry(incomplete, "w1", report) is None
    assert report.counts() == {"geometry_invalid": 1}
