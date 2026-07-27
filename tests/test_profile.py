import base64

from kiln.profile import (
    BOUNDARY_EXTENSION_URL,
    DELIVERY_STRATEGY_EXTENSION_URL,
    GERS_SYSTEM,
    OVERLAYS_EXTENSION_URL,
    PCODE_SYSTEM,
    SETTLEMENT_TYPE_EXTENSION_URL,
    shred,
)
from kiln.report import Report

GEOJSON = b'{"type":"Polygon","coordinates":[[[3,6],[4,6],[4,7],[3,7],[3,6]]]}'


def a_location(**overrides) -> dict:
    resource = {
        "resourceType": "Location",
        "id": "loc-1",
        "name": "Nassarawa",
        "status": "active",
        "type": [{"coding": [{"code": "admin-unit"}]}],
        "physicalType": {"coding": [{"code": "jurisdiction"}]},
        "identifier": [
            {"system": PCODE_SYSTEM, "value": "NG001002"},
            {"system": GERS_SYSTEM, "value": "08f2a1b"},
            {"system": "http://example.org/other", "value": "keep-me"},
        ],
        "partOf": {"reference": "Location/loc-parent"},
        "meta": {"lastUpdated": "2026-07-01T00:00:00Z"},
    }
    resource.update(overrides)
    return resource


def test_shred_extracts_core_fields():
    raw = shred(a_location(), Report())

    assert raw.id == "loc-1"
    assert raw.name == "Nassarawa"
    assert raw.status == "active"
    assert raw.loc_type == "admin-unit"
    assert raw.physical_type == "jurisdiction"
    assert raw.last_updated == "2026-07-01T00:00:00Z"


def test_shred_promotes_pcode_and_gers_but_keeps_all_identifiers():
    raw = shred(a_location(), Report())

    assert raw.pcode == "NG001002"
    assert raw.gers_id == "08f2a1b"
    assert {i["value"] for i in raw.identifiers} == {"NG001002", "08f2a1b", "keep-me"}


def test_shred_strips_the_resource_type_prefix_from_partof():
    raw = shred(a_location(), Report())

    assert raw.parent_id == "loc-parent"


def test_shred_reads_position_longitude_first():
    resource = a_location(position={"longitude": 8.5, "latitude": 12.0})

    raw = shred(resource, Report())

    assert raw.position == (8.5, 12.0)


def test_shred_returns_no_position_when_absent():
    assert shred(a_location(), Report()).position is None


def test_shred_decodes_an_inline_base64_boundary():
    resource = a_location(
        extension=[
            {
                "url": BOUNDARY_EXTENSION_URL,
                "valueAttachment": {
                    "contentType": "application/geo+json",
                    "data": base64.b64encode(GEOJSON).decode(),
                },
            }
        ]
    )

    raw = shred(resource, Report())

    assert raw.boundary.data == GEOJSON
    assert raw.boundary.url is None


def test_shred_records_a_url_referenced_boundary_without_fetching():
    resource = a_location(
        extension=[
            {
                "url": BOUNDARY_EXTENSION_URL,
                "valueAttachment": {
                    "contentType": "application/geo+json",
                    "url": "https://example.org/Binary/abc",
                },
            }
        ]
    )

    raw = shred(resource, Report())

    assert raw.boundary.data is None
    assert raw.boundary.url == "https://example.org/Binary/abc"


def test_shred_rejects_a_boundary_with_the_wrong_content_type():
    report = Report()
    resource = a_location(
        extension=[
            {
                "url": BOUNDARY_EXTENSION_URL,
                "valueAttachment": {"contentType": "application/pdf", "data": "eyJ9"},
            }
        ]
    )

    raw = shred(resource, report)

    assert raw.boundary is None
    assert report.counts() == {"boundary_bad_content_type": 1}


def test_shred_collects_overlays_settlement_type_and_delivery_strategy():
    resource = a_location(
        extension=[
            {"url": OVERLAYS_EXTENSION_URL, "valueReference": {"reference": "Location/a"}},
            {"url": OVERLAYS_EXTENSION_URL, "valueReference": {"reference": "Location/b"}},
            {"url": SETTLEMENT_TYPE_EXTENSION_URL, "valueCode": "refugee-idp"},
            {"url": DELIVERY_STRATEGY_EXTENSION_URL, "valueCode": "door-to-door"},
        ]
    )

    raw = shred(resource, report=Report())

    assert raw.overlays_admin_unit_ids == ["a", "b"]
    assert raw.settlement_type == "refugee-idp"
    assert raw.delivery_strategy == "door-to-door"


def test_shred_reports_and_skips_a_resource_with_no_id():
    report = Report()
    resource = a_location()
    del resource["id"]

    assert shred(resource, report) is None
    assert report.counts() == {"missing_id": 1}


def test_shred_guards_against_non_dict_partof():
    """Non-dict partOf should be skipped, not raise."""
    report = Report()
    resource = a_location(partOf="not-a-dict")

    raw = shred(resource, report)

    assert raw is not None
    assert raw.id == "loc-1"
    assert raw.parent_id is None
    assert report.counts() == {"malformed_field": 1}


def test_shred_guards_against_non_dict_meta():
    """Non-dict meta should be skipped, not raise."""
    report = Report()
    resource = a_location(meta="not-a-dict")

    raw = shred(resource, report)

    assert raw is not None
    assert raw.id == "loc-1"
    assert raw.last_updated is None
    assert report.counts() == {"malformed_field": 1}


def test_shred_guards_against_non_dict_extension_entry():
    """Non-dict extension entry should be skipped, not raise."""
    report = Report()
    resource = a_location(
        extension=[
            {"url": "http://example.org/some-ext", "valueCode": "test"},
            "not-a-dict",
        ]
    )

    raw = shred(resource, report)

    assert raw is not None
    assert raw.id == "loc-1"
    assert report.counts() == {"malformed_field": 1}


def test_shred_guards_against_non_dict_identifier_entry():
    """Non-dict identifier entry should be skipped, not raise."""
    report = Report()
    resource = a_location(
        identifier=[
            {"system": PCODE_SYSTEM, "value": "NG001002"},
            "not-a-dict",
        ]
    )

    raw = shred(resource, report)

    assert raw is not None
    assert raw.id == "loc-1"
    assert raw.pcode == "NG001002"
    assert len(raw.identifiers) == 1
    assert report.counts() == {"malformed_field": 1}


def test_shred_guards_against_non_numeric_position_longitude():
    """Non-numeric position.longitude should be skipped with report, not raise."""
    report = Report()
    resource = a_location(position={"longitude": "not-a-number", "latitude": 12.0})

    raw = shred(resource, report)

    assert raw is not None
    assert raw.id == "loc-1"
    assert raw.position is None
    assert report.counts() == {"malformed_field": 1}


def test_shred_guards_against_non_numeric_position_latitude():
    """Non-numeric position.latitude should be skipped with report, not raise."""
    report = Report()
    resource = a_location(position={"longitude": 8.5, "latitude": "not-a-number"})

    raw = shred(resource, report)

    assert raw is not None
    assert raw.id == "loc-1"
    assert raw.position is None
    assert report.counts() == {"malformed_field": 1}


def test_shred_guards_against_non_dict_boundary_valueattachment():
    """Non-dict boundary valueAttachment should be skipped with report."""
    report = Report()
    resource = a_location(
        extension=[
            {
                "url": BOUNDARY_EXTENSION_URL,
                "valueAttachment": "not-a-dict",
            }
        ]
    )

    raw = shred(resource, report)

    assert raw is not None
    assert raw.id == "loc-1"
    assert raw.boundary is None
    assert report.counts() == {"malformed_field": 1}


def test_shred_guards_against_non_dict_overlays_valuereference():
    """Non-dict overlays valueReference should be skipped with report."""
    report = Report()
    resource = a_location(
        extension=[
            {
                "url": OVERLAYS_EXTENSION_URL,
                "valueReference": "not-a-dict",
            }
        ]
    )

    raw = shred(resource, report)

    assert raw is not None
    assert raw.id == "loc-1"
    assert raw.overlays_admin_unit_ids == []
    assert report.counts() == {"malformed_field": 1}
