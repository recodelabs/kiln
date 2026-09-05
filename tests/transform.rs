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
    assert!(stdout.contains("8 rows across 3 partitions"), "{stdout}");
    assert!(
        stdout.contains("locations/country=NG/geom_type=polygon/part-0.parquet"),
        "{stdout}"
    );
    assert!(stdout.contains("row_groups=3"), "{stdout}");
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
    assert!(stdout.contains("8 rows across 3 partitions"), "{stdout}");
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
