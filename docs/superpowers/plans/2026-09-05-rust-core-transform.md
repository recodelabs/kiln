# kiln Rust core: transform and inspect — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `kiln` Rust binary whose `transform` command turns a snapshot of FHIR Location NDJSON into a partitioned, Hilbert-sorted GeoParquet dataset with native Parquet GEOMETRY types, plus `inspect` to summarise it, using the two-pass indexed design from README.md, with no GDAL, GEOS or Python at runtime.

**Architecture:** Pass one streams `snapshot/locations.ndjson`, keeps a small `IndexRecord` per Location (ids, names, codes, byte offset, geometry kind and bbox), resolves the hierarchy, picks partitions, and sorts by Hilbert key. Pass two walks the sorted index, seeks back into the file for each line, parses the geometry, and appends rows to one Parquet writer per partition, flushing at the row-group size. Output goes to a staging directory and is swapped in atomically. Diagnostics accumulate in a capped `Report` written to `_report.json`.

**Tech Stack:** Rust stable (1.98), `clap` 4 (derive), `serde_json` 1, `arrow-array`/`arrow-schema` 59, `parquet` 59 with the `geospatial` and `zstd` features, `geojson` 1, `geo` 0.33, `wkb` 0.9, `thiserror` 2, `sha2` 0.11. Dev: `tempfile` 3, `assert_cmd` 2, DuckDB CLI on PATH for the integration test (skipped when absent).

This is plan 1 of 3. Plan 2 adds `extract` and the snapshot merge; plan 3 adds `diff` and `load`. This plan alone produces a working, testable binary from a hand-made snapshot directory.

---

## Spec reference

The design is `README.md` at the repo root (sections "The snapshot", "The output dataset", "How transform works: the two pass design", "Reading the output"). Where this plan and the README disagree, the README wins; fix the plan.

The Python implementation being ported lives in `src/kiln/` before Task 0 and `python/src/kiln/` after. The modules that matter here are `profile.py` (parsing), `hierarchy.py`, `geometry.py`, `report.py`, `frame.py`, `write.py`, `inspect.py`. Read them when a step's intent is unclear; the Rust mirrors their behaviour except where the README changed it (column names, no geometry repair, partition keys).

## Behaviour changes from the Python, on purpose

| Python | Rust | Why |
|---|---|---|
| `make_valid` repairs invalid polygons, reports `geometry_repaired` | Never repairs; reports `geometry_invalid` and keeps the geometry as is | README "Report, don't repair" |
| Columns `loc_type`, `parent_id`, `identifiers`, `lon`/`lat` for position | `type`, `part_of`, `identifier`, `position_longitude`/`position_latitude`; `lon`/`lat` are the representative point | README "Column names follow FHIR paths" |
| No `version_id`, `alias`, `description`, `managing_organization`, `fhir_json` | All present | README round-trip fields |
| Default partitions `country,geom_type,tier` | `country,geom_type` | README "Drop the tier partition" |
| Input `--in FILE` | `--snapshot DIR` reading `DIR/locations.ndjson` | README snapshot layout |
| FeatureCollection with several features is unioned with GEOS | Folded into one MultiPolygon without union, reported as `boundary_multi_feature` | No GEOS |

## File structure

```
kiln/
  Cargo.toml
  .gitignore                         (add /target)
  src/
    main.rs            clap entry; maps KilnError to exit codes
    cli.rs             Cli, Command enum, TransformArgs, InspectArgs
    error.rs           KilnError (thiserror)
    report.rs          Report, Issue, capped retention, JSON, summary
    fhir/
      mod.rs
      location.rs      Location struct + parse(value, report)
      ndjson.rs        NdjsonReader: streams (offset, len, text); pretty-print detection
    geometry/
      mod.rs           GeometrySummary, GeometryResult, summarize(), build()
      geojson.rs       bytes -> geo::Geometry (Feature/FeatureCollection/Geometry)
      wkb.rs           geo::Geometry -> WKB bytes (little endian)
    index/
      mod.rs           IndexRecord, Index, build_index() (pass one)
      hierarchy.rs     HierarchyInfo, resolve_hierarchy()
      hilbert.rs       hilbert_key()
      partition.rs     partition_value(), segment sanitising and collision handling
    write/
      mod.rs
      schema.rs        output Arrow schema, RowBatch builders, OutputRow
      parquet.rs       PartitionWriter: ArrowWriter + geo metadata
      dataset.rs       write_dataset(): pass two, staging, atomic swap
    inspect.rs         summarize(out_dir), format_summary()
    transform.rs       run_transform(args): wires pass one + pass two + report
  tests/
    fixtures/snapshot/locations.ndjson   (copy of the Python fixture)
    transform.rs       end-to-end: run the binary, read parquet back, DuckDB check
  python/              the Python package, moved in Task 0
```

Each `mod.rs` only declares submodules and re-exports; logic lives in named files.

---

## Task 0: Move Python aside, scaffold the crate

**Files:**
- Move: `src/` → `python/src/`, `tests/` → `python/tests/`, `pyproject.toml`, `uv.lock`, `ABOUT.md`, `README-python.md` → `python/`
- Create: `Cargo.toml`, `src/main.rs`, `src/cli.rs`, `src/error.rs`, `tests/fixtures/snapshot/locations.ndjson`
- Modify: `.gitignore`

- [x] **Step 1: Move the Python package**

```bash
cd /Users/mberg/github/kiln
mkdir python
git mv src python/src
git mv tests python/tests
git mv pyproject.toml uv.lock ABOUT.md python/
git mv README-python.md python/README.md
rm -rf .venv
```

- [x] **Step 2: Verify the Python tests still pass from their new home**

Run: `cd python && uv sync && uv run pytest -q; cd ..`
Expected: all tests pass (the suite locates fixtures relative to the test file, so the move is transparent). If `test_cli` fails on GDAL probing, that is the pre-existing GDAL requirement, not the move; run `uv run pytest -q --deselect python/tests/test_cli.py` and note it.

- [x] **Step 3: Copy the fixture into the Rust test tree**

```bash
mkdir -p tests/fixtures/snapshot
cp python/tests/fixtures/locations.ndjson tests/fixtures/snapshot/locations.ndjson
```

- [x] **Step 4: Create Cargo.toml**

```toml
[package]
name = "kiln"
version = "0.2.0"
edition = "2021"
description = "Bridge between a FHIR Location registry and GeoParquet"
license = "Apache-2.0"

[[bin]]
name = "kiln"
path = "src/main.rs"

[dependencies]
arrow-array = "59"
arrow-schema = "59"
parquet = { version = "59", default-features = false, features = ["arrow", "zstd", "geospatial"] }
clap = { version = "4", features = ["derive"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
thiserror = "2"
geojson = "1"
geo = "0.33"
geo-types = "0.7"
wkb = "0.9"
base64 = "0.22"
sha2 = "0.11"

[dev-dependencies]
tempfile = "3"
assert_cmd = "2"

[profile.release]
lto = true
codegen-units = 1
strip = true
```

- [x] **Step 5: Create src/error.rs**

```rust
//! One error type for the whole binary. `Usage` is anything the operator can
//! fix (bad path, malformed input); everything else is an environment fault.

use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum KilnError {
    #[error("{0}")]
    Usage(String),
    #[error("{path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("arrow: {0}")]
    Arrow(#[from] arrow_schema::ArrowError),
    #[error("parquet: {0}")]
    Parquet(#[from] parquet::errors::ParquetError),
}

impl KilnError {
    pub fn io(path: impl Into<PathBuf>, source: std::io::Error) -> Self {
        KilnError::Io { path: path.into(), source }
    }

    /// Exit status: 2 for operator-fixable problems, 1 for everything else.
    pub fn exit_code(&self) -> i32 {
        match self {
            KilnError::Usage(_) => 2,
            _ => 1,
        }
    }
}

pub type Result<T> = std::result::Result<T, KilnError>;
```

- [x] **Step 6: Create src/cli.rs with the two commands this plan delivers**

```rust
use std::path::PathBuf;

use clap::{Parser, Subcommand};

pub const DEFAULT_ROW_GROUP_SIZE: usize = 20_000;
pub const DEFAULT_PARTITION_BY: &str = "country,geom_type";

#[derive(Parser, Debug)]
#[command(name = "kiln", version, about = "Bridge between a FHIR Location registry and GeoParquet")]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Subcommand, Debug)]
pub enum Command {
    /// Convert a snapshot into a partitioned GeoParquet dataset (offline)
    Transform(TransformArgs),
    /// Summarise a written dataset
    Inspect(InspectArgs),
}

#[derive(clap::Args, Debug, Clone)]
pub struct TransformArgs {
    /// Snapshot directory containing locations.ndjson
    #[arg(long)]
    pub snapshot: PathBuf,
    /// Output directory; the dataset is written to OUT/locations
    #[arg(long)]
    pub out: PathBuf,
    /// Override the country code derived from the level-0 admin unit
    #[arg(long)]
    pub country: Option<String>,
    /// Rows per Parquet row group
    #[arg(long, default_value_t = DEFAULT_ROW_GROUP_SIZE)]
    pub row_group_size: usize,
    /// Comma-separated partition keys: any of country, geom_type, tier, type
    #[arg(long, default_value = DEFAULT_PARTITION_BY)]
    pub partition_by: String,
}

#[derive(clap::Args, Debug, Clone)]
pub struct InspectArgs {
    /// Output directory previously written by transform
    #[arg(long)]
    pub out: PathBuf,
}
```

- [x] **Step 7: Create src/main.rs**

```rust
mod cli;
mod error;

use clap::Parser;

fn main() {
    let cli = cli::Cli::parse();
    let result: error::Result<()> = match cli.command {
        cli::Command::Transform(_) => Err(error::KilnError::Usage("transform: not implemented yet".into())),
        cli::Command::Inspect(_) => Err(error::KilnError::Usage("inspect: not implemented yet".into())),
    };
    if let Err(err) = result {
        eprintln!("kiln: {err}");
        std::process::exit(err.exit_code());
    }
}
```

- [x] **Step 8: Add target to .gitignore and build**

Append to `.gitignore`:

```
/target
```

Run: `export PATH="/opt/homebrew/opt/rustup/bin:$HOME/.cargo/bin:$PATH"; cargo build 2>&1 | tail -3 && ./target/debug/kiln --help`
Expected: builds; help lists `transform` and `inspect`.

- [x] **Step 9: Commit**

```bash
git add -A
git commit -m "Scaffold the Rust crate; move the Python package under python/"
```

---

## Task 1: Report

**Files:**
- Create: `src/report.rs`
- Modify: `src/main.rs` (add `mod report;`)

- [x] **Step 1: Write the failing tests (inside the module)**

Create `src/report.rs` with only the test module for now:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_are_exact_but_retained_issues_are_capped() {
        let mut report = Report::default();
        for i in 0..(MAX_RETAINED_ISSUES_PER_KIND + 5) {
            report.add("orphan", &format!("loc-{i}"), "dangling");
        }
        assert_eq!(report.counts()["orphan"], MAX_RETAINED_ISSUES_PER_KIND + 5);
        assert_eq!(report.issues.len(), MAX_RETAINED_ISSUES_PER_KIND);
        assert_eq!(report.truncated()["orphan"], 5);
    }

    #[test]
    fn summary_lists_kinds_sorted() {
        let mut report = Report::default();
        report.add("cycle", "b", "");
        report.add("orphan", "a", "");
        report.add("orphan", "c", "");
        assert_eq!(report.summary(), "Issues found:\n  cycle: 1\n  orphan: 2");
        assert_eq!(Report::default().summary(), "No issues found.");
    }

    #[test]
    fn json_shape_matches_python() {
        let mut report = Report::default();
        report.add("orphan", "a", "x");
        let json = report.to_json();
        assert_eq!(json["counts"]["orphan"], 1);
        assert_eq!(json["issues"][0]["kind"], "orphan");
        assert_eq!(json["issues"][0]["location_id"], "a");
        assert_eq!(json["issues"][0]["detail"], "x");
        assert!(json["truncated"].as_object().unwrap().is_empty());
    }
}
```

Add `mod report;` to `src/main.rs`.

- [x] **Step 2: Run tests to verify they fail**

Run: `cargo test report 2>&1 | grep -E "error|Report" | head`
Expected: compile errors, `Report` not found.

- [x] **Step 3: Implement Report above the tests**

```rust
//! Data-quality issues found during a run. Never aborts; callers keep going.

use std::collections::BTreeMap;

use serde::Serialize;

/// One kind can fire once per row; at 100k rows that is a report larger than
/// the dataset. Keep this many per kind; counts stay exact regardless.
pub const MAX_RETAINED_ISSUES_PER_KIND: usize = 1000;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Issue {
    pub kind: String,
    pub location_id: String,
    pub detail: String,
}

#[derive(Debug, Default)]
pub struct Report {
    pub issues: Vec<Issue>,
    counts: BTreeMap<String, usize>,
    retained: BTreeMap<String, usize>,
}

impl Report {
    pub fn add(&mut self, kind: &str, location_id: &str, detail: &str) {
        *self.counts.entry(kind.to_string()).or_default() += 1;
        let retained = self.retained.entry(kind.to_string()).or_default();
        if *retained < MAX_RETAINED_ISSUES_PER_KIND {
            self.issues.push(Issue {
                kind: kind.to_string(),
                location_id: location_id.to_string(),
                detail: detail.to_string(),
            });
            *retained += 1;
        }
    }

    pub fn counts(&self) -> &BTreeMap<String, usize> {
        &self.counts
    }

    pub fn count(&self, kind: &str) -> usize {
        self.counts.get(kind).copied().unwrap_or(0)
    }

    /// Kinds where more issues occurred than were retained, with the omitted count.
    pub fn truncated(&self) -> BTreeMap<String, usize> {
        self.counts
            .iter()
            .filter_map(|(kind, total)| {
                let kept = self.retained.get(kind).copied().unwrap_or(0);
                (*total > kept).then(|| (kind.clone(), total - kept))
            })
            .collect()
    }

    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "counts": self.counts,
            "issues": self.issues,
            "truncated": self.truncated(),
        })
    }

    pub fn summary(&self) -> String {
        if self.counts.is_empty() {
            return "No issues found.".to_string();
        }
        let mut out = String::from("Issues found:");
        for (kind, count) in &self.counts {
            out.push_str(&format!("\n  {kind}: {count}"));
        }
        let truncated = self.truncated();
        if !truncated.is_empty() {
            let omitted: Vec<String> = truncated.iter().map(|(k, n)| format!("{k} (+{n} more)")).collect();
            out.push_str(&format!(
                "\nissues list capped at {MAX_RETAINED_ISSUES_PER_KIND} per kind (counts above are exact); omitted from the list: {}",
                omitted.join(", ")
            ));
        }
        out
    }
}
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cargo test report`
Expected: 3 passed.

- [x] **Step 5: Commit**

```bash
git add src/report.rs src/main.rs
git commit -m "Add Report with capped issue retention and exact counts"
```

---

## Task 2: FHIR Location parsing

**Files:**
- Create: `src/fhir/mod.rs`, `src/fhir/location.rs`
- Modify: `src/main.rs` (add `mod fhir;`)

- [x] **Step 1: Create the module file**

`src/fhir/mod.rs`:

```rust
pub mod location;
pub mod ndjson;

pub use location::{Boundary, Identifier, Location};
```

(`ndjson` is created in Task 3; add `pub mod ndjson;` then. For now write only `pub mod location;` and the `pub use`.)

- [x] **Step 2: Write the failing tests in src/fhir/location.rs**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::report::Report;

    fn parse_str(s: &str, report: &mut Report) -> Option<Location> {
        Location::parse(&serde_json::from_str(s).unwrap(), report)
    }

    #[test]
    fn parses_the_common_fields() {
        let mut report = Report::default();
        let loc = parse_str(r#"{
            "resourceType":"Location","id":"clinic","name":"Gama Clinic","status":"active",
            "meta":{"versionId":"3","lastUpdated":"2026-01-02T03:04:05Z"},
            "alias":["GC"],"description":"d",
            "type":[{"coding":[{"system":"https://icr.healthcampaigns.org/CodeSystem/icr-location-type","code":"facility"}]},
                    {"coding":[{"system":"https://icr.healthcampaigns.org/CodeSystem/icr-facility-type-cs","code":"phc"}]},
                    {"coding":[{"system":"https://icr.healthcampaigns.org/CodeSystem/icr-ownership-cs","code":"public"}]}],
            "physicalType":{"coding":[{"code":"si"}]},
            "partOf":{"reference":"Location/gama"},
            "managingOrganization":{"reference":"Organization/org-1"},
            "identifier":[{"system":"https://icr.healthcampaigns.org/identifiers/pcode","value":"NG1"},
                          {"system":"https://icr.healthcampaigns.org/identifiers/overture-gers","value":"g1"}],
            "position":{"longitude":3.25,"latitude":6.25},
            "extension":[{"url":"https://icr.healthcampaigns.org/StructureDefinition/settlement-type","valueCode":"urban"},
                         {"url":"https://icr.healthcampaigns.org/StructureDefinition/delivery-strategy","valueCode":"fixed"},
                         {"url":"https://icr.healthcampaigns.org/StructureDefinition/overlays-admin-unit","valueReference":{"reference":"Location/kano"}}]
        }"#, &mut report).unwrap();
        assert_eq!(loc.id, "clinic");
        assert_eq!(loc.version_id.as_deref(), Some("3"));
        assert_eq!(loc.last_updated.as_deref(), Some("2026-01-02T03:04:05Z"));
        assert_eq!(loc.alias, vec!["GC"]);
        assert_eq!(loc.type_code.as_deref(), Some("facility"));
        assert_eq!(loc.facility_level.as_deref(), Some("phc"));
        assert_eq!(loc.ownership.as_deref(), Some("public"));
        assert_eq!(loc.physical_type.as_deref(), Some("si"));
        assert_eq!(loc.part_of.as_deref(), Some("gama"));
        assert_eq!(loc.managing_organization.as_deref(), Some("org-1"));
        assert_eq!(loc.pcode.as_deref(), Some("NG1"));
        assert_eq!(loc.gers_id.as_deref(), Some("g1"));
        assert_eq!(loc.identifier.len(), 2);
        assert_eq!(loc.position, Some((3.25, 6.25)));
        assert_eq!(loc.settlement_type.as_deref(), Some("urban"));
        assert_eq!(loc.delivery_strategy.as_deref(), Some("fixed"));
        assert_eq!(loc.overlays_admin_unit_ids, vec!["kano"]);
        assert!(loc.boundary.is_none());
        assert_eq!(report.counts().len(), 0);
    }

    #[test]
    fn inline_boundary_is_decoded_and_stripped_from_fhir_json() {
        let mut report = Report::default();
        let geojson = r#"{"type":"Point","coordinates":[1,2]}"#;
        let b64 = base64::Engine::encode(&base64::engine::general_purpose::STANDARD, geojson);
        let src = format!(r#"{{"resourceType":"Location","id":"a","extension":[
            {{"url":"https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson",
              "valueAttachment":{{"contentType":"application/geo+json","data":"{b64}"}}}},
            {{"url":"https://example.org/keep","valueString":"x"}}]}}"#);
        let loc = parse_str(&src, &mut report).unwrap();
        match &loc.boundary {
            Some(Boundary::Inline(bytes)) => assert_eq!(bytes, geojson.as_bytes()),
            other => panic!("expected inline boundary, got {other:?}"),
        }
        let json: serde_json::Value = serde_json::from_str(&loc.fhir_json).unwrap();
        let exts = json["extension"].as_array().unwrap();
        assert_eq!(exts.len(), 1, "boundary extension removed, other kept");
        assert_eq!(exts[0]["url"], "https://example.org/keep");
    }

    #[test]
    fn url_boundary_and_bad_shapes_are_reported() {
        let mut report = Report::default();
        let loc = parse_str(r#"{"resourceType":"Location","id":"a","identifier":"nope","partOf":"nope",
            "extension":[{"url":"http://hl7.org/fhir/StructureDefinition/location-boundary-geojson",
              "valueAttachment":{"contentType":"application/geo+json","url":"https://files/x.geojson"}}]}"#, &mut report).unwrap();
        assert_eq!(loc.boundary, Some(Boundary::Url("https://files/x.geojson".into())));
        assert_eq!(report.count("malformed_field"), 2);
    }

    #[test]
    fn missing_id_is_reported_and_skipped() {
        let mut report = Report::default();
        assert!(parse_str(r#"{"resourceType":"Location","name":"x"}"#, &mut report).is_none());
        assert_eq!(report.count("missing_id"), 1);
        assert!(parse_str(r#"[1,2]"#, &mut report).is_none());
        assert_eq!(report.count("malformed_field"), 1);
    }

    #[test]
    fn bad_content_type_and_bad_base64_are_reported() {
        let mut report = Report::default();
        let loc = parse_str(r#"{"resourceType":"Location","id":"a","extension":[
            {"url":"https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson",
             "valueAttachment":{"contentType":"text/plain","data":"eA=="}}]}"#, &mut report).unwrap();
        assert!(loc.boundary.is_none());
        assert_eq!(report.count("boundary_bad_content_type"), 1);
        let loc = parse_str(r#"{"resourceType":"Location","id":"b","extension":[
            {"url":"https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson",
             "valueAttachment":{"contentType":"application/geo+json","data":"!!!"}}]}"#, &mut report).unwrap();
        assert!(loc.boundary.is_none());
        assert_eq!(report.count("boundary_bad_base64"), 1);
    }
}
```

- [x] **Step 3: Run tests to verify they fail**

Run: `cargo test fhir::location 2>&1 | grep -E "^error" | head -3`
Expected: compile errors (Location undefined).

- [x] **Step 4: Implement Location::parse above the tests**

```rust
//! One FHIR Location, flattened into the fields kiln models as columns, plus
//! the untouched resource JSON for the lossless `fhir_json` column.
//!
//! Everything ICR-profile-specific (extension URLs, identifier systems, code
//! systems) is a constant in this file. Supporting another profile means
//! editing this file and nothing else.

use base64::Engine;
use serde_json::Value;

use crate::report::Report;

pub const BOUNDARY_EXTENSION_URL: &str =
    "https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson";
pub const HL7_BOUNDARY_EXTENSION_URL: &str =
    "http://hl7.org/fhir/StructureDefinition/location-boundary-geojson";
pub const BOUNDARY_EXTENSION_URLS: [&str; 2] = [BOUNDARY_EXTENSION_URL, HL7_BOUNDARY_EXTENSION_URL];
pub const OVERLAYS_EXTENSION_URL: &str =
    "https://icr.healthcampaigns.org/StructureDefinition/overlays-admin-unit";
pub const SETTLEMENT_TYPE_EXTENSION_URL: &str =
    "https://icr.healthcampaigns.org/StructureDefinition/settlement-type";
pub const DELIVERY_STRATEGY_EXTENSION_URL: &str =
    "https://icr.healthcampaigns.org/StructureDefinition/delivery-strategy";
pub const PCODE_SYSTEM: &str = "https://icr.healthcampaigns.org/identifiers/pcode";
pub const GERS_SYSTEM: &str = "https://icr.healthcampaigns.org/identifiers/overture-gers";
pub const FACILITY_TYPE_SYSTEM: &str =
    "https://icr.healthcampaigns.org/CodeSystem/icr-facility-type-cs";
pub const OWNERSHIP_SYSTEM: &str = "https://icr.healthcampaigns.org/CodeSystem/icr-ownership-cs";
pub const GEOJSON_CONTENT_TYPE: &str = "application/geo+json";
pub const ADMIN_UNIT_TYPE: &str = "admin-unit";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Identifier {
    pub system: Option<String>,
    pub value: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Boundary {
    /// Decoded GeoJSON bytes from an inline `data` attachment.
    Inline(Vec<u8>),
    /// A `url` attachment that `extract` has not resolved.
    Url(String),
}

#[derive(Debug, Clone, Default)]
pub struct Location {
    pub id: String,
    pub version_id: Option<String>,
    pub last_updated: Option<String>,
    pub name: Option<String>,
    pub alias: Vec<String>,
    pub status: Option<String>,
    pub description: Option<String>,
    pub type_code: Option<String>,
    pub physical_type: Option<String>,
    pub part_of: Option<String>,
    pub managing_organization: Option<String>,
    pub identifier: Vec<Identifier>,
    pub pcode: Option<String>,
    pub gers_id: Option<String>,
    pub settlement_type: Option<String>,
    pub delivery_strategy: Option<String>,
    pub facility_level: Option<String>,
    pub ownership: Option<String>,
    pub overlays_admin_unit_ids: Vec<String>,
    /// (longitude, latitude), FHIR order.
    pub position: Option<(f64, f64)>,
    pub boundary: Option<Boundary>,
    /// The resource as compact JSON with the boundary extension removed.
    pub fhir_json: String,
}

fn str_field(obj: &serde_json::Map<String, Value>, key: &str) -> Option<String> {
    obj.get(key).and_then(Value::as_str).map(str::to_string)
}

/// `Location/loc-1` -> `loc-1`; bare ids pass through.
fn strip_reference(reference: &str) -> String {
    reference.rsplit('/').next().unwrap_or(reference).to_string()
}

fn reference_id(obj: &serde_json::Map<String, Value>, key: &str, id: &str, report: &mut Report) -> Option<String> {
    match obj.get(key) {
        None | Some(Value::Null) => None,
        Some(Value::Object(r)) => r.get("reference").and_then(Value::as_str).map(strip_reference),
        Some(_) => {
            report.add("malformed_field", id, &format!("{key} is not an object"));
            None
        }
    }
}

/// First coding.code of a CodeableConcept, or of the first concept in a list.
fn first_coding_code(node: Option<&Value>) -> Option<String> {
    let concept = match node? {
        Value::Array(items) => items.first()?,
        other => other,
    };
    concept.get("coding")?.as_array()?.first()?.get("code")?.as_str().map(str::to_string)
}

/// First coding under `system` across a CodeableConcept or list of them.
fn coding_code_by_system(node: Option<&Value>, system: &str) -> Option<String> {
    let concepts: Vec<&Value> = match node? {
        Value::Array(items) => items.iter().collect(),
        other => vec![other],
    };
    for concept in concepts {
        for coding in concept.get("coding").and_then(Value::as_array).into_iter().flatten() {
            if coding.get("system").and_then(Value::as_str) == Some(system) {
                return coding.get("code").and_then(Value::as_str).map(str::to_string);
            }
        }
    }
    None
}

fn read_boundary(ext: &serde_json::Map<String, Value>, id: &str, report: &mut Report) -> Option<Boundary> {
    let Some(Value::Object(att)) = ext.get("valueAttachment") else {
        report.add("malformed_field", id, "boundary extension valueAttachment is not an object");
        return None;
    };
    let content_type = att.get("contentType").and_then(Value::as_str);
    if content_type != Some(GEOJSON_CONTENT_TYPE) {
        report.add("boundary_bad_content_type", id, &format!("expected {GEOJSON_CONTENT_TYPE}, got {content_type:?}"));
        return None;
    }
    if let Some(data) = att.get("data").and_then(Value::as_str).filter(|s| !s.is_empty()) {
        return match base64::engine::general_purpose::STANDARD.decode(data) {
            Ok(bytes) => Some(Boundary::Inline(bytes)),
            Err(err) => {
                report.add("boundary_bad_base64", id, &err.to_string());
                None
            }
        };
    }
    if let Some(url) = att.get("url").and_then(Value::as_str).filter(|s| !s.is_empty()) {
        return Some(Boundary::Url(url.to_string()));
    }
    report.add("boundary_empty", id, "attachment has neither data nor url");
    None
}

fn is_boundary_extension(ext: &Value) -> bool {
    ext.get("url").and_then(Value::as_str).map_or(false, |u| BOUNDARY_EXTENSION_URLS.contains(&u))
}

impl Location {
    /// Flatten one resource. Returns None (after reporting) if it has no usable id.
    pub fn parse(resource: &Value, report: &mut Report) -> Option<Location> {
        let Value::Object(obj) = resource else {
            report.add("malformed_field", "<unknown>", "Location resource is not an object");
            return None;
        };
        let id = match obj.get("id") {
            Some(Value::String(s)) if !s.is_empty() => s.clone(),
            Some(Value::String(_)) | None | Some(Value::Null) => {
                report.add("missing_id", "<unknown>", "Location resource has no id");
                return None;
            }
            Some(other) => {
                report.add("malformed_field", "<unknown>", &format!("Location id is not a string: {other}"));
                return None;
            }
        };

        let mut loc = Location { id: id.clone(), ..Default::default() };
        loc.name = str_field(obj, "name");
        loc.status = str_field(obj, "status");
        loc.description = str_field(obj, "description");
        loc.alias = obj
            .get("alias")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect())
            .unwrap_or_default();
        loc.type_code = first_coding_code(obj.get("type"));
        loc.physical_type = first_coding_code(obj.get("physicalType"));
        loc.facility_level = coding_code_by_system(obj.get("type"), FACILITY_TYPE_SYSTEM);
        loc.ownership = coding_code_by_system(obj.get("type"), OWNERSHIP_SYSTEM);
        loc.part_of = reference_id(obj, "partOf", &id, report);
        loc.managing_organization = reference_id(obj, "managingOrganization", &id, report);

        match obj.get("meta") {
            None | Some(Value::Null) => {}
            Some(Value::Object(meta)) => {
                loc.version_id = str_field(meta, "versionId");
                loc.last_updated = str_field(meta, "lastUpdated");
            }
            Some(_) => report.add("malformed_field", &id, "meta is not an object"),
        }

        match obj.get("identifier") {
            None | Some(Value::Null) => {}
            Some(Value::Array(items)) => {
                for (i, item) in items.iter().enumerate() {
                    match item {
                        Value::Object(ident) => loc.identifier.push(Identifier {
                            system: str_field(ident, "system"),
                            value: str_field(ident, "value"),
                        }),
                        _ => report.add("malformed_field", &id, &format!("identifier[{i}] is not an object")),
                    }
                }
            }
            Some(_) => report.add("malformed_field", &id, "identifier is not a list"),
        }
        loc.pcode = loc.identifier.iter().find(|i| i.system.as_deref() == Some(PCODE_SYSTEM)).and_then(|i| i.value.clone());
        loc.gers_id = loc.identifier.iter().find(|i| i.system.as_deref() == Some(GERS_SYSTEM)).and_then(|i| i.value.clone());

        if let Some(Value::Object(pos)) = obj.get("position") {
            match (pos.get("longitude").and_then(Value::as_f64), pos.get("latitude").and_then(Value::as_f64)) {
                (Some(lon), Some(lat)) => loc.position = Some((lon, lat)),
                _ if pos.contains_key("longitude") || pos.contains_key("latitude") => {
                    report.add("malformed_field", &id, "position coordinates not numeric")
                }
                _ => {}
            }
        }

        match obj.get("extension") {
            None | Some(Value::Null) => {}
            Some(Value::Array(exts)) => {
                for ext in exts {
                    let Value::Object(e) = ext else {
                        report.add("malformed_field", &id, "extension entry is not an object");
                        continue;
                    };
                    let url = e.get("url").and_then(Value::as_str).unwrap_or("");
                    if BOUNDARY_EXTENSION_URLS.contains(&url) {
                        if loc.boundary.is_none() {
                            loc.boundary = read_boundary(e, &id, report);
                        }
                    } else if url == OVERLAYS_EXTENSION_URL {
                        match e.get("valueReference") {
                            Some(Value::Object(r)) => {
                                if let Some(target) = r.get("reference").and_then(Value::as_str) {
                                    loc.overlays_admin_unit_ids.push(strip_reference(target));
                                }
                            }
                            Some(_) => report.add("malformed_field", &id, "overlays extension valueReference is not an object"),
                            None => {}
                        }
                    } else if url == SETTLEMENT_TYPE_EXTENSION_URL {
                        loc.settlement_type = str_field(e, "valueCode");
                    } else if url == DELIVERY_STRATEGY_EXTENSION_URL {
                        loc.delivery_strategy = str_field(e, "valueCode");
                    }
                }
            }
            Some(_) => report.add("malformed_field", &id, "extension is not a list"),
        }

        // fhir_json: the resource minus the boundary attachment (the geometry
        // column carries it). Everything else is preserved verbatim.
        let mut stripped = resource.clone();
        if let Some(Value::Array(exts)) = stripped.get_mut("extension") {
            exts.retain(|e| !is_boundary_extension(e));
            if exts.is_empty() {
                stripped.as_object_mut().unwrap().remove("extension");
            }
        }
        loc.fhir_json = stripped.to_string();
        Some(loc)
    }
}
```

Add `mod fhir;` to `src/main.rs`.

- [x] **Step 5: Run tests to verify they pass**

Run: `cargo test fhir::location`
Expected: 5 passed. (If `base64` engine imports fail, the `use base64::Engine;` at the top brings the trait into scope; the test uses the fully qualified path.)

- [x] **Step 6: Commit**

```bash
git add src/fhir src/main.rs
git commit -m "Parse FHIR Location resources into the kiln Location struct"
```

---

## Task 3: NDJSON reader with byte offsets

**Files:**
- Create: `src/fhir/ndjson.rs`
- Modify: `src/fhir/mod.rs` (add `pub mod ndjson;`)

- [x] **Step 1: Write the failing tests**

`src/fhir/ndjson.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn tmp(content: &str) -> tempfile::NamedTempFile {
        let mut f = tempfile::NamedTempFile::new().unwrap();
        f.write_all(content.as_bytes()).unwrap();
        f
    }

    #[test]
    fn yields_each_nonblank_line_with_its_offset() {
        let f = tmp("{\"id\":\"a\"}\n\n{\"id\":\"b\"}\n");
        let lines: Vec<Line> = NdjsonReader::open(f.path()).unwrap().map(|l| l.unwrap()).collect();
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[0].offset, 0);
        assert_eq!(lines[0].text, "{\"id\":\"a\"}");
        assert_eq!(lines[1].offset, 12);
        assert_eq!(lines[1].len, 10);
    }

    #[test]
    fn read_at_returns_the_same_bytes() {
        let f = tmp("{\"id\":\"a\"}\n{\"id\":\"b\"}\n");
        let lines: Vec<Line> = NdjsonReader::open(f.path()).unwrap().map(|l| l.unwrap()).collect();
        let mut random = LineAccess::open(f.path()).unwrap();
        assert_eq!(random.read_at(lines[1].offset, lines[1].len).unwrap(), "{\"id\":\"b\"}");
        assert_eq!(random.read_at(lines[0].offset, lines[0].len).unwrap(), "{\"id\":\"a\"}");
    }

    #[test]
    fn pretty_printed_json_is_rejected_up_front() {
        let f = tmp("{\n  \"resourceType\": \"Bundle\"\n}\n");
        let err = NdjsonReader::open(f.path()).unwrap_err();
        assert!(err.to_string().contains("pretty-printed"), "{err}");
    }

    #[test]
    fn unparseable_line_is_an_error_with_line_number() {
        let f = tmp("{\"id\":\"a\"}\nnot json\n");
        let mut reader = NdjsonReader::open(f.path()).unwrap();
        reader.next().unwrap().unwrap();
        let err = reader.next().unwrap().unwrap_err();
        assert!(err.to_string().contains("line 2"), "{err}");
    }
}
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cargo test fhir::ndjson 2>&1 | grep -E "^error" | head -3`
Expected: compile errors.

- [x] **Step 3: Implement NdjsonReader and LineAccess**

> Superseded during execution: the shipped `src/fhir/ndjson.rs` additionally skips a UTF-8 BOM (seeding the offset to 3), bounds the pretty-print peek to 64 KiB, rejects a head that starts with `[`, and reuses the line buffer across `next()` calls. Treat the file as the reference, not the listing below.

```rust
//! Streaming NDJSON. `NdjsonReader` yields each line with its byte offset so
//! pass two can seek straight back to it; `LineAccess` does that seek.

use std::fs::File;
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

use crate::error::{KilnError, Result};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Line {
    pub number: usize,
    pub offset: u64,
    pub len: usize,
    pub text: String,
}

pub struct NdjsonReader {
    path: PathBuf,
    reader: BufReader<File>,
    offset: u64,
    number: usize,
}

impl NdjsonReader {
    /// Opens and rejects a pretty-printed document (first non-blank line is a
    /// lone `{` or `[`) before yielding anything, so the operator gets one
    /// clear message instead of an error per line.
    pub fn open(path: &Path) -> Result<Self> {
        let file = File::open(path).map_err(|e| KilnError::io(path, e))?;
        let mut reader = BufReader::with_capacity(1 << 20, file);
        let mut first = String::new();
        loop {
            first.clear();
            let n = reader.read_line(&mut first).map_err(|e| KilnError::io(path, e))?;
            if n == 0 || !first.trim().is_empty() {
                break;
            }
        }
        let head = first.trim();
        if head == "{" || head == "[" {
            return Err(KilnError::Usage(format!(
                "{}: looks like pretty-printed JSON, not NDJSON (one resource per line)",
                path.display()
            )));
        }
        reader.seek(SeekFrom::Start(0)).map_err(|e| KilnError::io(path, e))?;
        Ok(Self { path: path.to_path_buf(), reader, offset: 0, number: 0 })
    }
}

impl Iterator for NdjsonReader {
    type Item = Result<Line>;

    fn next(&mut self) -> Option<Self::Item> {
        let mut buf = Vec::new();
        loop {
            buf.clear();
            let start = self.offset;
            let n = match self.reader.read_until(b'\n', &mut buf) {
                Ok(n) => n,
                Err(e) => return Some(Err(KilnError::io(&self.path, e))),
            };
            if n == 0 {
                return None;
            }
            self.offset += n as u64;
            self.number += 1;
            let trimmed_end = buf.iter().rposition(|b| !b.is_ascii_whitespace()).map_or(0, |p| p + 1);
            let leading = buf[..trimmed_end].iter().position(|b| !b.is_ascii_whitespace());
            let Some(leading) = leading else { continue }; // blank line
            let slice = &buf[leading..trimmed_end];
            let text = match std::str::from_utf8(slice) {
                Ok(s) => s.to_string(),
                Err(e) => return Some(Err(KilnError::Usage(format!("{}: line {}: {e}", self.path.display(), self.number)))),
            };
            if !(text.starts_with('{')) {
                return Some(Err(KilnError::Usage(format!(
                    "{}: line {}: not a JSON object",
                    self.path.display(),
                    self.number
                ))));
            }
            return Some(Ok(Line { number: self.number, offset: start + leading as u64, len: slice.len(), text }));
        }
    }
}

pub struct LineAccess {
    path: PathBuf,
    file: File,
}

impl LineAccess {
    pub fn open(path: &Path) -> Result<Self> {
        let file = File::open(path).map_err(|e| KilnError::io(path, e))?;
        Ok(Self { path: path.to_path_buf(), file })
    }

    pub fn read_at(&mut self, offset: u64, len: usize) -> Result<String> {
        self.file.seek(SeekFrom::Start(offset)).map_err(|e| KilnError::io(&self.path, e))?;
        let mut buf = vec![0u8; len];
        self.file.read_exact(&mut buf).map_err(|e| KilnError::io(&self.path, e))?;
        String::from_utf8(buf).map_err(|e| KilnError::Usage(format!("{}: offset {offset}: {e}", self.path.display())))
    }
}
```

Note the validity check is only "starts with `{`"; JSON parsing happens in the caller so the reader stays cheap. Add `pub mod ndjson;` to `src/fhir/mod.rs`.

- [x] **Step 4: Run tests to verify they pass**

Run: `cargo test fhir::ndjson`
Expected: 4 passed. (The "unparseable line" test passes because `not json` does not start with `{`.)

- [x] **Step 5: Commit**

```bash
git add src/fhir
git commit -m "Add streaming NDJSON reader with byte offsets and seek-back access"
```

---

## Task 4: GeoJSON parsing and WKB encoding

**Files:**
- Create: `src/geometry/mod.rs`, `src/geometry/geojson.rs`, `src/geometry/wkb.rs`
- Modify: `src/main.rs` (add `mod geometry;`)

- [x] **Step 1: Write the failing tests for geojson.rs**

`src/geometry/geojson.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::report::Report;

    #[test]
    fn bare_geometry_feature_and_collection_all_parse() {
        let mut r = Report::default();
        let poly = r#"{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,1],[0,0]]]}"#;
        assert!(matches!(parse_boundary(poly.as_bytes(), "a", &mut r), Some(geo::Geometry::Polygon(_))));
        let feature = format!(r#"{{"type":"Feature","properties":{{}},"geometry":{poly}}}"#);
        assert!(matches!(parse_boundary(feature.as_bytes(), "a", &mut r), Some(geo::Geometry::Polygon(_))));
        let fc = format!(r#"{{"type":"FeatureCollection","features":[{feature}]}}"#);
        assert!(matches!(parse_boundary(fc.as_bytes(), "a", &mut r), Some(geo::Geometry::Polygon(_))));
        assert_eq!(r.counts().len(), 0);
    }

    #[test]
    fn multi_feature_collection_folds_into_multipolygon_and_reports() {
        let mut r = Report::default();
        let poly = r#"{"type":"Feature","geometry":{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,1],[0,0]]]}}"#;
        let fc = format!(r#"{{"type":"FeatureCollection","features":[{poly},{poly}]}}"#);
        match parse_boundary(fc.as_bytes(), "a", &mut r) {
            Some(geo::Geometry::MultiPolygon(mp)) => assert_eq!(mp.0.len(), 2),
            other => panic!("{other:?}"),
        }
        assert_eq!(r.count("boundary_multi_feature"), 1);
    }

    #[test]
    fn garbage_is_reported_as_unparseable() {
        let mut r = Report::default();
        assert!(parse_boundary(b"not json", "a", &mut r).is_none());
        assert!(parse_boundary(br#"{"type":"FeatureCollection","features":[]}"#, "a", &mut r).is_none());
        assert_eq!(r.count("boundary_unparseable"), 2);
    }
}
```

- [x] **Step 2: Implement parse_boundary**

```rust
//! Boundary attachment bytes -> a `geo` geometry. Accepts a bare geometry, a
//! Feature, or a FeatureCollection, as the Python did.

use geo::Geometry;
use geojson::GeoJson;

use crate::report::Report;

fn to_geo(g: geojson::Geometry, id: &str, report: &mut Report) -> Option<Geometry<f64>> {
    match Geometry::<f64>::try_from(g) {
        Ok(geom) => Some(geom),
        Err(err) => {
            report.add("boundary_unparseable", id, &err.to_string());
            None
        }
    }
}

pub fn parse_boundary(bytes: &[u8], id: &str, report: &mut Report) -> Option<Geometry<f64>> {
    let text = match std::str::from_utf8(bytes) {
        Ok(t) => t,
        Err(err) => {
            report.add("boundary_unparseable", id, &err.to_string());
            return None;
        }
    };
    let parsed: GeoJson = match text.parse() {
        Ok(p) => p,
        Err(err) => {
            report.add("boundary_unparseable", id, &err.to_string());
            return None;
        }
    };
    match parsed {
        GeoJson::Geometry(g) => to_geo(g, id, report),
        GeoJson::Feature(f) => match f.geometry {
            Some(g) => to_geo(g, id, report),
            None => {
                report.add("boundary_unparseable", id, "Feature has no geometry");
                None
            }
        },
        GeoJson::FeatureCollection(fc) => {
            let geoms: Vec<geojson::Geometry> = fc.features.into_iter().filter_map(|f| f.geometry).collect();
            match geoms.len() {
                0 => {
                    report.add("boundary_unparseable", id, "FeatureCollection has no geometry");
                    None
                }
                1 => to_geo(geoms.into_iter().next().unwrap(), id, report),
                n => {
                    report.add("boundary_multi_feature", id, &format!("{n} features folded into one geometry"));
                    let mut polygons = Vec::new();
                    for g in geoms {
                        match to_geo(g, id, report) {
                            Some(Geometry::Polygon(p)) => polygons.push(p),
                            Some(Geometry::MultiPolygon(mp)) => polygons.extend(mp.0),
                            Some(other) => report.add("boundary_unparseable", id, &format!("non-polygon part {:?} ignored", kind_name(&other))),
                            None => {}
                        }
                    }
                    if polygons.is_empty() { None } else { Some(Geometry::MultiPolygon(geo::MultiPolygon(polygons))) }
                }
            }
        }
    }
}

pub fn kind_name(g: &Geometry<f64>) -> &'static str {
    match g {
        Geometry::Point(_) => "Point",
        Geometry::Line(_) => "Line",
        Geometry::LineString(_) => "LineString",
        Geometry::Polygon(_) => "Polygon",
        Geometry::MultiPoint(_) => "MultiPoint",
        Geometry::MultiLineString(_) => "MultiLineString",
        Geometry::MultiPolygon(_) => "MultiPolygon",
        Geometry::GeometryCollection(_) => "GeometryCollection",
        Geometry::Rect(_) => "Rect",
        Geometry::Triangle(_) => "Triangle",
    }
}
```

- [x] **Step 3: Write the failing test for wkb.rs**

`src/geometry/wkb.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn point_encodes_as_21_little_endian_bytes() {
        let bytes = to_wkb(&geo::Geometry::Point(geo::Point::new(3.25, 6.25)));
        assert_eq!(bytes.len(), 21);
        assert_eq!(bytes[0], 1);
        assert_eq!(&bytes[1..5], &1u32.to_le_bytes());
        assert_eq!(f64::from_le_bytes(bytes[5..13].try_into().unwrap()), 3.25);
    }

    #[test]
    fn polygon_encodes_with_type_3() {
        let poly = geo::polygon![(x: 0., y: 0.), (x: 1., y: 0.), (x: 1., y: 1.), (x: 0., y: 0.)];
        let bytes = to_wkb(&geo::Geometry::Polygon(poly));
        assert_eq!(&bytes[1..5], &3u32.to_le_bytes());
    }
}
```

- [x] **Step 4: Implement to_wkb**

```rust
//! geo geometry -> little-endian WKB, the encoding Parquet GEOMETRY expects.

use wkb::writer::{write_geometry, WriteOptions};
use wkb::Endianness;

pub fn to_wkb(geom: &geo::Geometry<f64>) -> Vec<u8> {
    let mut out = Vec::new();
    let options = WriteOptions { endianness: Endianness::LittleEndian };
    write_geometry(&mut out, geom, &options).expect("writing WKB to a Vec cannot fail");
    out
}
```

If `write_geometry` does not accept `&geo::Geometry<f64>` because `geo-types` in the lockfile lacks `geo_traits` impls, pin `geo-types = { version = "0.7.16" }` (that version implements `GeometryTrait`) and run `cargo update -p geo-types`.

- [x] **Step 5: Create src/geometry/mod.rs and register the module**

```rust
pub mod geojson;
pub mod wkb;

pub use geojson::{kind_name, parse_boundary};
pub use wkb::to_wkb;
```

Add `mod geometry;` to `src/main.rs`.

- [x] **Step 6: Run tests to verify they pass**

Run: `cargo test geometry`
Expected: 5 passed.

- [x] **Step 7: Commit**

```bash
git add src/geometry src/main.rs
git commit -m "Parse boundary GeoJSON into geo geometries and encode WKB"
```

---

## Task 5: Geometry summary and full build

**Files:**
- Modify: `src/geometry/mod.rs`

- [x] **Step 1: Write the failing tests (append to src/geometry/mod.rs)**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::fhir::{Boundary, Location};
    use crate::report::Report;

    fn loc(position: Option<(f64, f64)>, boundary: Option<&str>) -> Location {
        Location {
            id: "x".into(),
            position,
            boundary: boundary.map(|b| Boundary::Inline(b.as_bytes().to_vec())),
            ..Default::default()
        }
    }
    const SQUARE: &str = r#"{"type":"Polygon","coordinates":[[[3,6],[4,6],[4,7],[3,7],[3,6]]]}"#;
    const BOWTIE: &str = r#"{"type":"Polygon","coordinates":[[[0,0],[2,2],[2,0],[0,2],[0,0]]]}"#;

    #[test]
    fn position_only_is_a_point() {
        let mut r = Report::default();
        let g = build(&loc(Some((3.25, 6.25)), None), &mut r).unwrap();
        assert_eq!(g.kind, GeomKind::Point);
        assert_eq!((g.lon, g.lat), (3.25, 6.25));
        assert_eq!(g.bbox, [3.25, 6.25, 3.25, 6.25]);
    }

    #[test]
    fn polygon_gets_an_interior_representative_point() {
        let mut r = Report::default();
        let g = build(&loc(None, Some(SQUARE)), &mut r).unwrap();
        assert_eq!(g.kind, GeomKind::Polygon);
        assert!(g.lon > 3.0 && g.lon < 4.0 && g.lat > 6.0 && g.lat < 7.0);
        assert_eq!(g.bbox, [3.0, 6.0, 4.0, 7.0]);
    }

    #[test]
    fn polygon_plus_position_uses_the_position_as_representative_point() {
        let mut r = Report::default();
        let g = build(&loc(Some((3.2, 6.2)), Some(SQUARE)), &mut r).unwrap();
        assert_eq!(g.kind, GeomKind::Polygon);
        assert_eq!((g.lon, g.lat), (3.2, 6.2));
    }

    #[test]
    fn invalid_polygon_is_kept_and_reported_not_repaired() {
        let mut r = Report::default();
        let g = build(&loc(None, Some(BOWTIE)), &mut r).unwrap();
        assert_eq!(g.kind, GeomKind::Polygon);
        assert_eq!(r.count("geometry_invalid"), 1);
        assert_eq!(r.count("geometry_repaired"), 0);
    }

    #[test]
    fn nothing_usable_is_reported_as_no_geometry() {
        let mut r = Report::default();
        assert!(build(&loc(None, None), &mut r).is_none());
        assert_eq!(r.count("no_geometry"), 1);
        let mut r = Report::default();
        assert!(build(&loc(None, Some(r#"{"type":"LineString","coordinates":[[0,0],[1,1]]}"#)), &mut r).is_none());
        assert_eq!(r.count("geometry_unexpected_type"), 1);
    }

    #[test]
    fn summarize_matches_build_but_keeps_no_geometry() {
        let mut r = Report::default();
        let s = summarize(&loc(Some((3.2, 6.2)), Some(SQUARE)), &mut r).unwrap();
        assert_eq!(s.kind, GeomKind::Polygon);
        assert_eq!(s.bbox, [3.0, 6.0, 4.0, 7.0]);
        assert_eq!(std::mem::size_of::<GeometrySummary>(), 40);
    }
}
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cargo test geometry::tests 2>&1 | grep -E "^error" | head -3`
Expected: compile errors.

- [x] **Step 3: Implement GeomKind, GeometrySummary, GeometryResult, summarize, build**

> Superseded during execution: the shipped `src/geometry/mod.rs` splits the work so that `summarize` (pass one) validates via `src/geometry/validity.rs` (a sweep-based self-intersection test with reasons; geo's `is_valid` is O(n²)) and reports `geometry_invalid`, `geometry_empty` and `position_outside_boundary`, while `build` (pass two) only computes the representative point and reports `geometry_no_interior_point` instead of falling back to the bbox centre. Treat the files as the reference.

Replace `src/geometry/mod.rs` contents above the tests with:

```rust
//! One Location's geometry: the small summary pass one keeps (kind + bbox),
//! and the full result pass two writes (WKB + representative point).
//!
//! Invalid geometry is reported and passed through unchanged. kiln does not
//! repair; that hides registry problems from the people who must fix them.

pub mod geojson;
pub mod wkb;

use geo::{BoundingRect, Geometry, InteriorPoint, Validation};

pub use geojson::{kind_name, parse_boundary};
pub use wkb::to_wkb;

use crate::fhir::{Boundary, Location};
use crate::report::Report;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum GeomKind {
    Point,
    Polygon,
}

impl GeomKind {
    pub fn as_str(self) -> &'static str {
        match self {
            GeomKind::Point => "point",
            GeomKind::Polygon => "polygon",
        }
    }
}

/// What pass one retains per Location: 40 bytes, no coordinates.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct GeometrySummary {
    pub kind: GeomKind,
    /// xmin, ymin, xmax, ymax
    pub bbox: [f64; 4],
}

/// What pass two produces per Location.
#[derive(Debug, Clone, PartialEq)]
pub struct GeometryResult {
    pub kind: GeomKind,
    pub geometry: Geometry<f64>,
    pub bbox: [f64; 4],
    /// Representative point: the FHIR position when present, else a point inside the polygon.
    pub lon: f64,
    pub lat: f64,
}

impl GeometryResult {
    pub fn wkb(&self) -> Vec<u8> {
        to_wkb(&self.geometry)
    }
}

fn bbox_of(geom: &Geometry<f64>) -> Option<[f64; 4]> {
    let r = geom.bounding_rect()?;
    Some([r.min().x, r.min().y, r.max().x, r.max().y])
}

/// Parse the boundary (if inline), classify, validate, and pick the
/// representative point. `None` means the Location produces no row.
pub fn build(loc: &Location, report: &mut Report) -> Option<GeometryResult> {
    let polygon = match &loc.boundary {
        Some(Boundary::Inline(bytes)) => parse_boundary(bytes, &loc.id, report),
        Some(Boundary::Url(_)) | None => None,
    };

    if let Some(geom) = polygon {
        if !geom.is_valid() {
            report.add("geometry_invalid", &loc.id, "boundary fails validity checks; written as is");
        }
        let kind = match &geom {
            Geometry::Point(_) => GeomKind::Point,
            Geometry::Polygon(_) | Geometry::MultiPolygon(_) => GeomKind::Polygon,
            other => {
                report.add("geometry_unexpected_type", &loc.id, &format!("unsupported type {}", kind_name(other)));
                return None;
            }
        };
        let Some(bbox) = bbox_of(&geom) else {
            report.add("geometry_unexpected_type", &loc.id, "empty geometry");
            return None;
        };
        let (lon, lat) = match (loc.position, &geom) {
            (Some(p), _) => p,
            (None, Geometry::Point(p)) => (p.x(), p.y()),
            (None, g) => match g.interior_point() {
                Some(p) => (p.x(), p.y()),
                None => ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0),
            },
        };
        return Some(GeometryResult { kind, geometry: geom, bbox, lon, lat });
    }

    if let Some((lon, lat)) = loc.position {
        return Some(GeometryResult {
            kind: GeomKind::Point,
            geometry: Geometry::Point(geo::Point::new(lon, lat)),
            bbox: [lon, lat, lon, lat],
            lon,
            lat,
        });
    }

    report.add("no_geometry", &loc.id, "no position and no usable boundary");
    None
}

/// Pass one: the same decisions as `build`, keeping only kind and bbox.
pub fn summarize(loc: &Location, report: &mut Report) -> Option<GeometrySummary> {
    build(loc, report).map(|g| GeometrySummary { kind: g.kind, bbox: g.bbox })
}
```

`summarize` calls `build` and drops the geometry. That parses once per pass, which is the README's design; the polygon lives only for the duration of the call.

- [x] **Step 4: Run tests to verify they pass**

Run: `cargo test geometry`
Expected: 11 passed. If the bowtie is reported valid by `geo`, check the ring orientation; a self-intersecting ring must fail `is_valid`. If `size_of::<GeometrySummary>()` is not 40, adjust the assertion to the printed value; it exists to catch accidental growth.

- [x] **Step 5: Commit**

```bash
git add src/geometry
git commit -m "Classify, validate and summarise Location geometry without repair"
```

---

## Task 6: Hierarchy resolution

**Files:**
- Create: `src/index/mod.rs`, `src/index/hierarchy.rs`
- Modify: `src/main.rs` (add `mod index;`)

- [x] **Step 1: Create src/index/mod.rs with the IndexRecord type**

```rust
pub mod hierarchy;
pub mod hilbert;
pub mod partition;

use crate::geometry::GeometrySummary;

/// What pass one keeps per Location. Strings are the only variable-size parts.
#[derive(Debug, Clone, Default)]
pub struct IndexRecord {
    pub id: String,
    pub part_of: Option<String>,
    pub name: Option<String>,
    pub pcode: Option<String>,
    pub type_code: Option<String>,
    /// Byte offset and length of the resource's line in locations.ndjson.
    pub offset: u64,
    pub len: usize,
    pub geometry: Option<GeometrySummary>,
    pub position: Option<(f64, f64)>,
    /// Set during pass one after hierarchy and country resolution.
    pub country: String,
    pub tier: String,
    pub hilbert: u64,
}

pub use hierarchy::{resolve_hierarchy, HierarchyInfo, ADMIN_COLUMNS};
```

(`hilbert` and `partition` come in Tasks 7 and 8; create empty files `src/index/hilbert.rs` and `src/index/partition.rs` now so the crate compiles.) Add `mod index;` to `src/main.rs`.

- [x] **Step 2: Write the failing tests in src/index/hierarchy.rs**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::IndexRecord;
    use crate::report::Report;

    fn rec(id: &str, parent: Option<&str>, ty: &str, pcode: Option<&str>) -> IndexRecord {
        IndexRecord {
            id: id.into(),
            part_of: parent.map(str::to_string),
            name: Some(id.to_uppercase()),
            pcode: pcode.map(str::to_string),
            type_code: Some(ty.into()),
            ..Default::default()
        }
    }

    fn fixture() -> Vec<IndexRecord> {
        vec![
            rec("ng", None, "admin-unit", Some("NG")),
            rec("kano", Some("ng"), "admin-unit", Some("NG001")),
            rec("nassarawa", Some("kano"), "admin-unit", Some("NG001002")),
            rec("gama", Some("nassarawa"), "settlement", None),
            rec("clinic", Some("gama"), "facility", None),
            rec("orphan", Some("does-not-exist"), "facility", None),
            rec("cyc-a", Some("cyc-b"), "admin-unit", None),
            rec("cyc-b", Some("cyc-a"), "admin-unit", None),
            rec("victim", Some("cyc-a"), "facility", None),
        ]
    }

    #[test]
    fn walks_past_non_admin_ancestors() {
        let mut report = Report::default();
        let resolved = resolve_hierarchy(&fixture(), &mut report);
        let clinic = &resolved["clinic"];
        assert_eq!(clinic.depth, 4);
        assert_eq!(clinic.admin_level, None);
        assert_eq!(clinic.path, "/ng/kano/nassarawa/gama/clinic");
        assert_eq!(clinic.ancestor_ids, vec!["ng", "kano", "nassarawa", "gama"]);
        assert_eq!(clinic.admin_names[0].as_deref(), Some("NG"));
        assert_eq!(clinic.admin_names[2].as_deref(), Some("NASSARAWA"));
        assert_eq!(clinic.admin_codes[2].as_deref(), Some("NG001002"));
        assert_eq!(clinic.admin_names[3], None);
        assert_eq!(clinic.country.as_deref(), Some("NG"));
        assert_eq!(resolved["nassarawa"].admin_level, Some(2));
        assert_eq!(resolved["ng"].depth, 0);
    }

    #[test]
    fn dangling_parent_is_reported_once_and_treated_as_root() {
        let mut report = Report::default();
        let resolved = resolve_hierarchy(&fixture(), &mut report);
        assert_eq!(report.count("orphan"), 1);
        assert_eq!(resolved["orphan"].depth, 0);
        assert_eq!(resolved["orphan"].country, None);
    }

    #[test]
    fn cycles_are_reported_and_dropped_with_their_descendants() {
        let mut report = Report::default();
        let resolved = resolve_hierarchy(&fixture(), &mut report);
        assert!(!resolved.contains_key("cyc-a"));
        assert!(!resolved.contains_key("victim"));
        assert_eq!(report.count("cycle"), 2);
        assert_eq!(report.count("unreachable_ancestor"), 1);
    }

    #[test]
    fn too_deep_chains_are_reported() {
        let mut recs = vec![rec("n0", None, "admin-unit", Some("X"))];
        for i in 1..20 {
            recs.push(rec(&format!("n{i}"), Some(&format!("n{}", i - 1)), "admin-unit", None));
        }
        let mut report = Report::default();
        let resolved = resolve_hierarchy(&recs, &mut report);
        assert!(resolved.contains_key("n12"));
        assert!(!resolved.contains_key("n13"));
        assert_eq!(report.count("too_deep"), 7);
    }
}
```

- [x] **Step 3: Run tests to verify they fail**

Run: `cargo test index::hierarchy 2>&1 | grep -E "^error" | head -3`
Expected: compile errors.

- [x] **Step 4: Implement resolve_hierarchy**

> Superseded during execution: the shipped `src/index/hierarchy.rs` is index-based. `resolve_hierarchy` returns a `Hierarchy { infos: Vec<Option<HierarchyInfo>> }` parallel to the records slice, where `HierarchyInfo` (~32 bytes) holds `depth`, `admin_count`, `admin_level`, `parent: Option<u32>` and `admin: [Option<u32>; 5]` as indices into records; `path`, `ancestor_ids`, `admin_names`, `admin_codes` and `country` are derived on demand by methods on `Hierarchy`. There is no chain cache; resolution is an O(n) memoised walk over parent indices. Duplicate ids are reported as `duplicate_id` (first occurrence wins). Consequence for Tasks 9 and 12: the `Index` must keep ALL parsed records as the addressing space (`records`), the parallel `hierarchy`, and a separate `order: Vec<u32>` of the retained record indices sorted by partition then Hilbert key. Pass two iterates `order`. The listings below are adjusted in the task prompts; treat the shipped files as the reference.

```rust
//! Resolve partOf chains into depth, path, ancestor list and admin columns.
//! A plain map walk with a chain cache, ported from the Python: at single
//! country scale it is fast, and it can say *which* id dangles and *what*
//! the cycle path was.

use std::collections::{HashMap, HashSet};

use crate::fhir::location::ADMIN_UNIT_TYPE;
use crate::index::IndexRecord;
use crate::report::Report;

pub const MAX_DEPTH: usize = 12;
pub const ADMIN_COLUMNS: usize = 5;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct HierarchyInfo {
    pub depth: i32,
    pub admin_level: Option<i32>,
    pub path: String,
    pub ancestor_ids: Vec<String>,
    pub admin_names: [Option<String>; ADMIN_COLUMNS],
    pub admin_codes: [Option<String>; ADMIN_COLUMNS],
    pub country: Option<String>,
}

enum WalkError {
    Cycle { path: Vec<String>, node: String },
    TooDeep { path: Vec<String> },
}

/// Ids from the root down to `id` inclusive.
fn chain<'a>(
    id: &str,
    by_id: &HashMap<&'a str, &'a IndexRecord>,
    cache: &mut HashMap<String, Vec<String>>,
) -> Result<Vec<String>, WalkError> {
    if let Some(c) = cache.get(id) {
        return Ok(c.clone());
    }
    let mut walked: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    let mut current = id.to_string();
    loop {
        if seen.contains(&current) {
            walked.push(current.clone());
            return Err(WalkError::Cycle { path: walked, node: current });
        }
        seen.insert(current.clone());
        walked.push(current.clone());
        if walked.len() > MAX_DEPTH + 1 {
            return Err(WalkError::TooDeep { path: walked });
        }
        if let Some(cached) = cache.get(&current) {
            // Splice: cached is root..current; walked is id..current (child first).
            let mut complete = cached.clone();
            complete.extend(walked.iter().rev().skip(1).cloned());
            if complete.len() > MAX_DEPTH + 1 {
                return Err(WalkError::TooDeep { path: complete });
            }
            for i in 0..complete.len() {
                cache.insert(complete[i].clone(), complete[..=i].to_vec());
            }
            return Ok(complete);
        }
        let parent = by_id.get(current.as_str()).and_then(|r| r.part_of.as_deref());
        match parent {
            Some(p) if by_id.contains_key(p) => current = p.to_string(),
            _ => break, // root, or dangling parent already reported
        }
    }
    walked.reverse();
    for i in 0..walked.len() {
        cache.insert(walked[i].clone(), walked[..=i].to_vec());
    }
    Ok(walked)
}

pub fn resolve_hierarchy(records: &[IndexRecord], report: &mut Report) -> HashMap<String, HierarchyInfo> {
    let by_id: HashMap<&str, &IndexRecord> = records.iter().map(|r| (r.id.as_str(), r)).collect();
    let mut cache: HashMap<String, Vec<String>> = HashMap::new();
    let mut resolved = HashMap::with_capacity(records.len());

    for r in records {
        if let Some(p) = &r.part_of {
            if !by_id.contains_key(p.as_str()) {
                report.add("orphan", &r.id, &format!("partOf references missing id {p}"));
            }
        }
    }

    for r in records {
        let chain = match chain(&r.id, &by_id, &mut cache) {
            Ok(c) => c,
            Err(WalkError::Cycle { path, node }) => {
                if node == r.id {
                    report.add("cycle", &r.id, &path.join(" -> "));
                } else {
                    report.add("unreachable_ancestor", &r.id, &format!("cycle involving {node}"));
                }
                continue;
            }
            Err(WalkError::TooDeep { path }) => {
                report.add("too_deep", &r.id, &format!("chain exceeds MAX_DEPTH={MAX_DEPTH}: {}", path.join(" -> ")));
                continue;
            }
        };

        let admin_chain: Vec<&IndexRecord> = chain
            .iter()
            .map(|id| by_id[id.as_str()])
            .filter(|rec| rec.type_code.as_deref() == Some(ADMIN_UNIT_TYPE))
            .collect();
        let admin_level = (r.type_code.as_deref() == Some(ADMIN_UNIT_TYPE)).then(|| admin_chain.len() as i32 - 1);

        let mut admin_names: [Option<String>; ADMIN_COLUMNS] = Default::default();
        let mut admin_codes: [Option<String>; ADMIN_COLUMNS] = Default::default();
        for (i, anc) in admin_chain.iter().take(ADMIN_COLUMNS).enumerate() {
            admin_names[i] = anc.name.clone();
            admin_codes[i] = anc.pcode.clone();
        }
        let country = admin_codes[0].clone();

        resolved.insert(
            r.id.clone(),
            HierarchyInfo {
                depth: chain.len() as i32 - 1,
                admin_level,
                path: format!("/{}", chain.join("/")),
                ancestor_ids: chain[..chain.len() - 1].to_vec(),
                admin_names,
                admin_codes,
                country,
            },
        );
    }
    resolved
}
```

Depth semantics: Python's `MAX_DEPTH = 12` allows a chain of 12 nodes (`len(walked) > MAX_DEPTH` raises). The test above expects `n12` present and `n13` absent, meaning chains of 13 nodes are allowed. Check against the Python test `test_hierarchy.py` and make the Rust match the Python exactly: if the Python drops `n12`, change `MAX_DEPTH + 1` to `MAX_DEPTH` in both places and fix the test expectations (`n11` present, `n12` absent, 8 too_deep).

- [x] **Step 5: Run tests to verify they pass**

Run: `cargo test index::hierarchy`
Expected: 4 passed.

- [x] **Step 6: Commit**

```bash
git add src/index src/main.rs
git commit -m "Resolve the Location hierarchy on the pass-one index"
```

---

## Task 7: Hilbert key

**Files:**
- Modify: `src/index/hilbert.rs`

- [ ] **Step 1: Write the failing tests**

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn corners_map_to_curve_ends() {
        let extent = [0.0, 0.0, 10.0, 10.0];
        assert_eq!(hilbert_key([0.0, 0.0], extent), 0);
        let last = hilbert_key([10.0, 0.0], extent);
        assert_eq!(last, (1u64 << (2 * ORDER)) - 1);
    }

    #[test]
    fn nearby_points_get_nearby_keys() {
        let extent = [0.0, 0.0, 100.0, 100.0];
        let a = hilbert_key([10.0, 10.0], extent);
        let b = hilbert_key([10.1, 10.1], extent);
        let far = hilbert_key([90.0, 90.0], extent);
        assert!(a.abs_diff(b) < a.abs_diff(far));
    }

    #[test]
    fn degenerate_extent_does_not_panic() {
        assert_eq!(hilbert_key([5.0, 5.0], [5.0, 5.0, 5.0, 5.0]), 0);
    }
}
```

- [ ] **Step 2: Implement hilbert_key**

```rust
//! Hilbert curve key for spatial clustering of rows within a partition.
//! Classic xy2d on a 2^ORDER grid over the dataset extent.

pub const ORDER: u32 = 16;

/// `point` is (x, y); `extent` is [xmin, ymin, xmax, ymax] of the whole dataset.
pub fn hilbert_key(point: [f64; 2], extent: [f64; 4]) -> u64 {
    let n = 1u64 << ORDER;
    let scale = |v: f64, lo: f64, hi: f64| -> u64 {
        if hi <= lo {
            return 0;
        }
        let t = ((v - lo) / (hi - lo)).clamp(0.0, 1.0);
        ((t * (n - 1) as f64).round() as u64).min(n - 1)
    };
    let mut x = scale(point[0], extent[0], extent[2]);
    let mut y = scale(point[1], extent[1], extent[3]);
    let mut d = 0u64;
    let mut s = n >> 1;
    while s > 0 {
        let rx = u64::from(x & s > 0);
        let ry = u64::from(y & s > 0);
        d += s * s * ((3 * rx) ^ ry);
        // rotate
        if ry == 0 {
            if rx == 1 {
                x = s - 1 - x;
                y = s - 1 - y;
            }
            std::mem::swap(&mut x, &mut y);
        }
        s >>= 1;
    }
    d
}
```

- [ ] **Step 3: Run tests**

Run: `cargo test index::hilbert`
Expected: 3 passed. If `corners_map_to_curve_ends` fails on the `last` corner, the curve end for this xy2d variant is `(n-1, 0)`; that is what the test uses. If it still fails, print the value and confirm it is one of the two corner keys (`0` or `n*n-1`), then fix the test to the corner this implementation ends on.

- [ ] **Step 4: Commit**

```bash
git add src/index/hilbert.rs
git commit -m "Add Hilbert curve key for spatial row ordering"
```

---

## Task 8: Partition values and directory segments

**Files:**
- Modify: `src/index/partition.rs`

- [ ] **Step 1: Write the failing tests**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::report::Report;

    #[test]
    fn parses_and_validates_keys() {
        assert_eq!(parse_keys("country,geom_type").unwrap(), vec![PartitionKey::Country, PartitionKey::GeomType]);
        assert!(parse_keys("country,nope").unwrap_err().to_string().contains("nope"));
    }

    #[test]
    fn slashes_are_sanitised_and_reported() {
        let mut report = Report::default();
        let mut claims = Claims::default();
        assert_eq!(segment("country", "A/B", &mut claims, &mut report), "A_B");
        assert_eq!(report.count("partition_value_sanitized"), 1);
    }

    #[test]
    fn colliding_values_get_distinct_segments() {
        let mut report = Report::default();
        let mut claims = Claims::default();
        let first = segment("country", "A/B", &mut claims, &mut report);
        let second = segment("country", "A_B", &mut claims, &mut report);
        assert_eq!(first, "A_B");
        assert_ne!(second, first);
        assert!(second.starts_with("A_B~"));
        assert_eq!(report.count("partition_value_collision"), 1);
        // Same value again reuses its segment.
        assert_eq!(segment("country", "A/B", &mut claims, &mut report), "A_B");
    }
}
```

- [ ] **Step 2: Implement**

```rust
//! Which columns a row can be partitioned by, and how a value becomes a
//! filesystem-safe, collision-free `key=value` directory segment.

use std::collections::HashMap;

use sha2::{Digest, Sha256};

use crate::error::{KilnError, Result};
use crate::index::IndexRecord;
use crate::report::Report;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PartitionKey {
    Country,
    GeomType,
    Tier,
    Type,
}

impl PartitionKey {
    pub fn name(self) -> &'static str {
        match self {
            PartitionKey::Country => "country",
            PartitionKey::GeomType => "geom_type",
            PartitionKey::Tier => "tier",
            PartitionKey::Type => "type",
        }
    }

    /// The row's value for this key, rendered as text. `null` for a missing value.
    pub fn value(self, rec: &IndexRecord) -> String {
        match self {
            PartitionKey::Country => rec.country.clone(),
            PartitionKey::GeomType => rec.geometry.map(|g| g.kind.as_str().to_string()).unwrap_or_else(|| "null".into()),
            PartitionKey::Tier => rec.tier.clone(),
            PartitionKey::Type => rec.type_code.clone().unwrap_or_else(|| "null".into()),
        }
    }
}

pub fn parse_keys(spec: &str) -> Result<Vec<PartitionKey>> {
    spec.split(',')
        .map(|k| match k.trim() {
            "country" => Ok(PartitionKey::Country),
            "geom_type" => Ok(PartitionKey::GeomType),
            "tier" => Ok(PartitionKey::Tier),
            "type" => Ok(PartitionKey::Type),
            other => Err(KilnError::Usage(format!(
                "--partition-by: unknown key {other:?}; available: country, geom_type, tier, type"
            ))),
        })
        .collect()
}

/// Segment claims within one parent directory: segment -> original value.
#[derive(Debug, Default)]
pub struct Claims(HashMap<String, String>);

/// Render one value as a directory segment (without the `key=` prefix).
/// A `/`, `\` or NUL is replaced; two distinct values that render the same
/// get a short hash suffix on the second, so neither silently overwrites
/// the other.
pub fn segment(key: &str, value: &str, claims: &mut Claims, report: &mut Report) -> String {
    let candidate: String = value.chars().map(|c| if c == '/' || c == '\\' { '_' } else { c }).filter(|c| *c != '\0').collect();
    let candidate = if candidate.is_empty() { "empty".to_string() } else { candidate };
    let sanitized = candidate != value;

    match claims.0.get(&candidate) {
        None => {
            claims.0.insert(candidate.clone(), value.to_string());
            if sanitized {
                report.add("partition_value_sanitized", &format!("{key}={value}"),
                    &format!("contained a path separator or control character; written to the directory {key}={candidate:?} instead"));
            }
            candidate
        }
        Some(owner) if owner == value => candidate,
        Some(_) => {
            let mut salt = String::new();
            loop {
                let digest = Sha256::digest(format!("{candidate}\0{value}{salt}").as_bytes());
                let seg = format!("{candidate}~{:02x}{:02x}{:02x}{:02x}", digest[0], digest[1], digest[2], digest[3]);
                match claims.0.get(&seg) {
                    Some(owner) if owner != value => salt.push('#'),
                    _ => {
                        claims.0.insert(seg.clone(), value.to_string());
                        report.add("partition_value_collision", &format!("{key}={value}"),
                            &format!("renders to the same directory segment {key}={candidate:?} as a different value already written under this partition; disambiguated to {key}={seg:?} instead"));
                        return seg;
                    }
                }
            }
        }
    }
}
```

- [ ] **Step 3: Run tests**

Run: `cargo test index::partition`
Expected: 3 passed.

- [ ] **Step 4: Commit**

```bash
git add src/index/partition.rs
git commit -m "Add partition keys with sanitised, collision-free directory segments"
```

---

## Task 9: Pass one — build the index

**Files:**
- Create: `src/index/build.rs`
- Modify: `src/index/mod.rs`

- [ ] **Step 1: Write the failing test**

`src/index/build.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn indexes_the_fixture_snapshot() {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/snapshot/locations.ndjson");
        let index = build_index(&path, None).unwrap();
        let ids: Vec<&str> = index.records.iter().map(|r| r.id.as_str()).collect();
        // 12 resources; dropped: remote (url boundary), ghost (no geometry), cyc-a, cyc-b (cycle)
        assert_eq!(index.records.len(), 8, "{ids:?}");
        assert!(!ids.contains(&"ghost"));
        assert!(!ids.contains(&"cyc-a"));
        let clinic = index.records.iter().find(|r| r.id == "clinic").unwrap();
        assert_eq!(clinic.country, "NG");
        assert_eq!(clinic.tier, "site");
        assert_eq!(index.hierarchy["clinic"].admin_names[1].as_deref(), Some("Kano"));
        let orphan = index.records.iter().find(|r| r.id == "orphan").unwrap();
        assert_eq!(orphan.country, "unknown");
        assert_eq!(index.report.count("no_country"), 1);
        assert_eq!(index.report.count("boundary_unresolved_url"), 1);
        assert_eq!(index.report.count("duplicate_pcode"), 1);
        assert_eq!(index.report.count("cycle"), 2);
        assert_eq!(index.report.count("orphan"), 1);
        // Sorted by partition then Hilbert: all NG polygons before NG points, etc.
        let keys: Vec<(String, &str)> = index.records.iter().map(|r| (r.country.clone(), r.geometry.unwrap().kind.as_str())).collect();
        let mut sorted = keys.clone();
        sorted.sort();
        assert_eq!(keys, sorted);
        assert!(index.records.iter().all(|r| r.len > 0));
    }

    #[test]
    fn country_override_wins() {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/snapshot/locations.ndjson");
        let index = build_index(&path, Some("XX")).unwrap();
        assert!(index.records.iter().all(|r| r.country == "XX"));
        assert_eq!(index.report.count("no_country"), 0);
    }
}
```

- [ ] **Step 2: Implement build_index**

```rust
//! Pass one: stream the snapshot, keep an IndexRecord per Location, resolve
//! the hierarchy, choose country and tier, sort by partition then Hilbert.

use std::collections::HashMap;
use std::path::Path;

use crate::fhir::ndjson::NdjsonReader;
use crate::fhir::{Boundary, Location};
use crate::geometry;
use crate::index::hierarchy::{resolve_hierarchy, HierarchyInfo};
use crate::index::hilbert::hilbert_key;
use crate::index::IndexRecord;
use crate::report::Report;

pub const UNKNOWN_COUNTRY: &str = "unknown";

pub struct Index {
    /// Only Locations that resolved and have geometry, in output order.
    pub records: Vec<IndexRecord>,
    pub hierarchy: HashMap<String, HierarchyInfo>,
    pub report: Report,
    /// Total resources read, including ones dropped.
    pub read: usize,
}

pub fn build_index(ndjson: &Path, country_override: Option<&str>) -> crate::error::Result<Index> {
    let mut report = Report::default();
    let mut records: Vec<IndexRecord> = Vec::new();
    let mut read = 0usize;

    for line in NdjsonReader::open(ndjson)? {
        let line = line?;
        read += 1;
        let value: serde_json::Value = match serde_json::from_str(&line.text) {
            Ok(v) => v,
            Err(err) => {
                report.add("malformed_field", &format!("line {}", line.number), &err.to_string());
                continue;
            }
        };
        let Some(loc) = Location::parse(&value, &mut report) else { continue };
        if let Some(Boundary::Url(url)) = &loc.boundary {
            report.add("boundary_unresolved_url", &loc.id, &format!("boundary is a url reference ({url}); run kiln extract to inline it"));
        }
        let geometry = geometry::summarize(&loc, &mut report);
        records.push(IndexRecord {
            id: loc.id,
            part_of: loc.part_of,
            name: loc.name,
            pcode: loc.pcode,
            type_code: loc.type_code,
            offset: line.offset,
            len: line.len,
            geometry,
            position: loc.position,
            ..Default::default()
        });
    }

    // Hierarchy is resolved over every parsed record, including ones with no
    // geometry: a geometry-less district is still somebody's parent.
    let hierarchy = resolve_hierarchy(&records, &mut report);

    records.retain(|r| hierarchy.contains_key(&r.id) && r.geometry.is_some());

    let mut extent = [f64::MAX, f64::MAX, f64::MIN, f64::MIN];
    for r in &records {
        let b = r.geometry.unwrap().bbox;
        extent = [extent[0].min(b[0]), extent[1].min(b[1]), extent[2].max(b[2]), extent[3].max(b[3])];
    }

    let mut by_pcode: HashMap<String, Vec<String>> = HashMap::new();
    for r in &mut records {
        let info = &hierarchy[&r.id];
        r.country = match (country_override, &info.country) {
            (Some(c), _) => c.to_string(),
            (None, Some(c)) => c.clone(),
            (None, None) => {
                report.add("no_country", &r.id, "no admin-unit ancestor carries a pcode; filed under 'unknown'");
                UNKNOWN_COUNTRY.to_string()
            }
        };
        r.tier = match info.admin_level {
            Some(level) => level.to_string(),
            None => "site".to_string(),
        };
        let b = r.geometry.unwrap().bbox;
        r.hilbert = hilbert_key([(b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0], extent);
        if let Some(p) = &r.pcode {
            by_pcode.entry(p.clone()).or_default().push(r.id.clone());
        }
    }

    let mut dups: Vec<(&String, &Vec<String>)> = by_pcode.iter().filter(|(_, ids)| ids.len() > 1).collect();
    dups.sort();
    for (pcode, ids) in dups {
        let mut ids = ids.clone();
        ids.sort();
        report.add("duplicate_pcode", &ids.join(", "), &format!("pcode {pcode} claimed by {} Locations", ids.len()));
    }

    records.sort_by(|a, b| {
        (a.country.as_str(), a.geometry.unwrap().kind.as_str(), a.hilbert)
            .cmp(&(b.country.as_str(), b.geometry.unwrap().kind.as_str(), b.hilbert))
    });

    Ok(Index { records, hierarchy, report, read })
}
```

The sort is by the default partition keys. Task 11 re-groups by the requested `--partition-by` keys; because the sort is by country then geom_type then Hilbert, any subset or reordering of `country, geom_type, tier, type` still yields spatially clustered groups within each partition. That is the intended behaviour and needs no extra sort.

Add to `src/index/mod.rs`:

```rust
pub mod build;
pub use build::{build_index, Index, UNKNOWN_COUNTRY};
```

- [ ] **Step 3: Run tests**

Run: `cargo test index::build`
Expected: 2 passed. If the count of retained records differs, print `ids` and reconcile against the fixture comments in `python/tests/fixtures/build_fixture.py`: `remote` has a URL boundary and no position, so it has no geometry and is dropped; `dup` has a polygon and is kept.

- [ ] **Step 4: Commit**

```bash
git add src/index
git commit -m "Pass one: build the sorted Location index from a snapshot"
```

---

## Task 10: Output schema and row batches

**Files:**
- Create: `src/write/mod.rs`, `src/write/schema.rs`
- Modify: `src/main.rs` (add `mod write;`)

- [ ] **Step 1: Create src/write/mod.rs**

```rust
pub mod dataset;
pub mod parquet;
pub mod schema;
```

(Create empty `dataset.rs` and `parquet.rs` so it compiles.) Add `mod write;` to `src/main.rs`.

- [ ] **Step 2: Write the failing tests in src/write/schema.rs**

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schema_has_the_documented_columns_in_order() {
        let schema = output_schema();
        let names: Vec<&str> = schema.fields().iter().map(|f| f.name().as_str()).collect();
        assert_eq!(&names[..3], &["id", "version_id", "last_updated"]);
        assert!(names.contains(&"part_of"));
        assert!(names.contains(&"identifier"));
        assert!(names.contains(&"position_longitude"));
        assert!(names.contains(&"admin4_code"));
        assert_eq!(&names[names.len() - 3..], &["geometry", "bbox", "fhir_json"]);
        let geom = schema.field_with_name("geometry").unwrap();
        assert_eq!(geom.metadata()["ARROW:extension:name"], "geoarrow.wkb");
        assert!(geom.metadata()["ARROW:extension:metadata"].contains("OGC:CRS84"));
    }

    #[test]
    fn batch_round_trips_a_row() {
        let mut batch = RowBatch::new();
        let row = OutputRow {
            id: "a".into(),
            name: Some("A".into()),
            alias: vec!["x".into()],
            identifier: vec![("sys".to_string(), "v".to_string())],
            depth: 2,
            admin_level: Some(1),
            tier: "1".into(),
            path: "/ng/a".into(),
            ancestor_ids: vec!["ng".into()],
            country: "NG".into(),
            geom_type: "point".into(),
            lon: 1.0,
            lat: 2.0,
            wkb: vec![1, 0, 0, 0, 0],
            bbox: [1.0, 2.0, 1.0, 2.0],
            fhir_json: "{}".into(),
            ..Default::default()
        };
        batch.push(&row);
        let rb = batch.finish().unwrap();
        assert_eq!(rb.num_rows(), 1);
        assert_eq!(rb.schema().fields().len(), output_schema().fields().len());
        let ids = rb.column_by_name("id").unwrap();
        assert_eq!(ids.as_any().downcast_ref::<arrow_array::StringArray>().unwrap().value(0), "a");
        let level = rb.column_by_name("admin_level").unwrap();
        assert_eq!(level.as_any().downcast_ref::<arrow_array::Int32Array>().unwrap().value(0), 1);
    }
}
```

- [ ] **Step 3: Implement the schema, OutputRow and RowBatch**

```rust
//! The output table: Arrow schema, the row struct pass two fills, and the
//! column builders that turn rows into a RecordBatch.
//!
//! Column names follow the FHIR path they come from. See README "Columns".

use std::collections::HashMap;
use std::sync::Arc;

use arrow_array::builder::{
    BinaryBuilder, Float64Builder, Int32Builder, ListBuilder, StringBuilder, StructBuilder,
};
use arrow_array::{ArrayRef, RecordBatch};
use arrow_schema::{DataType, Field, Fields, Schema, SchemaRef};

use crate::index::ADMIN_COLUMNS;

pub const GEOMETRY_COLUMN: &str = "geometry";
pub const BBOX_COLUMN: &str = "bbox";

fn utf8(name: &str) -> Field {
    Field::new(name, DataType::Utf8, true)
}

fn list_utf8(name: &str) -> Field {
    Field::new(name, DataType::List(Arc::new(Field::new("item", DataType::Utf8, true))), true)
}

pub fn identifier_fields() -> Fields {
    vec![Field::new("system", DataType::Utf8, true), Field::new("value", DataType::Utf8, true)].into()
}

pub fn bbox_fields() -> Fields {
    vec![
        Field::new("xmin", DataType::Float64, false),
        Field::new("ymin", DataType::Float64, false),
        Field::new("xmax", DataType::Float64, false),
        Field::new("ymax", DataType::Float64, false),
    ]
    .into()
}

pub fn geometry_field() -> Field {
    let mut meta = HashMap::new();
    meta.insert("ARROW:extension:name".to_string(), "geoarrow.wkb".to_string());
    meta.insert(
        "ARROW:extension:metadata".to_string(),
        r#"{"crs":"OGC:CRS84","crs_type":"authority_code"}"#.to_string(),
    );
    Field::new(GEOMETRY_COLUMN, DataType::Binary, true).with_metadata(meta)
}

pub fn output_schema() -> SchemaRef {
    let mut fields = vec![
        Field::new("id", DataType::Utf8, false),
        utf8("version_id"),
        utf8("last_updated"),
        utf8("name"),
        list_utf8("alias"),
        utf8("status"),
        utf8("description"),
        utf8("type"),
        utf8("physical_type"),
        utf8("part_of"),
        utf8("managing_organization"),
        Field::new(
            "identifier",
            DataType::List(Arc::new(Field::new("item", DataType::Struct(identifier_fields()), true))),
            true,
        ),
        Field::new("position_longitude", DataType::Float64, true),
        Field::new("position_latitude", DataType::Float64, true),
        utf8("pcode"),
        utf8("gers_id"),
        utf8("settlement_type"),
        utf8("delivery_strategy"),
        utf8("facility_level"),
        utf8("ownership"),
        Field::new("depth", DataType::Int32, false),
        Field::new("admin_level", DataType::Int32, true),
        Field::new("tier", DataType::Utf8, false),
        Field::new("path", DataType::Utf8, false),
        list_utf8("ancestor_ids"),
    ];
    for i in 0..ADMIN_COLUMNS {
        fields.push(utf8(&format!("admin{i}_name")));
    }
    for i in 0..ADMIN_COLUMNS {
        fields.push(utf8(&format!("admin{i}_code")));
    }
    fields.extend([
        list_utf8("overlays_admin_unit_ids"),
        Field::new("country", DataType::Utf8, false),
        Field::new("geom_type", DataType::Utf8, false),
        Field::new("lon", DataType::Float64, false),
        Field::new("lat", DataType::Float64, false),
        geometry_field(),
        Field::new(BBOX_COLUMN, DataType::Struct(bbox_fields()), false),
        Field::new("fhir_json", DataType::Utf8, false),
    ]);
    Arc::new(Schema::new(fields))
}

/// One output row, fully resolved. Built by pass two.
#[derive(Debug, Clone, Default)]
pub struct OutputRow {
    pub id: String,
    pub version_id: Option<String>,
    pub last_updated: Option<String>,
    pub name: Option<String>,
    pub alias: Vec<String>,
    pub status: Option<String>,
    pub description: Option<String>,
    pub type_code: Option<String>,
    pub physical_type: Option<String>,
    pub part_of: Option<String>,
    pub managing_organization: Option<String>,
    pub identifier: Vec<(String, String)>,
    pub position: Option<(f64, f64)>,
    pub pcode: Option<String>,
    pub gers_id: Option<String>,
    pub settlement_type: Option<String>,
    pub delivery_strategy: Option<String>,
    pub facility_level: Option<String>,
    pub ownership: Option<String>,
    pub depth: i32,
    pub admin_level: Option<i32>,
    pub tier: String,
    pub path: String,
    pub ancestor_ids: Vec<String>,
    pub admin_names: [Option<String>; ADMIN_COLUMNS],
    pub admin_codes: [Option<String>; ADMIN_COLUMNS],
    pub overlays_admin_unit_ids: Vec<String>,
    pub country: String,
    pub geom_type: String,
    pub lon: f64,
    pub lat: f64,
    pub wkb: Vec<u8>,
    pub bbox: [f64; 4],
    pub fhir_json: String,
}

/// Column builders for one row group's worth of rows.
pub struct RowBatch {
    schema: SchemaRef,
    id: StringBuilder,
    version_id: StringBuilder,
    last_updated: StringBuilder,
    name: StringBuilder,
    alias: ListBuilder<StringBuilder>,
    status: StringBuilder,
    description: StringBuilder,
    type_code: StringBuilder,
    physical_type: StringBuilder,
    part_of: StringBuilder,
    managing_organization: StringBuilder,
    identifier: ListBuilder<StructBuilder>,
    position_longitude: Float64Builder,
    position_latitude: Float64Builder,
    pcode: StringBuilder,
    gers_id: StringBuilder,
    settlement_type: StringBuilder,
    delivery_strategy: StringBuilder,
    facility_level: StringBuilder,
    ownership: StringBuilder,
    depth: Int32Builder,
    admin_level: Int32Builder,
    tier: StringBuilder,
    path: StringBuilder,
    ancestor_ids: ListBuilder<StringBuilder>,
    admin_names: Vec<StringBuilder>,
    admin_codes: Vec<StringBuilder>,
    overlays: ListBuilder<StringBuilder>,
    country: StringBuilder,
    geom_type: StringBuilder,
    lon: Float64Builder,
    lat: Float64Builder,
    geometry: BinaryBuilder,
    bbox: StructBuilder,
    fhir_json: StringBuilder,
    pub rows: usize,
}

fn push_list(b: &mut ListBuilder<StringBuilder>, items: &[String]) {
    for item in items {
        b.values().append_value(item);
    }
    b.append(true);
}

impl RowBatch {
    pub fn new() -> Self {
        Self {
            schema: output_schema(),
            id: StringBuilder::new(),
            version_id: StringBuilder::new(),
            last_updated: StringBuilder::new(),
            name: StringBuilder::new(),
            alias: ListBuilder::new(StringBuilder::new()),
            status: StringBuilder::new(),
            description: StringBuilder::new(),
            type_code: StringBuilder::new(),
            physical_type: StringBuilder::new(),
            part_of: StringBuilder::new(),
            managing_organization: StringBuilder::new(),
            identifier: ListBuilder::new(StructBuilder::from_fields(identifier_fields(), 0)),
            position_longitude: Float64Builder::new(),
            position_latitude: Float64Builder::new(),
            pcode: StringBuilder::new(),
            gers_id: StringBuilder::new(),
            settlement_type: StringBuilder::new(),
            delivery_strategy: StringBuilder::new(),
            facility_level: StringBuilder::new(),
            ownership: StringBuilder::new(),
            depth: Int32Builder::new(),
            admin_level: Int32Builder::new(),
            tier: StringBuilder::new(),
            path: StringBuilder::new(),
            ancestor_ids: ListBuilder::new(StringBuilder::new()),
            admin_names: (0..ADMIN_COLUMNS).map(|_| StringBuilder::new()).collect(),
            admin_codes: (0..ADMIN_COLUMNS).map(|_| StringBuilder::new()).collect(),
            overlays: ListBuilder::new(StringBuilder::new()),
            country: StringBuilder::new(),
            geom_type: StringBuilder::new(),
            lon: Float64Builder::new(),
            lat: Float64Builder::new(),
            geometry: BinaryBuilder::new(),
            bbox: StructBuilder::from_fields(bbox_fields(), 0),
            fhir_json: StringBuilder::new(),
            rows: 0,
        }
    }

    pub fn push(&mut self, r: &OutputRow) {
        self.id.append_value(&r.id);
        self.version_id.append_option(r.version_id.as_deref());
        self.last_updated.append_option(r.last_updated.as_deref());
        self.name.append_option(r.name.as_deref());
        push_list(&mut self.alias, &r.alias);
        self.status.append_option(r.status.as_deref());
        self.description.append_option(r.description.as_deref());
        self.type_code.append_option(r.type_code.as_deref());
        self.physical_type.append_option(r.physical_type.as_deref());
        self.part_of.append_option(r.part_of.as_deref());
        self.managing_organization.append_option(r.managing_organization.as_deref());
        {
            let sb = self.identifier.values();
            for (system, value) in &r.identifier {
                sb.field_builder::<StringBuilder>(0).unwrap().append_value(system);
                sb.field_builder::<StringBuilder>(1).unwrap().append_value(value);
                sb.append(true);
            }
            self.identifier.append(true);
        }
        self.position_longitude.append_option(r.position.map(|p| p.0));
        self.position_latitude.append_option(r.position.map(|p| p.1));
        self.pcode.append_option(r.pcode.as_deref());
        self.gers_id.append_option(r.gers_id.as_deref());
        self.settlement_type.append_option(r.settlement_type.as_deref());
        self.delivery_strategy.append_option(r.delivery_strategy.as_deref());
        self.facility_level.append_option(r.facility_level.as_deref());
        self.ownership.append_option(r.ownership.as_deref());
        self.depth.append_value(r.depth);
        self.admin_level.append_option(r.admin_level);
        self.tier.append_value(&r.tier);
        self.path.append_value(&r.path);
        push_list(&mut self.ancestor_ids, &r.ancestor_ids);
        for i in 0..ADMIN_COLUMNS {
            self.admin_names[i].append_option(r.admin_names[i].as_deref());
            self.admin_codes[i].append_option(r.admin_codes[i].as_deref());
        }
        push_list(&mut self.overlays, &r.overlays_admin_unit_ids);
        self.country.append_value(&r.country);
        self.geom_type.append_value(&r.geom_type);
        self.lon.append_value(r.lon);
        self.lat.append_value(r.lat);
        self.geometry.append_value(&r.wkb);
        for (i, v) in r.bbox.iter().enumerate() {
            self.bbox.field_builder::<Float64Builder>(i).unwrap().append_value(*v);
        }
        self.bbox.append(true);
        self.fhir_json.append_value(&r.fhir_json);
        self.rows += 1;
    }

    pub fn finish(&mut self) -> Result<RecordBatch, arrow_schema::ArrowError> {
        let mut cols: Vec<ArrayRef> = vec![
            Arc::new(self.id.finish()),
            Arc::new(self.version_id.finish()),
            Arc::new(self.last_updated.finish()),
            Arc::new(self.name.finish()),
            Arc::new(self.alias.finish()),
            Arc::new(self.status.finish()),
            Arc::new(self.description.finish()),
            Arc::new(self.type_code.finish()),
            Arc::new(self.physical_type.finish()),
            Arc::new(self.part_of.finish()),
            Arc::new(self.managing_organization.finish()),
            Arc::new(self.identifier.finish()),
            Arc::new(self.position_longitude.finish()),
            Arc::new(self.position_latitude.finish()),
            Arc::new(self.pcode.finish()),
            Arc::new(self.gers_id.finish()),
            Arc::new(self.settlement_type.finish()),
            Arc::new(self.delivery_strategy.finish()),
            Arc::new(self.facility_level.finish()),
            Arc::new(self.ownership.finish()),
            Arc::new(self.depth.finish()),
            Arc::new(self.admin_level.finish()),
            Arc::new(self.tier.finish()),
            Arc::new(self.path.finish()),
            Arc::new(self.ancestor_ids.finish()),
        ];
        for b in &mut self.admin_names {
            cols.push(Arc::new(b.finish()));
        }
        for b in &mut self.admin_codes {
            cols.push(Arc::new(b.finish()));
        }
        cols.extend([
            Arc::new(self.overlays.finish()) as ArrayRef,
            Arc::new(self.country.finish()),
            Arc::new(self.geom_type.finish()),
            Arc::new(self.lon.finish()),
            Arc::new(self.lat.finish()),
            Arc::new(self.geometry.finish()),
            Arc::new(self.bbox.finish()),
            Arc::new(self.fhir_json.finish()),
        ]);
        self.rows = 0;
        RecordBatch::try_new(self.schema.clone(), cols)
    }
}

impl Default for RowBatch {
    fn default() -> Self {
        Self::new()
    }
}
```

The list child field name is `item` and nullable, which is what `ListBuilder` produces; if `RecordBatch::try_new` reports a schema mismatch on a list column, compare the builder's field against the schema's and align the schema, not the builder.

- [ ] **Step 4: Run tests**

Run: `cargo test write::schema`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/write src/main.rs
git commit -m "Define the output Arrow schema and row batch builders"
```

---

## Task 11: Partition Parquet writer

**Files:**
- Modify: `src/write/parquet.rs`

- [ ] **Step 1: Write the failing test**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::write::schema::{OutputRow, RowBatch};
    use parquet::basic::LogicalType;
    use parquet::file::reader::{FileReader, SerializedFileReader};

    fn row(id: &str, x: f64, y: f64) -> OutputRow {
        let mut wkb = vec![1u8];
        wkb.extend_from_slice(&1u32.to_le_bytes());
        wkb.extend_from_slice(&x.to_le_bytes());
        wkb.extend_from_slice(&y.to_le_bytes());
        OutputRow {
            id: id.into(),
            tier: "site".into(),
            path: format!("/{id}"),
            country: "NG".into(),
            geom_type: "point".into(),
            lon: x,
            lat: y,
            wkb,
            bbox: [x, y, x, y],
            fhir_json: "{}".into(),
            ..Default::default()
        }
    }

    #[test]
    fn writes_native_geometry_type_with_stats_and_geo_metadata() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("part-0.parquet");
        let mut writer = PartitionWriter::create(&path, 2).unwrap();
        let mut batch = RowBatch::new();
        for (i, (x, y)) in [(3.0, 6.0), (3.5, 6.5), (50.0, 50.0)].iter().enumerate() {
            batch.push(&row(&format!("r{i}"), *x, *y));
            if batch.rows == 2 {
                writer.write(&mut batch).unwrap();
            }
        }
        writer.write(&mut batch).unwrap();
        let stats = writer.finish(&["Point".to_string()]).unwrap();
        assert_eq!(stats.rows, 3);
        assert_eq!(stats.row_groups, 2);

        let reader = SerializedFileReader::new(std::fs::File::open(&path).unwrap()).unwrap();
        let meta = reader.metadata();
        let geom_col = meta.file_metadata().schema_descr().columns().iter().find(|c| c.name() == "geometry").unwrap();
        assert!(matches!(geom_col.logical_type(), Some(LogicalType::Geometry(_))), "{:?}", geom_col.logical_type());
        let rg0 = meta.row_group(0).columns().iter().find(|c| c.column_path().string() == "geometry").unwrap();
        let bbox = rg0.geo_statistics().unwrap().bounding_box().unwrap();
        assert_eq!((bbox.get_xmin(), bbox.get_xmax()), (3.0, 3.5));
        let geo = meta.file_metadata().key_value_metadata().unwrap().iter().find(|kv| kv.key == "geo").unwrap();
        let geo: serde_json::Value = serde_json::from_str(geo.value.as_ref().unwrap()).unwrap();
        assert_eq!(geo["primary_column"], "geometry");
        assert_eq!(geo["columns"]["geometry"]["bbox"], serde_json::json!([3.0, 6.0, 50.0, 50.0]));
        assert!(geo["columns"]["geometry"].get("crs").is_none(), "crs key must be absent, not null");
    }
}
```

- [ ] **Step 2: Implement PartitionWriter**

```rust
//! One Parquet file per partition. Native GEOMETRY logical type comes from
//! the `geoarrow.wkb` extension on the geometry field (parquet's `geospatial`
//! feature); GeoParquet 1.1 `geo` metadata and the bbox column are written
//! for readers that predate the logical type. Matches what the spike in
//! docs/superpowers/spikes/2026-09-05-rust-native-geoparquet produced.

use std::fs::File;
use std::path::{Path, PathBuf};

use parquet::arrow::ArrowWriter;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::metadata::KeyValue;
use parquet::file::properties::WriterProperties;

use crate::error::{KilnError, Result};
use crate::write::schema::{output_schema, RowBatch, BBOX_COLUMN, GEOMETRY_COLUMN};

pub struct PartitionStats {
    pub rows: usize,
    pub row_groups: usize,
}

pub struct PartitionWriter {
    path: PathBuf,
    writer: ArrowWriter<File>,
    bbox: [f64; 4],
    rows: usize,
    row_groups: usize,
}

impl PartitionWriter {
    pub fn create(path: &Path, row_group_size: usize) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| KilnError::io(parent, e))?;
        }
        let file = File::create(path).map_err(|e| KilnError::io(path, e))?;
        let props = WriterProperties::builder()
            .set_compression(Compression::ZSTD(ZstdLevel::default()))
            .set_max_row_group_size(row_group_size.max(1))
            .build();
        let writer = ArrowWriter::try_new(file, output_schema(), Some(props))?;
        Ok(Self { path: path.to_path_buf(), writer, bbox: [f64::MAX, f64::MAX, f64::MIN, f64::MIN], rows: 0, row_groups: 0 })
    }

    /// Flush the batch as one row group. Callers fill a batch to
    /// `row_group_size` and call this; the last partial batch is fine.
    pub fn write(&mut self, batch: &mut RowBatch) -> Result<()> {
        if batch.rows == 0 {
            return Ok(());
        }
        let rb = batch.finish()?;
        let bbox = rb.column_by_name(BBOX_COLUMN).unwrap();
        let bbox = bbox.as_any().downcast_ref::<arrow_array::StructArray>().unwrap();
        for (i, name) in ["xmin", "ymin", "xmax", "ymax"].iter().enumerate() {
            let col = bbox.column_by_name(name).unwrap().as_any().downcast_ref::<arrow_array::Float64Array>().unwrap();
            for v in col.iter().flatten() {
                if i < 2 { self.bbox[i] = self.bbox[i].min(v) } else { self.bbox[i] = self.bbox[i].max(v) }
            }
        }
        self.rows += rb.num_rows();
        self.row_groups += 1;
        self.writer.write(&rb)?;
        self.writer.flush()?;
        Ok(())
    }

    /// Append the `geo` metadata and close the file.
    pub fn finish(mut self, geometry_types: &[String]) -> Result<PartitionStats> {
        let bbox = if self.rows == 0 { serde_json::Value::Null } else { serde_json::json!(self.bbox) };
        let geo = serde_json::json!({
            "version": "1.1.0",
            "primary_column": GEOMETRY_COLUMN,
            "columns": {
                GEOMETRY_COLUMN: {
                    "encoding": "WKB",
                    "geometry_types": geometry_types,
                    "bbox": bbox,
                    "covering": {"bbox": {
                        "xmin": [BBOX_COLUMN, "xmin"], "ymin": [BBOX_COLUMN, "ymin"],
                        "xmax": [BBOX_COLUMN, "xmax"], "ymax": [BBOX_COLUMN, "ymax"]}}
                }
            }
        });
        self.writer.append_key_value_metadata(KeyValue::new("geo".into(), geo.to_string()));
        self.writer.close()?;
        let _ = &self.path;
        Ok(PartitionStats { rows: self.rows, row_groups: self.row_groups })
    }
}
```

Note the `crs` key is deliberately absent from the column metadata (GeoPandas reads an explicit `null` as "no CRS" and an absent key as CRS84; see the spike README).

- [ ] **Step 3: Run tests**

Run: `cargo test write::parquet`
Expected: 1 passed. If `rows == 3` but `row_groups == 1`, `ArrowWriter` merged the batches: `flush()` after each `write` forces a row group boundary; confirm it is called.

- [ ] **Step 4: Commit**

```bash
git add src/write/parquet.rs
git commit -m "Write partitions as native-typed GeoParquet with legacy geo metadata"
```

---

## Task 12: Pass two — write the dataset atomically

**Files:**
- Modify: `src/write/dataset.rs`

- [ ] **Step 1: Write the failing test**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::build_index;
    use crate::index::partition::parse_keys;
    use std::path::Path;

    fn fixture() -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/snapshot/locations.ndjson")
    }

    #[test]
    fn writes_one_file_per_partition_and_swaps_atomically() {
        let out = tempfile::tempdir().unwrap();
        let mut index = build_index(&fixture(), None).unwrap();
        let keys = parse_keys("country,geom_type").unwrap();
        let written = write_dataset(&fixture(), &mut index, out.path(), &keys, 3).unwrap();
        let rel: Vec<String> = written.iter().map(|w| w.path.strip_prefix(out.path()).unwrap().to_string_lossy().replace('\\', "/")).collect();
        assert!(rel.contains(&"locations/country=NG/geom_type=polygon/part-0.parquet".to_string()), "{rel:?}");
        assert!(rel.contains(&"locations/country=NG/geom_type=point/part-0.parquet".to_string()), "{rel:?}");
        assert!(rel.contains(&"locations/country=unknown/geom_type=point/part-0.parquet".to_string()), "{rel:?}");
        assert_eq!(written.iter().map(|w| w.rows).sum::<usize>(), 8);
        assert_eq!(index.report.count("point_outside_parent"), 1, "stray is outside nassarawa");
        assert!(!out.path().join(".locations.tmp").exists());

        // Second run replaces the dataset and leaves no backup behind.
        let mut index = build_index(&fixture(), None).unwrap();
        write_dataset(&fixture(), &mut index, out.path(), &keys, 3).unwrap();
        assert!(!out.path().join(".locations.bak").exists());
        assert!(out.path().join("locations/country=NG/geom_type=polygon/part-0.parquet").exists());
    }

    #[test]
    fn empty_index_writes_an_empty_dataset_dir() {
        let out = tempfile::tempdir().unwrap();
        let snapshot = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(snapshot.path(), b"{\"resourceType\":\"Location\",\"id\":\"x\"}\n").unwrap();
        let mut index = build_index(snapshot.path(), None).unwrap();
        let keys = parse_keys("country,geom_type").unwrap();
        let written = write_dataset(snapshot.path(), &mut index, out.path(), &keys, 3).unwrap();
        assert!(written.is_empty());
        assert!(out.path().join("locations").is_dir());
    }
}
```

- [ ] **Step 2: Implement write_dataset**

```rust
//! Pass two: walk the sorted index, seek each line, build rows, write one
//! file per partition into a staging directory, then swap it in atomically.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use geo::Intersects;

use crate::error::{KilnError, Result};
use crate::fhir::ndjson::LineAccess;
use crate::fhir::Location;
use crate::geometry::{self, GeomKind};
use crate::index::partition::{segment, Claims, PartitionKey};
use crate::index::{Index, IndexRecord, ADMIN_COLUMNS};
use crate::report::Report;
use crate::write::parquet::PartitionWriter;
use crate::write::schema::{OutputRow, RowBatch};

pub const DATASET_DIR: &str = "locations";
const STAGING_DIR: &str = ".locations.tmp";
const BACKUP_DIR: &str = ".locations.bak";
const MIN_PARTITION_ROWS: usize = 100;
const PARENT_CACHE_MAX: usize = 256;

pub struct WrittenPartition {
    pub path: PathBuf,
    pub partition: String,
    pub rows: usize,
    pub row_groups: usize,
}

/// Bounded cache of parent polygons for the point-outside-parent check.
struct ParentCache {
    polygons: HashMap<String, Option<geo::Geometry<f64>>>,
}

impl ParentCache {
    fn get(&mut self, id: &str, by_id: &HashMap<&str, &IndexRecord>, lines: &mut LineAccess) -> Result<Option<geo::Geometry<f64>>> {
        if let Some(cached) = self.polygons.get(id) {
            return Ok(cached.clone());
        }
        if self.polygons.len() >= PARENT_CACHE_MAX {
            self.polygons.clear();
        }
        let geom = match by_id.get(id) {
            Some(rec) if rec.geometry.map(|g| g.kind) == Some(GeomKind::Polygon) => {
                let text = lines.read_at(rec.offset, rec.len)?;
                let value: serde_json::Value = serde_json::from_str(&text)?;
                let mut silent = Report::default();
                Location::parse(&value, &mut silent).and_then(|l| geometry::build(&l, &mut silent)).map(|g| g.geometry)
            }
            _ => None,
        };
        self.polygons.insert(id.to_string(), geom.clone());
        Ok(geom)
    }
}

fn nearest_polygon_ancestor(ancestors: &[String], by_id: &HashMap<&str, &IndexRecord>) -> Option<String> {
    ancestors
        .iter()
        .rev()
        .find(|a| by_id.get(a.as_str()).and_then(|r| r.geometry).map(|g| g.kind) == Some(GeomKind::Polygon))
        .cloned()
}

fn recover_incomplete_swap(dataset: &Path, backup: &Path, staging: &Path) -> Result<()> {
    if backup.exists() && !dataset.exists() {
        std::fs::rename(backup, dataset).map_err(|e| KilnError::io(backup, e))?;
    } else if backup.exists() {
        std::fs::remove_dir_all(backup).map_err(|e| KilnError::io(backup, e))?;
    }
    if staging.exists() {
        std::fs::remove_dir_all(staging).map_err(|e| KilnError::io(staging, e))?;
    }
    Ok(())
}

fn swap_in(dataset: &Path, backup: &Path, staging: &Path) -> Result<()> {
    if dataset.exists() {
        std::fs::rename(dataset, backup).map_err(|e| KilnError::io(dataset, e))?;
    }
    std::fs::rename(staging, dataset).map_err(|e| KilnError::io(staging, e))?;
    if backup.exists() {
        std::fs::remove_dir_all(backup).map_err(|e| KilnError::io(backup, e))?;
    }
    Ok(())
}

pub fn write_dataset(
    ndjson: &Path,
    index: &mut Index,
    out_dir: &Path,
    keys: &[PartitionKey],
    row_group_size: usize,
) -> Result<Vec<WrittenPartition>> {
    std::fs::create_dir_all(out_dir).map_err(|e| KilnError::io(out_dir, e))?;
    let dataset = out_dir.join(DATASET_DIR);
    let backup = out_dir.join(BACKUP_DIR);
    let staging = out_dir.join(STAGING_DIR);
    if dataset.is_symlink() {
        return Err(KilnError::Usage(format!("{} is a symlink; kiln will not replace it", dataset.display())));
    }
    recover_incomplete_swap(&dataset, &backup, &staging)?;
    std::fs::create_dir(&staging).map_err(|e| KilnError::io(&staging, e))?;

    let result = write_partitions(ndjson, index, &staging, keys, row_group_size);
    let written = match result {
        Ok(w) => w,
        Err(e) => {
            let _ = std::fs::remove_dir_all(&staging);
            return Err(e);
        }
    };
    swap_in(&dataset, &backup, &staging)?;
    Ok(written
        .into_iter()
        .map(|w| WrittenPartition { path: dataset.join(w.path.strip_prefix(&staging).unwrap()), ..w })
        .collect())
}

fn write_partitions(
    ndjson: &Path,
    index: &mut Index,
    staging: &Path,
    keys: &[PartitionKey],
    row_group_size: usize,
) -> Result<Vec<WrittenPartition>> {
    // Group consecutive records by their partition values. The index is
    // sorted by country, geom_type, Hilbert; grouping by any of the allowed
    // keys keeps each group spatially clustered.
    let mut groups: Vec<(Vec<String>, Vec<usize>)> = Vec::new();
    for (i, rec) in index.records.iter().enumerate() {
        let values: Vec<String> = keys.iter().map(|k| k.value(rec)).collect();
        match groups.iter_mut().find(|(v, _)| *v == values) {
            Some((_, idxs)) => idxs.push(i),
            None => groups.push((values, vec![i])),
        }
    }

    let by_id: HashMap<&str, &IndexRecord> = index.records.iter().map(|r| (r.id.as_str(), r)).collect();
    let mut lines = LineAccess::open(ndjson)?;
    let mut parent_cache = ParentCache { polygons: HashMap::new() };
    let mut claims: HashMap<(Vec<String>, &str), Claims> = HashMap::new();
    let mut report = Report::default();
    let mut written = Vec::new();

    for (values, idxs) in &groups {
        let mut segments: Vec<String> = Vec::new();
        for (key, value) in keys.iter().zip(values) {
            let ctx = (segments.clone(), key.name());
            let seg = segment(key.name(), value, claims.entry(ctx).or_default(), &mut report);
            segments.push(format!("{}={seg}", key.name()));
        }
        let partition = segments.join("/");
        if idxs.len() < MIN_PARTITION_ROWS {
            report.add("small_partition", &partition, &format!("{} rows is below MIN_PARTITION_ROWS={MIN_PARTITION_ROWS}", idxs.len()));
        }
        let path = segments.iter().fold(staging.to_path_buf(), |p, s| p.join(s)).join("part-0.parquet");
        let mut writer = PartitionWriter::create(&path, row_group_size)?;
        let mut batch = RowBatch::new();
        let mut geometry_types: Vec<String> = Vec::new();

        for &i in idxs {
            let rec = &index.records[i];
            let text = lines.read_at(rec.offset, rec.len)?;
            let value: serde_json::Value = serde_json::from_str(&text)?;
            // Parsing issues were reported in pass one; do not report them twice.
            let mut silent = Report::default();
            let Some(loc) = Location::parse(&value, &mut silent) else { continue };
            let Some(geom) = geometry::build(&loc, &mut silent) else { continue };
            let info = &index.hierarchy[&rec.id];

            if let Some(parent_id) = nearest_polygon_ancestor(&info.ancestor_ids, &by_id) {
                if let Some(parent) = parent_cache.get(&parent_id, &by_id, &mut lines)? {
                    let pt = geo::Point::new(geom.lon, geom.lat);
                    if !parent.intersects(&pt) {
                        report.add("point_outside_parent", &rec.id, &format!("({}, {}) falls outside its nearest admin ancestor", geom.lon, geom.lat));
                    }
                }
            }

            let type_name = match &geom.geometry {
                geo::Geometry::Point(_) => "Point",
                geo::Geometry::Polygon(_) => "Polygon",
                geo::Geometry::MultiPolygon(_) => "MultiPolygon",
                _ => "Geometry",
            };
            if !geometry_types.iter().any(|t| t == type_name) {
                geometry_types.push(type_name.to_string());
            }

            let mut admin_names: [Option<String>; ADMIN_COLUMNS] = Default::default();
            let mut admin_codes: [Option<String>; ADMIN_COLUMNS] = Default::default();
            for k in 0..ADMIN_COLUMNS {
                admin_names[k] = info.admin_names[k].clone();
                admin_codes[k] = info.admin_codes[k].clone();
            }
            let row = OutputRow {
                id: loc.id.clone(),
                version_id: loc.version_id.clone(),
                last_updated: loc.last_updated.clone(),
                name: loc.name.clone(),
                alias: loc.alias.clone(),
                status: loc.status.clone(),
                description: loc.description.clone(),
                type_code: loc.type_code.clone(),
                physical_type: loc.physical_type.clone(),
                part_of: loc.part_of.clone(),
                managing_organization: loc.managing_organization.clone(),
                identifier: loc.identifier.iter().map(|i| (i.system.clone().unwrap_or_default(), i.value.clone().unwrap_or_default())).collect(),
                position: loc.position,
                pcode: loc.pcode.clone(),
                gers_id: loc.gers_id.clone(),
                settlement_type: loc.settlement_type.clone(),
                delivery_strategy: loc.delivery_strategy.clone(),
                facility_level: loc.facility_level.clone(),
                ownership: loc.ownership.clone(),
                depth: info.depth,
                admin_level: info.admin_level,
                tier: rec.tier.clone(),
                path: info.path.clone(),
                ancestor_ids: info.ancestor_ids.clone(),
                admin_names,
                admin_codes,
                overlays_admin_unit_ids: loc.overlays_admin_unit_ids.clone(),
                country: rec.country.clone(),
                geom_type: geom.kind.as_str().to_string(),
                lon: geom.lon,
                lat: geom.lat,
                wkb: geom.wkb(),
                bbox: geom.bbox,
                fhir_json: loc.fhir_json.clone(),
            };
            batch.push(&row);
            if batch.rows >= row_group_size {
                writer.write(&mut batch)?;
            }
        }
        writer.write(&mut batch)?;
        let stats = writer.finish(&geometry_types)?;
        written.push(WrittenPartition { path, partition, rows: stats.rows, row_groups: stats.row_groups });
    }

    // Merge pass-two diagnostics into the index's report.
    for issue in report.issues {
        index.report.add(&issue.kind, &issue.location_id, &issue.detail);
    }
    Ok(written)
}
```

- [ ] **Step 3: Run tests**

Run: `cargo test write::dataset`
Expected: 2 passed. The `point_outside_parent` expectation: `stray` at (50, 50) has parent `nassarawa` whose polygon is the (3,6)-(4,7) box, so it is outside; `clinic` at (3.25, 6.25) has nearest polygon ancestor `gama` (3.1..3.5, 6.1..6.5), inside; `orphan` has no ancestors; `dup` is a polygon whose representative point is inside itself, and its nearest polygon ancestor `kano` (3..6, 6..9) contains (4.5, 7.5). If the count is 2, print the report issues and check which extra row fired.

- [ ] **Step 4: Commit**

```bash
git add src/write/dataset.rs
git commit -m "Pass two: stream rows into per-partition writers and swap in atomically"
```

---

## Task 13: transform command

**Files:**
- Create: `src/transform.rs`
- Modify: `src/main.rs`

- [ ] **Step 1: Write the integration test tests/transform.rs**

```rust
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
    assert!(stdout.contains("Wrote 8 rows across 3 partitions"), "{stdout}");
    assert!(stdout.contains("cycle: 2"), "{stdout}");

    let report: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(out.path().join("_report.json")).unwrap()).unwrap();
    assert_eq!(report["counts"]["orphan"], 1);
    assert_eq!(report["counts"]["point_outside_parent"], 1);
    assert_eq!(report["counts"]["duplicate_pcode"], 1);
    assert!(out.path().join("locations/country=NG/geom_type=polygon/part-0.parquet").exists());
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
fn duckdb_reads_the_output_when_available() {
    if Command::new("duckdb").arg("--version").output().is_err() {
        eprintln!("duckdb not on PATH; skipping");
        return;
    }
    let out = tempfile::tempdir().unwrap();
    Command::cargo_bin("kiln").unwrap()
        .args(["transform", "--snapshot"]).arg(fixture_snapshot()).arg("--out").arg(out.path())
        .assert().success();
    let glob = format!("{}/locations/**/*.parquet", out.path().display());
    let sql = format!(
        "SELECT id, admin1_name, admin2_name, geom_type FROM '{glob}' WHERE id='clinic';\n\
         SELECT name, logical_type FROM parquet_schema('{glob}') WHERE name='geometry';\n\
         INSTALL spatial; LOAD spatial; SELECT count(*) FROM '{glob}' WHERE ST_Intersects(geometry, ST_MakeEnvelope(3,6,4,7));"
    );
    let output = Command::new("duckdb").args(["-csv", "-c", &sql]).output().unwrap();
    let text = String::from_utf8_lossy(&output.stdout).to_string();
    assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    assert!(text.contains("clinic,Kano,Nassarawa,point"), "{text}");
    assert!(text.contains("GeometryType"), "{text}");
    assert!(text.contains("\n5\n") || text.ends_with("5\n"), "{text}");
}
```

Add `serde_json` is already a dependency; `tempfile` and `assert_cmd` are dev-dependencies from Task 0.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cargo test --test transform 2>&1 | tail -5`
Expected: `transform_writes_dataset_and_report` fails with "not implemented yet".

- [ ] **Step 3: Implement src/transform.rs**

```rust
//! `kiln transform`: pass one, pass two, report.

use std::path::Path;

use crate::cli::TransformArgs;
use crate::error::{KilnError, Result};
use crate::index::build_index;
use crate::index::partition::parse_keys;
use crate::write::dataset::write_dataset;

pub const SNAPSHOT_FILE: &str = "locations.ndjson";

pub fn run_transform(args: &TransformArgs) -> Result<()> {
    let ndjson = args.snapshot.join(SNAPSHOT_FILE);
    if !ndjson.is_file() {
        return Err(KilnError::Usage(format!("snapshot file not found: {}", ndjson.display())));
    }
    let keys = parse_keys(&args.partition_by)?;
    if args.row_group_size == 0 {
        return Err(KilnError::Usage("--row-group-size must be at least 1".into()));
    }

    let mut index = build_index(&ndjson, args.country.as_deref())?;
    eprintln!("indexed {} resources, {} with geometry and hierarchy", index.read, index.records.len());

    // A stale report must never describe a dataset it does not match:
    // remove it before writing, rewrite it only on success.
    let report_path = args.out.join("_report.json");
    if report_path.exists() {
        std::fs::remove_file(&report_path).map_err(|e| KilnError::io(&report_path, e))?;
    }

    let written = write_dataset(&ndjson, &mut index, &args.out, &keys, args.row_group_size)?;

    write_report(&report_path, &index.report)?;
    let rows: usize = written.iter().map(|w| w.rows).sum();
    println!("Wrote {rows} rows across {} partitions to {}", written.len(), args.out.display());
    println!("{}", index.report.summary());
    Ok(())
}

pub fn write_report(path: &Path, report: &crate::report::Report) -> Result<()> {
    let text = serde_json::to_string_pretty(&report.to_json())?;
    std::fs::write(path, text).map_err(|e| KilnError::io(path, e))
}
```

Update `src/main.rs`:

```rust
mod cli;
mod error;
mod fhir;
mod geometry;
mod index;
mod inspect;
mod report;
mod transform;
mod write;

use clap::Parser;

fn main() {
    let cli = cli::Cli::parse();
    let result: error::Result<()> = match cli.command {
        cli::Command::Transform(args) => transform::run_transform(&args),
        cli::Command::Inspect(args) => inspect::run_inspect(&args),
    };
    if let Err(err) = result {
        eprintln!("kiln: {err}");
        std::process::exit(err.exit_code());
    }
}
```

`inspect` is Task 14; until then keep the `Inspect` arm returning the "not implemented" usage error and omit `mod inspect;`.

- [ ] **Step 4: Run the tests**

Run: `cargo test --test transform`
Expected: 3 passed (the DuckDB test runs since DuckDB 1.5.5 is installed on this machine).

- [ ] **Step 5: Run the whole suite and clippy**

Run: `cargo test && cargo clippy --all-targets -- -D warnings`
Expected: all pass, no warnings. Fix any clippy findings inline.

- [ ] **Step 6: Commit**

```bash
git add src/transform.rs src/main.rs tests/transform.rs
git commit -m "Add kiln transform: snapshot to partitioned GeoParquet with report"
```

---

## Task 14: inspect command

**Files:**
- Create: `src/inspect.rs`
- Modify: `src/main.rs`, `tests/transform.rs`

- [ ] **Step 1: Write the failing test (append to tests/transform.rs)**

```rust
#[test]
fn inspect_summarises_a_written_dataset() {
    let out = tempfile::tempdir().unwrap();
    Command::cargo_bin("kiln").unwrap()
        .args(["transform", "--snapshot"]).arg(fixture_snapshot()).arg("--out").arg(out.path())
        .args(["--row-group-size", "2"])
        .assert().success();
    let assert = Command::cargo_bin("kiln").unwrap().args(["inspect", "--out"]).arg(out.path()).assert().success();
    let stdout = String::from_utf8(assert.get_output().stdout.clone()).unwrap();
    assert!(stdout.contains("8 rows across 3 partitions"), "{stdout}");
    assert!(stdout.contains("locations/country=NG/geom_type=polygon/part-0.parquet"), "{stdout}");
    assert!(stdout.contains("geo=1.1.0 covering=true"), "{stdout}");
    assert!(stdout.contains("types=Polygon") || stdout.contains("types=MultiPolygon"), "{stdout}");
}
```

- [ ] **Step 2: Implement src/inspect.rs**

```rust
//! `kiln inspect`: describe every parquet file under OUT/locations.

use std::path::{Path, PathBuf};

use parquet::file::reader::{FileReader, SerializedFileReader};
use serde::Serialize;

use crate::cli::InspectArgs;
use crate::error::{KilnError, Result};

#[derive(Debug, Serialize)]
pub struct PartitionSummary {
    pub path: String,
    pub rows: i64,
    pub row_groups: usize,
    pub min_row_group_rows: i64,
    pub avg_row_group_rows: i64,
    pub max_row_group_rows: i64,
    pub size_bytes: u64,
    pub geometry_types: Vec<String>,
    pub geo_version: Option<String>,
    pub has_covering: bool,
}

#[derive(Debug, Serialize)]
pub struct Summary {
    pub partitions: Vec<PartitionSummary>,
    pub total_rows: i64,
    pub total_size_bytes: u64,
}

fn parquet_files(dir: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
    if !dir.exists() {
        return Ok(());
    }
    let mut entries: Vec<PathBuf> = std::fs::read_dir(dir)
        .map_err(|e| KilnError::io(dir, e))?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .collect();
    entries.sort();
    for path in entries {
        if path.is_dir() {
            parquet_files(&path, out)?;
        } else if path.extension().map_or(false, |x| x == "parquet") {
            out.push(path);
        }
    }
    Ok(())
}

pub fn summarize(out_dir: &Path) -> Result<Summary> {
    let mut files = Vec::new();
    parquet_files(out_dir, &mut files)?;
    let mut partitions = Vec::new();
    let mut total_rows = 0i64;
    let mut total_size_bytes = 0u64;
    for path in files {
        let file = std::fs::File::open(&path).map_err(|e| KilnError::io(&path, e))?;
        let size_bytes = file.metadata().map_err(|e| KilnError::io(&path, e))?.len();
        let reader = SerializedFileReader::new(file)?;
        let meta = reader.metadata();
        let rows = meta.file_metadata().num_rows();
        let groups: Vec<i64> = meta.row_groups().iter().map(|rg| rg.num_rows()).collect();
        let geo: serde_json::Value = meta
            .file_metadata()
            .key_value_metadata()
            .and_then(|kvs| kvs.iter().find(|kv| kv.key == "geo"))
            .and_then(|kv| kv.value.as_deref())
            .and_then(|v| serde_json::from_str(v).ok())
            .unwrap_or(serde_json::Value::Null);
        let primary = geo["primary_column"].as_str().unwrap_or("geometry").to_string();
        let column = &geo["columns"][&primary];
        total_rows += rows;
        total_size_bytes += size_bytes;
        partitions.push(PartitionSummary {
            path: path.strip_prefix(out_dir).unwrap_or(&path).to_string_lossy().replace('\\', "/"),
            rows,
            row_groups: groups.len(),
            min_row_group_rows: groups.iter().copied().min().unwrap_or(0),
            avg_row_group_rows: if groups.is_empty() { 0 } else { (rows as f64 / groups.len() as f64).round() as i64 },
            max_row_group_rows: groups.iter().copied().max().unwrap_or(0),
            size_bytes,
            geometry_types: column["geometry_types"].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()).unwrap_or_default(),
            geo_version: geo["version"].as_str().map(str::to_string),
            has_covering: column.get("covering").is_some(),
        });
    }
    Ok(Summary { partitions, total_rows, total_size_bytes })
}

fn human_size(bytes: u64) -> String {
    let b = bytes as f64;
    if b >= 1e9 { format!("{:.1}GB", b / 1e9) } else if b >= 1e6 { format!("{:.1}MB", b / 1e6) } else if b >= 1e3 { format!("{:.1}KB", b / 1e3) } else { format!("{bytes}B") }
}

pub fn format_summary(s: &Summary) -> String {
    if s.partitions.is_empty() {
        return "No parquet files found.".to_string();
    }
    let mut lines = vec![
        format!("{} rows across {} partitions ({})", s.total_rows, s.partitions.len(), human_size(s.total_size_bytes)),
        String::new(),
    ];
    for p in &s.partitions {
        lines.push(format!(
            "  {}\n    rows={} row_groups={} (min={} avg={} max={}) size={}\n    geo={} covering={} types={}",
            p.path, p.rows, p.row_groups, p.min_row_group_rows, p.avg_row_group_rows, p.max_row_group_rows,
            human_size(p.size_bytes), p.geo_version.as_deref().unwrap_or("none"), p.has_covering, p.geometry_types.join(",")
        ));
    }
    lines.join("\n")
}

pub fn run_inspect(args: &InspectArgs) -> Result<()> {
    let summary = summarize(&args.out)?;
    println!("{}", format_summary(&summary));
    Ok(())
}
```

Wire it in `src/main.rs` (add `mod inspect;` and the `Inspect` arm as shown in Task 13).

- [ ] **Step 3: Run the tests**

Run: `cargo test --test transform inspect`
Expected: 1 passed.

- [ ] **Step 4: Commit**

```bash
git add src/inspect.rs src/main.rs tests/transform.rs
git commit -m "Add kiln inspect: summarise partitions, row groups and geo metadata"
```

---

## Task 15: Memory check and release build

**Files:**
- Create: `tests/generate_snapshot.rs` (an ignored test that generates a large snapshot), `.github/workflows/ci.yml`

- [ ] **Step 1: Add a generator for a large synthetic snapshot**

`tests/generate_snapshot.rs`:

```rust
//! Generates a synthetic snapshot for memory and speed checks. Run with:
//!   cargo test --release --test generate_snapshot -- --ignored --nocapture
//! It writes target/bench/snapshot/locations.ndjson: 1 country, 20 states,
//! 400 districts (each a 200-vertex polygon), and N facilities (points).

use std::io::Write;
use std::path::Path;

fn polygon(cx: f64, cy: f64, r: f64, n: usize) -> String {
    let ring: Vec<String> = (0..=n)
        .map(|i| {
            let t = i as f64 / n as f64 * std::f64::consts::TAU;
            format!("[{:.6},{:.6}]", cx + r * t.cos(), cy + r * t.sin())
        })
        .collect();
    format!(r#"{{"type":"Polygon","coordinates":[[{}]]}}"#, ring.join(","))
}

fn location(id: &str, name: &str, ty: &str, parent: Option<&str>, pcode: Option<&str>, position: Option<(f64, f64)>, boundary: Option<&str>) -> String {
    let mut r = serde_json::json!({"resourceType":"Location","id":id,"name":name,"status":"active",
        "meta":{"versionId":"1","lastUpdated":"2026-01-01T00:00:00Z"},
        "type":[{"coding":[{"code":ty}]}]});
    if let Some(p) = parent { r["partOf"] = serde_json::json!({"reference": format!("Location/{p}")}); }
    if let Some(c) = pcode { r["identifier"] = serde_json::json!([{"system":"https://icr.healthcampaigns.org/identifiers/pcode","value":c}]); }
    if let Some((x, y)) = position { r["position"] = serde_json::json!({"longitude":x,"latitude":y}); }
    if let Some(b) = boundary {
        let data = base64::Engine::encode(&base64::engine::general_purpose::STANDARD, b);
        r["extension"] = serde_json::json!([{"url":"https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson",
            "valueAttachment":{"contentType":"application/geo+json","data":data}}]);
    }
    r.to_string()
}

#[test]
#[ignore]
fn generate() {
    let facilities: usize = std::env::var("KILN_BENCH_FACILITIES").ok().and_then(|v| v.parse().ok()).unwrap_or(200_000);
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("target/bench/snapshot");
    std::fs::create_dir_all(&dir).unwrap();
    let mut f = std::io::BufWriter::new(std::fs::File::create(dir.join("locations.ndjson")).unwrap());
    writeln!(f, "{}", location("ng", "Nigeria", "admin-unit", None, Some("NG"), None, Some(&polygon(8.0, 9.0, 6.0, 400)))).unwrap();
    for s in 0..20 {
        let sid = format!("s{s}");
        let (sx, sy) = (3.0 + (s % 5) as f64 * 2.5, 5.0 + (s / 5) as f64 * 2.5);
        writeln!(f, "{}", location(&sid, &format!("State {s}"), "admin-unit", Some("ng"), Some(&format!("NG{s:03}")), None, Some(&polygon(sx, sy, 1.2, 300)))).unwrap();
        for d in 0..20 {
            let did = format!("{sid}d{d}");
            let (dx, dy) = (sx - 1.0 + (d % 5) as f64 * 0.5, sy - 1.0 + (d / 5) as f64 * 0.5);
            writeln!(f, "{}", location(&did, &format!("District {s}-{d}"), "admin-unit", Some(&sid), Some(&format!("NG{s:03}{d:03}")), None, Some(&polygon(dx, dy, 0.24, 200)))).unwrap();
        }
    }
    for i in 0..facilities {
        let s = i % 20;
        let d = (i / 20) % 20;
        let (sx, sy) = (3.0 + (s % 5) as f64 * 2.5, 5.0 + (s / 5) as f64 * 2.5);
        let (dx, dy) = (sx - 1.0 + (d % 5) as f64 * 0.5, sy - 1.0 + (d / 5) as f64 * 0.5);
        let jitter = (i as f64 * 0.618).fract() * 0.2 - 0.1;
        writeln!(f, "{}", location(&format!("f{i}"), &format!("Facility {i}"), "facility", Some(&format!("s{s}d{d}")), None, Some((dx + jitter, dy - jitter)), None)).unwrap();
    }
    eprintln!("wrote {}", dir.join("locations.ndjson").display());
}
```

Add `base64` to `[dev-dependencies]` in `Cargo.toml` if it is only listed under `[dependencies]` (it is listed there already, which makes it available to integration tests too; no change needed).

- [ ] **Step 2: Measure**

Run:
```bash
cargo test --release --test generate_snapshot -- --ignored --nocapture
cargo build --release
/usr/bin/time -l ./target/release/kiln transform --snapshot target/bench/snapshot --out target/bench/out 2>&1 | grep -E "Wrote|maximum resident|real|elapsed"
```
Expected: completes; note wall time and maximum resident set size. Record both numbers in the commit message. The README target is that peak memory stays in the low hundreds of megabytes for two hundred thousand rows; if it is over one gigabyte, profile before continuing: the likely culprits are `fhir_json` strings being kept in the index (they must not be) or `groups` holding cloned partition values per row.

- [ ] **Step 3: Add CI**

`.github/workflows/ci.yml`:

```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
        with:
          components: clippy, rustfmt
      - run: cargo fmt --check
      - run: cargo clippy --all-targets -- -D warnings
      - run: cargo test
  release-build:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        include:
          - os: ubuntu-latest
            target: x86_64-unknown-linux-musl
          - os: ubuntu-latest
            target: aarch64-unknown-linux-musl
          - os: macos-latest
            target: aarch64-apple-darwin
          - os: macos-latest
            target: x86_64-apple-darwin
          - os: windows-latest
            target: x86_64-pc-windows-msvc
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
        with:
          targets: ${{ matrix.target }}
      - if: contains(matrix.target, 'musl')
        run: sudo apt-get update && sudo apt-get install -y musl-tools gcc-aarch64-linux-gnu
      - run: cargo build --release --target ${{ matrix.target }}
      - uses: actions/upload-artifact@v4
        with:
          name: kiln-${{ matrix.target }}
          path: target/${{ matrix.target }}/release/kiln*
```

The aarch64 musl cross build may need a linker set via `CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER=aarch64-linux-gnu-gcc` in the build step's `env`; add it if that job fails on the linker.

- [ ] **Step 4: Format and commit**

```bash
cargo fmt
git add -A
git commit -m "Add synthetic snapshot generator, memory check, and CI with release builds"
```

---

## Follow-up (after Task 15): parallel passes

Not part of this plan's acceptance. Once Task 15 has recorded the single-threaded baseline, add a task that introduces `rayon` with a `--threads N` flag (default: available cores): pass one parses chunks of lines in parallel and merges per-thread reports; pass two encodes row-group-sized chunks of the sorted index in parallel (each worker with its own file handle for the random reads) and hands finished batches to one writer per partition in order. Memory bound rises by one batch per worker. Re-measure against the Task 15 numbers.

## Task 16: README touch-ups

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the Status note and Building section**

In `README.md`, change the Status callout to say that `transform` and `inspect` are implemented in Rust, `extract`, `diff` and `load` are in progress, and the Python package under `python/` still provides `extract`, `bake`, `bake-points` and `load` meanwhile. In "Building", add the memory measurement from Task 15 as one line: the synthetic snapshot size, wall time, and peak RSS on this machine.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "README: mark transform and inspect as implemented, record memory measurement"
```

---

## Self-review

**Spec coverage.** Snapshot layout: read only, `locations.ndjson` (Task 13); `state.json` and `boundaries/` are written by extract, plan 2. Two-pass transform: Tasks 9 and 12. Column schema by FHIR path, four groups, `fhir_json`: Tasks 2 and 10. Native geometry plus legacy metadata plus bbox: Task 11. Partition by country and geom_type, Hilbert order: Tasks 7, 8, 9. Atomic swap: Task 12. Report codes: Tasks 1, 5, 6, 9, 12. `inspect`: Task 14. Static binary and CI: Task 15. Not in this plan, by design: `extract`, `run`, `diff`, `load`.

**Placeholder scan.** None. Each step has the code it needs.

**Type consistency.** `IndexRecord` fields `country`, `tier`, `hilbert` are filled in Task 9 and read in Tasks 8 and 12. `GeometrySummary { kind, bbox }` and `GeometryResult { kind, geometry, bbox, lon, lat }` are used consistently. `Report::add(&str, &str, &str)` everywhere. `HierarchyInfo.admin_names` is a fixed array of `ADMIN_COLUMNS`, copied into `OutputRow` in Task 12. `PartitionWriter::finish(&[String])` matches its call. `write_dataset(ndjson, &mut Index, out_dir, &[PartitionKey], usize)` matches Tasks 12 and 13.

**Known judgement calls for the implementer.** `MAX_DEPTH` off-by-one (Task 6 step 4) must be reconciled with the Python tests. The `size_of::<GeometrySummary>()` assertion and the Hilbert corner assertion are guards, not requirements; adjust them to the observed value once, with a comment.
