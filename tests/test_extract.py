import base64
import json

import httpx
import pytest

from kiln.extract import (
    fetch_locations,
    read_ndjson,
    resolve_boundary_urls,
    write_ndjson,
)
from kiln.profile import BOUNDARY_EXTENSION_URL
from kiln.report import Report

GEOJSON = b'{"type":"Polygon","coordinates":[[[3,6],[4,6],[4,7],[3,7],[3,6]]]}'


def bundle(resources, next_url=None) -> dict:
    payload = {
        "resourceType": "Bundle",
        "entry": [{"resource": r} for r in resources],
    }
    if next_url:
        payload["link"] = [{"relation": "next", "url": next_url}]
    return payload


def test_fetch_follows_next_links_across_pages():
    page_two = "https://fhir.test/Location?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=bundle([{"id": "b"}]))
        return httpx.Response(200, json=bundle([{"id": "a"}], next_url=page_two))

    client = httpx.Client(transport=httpx.MockTransport(handler))

    ids = [r["id"] for r in fetch_locations("https://fhir.test", None, client=client)]

    assert ids == ["a", "b"]


def test_fetch_sends_the_bearer_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=bundle([]))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    list(fetch_locations("https://fhir.test", "tok-123", client=client))

    assert seen["auth"] == "Bearer tok-123"


def test_fetch_passes_since_when_given():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["since"] = request.url.params.get("_lastUpdated")
        return httpx.Response(200, json=bundle([]))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    list(fetch_locations("https://fhir.test", None, since="2026-01-01", client=client))

    assert seen["since"] == "gt2026-01-01"


def test_fetch_raises_with_the_server_body_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="403"):
        list(fetch_locations("https://fhir.test", None, client=client))


def a_resource_with_boundary_url(url: str) -> dict:
    return {
        "id": "loc-1",
        "extension": [
            {
                "url": BOUNDARY_EXTENSION_URL,
                "valueAttachment": {"contentType": "application/geo+json", "url": url},
            }
        ],
    }


def test_resolve_inlines_a_plain_geojson_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resources = [a_resource_with_boundary_url("https://files.test/a.geojson")]

    resolve_boundary_urls(resources, Report(), client=client)

    attachment = resources[0]["extension"][0]["valueAttachment"]
    assert base64.b64decode(attachment["data"]) == GEOJSON
    assert "url" not in attachment


def test_resolve_unwraps_a_binary_resource():
    payload = {
        "resourceType": "Binary",
        "contentType": "application/geo+json",
        "data": base64.b64encode(GEOJSON).decode(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resources = [a_resource_with_boundary_url("https://fhir.test/Binary/abc")]

    resolve_boundary_urls(resources, Report(), client=client)

    assert base64.b64decode(resources[0]["extension"][0]["valueAttachment"]["data"]) == GEOJSON


def test_a_failed_boundary_fetch_is_reported_and_does_not_abort_the_run():
    report = Report()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resources = [a_resource_with_boundary_url("https://files.test/missing.geojson")]

    resolve_boundary_urls(resources, report, client=client)

    assert report.counts() == {"boundary_fetch_failed": 1}
    assert "data" not in resources[0]["extension"][0]["valueAttachment"]


def test_ndjson_round_trips(tmp_path):
    path = tmp_path / "locations.ndjson"

    assert write_ndjson([{"id": "a"}, {"id": "b"}], path) == 2
    assert [r["id"] for r in read_ndjson(path)] == ["a", "b"]


def test_read_ndjson_also_accepts_a_bundle_json_file(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle([{"id": "a"}, {"id": "b"}])))

    assert [r["id"] for r in read_ndjson(path)] == ["a", "b"]


def test_read_ndjson_accepts_a_compact_bundle_json_file(tmp_path):
    """Test that Bundle detection works with compact JSON (no spaces after colons)."""
    path = tmp_path / "bundle.json"
    # Use separators=(",", ":") to create compact JSON without spaces
    compact_bundle = json.dumps(
        bundle([{"id": "a"}, {"id": "b"}]), separators=(",", ":")
    )
    path.write_text(compact_bundle)

    assert [r["id"] for r in read_ndjson(path)] == ["a", "b"]


def test_read_ndjson_reports_a_malformed_line_and_keeps_reading(tmp_path):
    """The NDJSON seam is the documented third-party hand-off point: one bad
    line must be reported and skipped, not abort the whole read (previously
    a bare json.loads(line) let a JSONDecodeError propagate as a traceback).
    """
    path = tmp_path / "locations.ndjson"
    path.write_text('{"resourceType":"Location","id":"a"}\nnot json\n{"id":"b"}\n')
    report = Report()

    resources = list(read_ndjson(path, report))

    assert [r["id"] for r in resources] == ["a", "b"]
    assert report.counts() == {"malformed_field": 1}
    assert "line 2" in report.issues[0].detail


def test_read_ndjson_without_a_report_still_skips_malformed_lines(tmp_path):
    """report is optional -- existing callers that don't pass one must keep
    working exactly as before, just without the detail.
    """
    path = tmp_path / "locations.ndjson"
    path.write_text('{"id":"a"}\nnot json\n')

    assert [r["id"] for r in read_ndjson(path)] == ["a"]


def test_read_ndjson_skips_a_non_list_bundle_entry_field(tmp_path):
    """A Bundle.json with a non-list entry field must not crash iteration."""
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps({"resourceType": "Bundle", "entry": "not-a-list"}))

    assert list(read_ndjson(path)) == []


def test_resolve_malformed_binary_base64_is_reported_and_does_not_abort():
    """Test that malformed base64 in Binary data is reported without aborting."""
    report = Report()
    payload = {
        "resourceType": "Binary",
        "contentType": "application/geo+json",
        "data": "not-valid-base64!!!",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resources = [a_resource_with_boundary_url("https://fhir.test/Binary/bad")]

    resolve_boundary_urls(resources, report, client=client)

    assert report.counts() == {"boundary_fetch_failed": 1}
    assert "data" not in resources[0]["extension"][0]["valueAttachment"]


@pytest.mark.parametrize("bad_data", [42, {"key": "value"}, [1, 2, 3], True])
def test_resolve_binary_with_non_string_data_is_reported_and_does_not_abort(
    bad_data,
):
    """Test that non-string Binary.data is reported without aborting."""
    report = Report()
    payload = {
        "resourceType": "Binary",
        "contentType": "application/geo+json",
        "data": bad_data,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resources = [a_resource_with_boundary_url("https://fhir.test/Binary/bad")]

    resolve_boundary_urls(resources, report, client=client)

    assert report.counts() == {"boundary_fetch_failed": 1}
    assert "data" not in resources[0]["extension"][0]["valueAttachment"]


def test_resolve_connection_error_is_reported_and_does_not_abort():
    """Test that connection errors are reported without aborting the run."""
    report = Report()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Network unreachable")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resources = [a_resource_with_boundary_url("https://files.test/a.geojson")]

    resolve_boundary_urls(resources, report, client=client)

    assert report.counts() == {"boundary_fetch_failed": 1}
    assert "data" not in resources[0]["extension"][0]["valueAttachment"]


def test_fetch_raises_on_cyclic_pagination():
    """Test that cyclic next links are detected and raise with a clear message."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Always return the same next link, creating a cycle
        return httpx.Response(
            200, json=bundle(
                [{"id": "a"}],
                next_url="https://fhir.test/Location?page=1"
            )
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="Cyclic pagination detected"):
        list(fetch_locations("https://fhir.test", None, client=client))


def test_fetch_raises_on_excessive_pagination():
    """Test that excessive pagination (too many pages) is detected and raises."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Always return a different next link
        page = request.url.params.get("page", "0")
        next_page = str(int(page) + 1)
        return httpx.Response(
            200, json=bundle(
                [{"id": f"loc-{page}"}],
                next_url=f"https://fhir.test/Location?page={next_page}"
            )
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="Exceeded maximum pagination limit"):
        list(fetch_locations("https://fhir.test", None, client=client))


def test_fetch_skips_non_dict_bundle_entries():
    """Non-dict bundle entries should be skipped without crashing."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={
                "resourceType": "Bundle",
                "entry": [
                    {"resource": {"id": "loc-1"}},
                    "not-a-dict",
                    {"resource": {"id": "loc-2"}},
                ],
            }
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    ids = [r["id"] for r in fetch_locations("https://fhir.test", None, client=client)]

    assert ids == ["loc-1", "loc-2"]


def test_fetch_raises_when_the_response_is_not_a_json_object():
    """A server returning a bare JSON array instead of a Bundle must abort clearly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "not-a-bundle"}])

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="not a JSON object"):
        list(fetch_locations("https://fhir.test", None, client=client))


def test_fetch_skips_a_non_list_bundle_entry_field():
    """A non-list Bundle.entry must not crash iteration."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": "not-a-list"})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert list(fetch_locations("https://fhir.test", None, client=client)) == []


def test_fetch_ignores_a_next_link_missing_its_url():
    """A next link with no url must stop pagination, not crash; entries still come through."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "entry": [{"resource": {"id": "loc-1"}}],
                "link": [{"relation": "next"}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    ids = [r["id"] for r in fetch_locations("https://fhir.test", None, client=client)]

    assert ids == ["loc-1"]


def test_fetch_skips_non_dict_bundle_links():
    """Non-dict bundle links should be skipped without crashing."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Return bundle with non-dict link entries, but don't follow them
        # (only test that non-dict links are skipped without crashing)
        return httpx.Response(
            200, json={
                "resourceType": "Bundle",
                "entry": [{"resource": {"id": "loc-1"}}],
                "link": [
                    "not-a-dict",
                    42,
                    {"relation": "other", "url": "https://fhir.test/Location"},
                ],
            }
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    # Main test: fetching doesn't crash with non-dict links
    ids = [r["id"] for r in fetch_locations("https://fhir.test", None, client=client)]

    assert ids == ["loc-1"]


def test_resolve_skips_a_non_dict_resource_and_still_processes_the_rest():
    """A non-dict item in the resource list must not crash the whole batch."""
    report = Report()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    good = a_resource_with_boundary_url("https://files.test/a.geojson")
    resources = ["not-a-dict-resource", good]

    resolve_boundary_urls(resources, report, client=client)

    assert report.counts() == {"boundary_fetch_failed": 1}
    attachment = resources[1]["extension"][0]["valueAttachment"]
    assert base64.b64decode(attachment["data"]) == GEOJSON


def test_resolve_skips_a_non_dict_extension_entry_and_still_processes_the_rest():
    """A non-dict extension entry must not abort resolution for the rest of it."""
    report = Report()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resource = a_resource_with_boundary_url("https://files.test/a.geojson")
    resource["extension"].insert(0, "not-a-dict")
    resources = [resource]

    resolve_boundary_urls(resources, report, client=client)

    assert report.counts() == {"boundary_fetch_failed": 1}
    attachment = resources[0]["extension"][1]["valueAttachment"]
    assert base64.b64decode(attachment["data"]) == GEOJSON


def test_resolve_skips_a_non_list_extension_field_without_crashing():
    """A non-list `extension` field must not crash iteration."""
    report = Report()
    resources = [{"id": "loc-1", "extension": "not-a-list"}]

    resolve_boundary_urls(resources, report, client=httpx.Client())

    assert resources == [{"id": "loc-1", "extension": "not-a-list"}]
    assert report.counts() == {}


def test_resolve_skips_a_non_dict_valueattachment_and_still_processes_the_rest():
    """A non-dict valueAttachment must not abort resolution for the rest of it."""
    report = Report()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    bad = {
        "id": "loc-bad",
        "extension": [{"url": BOUNDARY_EXTENSION_URL, "valueAttachment": "not-a-dict"}],
    }
    good = a_resource_with_boundary_url("https://files.test/a.geojson")
    resources = [bad, good]

    resolve_boundary_urls(resources, report, client=client)

    assert report.counts() == {"boundary_fetch_failed": 1}
    attachment = resources[1]["extension"][0]["valueAttachment"]
    assert base64.b64decode(attachment["data"]) == GEOJSON


def test_resolve_skips_a_non_string_boundary_url_without_crashing():
    """A non-string attachment.url must not crash httpx; siblings still resolve."""
    report = Report()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    bad = {
        "id": "loc-bad",
        "extension": [
            {
                "url": BOUNDARY_EXTENSION_URL,
                "valueAttachment": {"contentType": "application/geo+json", "url": 42},
            }
        ],
    }
    good = a_resource_with_boundary_url("https://files.test/a.geojson")
    resources = [bad, good]

    resolve_boundary_urls(resources, report, client=client)

    assert "data" not in resources[0]["extension"][0]["valueAttachment"]
    attachment = resources[1]["extension"][0]["valueAttachment"]
    assert base64.b64decode(attachment["data"]) == GEOJSON
