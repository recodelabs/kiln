import pytest
import httpx

import kiln.load as load_module
from kiln.load import LoadError, build_bundles, check_update_create, load, order_parents_first


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(load_module.time, "sleep", lambda seconds: None)


def location(location_id, parent_id=None):
    resource = {"resourceType": "Location", "id": location_id}
    if parent_id:
        resource["partOf"] = {"reference": f"Location/{parent_id}"}
    return resource


def test_order_parents_first_sorts_children_after_ancestors():
    resources = [
        location("ward", "lga"),
        location("country"),
        location("lga", "state"),
        location("state", "country"),
    ]
    ordered = [r["id"] for r in order_parents_first(resources)]
    assert ordered == ["country", "state", "lga", "ward"]


def test_order_parents_first_treats_external_parents_as_roots():
    # A partOf pointing outside the loaded set (already in the store) is fine.
    resources = [location("ward", "elsewhere"), location("country")]
    ordered = [r["id"] for r in order_parents_first(resources)]
    assert set(ordered) == {"ward", "country"}


def test_order_parents_first_rejects_a_cycle():
    with pytest.raises(LoadError, match="cycle"):
        order_parents_first([location("a", "b"), location("b", "a")])


def test_build_bundles_chunks_put_entries_parents_first():
    resources = [location("ward", "country"), location("country")]
    bundles = build_bundles(resources, batch_size=1)

    assert [b["resourceType"] for b in bundles] == ["Bundle", "Bundle"]
    assert all(b["type"] == "transaction" for b in bundles)
    first_entry = bundles[0]["entry"][0]
    assert first_entry["resource"]["id"] == "country"
    assert first_entry["request"] == {"method": "PUT", "url": "Location/country"}
    assert bundles[1]["entry"][0]["request"]["url"] == "Location/ward"


CAPABILITY_OK = {
    "resourceType": "CapabilityStatement",
    "rest": [
        {
            "mode": "server",
            "resource": [
                {"type": "Patient", "updateCreate": False},
                {"type": "Location", "updateCreate": True},
            ],
        }
    ],
}


def capability(update_create):
    payload = {
        "resourceType": "CapabilityStatement",
        "rest": [{"mode": "server", "resource": [{"type": "Location"}]}],
    }
    if update_create is not None:
        payload["rest"][0]["resource"][0]["updateCreate"] = update_create
    return payload


def test_check_update_create_passes_when_the_store_supports_it():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/metadata")
        return httpx.Response(200, json=CAPABILITY_OK)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    check_update_create("https://fhir.test/store/fhir", None, client)


def test_check_update_create_rejects_a_store_without_it():
    for payload in (capability(False), capability(None)):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda req, p=payload: httpx.Response(200, json=p))
        )
        with pytest.raises(LoadError, match="enableUpdateCreate"):
            check_update_create("https://fhir.test/store/fhir", None, client)


def test_load_puts_every_resource_and_returns_the_count():
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json=CAPABILITY_OK)
        assert request.method == "POST"
        import json as json_module

        posted.append(json_module.loads(request.content))
        return httpx.Response(
            200, json={"resourceType": "Bundle", "type": "transaction-response"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resources = [
        {"resourceType": "Location", "id": "country"},
        {"resourceType": "Location", "id": "state",
         "partOf": {"reference": "Location/country"}},
    ]

    count = load(resources, "https://fhir.test/store/fhir", "tok", client=client,
                 batch_size=1)

    assert count == 2
    assert len(posted) == 2
    assert posted[0]["entry"][0]["request"]["url"] == "Location/country"


def test_load_retries_a_503_then_succeeds():
    calls = {"bundle": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json=CAPABILITY_OK)
        calls["bundle"] += 1
        if calls["bundle"] == 1:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"resourceType": "Bundle", "type": "transaction-response"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    count = load(
        [{"resourceType": "Location", "id": "x"}],
        "https://fhir.test/store/fhir",
        None,
        client=client,
    )
    assert count == 1
    assert calls["bundle"] == 2


def test_load_rejects_a_non_dict_resource_without_any_network_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected network call: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(LoadError, match="index 1"):
        load(
            [{"resourceType": "Location", "id": "country"}, 5],
            "https://fhir.test/store/fhir",
            None,
            client=client,
        )


def test_load_rejects_a_resource_without_an_id():
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected network call: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(LoadError, match="index 0"):
        load(
            [{"resourceType": "Location"}],
            "https://fhir.test/store/fhir",
            None,
            client=client,
        )


def test_load_rejects_duplicate_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected network call: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(LoadError, match="country"):
        load(
            [
                {"resourceType": "Location", "id": "country"},
                {"resourceType": "Location", "id": "country"},
            ],
            "https://fhir.test/store/fhir",
            None,
            client=client,
        )


def test_load_raises_with_the_server_body_after_exhausting_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json=CAPABILITY_OK)
        return httpx.Response(
            400,
            json={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "details": {"text": "bad partOf"}}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(LoadError, match="bad partOf"):
        load(
            [{"resourceType": "Location", "id": "x"}],
            "https://fhir.test/store/fhir",
            None,
            client=client,
        )
