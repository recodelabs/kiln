use std::path::Path;
use std::process::Command;

use assert_cmd::prelude::*;

fn fixture_snapshot() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/snapshot")
}

#[test]
fn transform_writes_dataset_and_report() {
    let out = tempfile::tempdir().unwrap();
    let assert = Command::cargo_bin("kiln")
        .unwrap()
        .args(["transform", "--snapshot"])
        .arg(fixture_snapshot())
        .arg("--out")
        .arg(out.path())
        .assert()
        .success();
    let stdout = String::from_utf8(assert.get_output().stdout.clone()).unwrap();
    assert!(
        stdout.contains("Wrote 8 rows across 4 partitions"),
        "{stdout}"
    );
    assert!(stdout.contains("cycle: 2"), "{stdout}");

    let report: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(out.path().join("_report.json")).unwrap())
            .unwrap();
    assert_eq!(report["counts"]["orphan"], 1);
    assert_eq!(report["counts"]["point_outside_parent"], 1);
    assert_eq!(report["counts"]["duplicate_pcode"], 1);
    assert!(out
        .path()
        .join("locations/country=NG/geom_type=polygon/type=admin-unit/part-0.parquet")
        .exists());
}

#[test]
fn transform_rejects_missing_snapshot_with_exit_2() {
    let out = tempfile::tempdir().unwrap();
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["transform", "--snapshot", "/nonexistent", "--out"])
        .arg(out.path())
        .assert()
        .failure()
        .code(2)
        .stderr(predicates::str::contains("locations.ndjson"));
}

#[test]
fn transform_rejects_bad_partition_key_with_exit_2() {
    let out = tempfile::tempdir().unwrap();
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["transform", "--snapshot"])
        .arg(fixture_snapshot())
        .arg("--out")
        .arg(out.path())
        .args(["--partition-by", "country,nope"])
        .assert()
        .failure()
        .code(2)
        .stderr(predicates::str::contains("nope"));
}

#[test]
fn transform_rejects_an_out_path_that_is_a_file() {
    let dir = tempfile::tempdir().unwrap();
    let out_file = dir.path().join("not-a-dir");
    std::fs::write(&out_file, "not a directory").unwrap();
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["transform", "--snapshot"])
        .arg(fixture_snapshot())
        .arg("--out")
        .arg(&out_file)
        .assert()
        .failure()
        .code(2)
        .stderr(predicates::str::contains("--out"));
}

#[test]
fn a_stale_report_is_replaced() {
    let out = tempfile::tempdir().unwrap();
    std::fs::write(out.path().join("_report.json"), "stale").unwrap();
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["transform", "--snapshot"])
        .arg(fixture_snapshot())
        .arg("--out")
        .arg(out.path())
        .assert()
        .success();
    let text = std::fs::read_to_string(out.path().join("_report.json")).unwrap();
    assert!(text.contains("\"counts\""), "{text}");
}

#[cfg(unix)]
#[test]
fn a_failed_transform_removes_the_stale_report_and_keeps_the_dataset() {
    use std::os::unix::fs::symlink;

    let out = tempfile::tempdir().unwrap();
    let real = out.path().join("empty-real-dir");
    std::fs::create_dir_all(&real).unwrap();
    symlink(&real, out.path().join("locations")).unwrap();
    std::fs::write(out.path().join("_report.json"), "stale").unwrap();

    Command::cargo_bin("kiln")
        .unwrap()
        .args(["transform", "--snapshot"])
        .arg(fixture_snapshot())
        .arg("--out")
        .arg(out.path())
        .assert()
        .failure()
        .code(2);

    assert!(!out.path().join("_report.json").exists());
    assert!(
        out.path().join("locations").is_symlink(),
        "the dataset symlink must be left untouched"
    );
}

#[test]
fn duckdb_reads_the_output_when_available() {
    if Command::new("duckdb").arg("--version").output().is_err() {
        eprintln!("duckdb not on PATH; skipping");
        return;
    }
    let out = tempfile::tempdir().unwrap();
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["transform", "--snapshot"])
        .arg(fixture_snapshot())
        .arg("--out")
        .arg(out.path())
        .assert()
        .success();
    let glob = format!("{}/locations/**/*.parquet", out.path().display());

    // Hard assertion: DuckDB (with no extensions) can read the dataset's
    // columns and see the geometry column's native logical type.
    let sql = format!(
        "SELECT id, admin1_name, admin2_name, geom_type FROM '{glob}' WHERE id='clinic';\n\
         SELECT name, logical_type FROM parquet_schema('{glob}') WHERE name='geometry';"
    );
    let output = Command::new("duckdb")
        .args(["-csv", "-c", &sql])
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&output.stdout).to_string();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(text.contains("clinic,Kano,Nassarawa,point"), "{text}");
    assert!(text.contains("GeometryType"), "{text}");

    // Best-effort: the spatial extension may not be cached in offline CI,
    // so a failure to install/load it skips rather than fails the test.
    let spatial_sql = format!(
        "INSTALL spatial; LOAD spatial; SELECT count(*) FROM '{glob}' WHERE ST_Intersects(geometry, ST_MakeEnvelope(3,6,4,7));"
    );
    let spatial_output = Command::new("duckdb")
        .args(["-csv", "-c", &spatial_sql])
        .output()
        .unwrap();
    if !spatial_output.status.success() {
        eprintln!(
            "duckdb spatial extension unavailable; skipping envelope check: {}",
            String::from_utf8_lossy(&spatial_output.stderr)
        );
        return;
    }
    let spatial_text = String::from_utf8(spatial_output.stdout).unwrap();
    let last_line = spatial_text.lines().last().unwrap_or("").trim();
    // 7 of the 8 retained rows intersect the envelope (clinic, ng, kano,
    // dup, gama, nassarawa, orphan; only "stray" at (50, 50) is outside),
    // counting a boundary touch as an intersection per OGC semantics.
    assert_eq!(last_line, "7", "{spatial_text}");
}

#[test]
fn inspect_summarises_a_written_dataset() {
    let out = tempfile::tempdir().unwrap();
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["transform", "--snapshot"])
        .arg(fixture_snapshot())
        .arg("--out")
        .arg(out.path())
        .args(["--row-group-size", "2"])
        .assert()
        .success();
    let assert = Command::cargo_bin("kiln")
        .unwrap()
        .args(["inspect", "--out"])
        .arg(out.path())
        .assert()
        .success();
    let stdout = String::from_utf8(assert.get_output().stdout.clone()).unwrap();
    assert!(stdout.contains("8 rows across 4 partitions"), "{stdout}");
    assert!(
        stdout.contains("locations/country=NG/geom_type=polygon/type=admin-unit/part-0.parquet"),
        "{stdout}"
    );
    // Four admin-unit polygons at two rows per group.
    assert!(stdout.contains("row_groups=2"), "{stdout}");
    assert!(stdout.contains("geo=1.1.0 covering=true"), "{stdout}");
    assert!(stdout.contains("types=Polygon"), "{stdout}");
}

#[test]
fn inspect_of_an_empty_dir_says_so() {
    let out = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(out.path().join("locations")).unwrap();
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["inspect", "--out"])
        .arg(out.path())
        .assert()
        .success()
        .stdout(predicates::str::contains("No parquet files found."));
}

fn copy_dir_recursive(src: &Path, dst: &Path) {
    std::fs::create_dir_all(dst).unwrap();
    for entry in std::fs::read_dir(src).unwrap() {
        let entry = entry.unwrap();
        let from = entry.path();
        let to = dst.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_dir_recursive(&from, &to);
        } else {
            std::fs::copy(&from, &to).unwrap();
        }
    }
}

#[test]
fn inspect_ignores_stale_staging_and_backup_dirs() {
    let out = tempfile::tempdir().unwrap();
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["transform", "--snapshot"])
        .arg(fixture_snapshot())
        .arg("--out")
        .arg(out.path())
        .assert()
        .success();

    let locations = out.path().join("locations");
    copy_dir_recursive(&locations, &out.path().join(".locations.tmp"));
    copy_dir_recursive(&locations, &out.path().join(".locations.bak"));

    let assert = Command::cargo_bin("kiln")
        .unwrap()
        .args(["inspect", "--out"])
        .arg(out.path())
        .assert()
        .success();
    let stdout = String::from_utf8(assert.get_output().stdout.clone()).unwrap();
    assert!(stdout.contains("8 rows across 4 partitions"), "{stdout}");
    assert!(!stdout.contains(".locations"), "{stdout}");
}

#[test]
fn inspect_names_a_corrupt_file() {
    let out = tempfile::tempdir().unwrap();
    let locations = out.path().join("locations");
    std::fs::create_dir_all(&locations).unwrap();
    let bad = locations.join("part-0.parquet");
    std::fs::write(&bad, "not parquet").unwrap();

    Command::cargo_bin("kiln")
        .unwrap()
        .args(["inspect", "--out"])
        .arg(out.path())
        .assert()
        .failure()
        .stderr(predicates::str::contains(bad.to_string_lossy().to_string()));
}

#[test]
fn inspect_rejects_a_missing_dir_with_exit_2() {
    let out = tempfile::tempdir().unwrap();
    let missing = out.path().join("nonexistent");
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["inspect", "--out"])
        .arg(&missing)
        .assert()
        .failure()
        .code(2)
        .stderr(predicates::str::contains("no such directory"));
}

fn facility(id: &str, org: &str, lon: f64) -> String {
    serde_json::json!({"resourceType": "Location", "id": id, "name": id, "status": "active",
        "type": [{"coding": [{"code": "facility"}]}],
        "partOf": {"reference": "Location/kano"},
        "managingOrganization": {"reference": format!("Organization/{org}")},
        "position": {"longitude": lon, "latitude": 6.5}})
    .to_string()
}

fn organization(id: &str, name: &str) -> String {
    serde_json::json!({"resourceType": "Organization", "id": id, "active": true, "name": name,
        "identifier": [{"system": "https://icr.healthcampaigns.org/identifiers/nga-nhfr-code", "value": "05/08/1"},
                       {"system": "https://icr.healthcampaigns.org/identifiers/nga-nhfr-uid", "value": "21526030"}],
        "type": [{"coding": [{"system": "https://icr.healthcampaigns.org/CodeSystem/icr-facility-type-cs", "code": "primary"}], "text": "Health Post"},
                 {"coding": [{"system": "https://icr.healthcampaigns.org/CodeSystem/icr-ownership-cs", "code": "public"}], "text": "Local Government"}]})
    .to_string()
}

/// Reads one string column of the facility partition into `id -> value`.
fn column(out: &Path, name: &str) -> std::collections::HashMap<String, Option<String>> {
    use arrow_array::cast::AsArray;
    use arrow_array::Array;
    use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
    let file = std::fs::File::open(out.join("locations/country=NG/geom_type=point/type=facility/part-0.parquet")).unwrap();
    let reader = ParquetRecordBatchReaderBuilder::try_new(file).unwrap().build().unwrap();
    let mut got = std::collections::HashMap::new();
    for batch in reader {
        let batch = batch.unwrap();
        let ids = batch.column_by_name("id").unwrap().as_string::<i32>();
        let col = batch.column_by_name(name).unwrap().as_string::<i32>();
        for r in 0..batch.num_rows() {
            got.insert(ids.value(r).to_string(), (!col.is_null(r)).then(|| col.value(r).to_string()));
        }
    }
    got
}

fn transform_into(snap: &Path, out: &Path) {
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["transform", "--snapshot"])
        .arg(snap)
        .arg("--out")
        .arg(out)
        .assert()
        .success();
}

fn report_counts(out: &Path) -> serde_json::Value {
    let report: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(out.join("_report.json")).unwrap()).unwrap();
    report["counts"].clone()
}

#[test]
fn facility_rows_carry_their_organization() {
    let dir = tempfile::tempdir().unwrap();
    let snap = dir.path().join("snapshot");
    std::fs::create_dir_all(&snap).unwrap();
    let mut locations = std::fs::read_to_string(fixture_snapshot().join("locations.ndjson")).unwrap();
    locations.push_str(&facility("paired", "org-paired", 3.4));
    locations.push('\n');
    locations.push_str(&facility("lonely", "org-gone", 3.5));
    locations.push('\n');
    std::fs::write(snap.join("locations.ndjson"), locations).unwrap();
    std::fs::write(
        snap.join("organizations.ndjson"),
        format!("{}\n{}\n", organization("org-paired", "paired"), organization("org-other", "Other")),
    )
    .unwrap();
    let out = dir.path().join("out");
    transform_into(&snap, &out);

    let nhfr = column(&out, "nhfr_code");
    assert_eq!(nhfr["paired"].as_deref(), Some("05/08/1"));
    assert_eq!(nhfr["lonely"], None);
    assert_eq!(nhfr["clinic"], None, "a facility with no managingOrganization");
    assert_eq!(column(&out, "facility_level_text")["paired"].as_deref(), Some("Health Post"));
    assert_eq!(column(&out, "ownership_text")["paired"].as_deref(), Some("Local Government"));
    assert!(column(&out, "organization_json")["paired"].as_deref().unwrap().contains("\"id\":\"org-paired\""));
    let counts = report_counts(&out);
    assert_eq!(counts["organization_missing"], 1);
    assert!(counts.get("organization_name_mismatch").is_none());

    // A renamed Organization is drift, reported once.
    std::fs::write(snap.join("organizations.ndjson"), format!("{}\n", organization("org-paired", "Renamed"))).unwrap();
    transform_into(&snap, &out);
    assert_eq!(report_counts(&out)["organization_name_mismatch"], 1);

    // No organizations file at all: columns null, nothing reported.
    std::fs::remove_file(snap.join("organizations.ndjson")).unwrap();
    transform_into(&snap, &out);
    assert_eq!(column(&out, "nhfr_code")["paired"], None);
    assert!(report_counts(&out).get("organization_missing").is_none());
}
