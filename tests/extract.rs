//! Binary-level tests for `kiln extract` against a fake FHIR server.

use std::path::Path;
use std::process::Command;

use assert_cmd::assert::Assert;
use assert_cmd::prelude::*;
use httptest::{all_of, cycle, matchers::*, responders::*, Expectation, Server};
use serde_json::{json, Value};

const BOUNDARY_EXT: &str =
    "https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson";
const GEOJSON: &str = r#"{"type":"Point","coordinates":[3.25,6.25]}"#;

fn loc(id: &str, updated: &str, boundary: Option<&str>) -> Value {
    let mut r = json!({
        "resourceType": "Location",
        "id": id,
        "meta": {"lastUpdated": updated},
        "status": "active",
        "name": id,
        "position": {"longitude": 3.25, "latitude": 6.25},
        "type": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/location-physical-type",
            "code": "facility"
        }]}],
    });
    if let Some(url) = boundary {
        r["extension"] = json!([{
            "url": BOUNDARY_EXT,
            "valueAttachment": {"contentType": "application/geo+json", "url": url},
        }]);
    }
    r
}

fn bundle(entries: Vec<Value>, next: Option<&str>) -> String {
    let mut b = json!({
        "resourceType": "Bundle",
        "type": "searchset",
        "total": entries.len(),
        "entry": entries
            .into_iter()
            .map(|r| json!({"resource": r}))
            .collect::<Vec<_>>(),
    });
    if let Some(n) = next {
        b["link"] = json!([{"relation": "next", "url": n}]);
    }
    b.to_string()
}

fn kiln() -> Command {
    let mut c = Command::cargo_bin("kiln").unwrap();
    c.env_remove("KILN_TOKEN");
    c
}

fn extract_at(base: &str, snapshot: &Path, extra: &[&str]) -> Assert {
    kiln()
        .args(["extract", "--server", base, "--snapshot"])
        .arg(snapshot)
        .args(extra)
        .assert()
}

fn extract(server: &Server, snapshot: &Path, extra: &[&str]) -> Assert {
    extract_at(&server.url("/fhir").to_string(), snapshot, extra)
}

fn stdout(a: &Assert) -> String {
    String::from_utf8_lossy(&a.get_output().stdout).into_owned()
}

fn stderr(a: &Assert) -> String {
    String::from_utf8_lossy(&a.get_output().stderr).into_owned()
}

fn state(snapshot: &Path) -> Value {
    serde_json::from_str(&std::fs::read_to_string(snapshot.join("state.json")).unwrap()).unwrap()
}

fn lines(snapshot: &Path) -> Vec<Value> {
    std::fs::read_to_string(snapshot.join("locations.ndjson"))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

fn attachment(resource: &Value) -> &Value {
    &resource["extension"][0]["valueAttachment"]
}

fn inlined_bytes(resource: &Value) -> Vec<u8> {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD
        .decode(attachment(resource)["data"].as_str().unwrap())
        .unwrap()
}

type Req = httptest::http::Request<httptest::bytes::Bytes>;

/// `GET <base>/Location` with no `_lastUpdated` parameter: a full extract.
fn full_search() -> impl Matcher<Req> {
    all_of![
        request::method_path("GET", "/fhir/Location"),
        request::query(url_decoded(not(contains(key("_lastUpdated"))))),
    ]
}

/// `GET <base>/Location?_lastUpdated=ge<since>`: an incremental extract.
fn since_search(since: &str) -> impl Matcher<Req> {
    all_of![
        request::method_path("GET", "/fhir/Location"),
        request::query(url_decoded(contains((
            "_lastUpdated",
            format!("ge{since}")
        )))),
    ]
}

fn ok(body: String) -> ResponseBuilder<String> {
    status_code(200)
        .insert_header("content-type", "application/fhir+json")
        .body(body)
}

#[test]
fn full_extract_writes_snapshot_state_and_inlines_boundaries() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    let burl = server.url("/b/b.geojson").to_string();
    server.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(
                vec![
                    loc("a", "2026-01-01T00:00:00Z", None),
                    loc("b", "2026-01-02T00:00:00Z", Some(&burl)),
                ],
                None,
            ))),
    );
    server.expect(
        Expectation::matching(request::method_path("GET", "/b/b.geojson"))
            .times(1)
            .respond_with(status_code(200).body(GEOJSON)),
    );

    // A trailing slash on --server must not survive into state.json.
    let assert = extract_at(&format!("{}/", server.url("/fhir")), snap.path(), &[]);
    let assert = assert.success();

    let rows = lines(snap.path());
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0]["id"], "a");
    assert_eq!(rows[1]["id"], "b");
    assert!(attachment(&rows[1])["data"].is_string());
    assert!(attachment(&rows[1])["url"].is_null());
    assert_eq!(inlined_bytes(&rows[1]), GEOJSON.as_bytes());
    assert_eq!(attachment(&rows[1])["contentType"], "application/geo+json");

    let st = state(snap.path());
    assert_eq!(st["server"], server.url("/fhir").to_string());
    assert_eq!(st["watermark"], "2026-01-02T00:00:00Z");
    assert_eq!(st["count"], 2);
    assert_eq!(st["kiln_version"], env!("CARGO_PKG_VERSION"));
    assert!(st["completed_at"].as_str().unwrap().ends_with('Z'));

    let cached: Vec<_> = std::fs::read_dir(snap.path().join("boundaries"))
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    assert_eq!(cached.len(), 2, "{cached:?}");

    let err = stderr(&assert);
    assert!(
        err.contains("boundaries: 0 cached, 1 fetched, 0 failed"),
        "{err}"
    );
    let out = stdout(&assert);
    assert!(
        out.contains("snapshot: 2 resources, 2 new, 0 updated"),
        "{out}"
    );

    let report: Value = serde_json::from_str(
        &std::fs::read_to_string(snap.path().join("_extract_report.json")).unwrap(),
    )
    .unwrap();
    assert!(report["counts"].is_object(), "{report}");
    assert!(!snap.path().join(".incoming.ndjson").exists());
}

#[test]
fn second_run_with_nothing_new_leaves_the_snapshot_byte_identical() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    let burl = server.url("/b/b.geojson").to_string();
    server.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(
                vec![
                    loc("a", "2026-01-01T00:00:00Z", None),
                    loc("b", "2026-01-02T00:00:00Z", Some(&burl)),
                ],
                None,
            ))),
    );
    server.expect(
        Expectation::matching(since_search("2026-01-02T00:00:00Z"))
            .times(1)
            .respond_with(ok(bundle(vec![], None))),
    );
    server.expect(
        Expectation::matching(request::method_path("GET", "/b/b.geojson"))
            .times(1)
            .respond_with(status_code(200).body(GEOJSON)),
    );

    extract(&server, snap.path(), &[]).success();
    let first = std::fs::read(snap.path().join("locations.ndjson")).unwrap();
    let count = state(snap.path())["count"].clone();

    let second = extract(&server, snap.path(), &[]).success();
    let out = stdout(&second);
    assert!(out.contains("0 new, 0 updated"), "{out}");
    let err = stderr(&second);
    assert!(
        err.contains("incremental extract since 2026-01-02T00:00:00Z"),
        "{err}"
    );
    assert!(
        err.contains("boundaries: 0 cached, 0 fetched, 0 failed"),
        "{err}"
    );
    assert_eq!(
        std::fs::read(snap.path().join("locations.ndjson")).unwrap(),
        first
    );
    assert_eq!(state(snap.path())["count"], count);
}

#[test]
fn incremental_run_upserts_and_advances_the_watermark() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    let burl = server.url("/b/b.geojson").to_string();
    server.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(
                vec![
                    loc("a", "2026-01-01T00:00:00Z", None),
                    loc("b", "2026-01-02T00:00:00Z", None),
                ],
                None,
            ))),
    );
    server.expect(
        Expectation::matching(since_search("2026-01-02T00:00:00Z"))
            .times(1)
            .respond_with(ok(bundle(
                vec![
                    loc("b", "2026-01-03T00:00:00Z", Some(&burl)),
                    loc("c", "2026-01-04T00:00:00Z", None),
                ],
                None,
            ))),
    );
    server.expect(
        Expectation::matching(request::method_path("GET", "/b/b.geojson"))
            .times(1)
            .respond_with(status_code(200).body(GEOJSON)),
    );

    extract(&server, snap.path(), &[]).success();
    let second = extract(&server, snap.path(), &[]).success();

    let rows = lines(snap.path());
    assert_eq!(rows.len(), 3);
    assert_eq!(
        rows.iter()
            .map(|r| r["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["a", "b", "c"]
    );
    assert_eq!(inlined_bytes(&rows[1]), GEOJSON.as_bytes());
    let out = stdout(&second);
    assert!(out.contains("1 new, 1 updated"), "{out}");
    assert_eq!(state(snap.path())["watermark"], "2026-01-04T00:00:00Z");
    assert_eq!(state(snap.path())["count"], 3);
}

#[test]
fn full_flag_replaces_the_snapshot() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    server.expect(
        Expectation::matching(full_search())
            .times(2)
            .respond_with(cycle![
                ok(bundle(
                    vec![
                        loc("a", "2026-01-01T00:00:00Z", None),
                        loc("b", "2026-01-02T00:00:00Z", None),
                    ],
                    None,
                )),
                ok(bundle(vec![loc("a", "2026-01-01T00:00:00Z", None)], None,)),
            ]),
    );

    extract(&server, snap.path(), &[]).success();
    assert_eq!(lines(snap.path()).len(), 2);

    let second = extract(&server, snap.path(), &["--full"]).success();
    let rows = lines(snap.path());
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["id"], "a");
    let err = stderr(&second);
    assert!(err.contains("full extract"), "{err}");
    assert_eq!(state(snap.path())["count"], 1);
}

#[test]
fn snapshot_without_state_is_treated_as_full_unless_since() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    std::fs::write(
        snap.path().join("locations.ndjson"),
        format!("{}\n", loc("old", "2026-01-01T00:00:00Z", None)),
    )
    .unwrap();
    server.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(
                vec![loc("a", "2026-01-02T00:00:00Z", None)],
                None,
            ))),
    );
    server.expect(
        Expectation::matching(since_search("2026-01-01T00:00:00Z"))
            .times(1)
            .respond_with(ok(bundle(vec![], None))),
    );

    // No state.json: full, so `old` is dropped.
    extract(&server, snap.path(), &[]).success();
    let rows = lines(snap.path());
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["id"], "a");

    // --since overrides the stored watermark (2026-01-02) with an earlier one.
    extract(&server, snap.path(), &["--since", "2026-01-01T00:00:00Z"]).success();
}

#[test]
fn server_mismatch_is_a_usage_error_and_full_repoints() {
    let a = Server::run();
    let b = Server::run();
    let snap = tempfile::tempdir().unwrap();
    a.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(
                vec![loc("a", "2026-01-01T00:00:00Z", None)],
                None,
            ))),
    );
    b.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(
                vec![loc("z", "2026-01-05T00:00:00Z", None)],
                None,
            ))),
    );

    extract(&a, snap.path(), &[]).success();
    let mismatch = extract(&b, snap.path(), &[]).failure().code(2);
    let err = stderr(&mismatch);
    assert!(err.contains("--full"), "{err}");
    assert_eq!(state(snap.path())["server"], a.url("/fhir").to_string());

    extract(&b, snap.path(), &["--full"]).success();
    assert_eq!(state(snap.path())["server"], b.url("/fhir").to_string());
    assert_eq!(lines(snap.path())[0]["id"], "z");
}

#[test]
fn circuit_breaker_aborts_and_keeps_the_old_snapshot() {
    let good = Server::run();
    let bad = Server::run();
    let snap = tempfile::tempdir().unwrap();
    good.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(
                vec![loc("a", "2026-01-01T00:00:00Z", None)],
                None,
            ))),
    );
    extract(&good, snap.path(), &[]).success();
    let before = std::fs::read(snap.path().join("locations.ndjson")).unwrap();
    let state_before = std::fs::read(snap.path().join("state.json")).unwrap();

    let broken: Vec<Value> = (0..8)
        .map(|i| {
            loc(
                &format!("x{i}"),
                "2026-02-01T00:00:00Z",
                Some(&bad.url(&format!("/b/x{i}.geojson")).to_string()),
            )
        })
        .collect();
    bad.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(broken, None))),
    );
    bad.expect(
        Expectation::matching(request::path(matches("^/b/")))
            .times(0..)
            .respond_with(status_code(500).body("boom")),
    );

    let assert = extract(
        &bad,
        snap.path(),
        &[
            "--full",
            "--retries",
            "1",
            "--max-consecutive-failures",
            "3",
        ],
    )
    .failure()
    .code(1);
    let err = stderr(&assert);
    assert!(err.contains("consecutive"), "{err}");

    assert_eq!(
        std::fs::read(snap.path().join("locations.ndjson")).unwrap(),
        before
    );
    assert_eq!(lines(snap.path()).len(), 1);
    assert_eq!(
        std::fs::read(snap.path().join("state.json")).unwrap(),
        state_before
    );
}

#[test]
fn conflicting_flags_and_a_bad_since_are_usage_errors() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();

    let a = extract(&server, snap.path(), &["--no-cache", "--refresh"])
        .failure()
        .code(2);
    assert!(stderr(&a).contains("--no-cache"), "{}", stderr(&a));

    let b = extract(
        &server,
        snap.path(),
        &["--no-cache", "--cache-dir", "/tmp/x"],
    )
    .failure()
    .code(2);
    assert!(stderr(&b).contains("--no-cache"), "{}", stderr(&b));

    let c = extract(&server, snap.path(), &["--since", "nope"])
        .failure()
        .code(2);
    assert!(stderr(&c).contains("--since"), "{}", stderr(&c));

    let d = extract(&server, snap.path(), &["--concurrency", "0"])
        .failure()
        .code(2);
    assert!(stderr(&d).contains("--concurrency"), "{}", stderr(&d));

    let e = extract(
        &server,
        snap.path(),
        &["--full", "--since", "2026-01-01T00:00:00Z"],
    )
    .failure()
    .code(2);
    assert!(stderr(&e).contains("--since"), "{}", stderr(&e));

    // A --snapshot that cannot be a directory is the operator's mistake.
    let file = snap.path().join("not-a-dir");
    std::fs::write(&file, b"x").unwrap();
    let f = extract_at(&server.url("/fhir").to_string(), &file, &[])
        .failure()
        .code(2);
    assert!(stderr(&f).contains("--snapshot"), "{}", stderr(&f));
}

#[test]
fn extract_report_counts_skipped_entries() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    server.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(
                vec![
                    json!({"resourceType": "Location"}),
                    loc("a", "2026-01-01T00:00:00Z", None),
                ],
                None,
            ))),
    );

    extract(&server, snap.path(), &[]).success();
    let report: Value = serde_json::from_str(
        &std::fs::read_to_string(snap.path().join("_extract_report.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(report["counts"]["page_resource_skipped"], 1);
    assert_eq!(lines(snap.path()).len(), 1);
}

#[test]
fn no_cache_still_inlines() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    let burl = server.url("/b/b.geojson").to_string();
    server.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(
                vec![loc("b", "2026-01-02T00:00:00Z", Some(&burl))],
                None,
            ))),
    );
    server.expect(
        Expectation::matching(request::method_path("GET", "/b/b.geojson"))
            .times(1)
            .respond_with(status_code(200).body(GEOJSON)),
    );

    extract(&server, snap.path(), &["--no-cache"]).success();
    let rows = lines(snap.path());
    assert_eq!(inlined_bytes(&rows[0]), GEOJSON.as_bytes());
    assert!(!snap.path().join("boundaries").exists());
}

#[test]
fn second_run_hits_the_cache() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    let burl = server.url("/b/b.geojson").to_string();
    let rows = vec![loc("b", "2026-01-02T00:00:00Z", Some(&burl))];
    server.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(rows.clone(), None))),
    );
    // The `ge` comparison refetches the row that set the watermark.
    server.expect(
        Expectation::matching(since_search("2026-01-02T00:00:00Z"))
            .times(1)
            .respond_with(ok(bundle(rows, None))),
    );
    server.expect(
        Expectation::matching(request::method_path("GET", "/b/b.geojson"))
            .times(1)
            .respond_with(status_code(200).body(GEOJSON)),
    );

    extract(&server, snap.path(), &[]).success();
    let second = extract(&server, snap.path(), &[]).success();
    let err = stderr(&second);
    assert!(
        err.contains("boundaries: 1 cached, 0 fetched, 0 failed"),
        "{err}"
    );
    let out = stdout(&second);
    assert!(out.contains("1 resources, 0 new, 1 updated"), "{out}");
    assert_eq!(inlined_bytes(&lines(snap.path())[0]), GEOJSON.as_bytes());
}

#[test]
fn a_401_on_the_search_keeps_the_old_snapshot() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    server.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(
                vec![loc("a", "2026-01-01T00:00:00Z", None)],
                None,
            ))),
    );
    server.expect(
        Expectation::matching(since_search("2026-01-01T00:00:00Z"))
            .times(1)
            .respond_with(status_code(401).body("no token for you")),
    );

    extract(&server, snap.path(), &[]).success();
    let before = std::fs::read(snap.path().join("locations.ndjson")).unwrap();
    let state_before = std::fs::read(snap.path().join("state.json")).unwrap();

    let assert = extract(&server, snap.path(), &[]).failure().code(1);
    assert!(stderr(&assert).contains("401"), "{}", stderr(&assert));
    assert_eq!(
        std::fs::read(snap.path().join("locations.ndjson")).unwrap(),
        before
    );
    assert_eq!(
        std::fs::read(snap.path().join("state.json")).unwrap(),
        state_before
    );
    assert!(!snap.path().join(".incoming.ndjson").exists());
}

#[test]
fn cache_dir_is_shared_between_snapshots() {
    let server = Server::run();
    let a = tempfile::tempdir().unwrap();
    let b = tempfile::tempdir().unwrap();
    let burl = server.url("/b/b.geojson").to_string();
    server.expect(
        Expectation::matching(full_search())
            .times(2)
            .respond_with(ok(bundle(
                vec![loc("b", "2026-01-02T00:00:00Z", Some(&burl))],
                None,
            ))),
    );
    server.expect(
        Expectation::matching(request::method_path("GET", "/b/b.geojson"))
            .times(1)
            .respond_with(status_code(200).body(GEOJSON)),
    );

    extract(&server, a.path(), &[]).success();
    let shared = a.path().join("boundaries");
    let second = extract(
        &server,
        b.path(),
        &["--cache-dir", shared.to_str().unwrap()],
    )
    .success();
    let err = stderr(&second);
    assert!(
        err.contains("boundaries: 1 cached, 0 fetched, 0 failed"),
        "{err}"
    );
    assert_eq!(inlined_bytes(&lines(b.path())[0]), GEOJSON.as_bytes());
    assert!(!b.path().join("boundaries").exists());
}

#[test]
fn a_python_written_cache_entry_is_honoured() {
    use sha2::{Digest, Sha256};
    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }

    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    let burl = server.url("/b/b.geojson").to_string();
    server.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(
                vec![loc("b", "2026-01-02T00:00:00Z", Some(&burl))],
                None,
            ))),
    );
    // The boundary endpoint must never be touched.
    server.expect(
        Expectation::matching(request::method_path("GET", "/b/b.geojson"))
            .times(0)
            .respond_with(status_code(500)),
    );

    let dir = snap.path().join("boundaries");
    std::fs::create_dir_all(&dir).unwrap();
    let key = hex(&Sha256::digest(burl.as_bytes()));
    std::fs::write(dir.join(format!("{key}.bin")), GEOJSON).unwrap();
    std::fs::write(
        dir.join(format!("{key}.meta.json")),
        serde_json::to_string(&json!({
            "url": burl,
            "fetched_at": "2026-01-01T00:00:00Z",
            "sha256": hex(&Sha256::digest(GEOJSON.as_bytes())),
        }))
        .unwrap(),
    )
    .unwrap();

    let assert = extract(&server, snap.path(), &[]).success();
    let err = stderr(&assert);
    assert!(
        err.contains("boundaries: 1 cached, 0 fetched, 0 failed"),
        "{err}"
    );
    assert_eq!(inlined_bytes(&lines(snap.path())[0]), GEOJSON.as_bytes());
}

#[test]
fn refresh_with_a_failing_refetch_reports_stale_cache() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    let burl = server.url("/b/b.geojson").to_string();
    server.expect(
        Expectation::matching(full_search())
            .times(2)
            .respond_with(ok(bundle(
                vec![loc("b", "2026-01-02T00:00:00Z", Some(&burl))],
                None,
            ))),
    );
    server.expect(
        Expectation::matching(request::method_path("GET", "/b/b.geojson"))
            .times(2)
            .respond_with(cycle![
                status_code(200).body(GEOJSON),
                status_code(500).body("boom"),
            ]),
    );

    extract(&server, snap.path(), &[]).success();
    extract(
        &server,
        snap.path(),
        &["--full", "--refresh", "--retries", "1"],
    )
    .success();

    let report: Value = serde_json::from_str(
        &std::fs::read_to_string(snap.path().join("_extract_report.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(report["counts"]["boundary_stale_from_cache"], 1, "{report}");
    assert!(
        report["counts"]["boundary_fetch_failed"].is_null(),
        "{report}"
    );
    // The last known good copy is still inlined.
    assert_eq!(inlined_bytes(&lines(snap.path())[0]), GEOJSON.as_bytes());
}

#[test]
fn a_boundary_404_leaves_the_url_and_reports() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    let burl = server.url("/b/b.geojson").to_string();
    server.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(
                vec![loc("b", "2026-01-02T00:00:00Z", Some(&burl))],
                None,
            ))),
    );
    server.expect(
        Expectation::matching(request::method_path("GET", "/b/b.geojson"))
            .times(1)
            .respond_with(status_code(404).body("gone")),
    );

    let assert = extract(&server, snap.path(), &[]).success();
    let err = stderr(&assert);
    assert!(
        err.contains("boundaries: 0 cached, 0 fetched, 1 failed"),
        "{err}"
    );
    let rows = lines(snap.path());
    assert_eq!(attachment(&rows[0])["url"], burl);
    assert!(attachment(&rows[0])["data"].is_null());
    let report: Value = serde_json::from_str(
        &std::fs::read_to_string(snap.path().join("_extract_report.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(report["counts"]["boundary_fetch_failed"], 1, "{report}");
}
