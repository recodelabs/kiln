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
