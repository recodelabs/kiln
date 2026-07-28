import base64
import json
import threading
import time

import httpx
import pytest

import kiln.extract as extract_module
from kiln.cache import cache_key
from kiln.extract import (
    MalformedNdjsonError,
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


def test_read_ndjson_rejects_a_pretty_printed_multi_line_bundle_loudly(tmp_path):
    """A pretty-printed Bundle (json.dumps(..., indent=2)) breaks every
    single line, not just one: read_ndjson streams line by line, so this
    used to yield zero resources and a pile of malformed_field issues,
    exiting cleanly as if the run had legitimately resolved no data. That
    is indistinguishable from a real empty result once combined with
    write_dataset owning out/locations/ -- this must fail loudly instead.
    """
    path = tmp_path / "pretty_bundle.json"
    path.write_text(json.dumps(bundle([{"id": "a"}, {"id": "b"}]), indent=2))

    with pytest.raises(MalformedNdjsonError, match="pretty-printed"):
        list(read_ndjson(path))


def test_read_ndjson_rejects_a_pretty_printed_json_array_loudly(tmp_path):
    path = tmp_path / "pretty_array.json"
    path.write_text(json.dumps([{"id": "a"}], indent=2))

    with pytest.raises(MalformedNdjsonError, match="pretty-printed"):
        list(read_ndjson(path))


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


def _many_resources_with_boundary_urls(n: int) -> list[dict]:
    resources = []
    for i in range(n):
        resource = a_resource_with_boundary_url(f"https://files.test/{i}.geojson")
        resource["id"] = f"loc-{i}"
        resources.append(resource)
    return resources


def test_resolve_concurrently_inlines_many_boundaries():
    resources = _many_resources_with_boundary_urls(50)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()

    resolve_boundary_urls(resources, report, client=client, concurrency=8)

    assert report.counts() == {}
    for resource in resources:
        attachment = resource["extension"][0]["valueAttachment"]
        assert base64.b64decode(attachment["data"]) == GEOJSON
        assert "url" not in attachment


def test_resolve_runs_fetches_concurrently():
    """Prove the worker pool actually overlaps in-flight requests, not just
    that it produces correct output -- a bug that silently serialized the
    work (e.g. holding a lock across the whole fetch) would still pass every
    correctness assertion.
    """
    resources = _many_resources_with_boundary_urls(20)
    lock = threading.Lock()
    state = {"concurrent": 0, "max_concurrent": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            state["concurrent"] += 1
            state["max_concurrent"] = max(state["max_concurrent"], state["concurrent"])
        time.sleep(0.05)
        with lock:
            state["concurrent"] -= 1
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()

    resolve_boundary_urls(resources, report, client=client, concurrency=8)

    assert state["max_concurrent"] > 1
    assert report.counts() == {}


def test_resolve_report_ordering_is_deterministic():
    """Running the same input twice must produce identical report.issues,
    regardless of which worker thread happens to finish first each time.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        index = int(request.url.path.rsplit("/", 1)[-1].split(".")[0])
        if index % 3 == 0:
            return httpx.Response(404, text="gone")
        return httpx.Response(200, content=GEOJSON)

    def run_once():
        client = httpx.Client(transport=httpx.MockTransport(handler))
        report = Report()
        resources = _many_resources_with_boundary_urls(20)
        resolve_boundary_urls(resources, report, client=client, concurrency=6, retries=1)
        return report.issues

    first = run_once()
    second = run_once()

    assert first == second
    assert len(first) == 7  # indices 0, 3, 6, ..., 18 -> 7 of the 20


def test_resolve_retries_a_500_and_succeeds_on_a_later_attempt(monkeypatch):
    monkeypatch.setattr(extract_module.time, "sleep", lambda seconds: None)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()
    resources = [a_resource_with_boundary_url("https://files.test/a.geojson")]

    resolve_boundary_urls(resources, report, client=client, retries=3)

    assert len(calls) == 3
    assert report.counts() == {}
    attachment = resources[0]["extension"][0]["valueAttachment"]
    assert base64.b64decode(attachment["data"]) == GEOJSON


def test_resolve_does_not_retry_a_404(monkeypatch):
    monkeypatch.setattr(extract_module.time, "sleep", lambda seconds: None)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, text="gone")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()
    resources = [a_resource_with_boundary_url("https://files.test/missing.geojson")]

    resolve_boundary_urls(resources, report, client=client, retries=3)

    assert len(calls) == 1  # a 404 will still be a 404 on attempt 3 -- don't bother
    assert report.counts() == {"boundary_fetch_failed": 1}


def test_resolve_honours_retry_after_on_429(monkeypatch):
    sleeps = []
    monkeypatch.setattr(extract_module.time, "sleep", sleeps.append)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="slow down")
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()
    resources = [a_resource_with_boundary_url("https://files.test/a.geojson")]

    resolve_boundary_urls(resources, report, client=client, retries=3)

    assert len(calls) == 2
    assert sleeps == [2.0]
    assert report.counts() == {}


def test_resolve_reports_after_retries_exhausted_and_other_boundaries_still_resolve(
    monkeypatch,
):
    monkeypatch.setattr(extract_module.time, "sleep", lambda seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if "bad" in request.url.path:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()
    bad = a_resource_with_boundary_url("https://files.test/bad.geojson")
    bad["id"] = "loc-bad"
    good = a_resource_with_boundary_url("https://files.test/good.geojson")
    good["id"] = "loc-good"
    resources = [bad, good]

    resolve_boundary_urls(resources, report, client=client, retries=3)

    assert report.counts() == {"boundary_fetch_failed": 1}
    assert report.issues[0].location_id == "loc-bad"
    assert "data" not in bad["extension"][0]["valueAttachment"]
    good_attachment = good["extension"][0]["valueAttachment"]
    assert base64.b64decode(good_attachment["data"]) == GEOJSON


def test_resolve_with_concurrency_one_still_works():
    """--concurrency 1 must remain equivalent to the old sequential loop."""
    resources = _many_resources_with_boundary_urls(10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()

    resolve_boundary_urls(resources, report, client=client, concurrency=1)

    assert report.counts() == {}
    for resource in resources:
        attachment = resource["extension"][0]["valueAttachment"]
        assert base64.b64decode(attachment["data"]) == GEOJSON


# --- Boundary cache wiring -------------------------------------------------


def _counting_handler(response_bytes=GEOJSON, status=200):
    """A MockTransport handler that counts how many times it was invoked."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(status, content=response_bytes)

    handler.calls = calls
    return handler


def test_a_warm_cache_makes_zero_requests_and_reproduces_the_same_output(tmp_path):
    cache_dir = tmp_path / "cache"
    handler = _counting_handler()

    first_resources = _many_resources_with_boundary_urls(5)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolve_boundary_urls(first_resources, Report(), client=client, cache_dir=cache_dir)
    assert len(handler.calls) == 5

    second_resources = _many_resources_with_boundary_urls(5)
    client2 = httpx.Client(transport=httpx.MockTransport(handler))
    report2 = Report()
    resolve_boundary_urls(second_resources, report2, client=client2, cache_dir=cache_dir)

    assert len(handler.calls) == 5  # no new requests on the warm run
    assert report2.counts() == {}
    for first, second in zip(first_resources, second_resources, strict=True):
        assert (
            first["extension"][0]["valueAttachment"]["data"]
            == second["extension"][0]["valueAttachment"]["data"]
        )


def test_no_cache_dir_neither_reads_nor_writes(tmp_path):
    """cache_dir=None is what --no-cache maps to at the CLI layer: it must
    behave exactly as if caching didn't exist -- no reads, no writes."""
    cache_dir = tmp_path / "cache"
    handler = _counting_handler()

    resources = _many_resources_with_boundary_urls(3)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolve_boundary_urls(resources, Report(), client=client, cache_dir=None)

    assert len(handler.calls) == 3
    assert not cache_dir.exists()  # nothing was ever written

    # A second run over the same URLs still hits the network every time.
    resources2 = _many_resources_with_boundary_urls(3)
    client2 = httpx.Client(transport=httpx.MockTransport(handler))
    resolve_boundary_urls(resources2, Report(), client=client2, cache_dir=None)
    assert len(handler.calls) == 6


def test_refresh_re_fetches_despite_a_warm_cache_and_updates_the_entry(tmp_path):
    cache_dir = tmp_path / "cache"
    old_geojson = GEOJSON
    new_geojson = b'{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}'

    handler = _counting_handler(response_bytes=old_geojson)
    resources = [a_resource_with_boundary_url("https://files.test/a.geojson")]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolve_boundary_urls(resources, Report(), client=client, cache_dir=cache_dir)
    assert len(handler.calls) == 1

    # Warm run with the same handler would make zero requests; instead we
    # swap the handler's response and pass refresh=True, which must ignore
    # the cache and re-fetch.
    handler2 = _counting_handler(response_bytes=new_geojson)
    resources2 = [a_resource_with_boundary_url("https://files.test/a.geojson")]
    client2 = httpx.Client(transport=httpx.MockTransport(handler2))
    resolve_boundary_urls(resources2, Report(), client=client2, cache_dir=cache_dir, refresh=True)

    assert len(handler2.calls) == 1
    attachment = resources2[0]["extension"][0]["valueAttachment"]
    assert base64.b64decode(attachment["data"]) == new_geojson

    # And the cache entry itself was updated, not left stale.
    handler3 = _counting_handler(response_bytes=old_geojson)
    resources3 = [a_resource_with_boundary_url("https://files.test/a.geojson")]
    client3 = httpx.Client(transport=httpx.MockTransport(handler3))
    resolve_boundary_urls(resources3, Report(), client=client3, cache_dir=cache_dir)
    assert len(handler3.calls) == 0  # served from the refreshed cache
    attachment3 = resources3[0]["extension"][0]["valueAttachment"]
    assert base64.b64decode(attachment3["data"]) == new_geojson


def test_a_failed_fetch_is_not_cached_and_a_later_run_succeeds_normally(tmp_path):
    cache_dir = tmp_path / "cache"

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    resources = [a_resource_with_boundary_url("https://files.test/flaky.geojson")]
    client = httpx.Client(transport=httpx.MockTransport(failing_handler))
    report = Report()
    resolve_boundary_urls(resources, report, client=client, cache_dir=cache_dir)

    assert report.counts() == {"boundary_fetch_failed": 1}
    key = cache_key("https://files.test/flaky.geojson")
    assert not (cache_dir / f"{key}.bin").exists()
    assert not (cache_dir / f"{key}.meta.json").exists()

    # The URL "recovers" on a later run -- must not be permanently poisoned.
    handler = _counting_handler()
    resources2 = [a_resource_with_boundary_url("https://files.test/flaky.geojson")]
    client2 = httpx.Client(transport=httpx.MockTransport(handler))
    report2 = Report()
    resolve_boundary_urls(resources2, report2, client=client2, cache_dir=cache_dir)

    assert len(handler.calls) == 1
    assert report2.counts() == {}
    attachment = resources2[0]["extension"][0]["valueAttachment"]
    assert base64.b64decode(attachment["data"]) == GEOJSON
    assert (cache_dir / f"{key}.bin").exists()


def test_an_unwritable_cache_dir_degrades_gracefully(tmp_path):
    """A disk-full or permissions problem on the cache dir must not fail
    the export -- it degrades to no-cache-for-this-entry, reported.

    The directory itself is left readable+executable (0o555) so a cache
    *read* is a plain, unremarkable miss (the entry just isn't there yet);
    only the *write* -- creating a new temp file in a read-only directory
    -- is the operation that actually fails here, isolating this test to
    exactly the write-side failure it's meant to cover.
    """
    cache_dir = tmp_path / "boundaries"
    cache_dir.mkdir()
    cache_dir.chmod(0o555)

    try:
        handler = _counting_handler()
        resources = _many_resources_with_boundary_urls(3)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        report = Report()

        resolve_boundary_urls(resources, report, client=client, cache_dir=cache_dir)

        assert len(handler.calls) == 3  # export still happened
        assert report.counts() == {"cache_error": 3}
        for resource in resources:
            attachment = resource["extension"][0]["valueAttachment"]
            assert base64.b64decode(attachment["data"]) == GEOJSON
    finally:
        cache_dir.chmod(0o755)


def test_a_corrupt_cache_entry_does_not_crash_the_run(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    url = "https://files.test/a.geojson"
    key = cache_key(url)
    (cache_dir / f"{key}.bin").write_bytes(b"truncated garbage")
    (cache_dir / f"{key}.meta.json").write_text('{"url": "' + url + '", "sha256": "deadbeef"}')

    handler = _counting_handler()
    resources = [a_resource_with_boundary_url(url)]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()

    resolve_boundary_urls(resources, report, client=client, cache_dir=cache_dir)

    assert len(handler.calls) == 1  # fell back to a real fetch
    assert report.counts() == {"cache_error": 1}
    attachment = resources[0]["extension"][0]["valueAttachment"]
    assert base64.b64decode(attachment["data"]) == GEOJSON


def test_concurrent_workers_racing_on_the_same_url_do_not_corrupt_the_cache_entry(tmp_path):
    """Several Locations sharing one boundary URL, fetched concurrently: two
    workers can race to write the same cache entry. The entry that lands
    must always be a complete, valid one -- never a torn write."""
    cache_dir = tmp_path / "cache"
    url = "https://files.test/shared.geojson"

    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(0.01)
        return httpx.Response(200, content=GEOJSON)

    resources = []
    for i in range(20):
        resource = a_resource_with_boundary_url(url)
        resource["id"] = f"loc-{i}"
        resources.append(resource)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()
    resolve_boundary_urls(resources, report, client=client, cache_dir=cache_dir, concurrency=10)

    assert report.counts() == {}
    for resource in resources:
        attachment = resource["extension"][0]["valueAttachment"]
        assert base64.b64decode(attachment["data"]) == GEOJSON

    # The cache entry itself must be intact and re-readable.
    from kiln.cache import cache_read

    payload, error = cache_read(cache_dir, url)
    assert error is None
    assert payload == GEOJSON


# --- Retry-After edge cases -------------------------------------------------


def test_backoff_delay_is_capped_at_retry_max_delay():
    """The cap is the only defence against a hostile Retry-After (e.g.
    Retry-After: 99999) stalling a run indefinitely."""
    assert extract_module._backoff_delay(10) == extract_module._RETRY_MAX_DELAY
    assert extract_module._backoff_delay(1, retry_after=99999) == extract_module._RETRY_MAX_DELAY


def test_backoff_delay_clamps_a_negative_retry_after_to_zero():
    """A negative Retry-After must never reach time.sleep() -- it raises
    ValueError there, which used to escape all the way out of
    resolve_boundary_urls and abort the whole export."""
    assert extract_module._backoff_delay(1, retry_after=-5) == 0.0


def test_resolve_survives_a_negative_retry_after_header(monkeypatch):
    """A 429 with Retry-After: -5 must not crash the run: the negative value
    is clamped, the boundary is retried, and the run completes normally."""
    sleeps = []
    monkeypatch.setattr(extract_module.time, "sleep", sleeps.append)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "-5"}, text="slow down")
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()
    resources = [a_resource_with_boundary_url("https://files.test/a.geojson")]

    resolve_boundary_urls(resources, report, client=client, retries=3)

    assert len(calls) == 2
    assert sleeps == [0.0]  # clamped, not passed through raw and negative
    assert report.counts() == {}
    attachment = resources[0]["extension"][0]["valueAttachment"]
    assert base64.b64decode(attachment["data"]) == GEOJSON


def test_resolve_survives_a_non_numeric_retry_after_header(monkeypatch):
    """An unparseable Retry-After must not crash the run either -- it falls
    back to exponential backoff instead."""
    sleeps = []
    monkeypatch.setattr(extract_module.time, "sleep", sleeps.append)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "not-a-number"}, text="slow down")
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()
    resources = [a_resource_with_boundary_url("https://files.test/a.geojson")]

    resolve_boundary_urls(resources, report, client=client, retries=3)

    assert len(calls) == 2
    assert sleeps == [0.5]  # first exponential backoff step, not a crash
    assert report.counts() == {}


def test_resolve_honours_retry_after_on_503_too(monkeypatch):
    """README already claimed Retry-After is honoured for all retries, not
    just 429 -- the code must match that, and 503 is the other common
    source of this header."""
    sleeps = []
    monkeypatch.setattr(extract_module.time, "sleep", sleeps.append)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "7"}, text="unavailable")
        return httpx.Response(200, content=GEOJSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()
    resources = [a_resource_with_boundary_url("https://files.test/a.geojson")]

    resolve_boundary_urls(resources, report, client=client, retries=3)

    assert len(calls) == 2
    assert sleeps == [7.0]
    assert report.counts() == {}


def test_resolve_reports_a_malformed_boundary_url_instead_of_crashing():
    """httpx.InvalidURL (e.g. an unparseable port) is a plain Exception
    subclass, not an httpx.HTTPError -- it used to escape the retry loop's
    narrower except clause entirely and abort the whole export."""
    report = Report()
    resources = [a_resource_with_boundary_url("http://host:notaport/x")]
    client = httpx.Client()  # InvalidURL is raised client-side, no network needed

    resolve_boundary_urls(resources, report, client=client, retries=2)

    assert report.counts() == {"boundary_fetch_failed": 1}
    assert "data" not in resources[0]["extension"][0]["valueAttachment"]


# --- Circuit breaker for systematic failure ---------------------------------


def test_circuit_breaker_aborts_after_consecutive_failures_with_zero_successes(monkeypatch):
    """A fully down/misconfigured server (wrong token, bad base URL, ...)
    must not be retried thousands of times at TIMEOUT-per-attempt cost --
    the breaker should abort quickly instead."""
    monkeypatch.setattr(extract_module.time, "sleep", lambda seconds: None)
    resources = _many_resources_with_boundary_urls(200)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()

    with pytest.raises(extract_module.BoundaryFetchAborted, match="5 consecutive"):
        resolve_boundary_urls(
            resources,
            report,
            client=client,
            retries=1,
            concurrency=1,
            max_consecutive_failures=5,
        )

    # Aborted after exactly the threshold, not after grinding through all 200.
    assert report.counts() == {"boundary_fetch_failed": 5}


def test_circuit_breaker_does_not_trip_when_at_least_one_boundary_has_succeeded(monkeypatch):
    """A scattering of dead URLs in an otherwise-healthy registry is a data
    problem, not an environment one, and must never abort the run --
    requiring *zero* successes anywhere is what protects this case."""
    monkeypatch.setattr(extract_module.time, "sleep", lambda seconds: None)
    resources = _many_resources_with_boundary_urls(100)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/0.geojson" in str(request.url):
            return httpx.Response(200, content=GEOJSON)
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()

    resolve_boundary_urls(
        resources,
        report,
        client=client,
        retries=1,
        concurrency=1,
        max_consecutive_failures=5,
    )

    assert report.counts() == {"boundary_fetch_failed": 99}


def test_max_consecutive_failures_zero_disables_the_circuit_breaker(monkeypatch):
    monkeypatch.setattr(extract_module.time, "sleep", lambda seconds: None)
    resources = _many_resources_with_boundary_urls(80)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = Report()

    resolve_boundary_urls(
        resources,
        report,
        client=client,
        retries=1,
        concurrency=4,
        max_consecutive_failures=0,
    )

    assert report.counts() == {"boundary_fetch_failed": 80}


