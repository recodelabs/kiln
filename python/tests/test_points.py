
import pytest

from kiln.bake import BakeError
from kiln.points import (
    ParentSpec,
    bake_points,
    build_admin_index,
    parse_identifier_arg,
    parse_parent_arg,
    parse_where_arg,
)
from kiln.profile import NATIONAL_ADMIN_CODE_SYSTEM, build_location
from kiln.report import Report

NHFR = "https://icr.healthcampaigns.org/identifiers/nga-nhfr-code"
GRID3 = "https://icr.healthcampaigns.org/identifiers/grid3-globalid"


def admin_registry():
    """country -> state -> lga -> two wards, as bake would emit them."""
    ids = [(NATIONAL_ADMIN_CODE_SYSTEM, "x")]
    country = build_location("nga", "Nigeria", identifiers=ids)
    state = build_location("nga-ba", "Bauchi", parent_id="nga", identifiers=ids)
    lga = build_location("nga-ba-toro", "Toro", parent_id="nga-ba", identifiers=ids)
    ward = build_location(
        "nga-ba-toro-tilde", "Tilde", parent_id="nga-ba-toro",
        identifiers=ids, aliases=["Tilden Fulani"],
    )
    ward2 = build_location(
        "nga-ba-toro-toro", "Toro", parent_id="nga-ba-toro", identifiers=ids,
    )
    return [country, state, lga, ward, ward2]


def facility_row(**overrides):
    row = {
        "state": "Bauchi",
        "lga": "Toro",
        "ward": "Tilde",
        "facility_name": "Tilde Primary Health Care Center",
        "latitude": "10.5",
        "longitude": "9.9",
        "globalid": "9c2b58dd-1e99-49b9-970e-eabce1c7dd10",
        "nhfr_code": "08/07/1/1/1/0030",
    }
    row.update(overrides)
    return row


PARENTS = [
    ParentSpec(level="state", column="state"),
    ParentSpec(level="lga", column="lga"),
    ParentSpec(level="ward", column="ward"),
]


def run_bake_points(rows, report=None, where=None):
    return bake_points(
        rows,
        admin_registry(),
        type_code="facility",
        name_col="facility_name",
        lat_col="latitude",
        lon_col="longitude",
        id_col="globalid",
        parents=PARENTS,
        identifiers=[(NHFR, "nhfr_code"), (GRID3, "globalid")],
        where=where or [],
        report=report if report is not None else Report(),
    )


def test_parse_parent_arg():
    assert parse_parent_arg("state=state_name") == ParentSpec("state", "state_name")
    with pytest.raises(BakeError):
        parse_parent_arg("state")


def test_parse_identifier_arg_splits_on_last_equals_only():
    assert parse_identifier_arg(f"{NHFR}=nhfr_code") == (NHFR, "nhfr_code")
    with pytest.raises(BakeError):
        parse_identifier_arg("no-equals")


def test_parse_where_arg():
    assert parse_where_arg("state=Bauchi") == ("state", "Bauchi")
    with pytest.raises(BakeError):
        parse_where_arg("state")


def test_build_admin_index_matches_names_and_aliases_slugified():
    index = build_admin_index(admin_registry())
    assert index[""]["nigeria"] == "nga"
    assert index["nga"]["bauchi"] == "nga-ba"
    assert index["nga-ba-toro"]["tilde"] == "nga-ba-toro-tilde"
    # Alias, slug-normalized.
    assert index["nga-ba-toro"]["tilden-fulani"] == "nga-ba-toro-tilde"


def test_bake_points_builds_a_profiled_facility():
    report = Report()
    (resource,) = run_bake_points([facility_row()], report)

    assert resource["id"] == "9c2b58dd-1e99-49b9-970e-eabce1c7dd10"
    assert resource["name"] == "Tilde Primary Health Care Center"
    assert resource["status"] == "active"
    assert resource["type"][0]["coding"][0]["code"] == "facility"
    assert resource["physicalType"]["coding"][0]["code"] == "si"
    assert resource["partOf"] == {"reference": "Location/nga-ba-toro-tilde"}
    assert resource["position"] == {"longitude": 9.9, "latitude": 10.5}
    assert {(i["system"], i["value"]) for i in resource["identifier"]} == {
        (NHFR, "08/07/1/1/1/0030"),
        (GRID3, "9c2b58dd-1e99-49b9-970e-eabce1c7dd10"),
    }
    assert report.counts() == {}


def test_bake_points_falls_back_to_deepest_resolved_parent():
    report = Report()
    (resource,) = run_bake_points([facility_row(ward="Nowhere")], report)

    assert resource["partOf"] == {"reference": "Location/nga-ba-toro"}
    assert report.counts() == {"parent_unresolved": 1}


def test_bake_points_with_no_resolvable_parent_has_no_partof():
    report = Report()
    (resource,) = run_bake_points([facility_row(state="Elsewhere")], report)

    assert "partOf" not in resource
    assert report.counts() == {"parent_unresolved": 1}


def test_bake_points_writes_spatial_index_cells_for_positioned_rows():
    from kiln.spatial import SPATIAL_INDEX_EXTENSION_URL, quadkey

    report = Report()
    rows = [facility_row(), facility_row(globalid="no-position", latitude="", longitude="")]
    with_pos, without_pos = bake_points(
        rows,
        admin_registry(),
        type_code="facility",
        name_col="facility_name",
        lat_col="latitude",
        lon_col="longitude",
        id_col="globalid",
        parents=PARENTS,
        identifiers=[(GRID3, "globalid")],
        where=[],
        report=report,
        spatial_indexes=[("quadkey", 18), ("quadkey", 10)],
    )
    cells = [
        (e["extension"][0]["valueCode"], e["extension"][1]["valueUnsignedInt"], e["extension"][2]["valueString"])
        for e in with_pos["extension"]
        if e["url"] == SPATIAL_INDEX_EXTENSION_URL
    ]
    assert cells == [("quadkey", 18, quadkey(9.9, 10.5, 18)), ("quadkey", 10, quadkey(9.9, 10.5, 10))]
    assert cells[0][2].startswith(cells[1][2])
    assert "extension" not in without_pos
    assert report.counts() == {"missing_position": 1}


def test_bake_points_where_filters_rows():
    rows = [facility_row(), facility_row(state="Kano", globalid="other-id")]
    resources = run_bake_points(rows, where=[("state", "Bauchi")])
    assert len(resources) == 1


def test_bake_points_reports_bad_coordinates_and_emits_without_position():
    report = Report()
    (resource,) = run_bake_points([facility_row(latitude="")], report)
    assert "position" not in resource
    assert report.counts() == {"missing_position": 1}

    report = Report()
    (resource,) = run_bake_points([facility_row(latitude="95.0")], report)
    assert "position" not in resource
    assert report.counts() == {"missing_position": 1}


def test_bake_points_skips_rows_without_name_or_usable_id():
    report = Report()
    resources = run_bake_points(
        [facility_row(facility_name=""), facility_row(globalid="bad id!")], report
    )
    assert resources == []
    assert report.counts() == {"missing_field": 1, "invalid_id": 1}


def test_bake_points_empty_identifier_column_is_dropped_not_emitted():
    (resource,) = run_bake_points([facility_row(nhfr_code="")])
    assert {i["system"] for i in resource["identifier"]} == {GRID3}


def test_bake_points_duplicate_ids_are_fatal():
    with pytest.raises(BakeError, match="duplicate"):
        run_bake_points([facility_row(), facility_row()])


# --- paired facility Organizations -------------------------------------------

FACILITY_TYPE_CS = "https://icr.healthcampaigns.org/CodeSystem/icr-facility-type-cs"
OWNERSHIP_CS = "https://icr.healthcampaigns.org/CodeSystem/icr-ownership-cs"


def run_bake_points_paired(rows, report=None):
    return bake_points(
        rows,
        admin_registry(),
        type_code="facility",
        name_col="facility_name",
        lat_col="latitude",
        lon_col="longitude",
        id_col="globalid",
        parents=PARENTS,
        identifiers=[(GRID3, "globalid")],
        where=[],
        report=report if report is not None else Report(),
        paired_org=True,
        org_identifiers=[(NHFR, "nhfr_code")],
        org_type_codings=[
            (FACILITY_TYPE_CS, "level", "level_detail"),
            (OWNERSHIP_CS, "ownership", None),
        ],
    )


def paired_row(**overrides):
    row = facility_row(level="Primary", level_detail="Primary Health Center",
                       ownership="Public")
    row.update(overrides)
    return row


def test_paired_org_emits_organization_and_links_location():
    resources = run_bake_points_paired([paired_row()])

    assert [r["resourceType"] for r in resources] == ["Organization", "Location"]
    org, location = resources

    assert org["id"] == "org-9c2b58dd-1e99-49b9-970e-eabce1c7dd10"
    assert org["name"] == "Tilde Primary Health Care Center"
    assert org["active"] is True
    # prov + tier (with detail text) + ownership.
    codings = [c["coding"][0] for c in org["type"]]
    assert codings[0]["code"] == "prov"
    assert codings[1] == {
        "system": FACILITY_TYPE_CS, "code": "primary", "display": "Primary",
    }
    assert org["type"][1]["text"] == "Primary Health Center"
    assert codings[2]["code"] == "public"
    assert org["identifier"] == [{"system": NHFR, "value": "08/07/1/1/1/0030"}]

    assert location["managingOrganization"] == {
        "reference": "Organization/org-9c2b58dd-1e99-49b9-970e-eabce1c7dd10"
    }
    # Registry codes live on the org; the location keeps place identifiers only.
    assert {i["system"] for i in location["identifier"]} == {GRID3}


def test_paired_org_skips_empty_type_values():
    (org, _) = run_bake_points_paired([paired_row(level="", ownership="Unknown")])
    codings = [c["coding"][0] for c in org["type"]]
    assert [c["code"] for c in codings] == ["prov", "unknown"]


def test_unpaired_rows_have_no_managing_organization():
    (resource,) = run_bake_points([facility_row()])
    assert "managingOrganization" not in resource


def test_type_codings_duplicate_classification_onto_the_location():
    resources = bake_points(
        [paired_row()],
        admin_registry(),
        type_code="facility",
        name_col="facility_name",
        lat_col="latitude",
        lon_col="longitude",
        id_col="globalid",
        parents=PARENTS,
        identifiers=[(GRID3, "globalid")],
        where=[],
        report=Report(),
        paired_org=True,
        org_identifiers=[(NHFR, "nhfr_code")],
        org_type_codings=[
            (FACILITY_TYPE_CS, "level", "level_detail"),
            (OWNERSHIP_CS, "ownership", None),
        ],
        type_codings=[
            (FACILITY_TYPE_CS, "level", "level_detail"),
            (OWNERSHIP_CS, "ownership", None),
        ],
    )
    location = next(r for r in resources if r["resourceType"] == "Location")

    concepts = location["type"]
    # Generic functional code first, then the duplicated classification axes.
    assert concepts[0]["coding"][0]["code"] == "facility"
    assert concepts[1]["coding"][0] == {
        "system": FACILITY_TYPE_CS, "code": "primary", "display": "Primary",
    }
    assert concepts[1]["text"] == "Primary Health Center"
    assert concepts[2]["coding"][0]["code"] == "public"
