import pytest

from kiln.hierarchy import resolve_hierarchy
from kiln.profile import RawLocation
from kiln.report import Report


def loc(id_, parent=None, loc_type="admin-unit", name=None, pcode=None) -> RawLocation:
    return RawLocation(
        id=id_,
        name=name or id_,
        loc_type=loc_type,
        parent_id=parent,
        pcode=pcode,
    )


@pytest.fixture
def ragged_tree() -> list[RawLocation]:
    """Nigeria > Kano > Nassarawa > Gama (settlement) > Clinic (facility)."""
    return [
        loc("ng", name="Nigeria", pcode="NG"),
        loc("kano", parent="ng", name="Kano", pcode="NG001"),
        loc("nassarawa", parent="kano", name="Nassarawa", pcode="NG001002"),
        loc("gama", parent="nassarawa", name="Gama", loc_type="settlement"),
        loc("clinic", parent="gama", name="Clinic", loc_type="facility"),
    ]


def test_admin_units_get_a_level_equal_to_their_admin_depth(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())

    assert info["ng"].admin_level == 0
    assert info["kano"].admin_level == 1
    assert info["nassarawa"].admin_level == 2


def test_non_admin_types_have_no_admin_level(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())

    assert info["gama"].admin_level is None
    assert info["clinic"].admin_level is None


def test_depth_counts_every_node_not_just_admin_units(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())

    assert info["clinic"].depth == 4
    assert info["ng"].depth == 0


def test_a_facility_inherits_the_nearest_admin_ancestors_not_hop_count(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())["clinic"]

    assert info.admin_names[:3] == ["Nigeria", "Kano", "Nassarawa"]
    assert info.admin_codes[:3] == ["NG", "NG001", "NG001002"]
    assert info.admin_names[3] is None


def test_country_comes_from_the_root_admin_code(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())

    assert info["clinic"].country == "NG"
    assert info["ng"].country == "NG"


def test_path_and_ancestors_run_root_first(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())["clinic"]

    assert info.path == "/ng/kano/nassarawa/gama/clinic"
    assert info.ancestor_ids == ["ng", "kano", "nassarawa", "gama"]


def test_a_root_node_has_no_ancestors(ragged_tree):
    info = resolve_hierarchy(ragged_tree, Report())["ng"]

    assert info.ancestor_ids == []
    assert info.path == "/ng"


def test_a_dangling_parent_makes_the_node_a_root_and_is_reported():
    report = Report()
    locations = [loc("orphan", parent="missing", pcode="XX")]

    info = resolve_hierarchy(locations, report)

    assert info["orphan"].depth == 0
    assert info["orphan"].ancestor_ids == []
    assert report.counts() == {"orphan": 1}
    assert "missing" in report.issues[0].detail


def test_a_cycle_is_reported_with_its_path_and_excluded_from_output():
    report = Report()
    locations = [loc("a", parent="b"), loc("b", parent="a")]

    info = resolve_hierarchy(locations, report)

    assert "a" not in info
    assert "b" not in info
    assert report.counts()["cycle"] == 2
    assert "->" in report.issues[0].detail


def test_a_chain_deeper_than_max_depth_is_reported_as_too_deep():
    report = Report()
    locations = [loc("n0")] + [loc(f"n{i}", parent=f"n{i - 1}") for i in range(1, 20)]

    info = resolve_hierarchy(locations, report)

    assert "n19" not in info
    assert report.counts()["too_deep"] >= 1


def test_admin_columns_are_capped_at_five_levels():
    locations = [loc("a0")] + [loc(f"a{i}", parent=f"a{i - 1}") for i in range(1, 8)]

    info = resolve_hierarchy(locations, Report())["a7"]

    assert len(info.admin_names) == 5
    assert len(info.admin_codes) == 5
