import json

from kiln.report import Report


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
