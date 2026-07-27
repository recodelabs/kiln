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


def test_node_downstream_of_cycle_gets_unreachable_ancestor_not_cycle():
    report = Report()
    locations = [loc("a", parent="b"), loc("b", parent="a"), loc("c", parent="a")]

    info = resolve_hierarchy(locations, report)

    # c is not in the cycle but points to a member of the cycle
    assert "c" not in info
    # a and b are in the cycle
    assert "a" not in info
    assert "b" not in info

    # Check counts
    counts = report.counts()
    assert counts.get("cycle") == 2
    assert counts.get("unreachable_ancestor", 0) >= 1


def test_one_dangling_edge_with_descendants_produces_one_orphan_issue():
    report = Report()
    locations = [
        loc("orphan_parent", parent="missing"),
        loc("child1", parent="orphan_parent"),
        loc("child2", parent="orphan_parent"),
        loc("grandchild", parent="child1"),
    ]

    info = resolve_hierarchy(locations, report)

    # Should have exactly one orphan issue
    assert report.counts().get("orphan") == 1
    # But all nodes should resolve successfully (orphan becomes root)
    assert "orphan_parent" in info
    assert "child1" in info
    assert "child2" in info
    assert "grandchild" in info


def test_descendant_of_orphaned_ancestor_resolves_normally():
    report = Report()
    locations = [
        loc("orphan", parent="missing", pcode="XX"),
        loc("child", parent="orphan", pcode="XX001"),
        loc("grandchild", parent="child", pcode="XX001001"),
    ]

    info = resolve_hierarchy(locations, report)

    # orphan should be in output (as a root) and reported
    assert "orphan" in info
    assert info["orphan"].depth == 0
    assert info["orphan"].ancestor_ids == []

    # child and grandchild should also be in output
    assert "child" in info
    assert info["child"].depth == 1
    assert info["child"].ancestor_ids == ["orphan"]

    assert "grandchild" in info
    assert info["grandchild"].depth == 2
    assert info["grandchild"].ancestor_ids == ["orphan", "child"]

    # Only one orphan issue (for the directly orphaned node)
    assert report.counts().get("orphan") == 1


def test_max_depth_boundary():
    """Verify nodes at and around MAX_DEPTH=12 boundary resolve or fail correctly.

    With MAX_DEPTH=12, a chain of N nodes has depth N-1. Chains up to
    length 12 resolve (depths 0..11); chains longer than 12 fail with
    too_deep. This documents the exact boundary behavior.
    """
    report = Report()
    # Create a chain n0..n15 (16 nodes total)
    locations = [loc("n0")] + [loc(f"n{i}", parent=f"n{i - 1}") for i in range(1, 16)]

    info = resolve_hierarchy(locations, report)

    # Nodes n0..n11 should resolve (chain lengths 1..12 nodes, depths 0..11)
    for i in range(12):
        node_id = f"n{i}"
        assert node_id in info, f"{node_id} should resolve"
        assert info[node_id].depth == i, f"{node_id} should have depth {i}"

    # Nodes n12..n15 should be excluded (chain lengths 13..16, exceed MAX_DEPTH)
    for i in range(12, 16):
        node_id = f"n{i}"
        assert node_id not in info, f"{node_id} should be excluded (too_deep)"

    # Exactly 4 too_deep issues (one for each node past the boundary)
    assert report.counts()["too_deep"] == 4
