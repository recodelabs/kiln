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
