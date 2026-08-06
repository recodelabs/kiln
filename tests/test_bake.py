import json

import pytest

from kiln.bake import (
    BakeError,
    LevelSpec,
    bake,
    check_crs,
    normalize_geometry,
    parse_alias_args,
    parse_country_arg,
    parse_level_arg,
    slugify,
)
from kiln.profile import NATIONAL_ADMIN_CODE_SYSTEM
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


SQUARE = {
    "type": "Polygon",
    "coordinates": [[[3.0, 6.0], [4.0, 6.0], [4.0, 7.0], [3.0, 7.0], [3.0, 6.0]]],
}

LEVELS = [
    LevelSpec(name="state", name_prop="state", code_prop="statecode"),
    LevelSpec(name="lga", name_prop="lga", code_prop=None),
    LevelSpec(name="ward", name_prop="ward", code_prop=None),
]


def feature(
    state="Bauchi",
    statecode="BA",
    lga="Alkaleri",
    ward="Alkaleri East",
    geometry=SQUARE,
    **extra
):
    properties = {"state": state, "statecode": statecode, "lga": lga, "ward": ward}
    properties.update(extra)
    return {"type": "Feature", "properties": properties, "geometry": geometry}


def collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def test_bake_mints_the_full_hierarchy_parents_first():
    report = Report()
    resources = bake(
        collection(feature(), feature(ward="Alkaleri West")),
        ("Nigeria", "NGA"),
        LEVELS,
        {},
        NATIONAL_ADMIN_CODE_SYSTEM,
        report,
    )

    ids = [r["id"] for r in resources]
    assert ids == [
        "nga",
        "nga-ba",
        "nga-ba-alkaleri",
        "nga-ba-alkaleri-alkaleri-east",
        "nga-ba-alkaleri-alkaleri-west",
    ]
    by_id = {r["id"]: r for r in resources}
    assert by_id["nga-ba"]["partOf"] == {"reference": "Location/nga"}
    assert by_id["nga-ba"]["name"] == "Bauchi"
    # State code comes from statecode; codeless levels get the slug path.
    assert by_id["nga-ba"]["identifier"] == [
        {"system": NATIONAL_ADMIN_CODE_SYSTEM, "value": "BA"}
    ]
    assert by_id["nga-ba-alkaleri"]["identifier"] == [
        {"system": NATIONAL_ADMIN_CODE_SYSTEM, "value": "nga-ba-alkaleri"}
    ]
    # Only the feature level carries a boundary.
    assert "extension" not in by_id["nga-ba-alkaleri"]
    assert "extension" in by_id["nga-ba-alkaleri-alkaleri-east"]
    assert report.counts() == {}


def test_bake_slug_uses_code_when_the_level_has_one():
    resources = bake(
        collection(feature()),
        ("Nigeria", "NGA"),
        LEVELS,
        {},
        NATIONAL_ADMIN_CODE_SYSTEM,
        Report(),
    )
    assert resources[1]["id"] == "nga-ba"  # from statecode BA, not "bauchi"


def test_bake_splits_alias_properties_on_semicolons():
    resources = bake(
        collection(feature(ward_alt_names="Alkaleri E.; The East")),
        ("Nigeria", "NGA"),
        LEVELS,
        {"ward": "ward_alt_names"},
        NATIONAL_ADMIN_CODE_SYSTEM,
        Report(),
    )
    ward = resources[-1]
    assert ward["alias"] == ["Alkaleri E.", "The East"]


def test_bake_skips_a_feature_missing_a_level_name_and_reports_it():
    report = Report()
    resources = bake(
        collection(feature(), feature(ward="")),
        ("Nigeria", "NGA"),
        LEVELS,
        {},
        NATIONAL_ADMIN_CODE_SYSTEM,
        report,
    )
    assert len([r for r in resources if "extension" in r]) == 1
    assert report.counts() == {"missing_field": 1}


def test_bake_emits_a_boundaryless_unit_for_invalid_geometry():
    report = Report()
    resources = bake(
        collection(feature(geometry=None)),
        ("Nigeria", "NGA"),
        LEVELS,
        {},
        NATIONAL_ADMIN_CODE_SYSTEM,
        report,
    )
    ward = resources[-1]
    assert ward["id"] == "nga-ba-alkaleri-alkaleri-east"
    assert "extension" not in ward
    assert report.counts() == {"geometry_invalid": 1}


def test_bake_rejects_two_distinct_names_colliding_on_one_slug():
    with pytest.raises(BakeError, match="collision"):
        bake(
            collection(feature(ward="Alkaleri East"), feature(ward="Alkaleri  East!")),
            ("Nigeria", "NGA"),
            LEVELS,
            {},
            NATIONAL_ADMIN_CODE_SYSTEM,
            Report(),
        )


def test_bake_rejects_a_duplicate_leaf_feature():
    with pytest.raises(BakeError, match="duplicate"):
        bake(
            collection(feature(), feature()),
            ("Nigeria", "NGA"),
            LEVELS,
            {},
            NATIONAL_ADMIN_CODE_SYSTEM,
            Report(),
        )


def test_bake_rejects_a_level_property_present_on_no_feature():
    with pytest.raises(BakeError, match="wardd"):
        bake(
            collection(feature()),
            ("Nigeria", "NGA"),
            [LEVELS[0], LEVELS[1], LevelSpec("ward", "wardd", None)],
            {},
            NATIONAL_ADMIN_CODE_SYSTEM,
            Report(),
        )


def test_bake_rejects_a_non_feature_collection():
    with pytest.raises(BakeError):
        bake(
            {"type": "Feature"},
            ("Nigeria", "NGA"),
            LEVELS,
            {},
            NATIONAL_ADMIN_CODE_SYSTEM,
            Report(),
        )
