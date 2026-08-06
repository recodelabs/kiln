import pytest

from kiln.load import LoadError, build_bundles, order_parents_first


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
