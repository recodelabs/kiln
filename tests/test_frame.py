import geopandas as gpd

from kiln.frame import build_frame
from kiln.profile import BoundaryRef, RawLocation
from kiln.report import Report

SQUARE = b'{"type":"Polygon","coordinates":[[[3,6],[4,6],[4,7],[3,7],[3,6]]]}'


def tree() -> list[RawLocation]:
    return [
        RawLocation(id="ng", name="Nigeria", loc_type="admin-unit", pcode="NG"),
        RawLocation(
            id="kano",
            name="Kano",
            loc_type="admin-unit",
            pcode="NG001",
            parent_id="ng",
            boundary=BoundaryRef(data=SQUARE),
        ),
        RawLocation(
            id="clinic",
            name="Clinic",
            loc_type="facility",
            parent_id="kano",
            position=(3.2, 6.2),
        ),
    ]


def test_build_frame_returns_a_geodataframe_in_wgs84():
    frame = build_frame(tree(), Report())

    assert isinstance(frame, gpd.GeoDataFrame)
    assert frame.crs.to_string() == "EPSG:4326"


def test_rows_without_geometry_are_dropped():
    frame = build_frame(tree(), Report())

    assert set(frame["id"]) == {"kano", "clinic"}


def test_tier_is_the_admin_level_for_admin_units_and_site_otherwise():
    frame = build_frame(tree(), Report()).set_index("id")

    assert frame.loc["kano", "tier"] == "1"
    assert frame.loc["clinic", "tier"] == "site"


def test_geom_type_column_matches_the_geometry():
    frame = build_frame(tree(), Report()).set_index("id")

    assert frame.loc["kano", "geom_type"] == "polygon"
    assert frame.loc["clinic", "geom_type"] == "point"


def test_denormalized_admin_columns_are_present():
    frame = build_frame(tree(), Report()).set_index("id")

    assert frame.loc["clinic", "admin0_name"] == "Nigeria"
    assert frame.loc["clinic", "admin1_code"] == "NG001"
    assert frame.loc["clinic", "country"] == "NG"


def test_country_override_wins_over_the_derived_value():
    frame = build_frame(tree(), Report(), country_override="ZZ")

    assert set(frame["country"]) == {"ZZ"}


def test_frame_has_the_full_expected_column_set():
    frame = build_frame(tree(), Report())

    expected = {
        "id", "name", "status", "loc_type", "physical_type", "pcode", "gers_id",
        "identifiers", "parent_id", "depth", "admin_level", "path", "ancestor_ids",
        "country", "settlement_type", "delivery_strategy", "overlays_admin_unit_ids",
        "geom_type", "tier", "lon", "lat", "last_updated", "geometry",
    }
    expected |= {f"admin{i}_name" for i in range(5)}
    expected |= {f"admin{i}_code" for i in range(5)}

    assert set(frame.columns) == expected


def test_a_row_with_no_derivable_country_lands_in_unknown_and_is_reported():
    report = Report()
    locations = [
        RawLocation(id="orphan", loc_type="facility", parent_id="gone", position=(4.0, 7.0))
    ]

    frame = build_frame(locations, report)

    assert frame.iloc[0]["country"] == "unknown"
    assert report.counts()["no_country"] == 1


def test_nodes_excluded_by_the_hierarchy_are_absent_from_the_frame():
    locations = [
        RawLocation(id="a", parent_id="b", position=(1.0, 1.0)),
        RawLocation(id="b", parent_id="a", position=(2.0, 2.0)),
    ]

    frame = build_frame(locations, Report())

    assert frame.empty
