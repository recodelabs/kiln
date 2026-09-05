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
        stdout.contains("Wrote 8 rows across 3 partitions"),
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
        .join("locations/country=NG/geom_type=polygon/part-0.parquet")
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
        .code(2);
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
fn a_stale_report_is_removed_before_writing() {
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
    let sql = format!(
        "SELECT id, admin1_name, admin2_name, geom_type FROM '{glob}' WHERE id='clinic';\n\
         SELECT name, logical_type FROM parquet_schema('{glob}') WHERE name='geometry';\n\
         INSTALL spatial; LOAD spatial; SELECT count(*) FROM '{glob}' WHERE ST_Intersects(geometry, ST_MakeEnvelope(3,6,4,7));"
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
    // 7 of the 8 retained rows intersect the envelope (clinic, ng, kano,
    // dup, gama, nassarawa, orphan; only "stray" at (50, 50) is outside),
    // counting a boundary touch as an intersection per OGC semantics.
    assert!(text.contains("\n7\n") || text.ends_with("7\n"), "{text}");
}
