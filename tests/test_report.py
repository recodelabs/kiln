import json

from kiln.report import MAX_RETAINED_ISSUES_PER_KIND, Report, check_points_within_parents


def test_report_counts_issues_by_kind():
    report = Report()
    report.add("orphan", "loc-1", "partOf references missing id loc-99")
    report.add("orphan", "loc-2", "partOf references missing id loc-99")
    report.add("cycle", "loc-3", "loc-3 -> loc-4 -> loc-3")

    assert report.counts() == {"orphan": 2, "cycle": 1}


def test_report_retains_detail_for_each_issue():
    report = Report()
    report.add("orphan", "loc-1", "partOf references missing id loc-99")

    assert len(report.issues) == 1
    assert report.issues[0].kind == "orphan"
    assert report.issues[0].location_id == "loc-1"
    assert report.issues[0].detail == "partOf references missing id loc-99"


def test_report_to_dict_is_json_serializable():
    report = Report()
    report.add("cycle", "loc-3", "loc-3 -> loc-4 -> loc-3")

    payload = json.loads(json.dumps(report.to_dict()))

    assert payload["counts"] == {"cycle": 1}
    assert payload["issues"][0]["location_id"] == "loc-3"


def test_empty_report_summary_says_no_issues():
    assert Report().summary() == "No issues found."


def test_summary_lists_each_kind_with_a_count():
    report = Report()
    report.add("orphan", "loc-1", "x")
    report.add("orphan", "loc-2", "y")

    assert "orphan: 2" in Report.summary(report)


def test_counts_stay_exact_even_when_the_retained_list_is_capped():
    """A systematic fault can raise one issue per row; at scale that can
    make _report.json bigger than the dataset it describes. The retained
    issues list is capped, but counts() -- what users act on -- must never
    reflect the cap.
    """
    report = Report()
    extra = 5
    for i in range(MAX_RETAINED_ISSUES_PER_KIND + extra):
        report.add("point_outside_parent", f"loc-{i}", "detail")

    assert report.counts() == {"point_outside_parent": MAX_RETAINED_ISSUES_PER_KIND + extra}
    assert len(report.issues) == MAX_RETAINED_ISSUES_PER_KIND


def test_to_dict_flags_truncated_kinds():
    report = Report()
    for i in range(MAX_RETAINED_ISSUES_PER_KIND + 5):
        report.add("point_outside_parent", f"loc-{i}", "detail")
    report.add("orphan", "loc-x", "detail")

    payload = report.to_dict()

    assert payload["truncated"] == {"point_outside_parent": 5}
    assert "orphan" not in payload["truncated"]


def test_to_dict_truncated_is_empty_when_nothing_was_capped():
    report = Report()
    report.add("orphan", "loc-1", "x")

    assert report.to_dict()["truncated"] == {}


def test_summary_mentions_the_cap_only_when_it_triggers():
    report = Report()
    report.add("orphan", "loc-1", "x")
    assert "capped" not in report.summary()

    for i in range(MAX_RETAINED_ISSUES_PER_KIND + 1):
        report.add("point_outside_parent", f"loc-{i}", "detail")

    summary = report.summary()
    assert "capped" in summary
    assert "point_outside_parent (+1 more)" in summary


def test_points_with_a_nan_lon_are_skipped_rather_than_flagged():
    """lon is float64: a missing value surfaces as NaN, never None. The old
    `row.lon is None` guard never fired, so a row with no real point data
    could be spuriously flagged as falling outside its parent polygon.
    """
    import geopandas as gpd
    import shapely

    frame = gpd.GeoDataFrame(
        {
            "id": ["admin", "site"],
            "geom_type": ["polygon", "point"],
            "lon": [5.0, float("nan")],
            "lat": [5.0, float("nan")],
            "ancestor_ids": [[], ["admin"]],
        },
        geometry=[shapely.box(0, 0, 10, 10), shapely.Point(0, 0)],
    )
    report = Report()

    check_points_within_parents(frame, report)

    assert report.counts() == {}
