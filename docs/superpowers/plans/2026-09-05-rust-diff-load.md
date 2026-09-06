# kiln diff and load — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `kiln diff` turns an edited GeoJSON or GeoParquet file into FHIR NDJSON holding only the Locations that changed, complete and version-tagged; `kiln load` posts them as parents-first transaction bundles with `If-Match`, a capability preflight, retry, and a precise conflict list.

**Architecture:** diff indexes the snapshot by id and byte offset, streams the input one feature or record batch at a time into a format-neutral `InputRow`, rebuilds each named resource from its raw snapshot line by applying only the writable columns and the geometry rule, and emits it when the canonical JSON differs. load validates the NDJSON, checks `updateCreate` for creates, orders by `partOf` depth, chunks into bundles with `ifMatch`, posts through the existing retrying client, and on 412/409 reads back each entry's server version to name the conflicts.

**Tech Stack:** Existing crate (Rust stable, clap 4, serde_json with `preserve_order`, geojson 1, geo 0.33, wkb 0.9, parquet 59 with `arrow`, reqwest blocking, base64) plus `uuid` 1 (`v4`) and `geo-traits` 0.3 (already in the lock as wkb's dependency; needed directly for WKB → geo conversion). Dev: `tempfile`, `assert_cmd`, `predicates`, `httptest`.

Spec: `docs/superpowers/specs/2026-09-05-diff-load-design.md`. Where this plan and the spec disagree, the spec wins; fix the plan.

---

## Existing APIs you will use

- `src/error.rs`: `KilnError::{Usage(String), Io{path,source}, Json, ParquetAt{path,source}, Environment(String)}`, `KilnError::io(path, e)`, `KilnError::parquet_at(path, e)`, `exit_code()` (Usage → 2, else 1), `type Result<T>`.
- `src/report.rs`: `Report::default()`, `add(kind, location_id, detail)`, `counts()`, `count(kind)` (test only), `to_json()`, `summary()`.
- `src/transform.rs`: `SNAPSHOT_FILE = "locations.ndjson"`, `write_report(&Path, &Report)`.
- `src/fhir/ndjson.rs`: `NdjsonReader::open(&Path)` iterating `Result<Line { number, offset, len, text }>`; `LineAccess::open(&Path)` with `read_at(offset, len) -> Result<String>`.
- `src/fhir/location.rs`: `Location::parse(&Value, &mut Report) -> Option<Location>` with fields `id, position: Option<(f64,f64)>, boundary: Option<Boundary>`; `Boundary::{Inline(Vec<u8>), Url(String)}`; `Identifier { system: Option<String>, value: Option<String> }`; constants `BOUNDARY_EXTENSION_URL`, `BOUNDARY_EXTENSION_URLS`, `GEOJSON_CONTENT_TYPE`, `PCODE_SYSTEM`, `GERS_SYSTEM`, `SETTLEMENT_TYPE_EXTENSION_URL`, `DELIVERY_STRATEGY_EXTENSION_URL`, `FACILITY_TYPE_SYSTEM`, `OWNERSHIP_SYSTEM`.
- `src/geometry/geojson.rs`: `parse_boundary(bytes, id, &mut Report) -> Option<geo::Geometry<f64>>`, `kind_name(&Geometry) -> &str`. Task 3 adds `convert`.
- `src/geometry/wkb.rs`: `to_wkb(&Geometry) -> Vec<u8>`.
- `src/geometry/validity.rs`: `check(&Geometry) -> Result<(), String>`.
- `src/extract/client.rs`: `FhirClient::new(token, retries, timeout) -> Result<FhirClient>`, `get(&str) -> Result<Fetched{body}, FetchError>`, `FetchError::{Status{status,body}, Transport(String)}`. Task 10 adds `post_json`.
- `src/cli.rs`: `DEFAULT_RETRIES`, `DEFAULT_TIMEOUT_SECS`.

## File structure

```
Cargo.toml             + uuid, geo-traits
src/
  cli.rs               + DiffArgs, LoadArgs, Command::{Diff, Load}, DEFAULT_BATCH_SIZE
  main.rs              + mod diff; mod load; two match arms
  geometry/geojson.rs  + pub convert(geojson::Geometry) -> Result<(Geometry, bool), String>
  extract/client.rs    + post_json, shared send loop, any 2xx is success
  diff/
    mod.rs             run_diff: index, dispatch by extension, per-row process, atomic write
    compare.rs         round_coord, round_geometry, wkb_key, canonical
    input.rs           ColumnValue, InputRow, writable column sets, column_from_json
    geojson.rs         feature_to_row, read_feature_collection (streaming), read_feature_lines
    parquet.rs         read_geoparquet: geo metadata, cell -> JSON, WKB -> geo
    rebuild.rs         rebuild(): writable columns and geometry onto the snapshot resource
  load/
    mod.rs             run_load: read, validate, preflight, order, bundle, dry-run, post, probe
    order.rs           order_parents_first
    bundle.rs          Entry, Bundle, plan(), if_match()
    capability.rs      update_create_types(), check_update_create()
tests/
  diff.rs
  load.rs
```

---

## Task 0: Dependencies, CLI, stubs

**Files:**
- Modify: `Cargo.toml`
- Modify: `src/cli.rs`
- Modify: `src/main.rs`
- Create: `src/diff/mod.rs`, `src/load/mod.rs`

- [ ] **Step 1: Add dependencies**

In `Cargo.toml` under `[dependencies]`, after the `url` line, add:

```toml
uuid = { version = "1", features = ["v4"] }
geo-traits = "0.3"
```

- [ ] **Step 2: Add the CLI types**

In `src/cli.rs`, after `DEFAULT_TIMEOUT_SECS`, add:

```rust
pub const DEFAULT_BATCH_SIZE: usize = 100;
```

Add two variants to `Command`, after `Run(RunArgs)`:

```rust
    /// Compare an edited GeoJSON or GeoParquet file to the snapshot; write changed Locations as FHIR NDJSON (offline)
    Diff(DiffArgs),
    /// Send changed resources to a FHIR server as version-checked transaction bundles
    Load(LoadArgs),
```

Append at the end of the file:

```rust
#[derive(clap::Args, Debug, Clone)]
pub struct DiffArgs {
    /// Snapshot directory containing locations.ndjson
    #[arg(long)]
    pub snapshot: PathBuf,
    /// Edited file: .geojson/.json (FeatureCollection), .geojsonl/.geojsons (one Feature per line), or .parquet
    #[arg(long = "in")]
    pub input: PathBuf,
    /// Output NDJSON file of changed resources
    #[arg(long)]
    pub out: PathBuf,
    /// Also write the diff report as JSON to this file
    #[arg(long)]
    pub report: Option<PathBuf>,
}

#[derive(clap::Args, Debug, Clone)]
pub struct LoadArgs {
    /// Base URL of the FHIR server
    #[arg(long)]
    pub server: String,
    /// Bearer token; falls back to $KILN_TOKEN
    #[arg(long, env = "KILN_TOKEN", hide_env_values = true)]
    pub token: Option<String>,
    /// NDJSON of FHIR resources to load (normally diff output)
    #[arg(long = "in")]
    pub input: PathBuf,
    /// Run the preflight and print the bundle plan without posting
    #[arg(long)]
    pub dry_run: bool,
    /// Resources per transaction bundle
    #[arg(long, default_value_t = DEFAULT_BATCH_SIZE)]
    pub batch_size: usize,
    /// Attempts per request
    #[arg(long, default_value_t = DEFAULT_RETRIES)]
    pub retries: usize,
    /// Total timeout per HTTP request, in seconds
    #[arg(long, default_value_t = DEFAULT_TIMEOUT_SECS)]
    pub timeout: u64,
}
```

- [ ] **Step 3: Stub the modules and wire main**

Create `src/diff/mod.rs`:

```rust
//! `kiln diff`: edited GeoJSON or GeoParquet -> FHIR NDJSON of changed Locations.

use crate::cli::DiffArgs;
use crate::error::{KilnError, Result};

pub fn run_diff(_args: &DiffArgs) -> Result<()> {
    Err(KilnError::Usage("diff is not implemented yet".into()))
}
```

Create `src/load/mod.rs`:

```rust
//! `kiln load`: FHIR NDJSON -> transaction bundles with If-Match.

use crate::cli::LoadArgs;
use crate::error::{KilnError, Result};

pub fn run_load(_args: &LoadArgs) -> Result<()> {
    Err(KilnError::Usage("load is not implemented yet".into()))
}
```

In `src/main.rs`, add `mod diff;` after `mod cli;` and `mod load;` after `mod inspect;` (keep the list alphabetical), and add two match arms after the `Run` arm:

```rust
        cli::Command::Diff(args) => diff::run_diff(&args),
        cli::Command::Load(args) => load::run_load(&args),
```

- [ ] **Step 4: Build and check the help**

Run: `cargo build 2>&1 | tail -3 && ./target/debug/kiln diff --help | head -3 && ./target/debug/kiln load --help | grep -c -- '--'`
Expected: a clean build, the diff help header, and `7` (server, token, in, dry-run, batch-size, retries, timeout, plus help/version lines count too; anything ≥ 7 is fine).

- [ ] **Step 5: Commit**

```bash
git add Cargo.toml Cargo.lock src/cli.rs src/main.rs src/diff/mod.rs src/load/mod.rs
git commit -m "diff, load: CLI surface and stubs"
```

---

## Task 1: Coordinate rounding and canonical JSON

**Files:**
- Create: `src/diff/compare.rs`
- Modify: `src/diff/mod.rs`

- [ ] **Step 1: Write the failing tests**

Create `src/diff/compare.rs` with only the test module for now:

```rust
//! What "changed" means: coordinates compare after rounding to seven
//! decimals (about a centimetre), so a float round trip through a GIS
//! tool is not an edit; resources compare as canonical JSON.

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn rounds_to_seven_decimals() {
        assert_eq!(round_coord(3.123456789), 3.1234568);
        assert_eq!(round_coord(3.25), 3.25);
        assert_eq!(round_coord(-0.00000004), -0.0);
    }

    #[test]
    fn float_noise_in_the_eighth_decimal_gives_the_same_key() {
        let a = geo::Geometry::Point(geo::Point::new(3.25, 6.25));
        let b = geo::Geometry::Point(geo::Point::new(3.250000004, 6.249999996));
        assert_eq!(wkb_key(&a), wkb_key(&b));
        let c = geo::Geometry::Point(geo::Point::new(3.2500001, 6.25));
        assert_ne!(wkb_key(&a), wkb_key(&c));
    }

    #[test]
    fn canonical_sorts_keys_recursively_and_keeps_arrays_in_order() {
        let v = json!({"b": [{"z": 1, "a": 2}], "a": {"y": null, "x": "s"}});
        assert_eq!(
            canonical(&v).to_string(),
            r#"{"a":{"x":"s","y":null},"b":[{"a":2,"z":1}]}"#
        );
    }
}
```

- [ ] **Step 2: Run the tests to see them fail**

Add `pub mod compare;` at the top of `src/diff/mod.rs` (after the module doc comment).
Run: `cargo test diff::compare 2>&1 | grep -E 'error\[|cannot find' | head -3`
Expected: errors that `round_coord`, `wkb_key`, `canonical` are not found.

- [ ] **Step 3: Implement**

Insert above the test module in `src/diff/compare.rs`:

```rust
use geo::{Geometry, MapCoords};
use serde_json::Value;

use crate::geometry::to_wkb;

pub const DECIMALS: i32 = 7;

pub fn round_coord(v: f64) -> f64 {
    let f = 10f64.powi(DECIMALS);
    (v * f).round() / f
}

pub fn round_geometry(g: &Geometry<f64>) -> Geometry<f64> {
    g.map_coords(|c| geo::coord! { x: round_coord(c.x), y: round_coord(c.y) })
}

/// The comparison key for a geometry: little-endian WKB of the rounded
/// geometry. Two geometries with the same key are the same edit.
pub fn wkb_key(g: &Geometry<f64>) -> Vec<u8> {
    to_wkb(&round_geometry(g))
}

/// Object keys sorted recursively; arrays keep their order. `serde_json`
/// is built with `preserve_order`, so inserting in sorted order yields
/// sorted output.
pub fn canonical(v: &Value) -> Value {
    match v {
        Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            let mut out = serde_json::Map::new();
            for k in keys {
                out.insert(k.clone(), canonical(&m[k]));
            }
            Value::Object(out)
        }
        Value::Array(a) => Value::Array(a.iter().map(canonical).collect()),
        other => other.clone(),
    }
}
```

- [ ] **Step 4: Run the tests**

Run: `cargo test diff::compare 2>&1 | grep 'test result'`
Expected: `test result: ok. 3 passed`

- [ ] **Step 5: Commit**

```bash
git add src/diff/compare.rs src/diff/mod.rs
git commit -m "diff: seven-decimal rounding, WKB key, canonical JSON"
```

---

## Task 2: The format-neutral input row

**Files:**
- Create: `src/diff/input.rs`
- Modify: `src/diff/mod.rs`

- [ ] **Step 1: Write the failing tests**

Create `src/diff/input.rs` with the test module:

```rust
//! One edited row, whatever file it came from. Only the README's writable
//! columns are kept; each reader coerces its native types into
//! `ColumnValue` and the rebuild step never sees a file format.

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn writable_columns_are_the_readme_second_group() {
        assert!(is_writable("name"));
        assert!(is_writable("position_longitude"));
        assert!(is_writable("identifier"));
        assert!(is_writable("pcode"));
        assert!(!is_writable("admin1_name"));
        assert!(!is_writable("version_id"));
        assert!(!is_writable("id"));
        assert!(!is_writable("geometry"));
    }

    #[test]
    fn text_columns_coerce_and_empty_string_clears() {
        assert_eq!(column_from_json("name", &json!("Gama")), Ok(ColumnValue::Text("Gama".into())));
        assert_eq!(column_from_json("name", &json!("")), Ok(ColumnValue::Null));
        assert_eq!(column_from_json("name", &json!(null)), Ok(ColumnValue::Null));
        assert!(column_from_json("name", &json!(5)).is_err());
    }

    #[test]
    fn number_columns_accept_numbers_and_numeric_strings() {
        assert_eq!(column_from_json("position_longitude", &json!(3.25)), Ok(ColumnValue::Number(3.25)));
        assert_eq!(column_from_json("position_latitude", &json!("6.25")), Ok(ColumnValue::Number(6.25)));
        assert!(column_from_json("position_latitude", &json!("north")).is_err());
        assert!(column_from_json("position_latitude", &json!(true)).is_err());
    }

    #[test]
    fn alias_accepts_a_list_or_a_json_string_of_one() {
        let want = Ok(ColumnValue::TextList(vec!["a".into(), "b".into()]));
        assert_eq!(column_from_json("alias", &json!(["a", "b"])), want);
        assert_eq!(column_from_json("alias", &json!("[\"a\",\"b\"]")), want);
        assert_eq!(column_from_json("alias", &json!([])), Ok(ColumnValue::Null));
        assert!(column_from_json("alias", &json!([1])).is_err());
        assert!(column_from_json("alias", &json!("not json")).is_err());
    }

    #[test]
    fn identifier_accepts_objects_or_a_json_string_of_them() {
        let want = Ok(ColumnValue::Identifiers(vec![
            Identifier { system: Some("s".into()), value: Some("v".into()) },
            Identifier { system: None, value: Some("w".into()) },
        ]));
        let list = json!([{"system": "s", "value": "v"}, {"value": "w"}]);
        assert_eq!(column_from_json("identifier", &list), want);
        assert_eq!(column_from_json("identifier", &json!(list.to_string())), want);
        assert!(column_from_json("identifier", &json!(["s|v"])).is_err());
    }
}
```

- [ ] **Step 2: Run the tests to see them fail**

Add `pub mod input;` to `src/diff/mod.rs`.
Run: `cargo test diff::input 2>&1 | grep -c 'cannot find'`
Expected: a non-zero count.

- [ ] **Step 3: Implement**

Insert above the test module:

```rust
use std::collections::BTreeMap;

use geo::Geometry;
use serde_json::Value;

pub use crate::fhir::location::Identifier;

pub const TEXT_COLUMNS: [&str; 13] = [
    "name",
    "status",
    "description",
    "type",
    "physical_type",
    "part_of",
    "managing_organization",
    "pcode",
    "gers_id",
    "settlement_type",
    "delivery_strategy",
    "facility_level",
    "ownership",
];
pub const NUMBER_COLUMNS: [&str; 2] = ["position_longitude", "position_latitude"];
pub const ALIAS_COLUMN: &str = "alias";
pub const IDENTIFIER_COLUMN: &str = "identifier";
pub const ID_COLUMN: &str = "id";

#[derive(Debug, Clone, PartialEq)]
pub enum ColumnValue {
    /// Present and empty: the FHIR field is removed.
    Null,
    Text(String),
    Number(f64),
    TextList(Vec<String>),
    Identifiers(Vec<Identifier>),
}

#[derive(Debug, Clone, Default)]
pub struct InputRow {
    pub id: Option<String>,
    /// Only writable columns, keyed by README column name. Absent means untouched.
    pub columns: BTreeMap<String, ColumnValue>,
    /// 2D, unrounded; `None` means no geometry edit.
    pub geometry: Option<Geometry<f64>>,
    /// 1-based feature or row index, for messages.
    pub line: usize,
}

pub fn is_writable(name: &str) -> bool {
    TEXT_COLUMNS.contains(&name)
        || NUMBER_COLUMNS.contains(&name)
        || name == ALIAS_COLUMN
        || name == IDENTIFIER_COLUMN
}

fn text_list(v: &Value) -> Result<ColumnValue, String> {
    let Value::Array(items) = v else {
        return Err("expected a list of strings".into());
    };
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        match item.as_str() {
            Some(s) => out.push(s.to_string()),
            None => return Err("expected a list of strings".into()),
        }
    }
    Ok(if out.is_empty() {
        ColumnValue::Null
    } else {
        ColumnValue::TextList(out)
    })
}

fn identifiers(v: &Value) -> Result<ColumnValue, String> {
    let Value::Array(items) = v else {
        return Err("expected a list of {system, value} objects".into());
    };
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        let Value::Object(o) = item else {
            return Err("expected a list of {system, value} objects".into());
        };
        out.push(Identifier {
            system: o.get("system").and_then(Value::as_str).map(str::to_string),
            value: o.get("value").and_then(Value::as_str).map(str::to_string),
        });
    }
    Ok(if out.is_empty() {
        ColumnValue::Null
    } else {
        ColumnValue::Identifiers(out)
    })
}

/// A string holding JSON of the expected shape is accepted for the list
/// columns: GDAL flattens nested fields to JSON text on export.
fn list_or_json_string(v: &Value, parse: fn(&Value) -> Result<ColumnValue, String>) -> Result<ColumnValue, String> {
    match v {
        Value::String(s) if s.trim().is_empty() => Ok(ColumnValue::Null),
        Value::String(s) => match serde_json::from_str::<Value>(s) {
            Ok(inner @ Value::Array(_)) => parse(&inner),
            _ => Err("expected a list, or a string holding a JSON list".into()),
        },
        other => parse(other),
    }
}

/// Coerce one cell to the column's type. `Err` carries a message for the
/// `input_column_type` report; the caller drops the column for that row.
pub fn column_from_json(name: &str, v: &Value) -> Result<ColumnValue, String> {
    if v.is_null() {
        return Ok(ColumnValue::Null);
    }
    if TEXT_COLUMNS.contains(&name) {
        return match v {
            Value::String(s) if s.is_empty() => Ok(ColumnValue::Null),
            Value::String(s) => Ok(ColumnValue::Text(s.clone())),
            _ => Err("expected text".into()),
        };
    }
    if NUMBER_COLUMNS.contains(&name) {
        return match v {
            Value::Number(n) => n.as_f64().map(ColumnValue::Number).ok_or_else(|| "expected a number".into()),
            Value::String(s) if s.trim().is_empty() => Ok(ColumnValue::Null),
            Value::String(s) => s.trim().parse::<f64>().map(ColumnValue::Number).map_err(|_| "expected a number".into()),
            _ => Err("expected a number".into()),
        };
    }
    if name == ALIAS_COLUMN {
        return list_or_json_string(v, text_list);
    }
    if name == IDENTIFIER_COLUMN {
        return list_or_json_string(v, identifiers);
    }
    Err(format!("{name} is not a writable column"))
}
```

- [ ] **Step 4: Run the tests**

Run: `cargo test diff::input 2>&1 | grep 'test result'`
Expected: `test result: ok. 5 passed`

- [ ] **Step 5: Commit**

```bash
git add src/diff/input.rs src/diff/mod.rs
git commit -m "diff: InputRow and column coercion"
```

---

## Task 3: GeoJSON readers, streaming

**Files:**
- Modify: `src/geometry/geojson.rs`
- Create: `src/diff/geojson.rs`
- Modify: `src/diff/mod.rs`

- [ ] **Step 1: Expose the GeoJSON → geo conversion**

In `src/geometry/geojson.rs`, replace the whole `to_geo` function with a public conversion plus a thin reporting wrapper:

```rust
/// GeoJSON geometry -> 2D `geo` geometry. `Ok((geom, true))` means a
/// third coordinate was dropped. Shared by the boundary path (which
/// reports as `boundary_*`) and diff's input path (`geometry_*`).
pub fn convert(g: geojson::Geometry) -> Result<(Geometry<f64>, bool), String> {
    let has_extra = validate_positions(&g.value)
        .map_err(|()| "coordinate position with fewer than 2 numbers".to_string())?;
    let geom = Geometry::<f64>::try_from(g).map_err(|err| err.to_string())?;
    Ok((geom, has_extra))
}

fn to_geo(g: geojson::Geometry, id: &str, report: &mut Report) -> Option<Geometry<f64>> {
    match convert(g) {
        Ok((geom, has_extra)) => {
            if has_extra {
                report.add("boundary_z_dropped", id, "3D coordinates flattened to 2D");
            }
            Some(geom)
        }
        Err(msg) => {
            report.add("boundary_unparseable", id, &msg);
            None
        }
    }
}
```

In `src/geometry/mod.rs` change the re-export line to `pub use geojson::{convert, kind_name, parse_boundary};`.

Run: `cargo test geometry 2>&1 | grep 'test result'`
Expected: all geometry tests still pass.

- [ ] **Step 2: Write the failing tests**

Create `src/diff/geojson.rs` with the test module:

```rust
//! GeoJSON input: a FeatureCollection streamed one Feature at a time by a
//! serde visitor, or one Feature per line. A whole-country export never
//! sits in memory at once.

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    use crate::diff::input::ColumnValue;

    fn tmp(content: &str) -> tempfile::NamedTempFile {
        let mut f = tempfile::NamedTempFile::new().unwrap();
        f.write_all(content.as_bytes()).unwrap();
        f
    }

    fn feature(json: &str) -> Feature {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn id_comes_from_the_property_then_the_feature_id() {
        let mut r = Report::default();
        let row = feature_to_row(feature(r#"{"type":"Feature","id":"f1","properties":{"id":"p1"},"geometry":null}"#), 1, &mut r);
        assert_eq!(row.id.as_deref(), Some("p1"));
        let row = feature_to_row(feature(r#"{"type":"Feature","id":"f1","properties":{},"geometry":null}"#), 2, &mut r);
        assert_eq!(row.id.as_deref(), Some("f1"));
        let row = feature_to_row(feature(r#"{"type":"Feature","id":7,"properties":{"id":""},"geometry":null}"#), 3, &mut r);
        assert_eq!(row.id.as_deref(), Some("7"));
        let row = feature_to_row(feature(r#"{"type":"Feature","properties":null,"geometry":null}"#), 4, &mut r);
        assert_eq!(row.id, None);
        assert_eq!(row.line, 4);
    }

    #[test]
    fn writable_columns_are_kept_derived_ignored_bad_types_reported() {
        let mut r = Report::default();
        let row = feature_to_row(
            feature(r#"{"type":"Feature","properties":{"id":"a","name":"N","admin1_name":"stale","alias":["x"],"position_latitude":"oops"},"geometry":{"type":"Point","coordinates":[3.25,6.25,10]}}"#),
            1,
            &mut r,
        );
        assert_eq!(row.columns.get("name"), Some(&ColumnValue::Text("N".into())));
        assert_eq!(row.columns.get("alias"), Some(&ColumnValue::TextList(vec!["x".into()])));
        assert!(!row.columns.contains_key("admin1_name"));
        assert!(!row.columns.contains_key("position_latitude"));
        assert_eq!(r.count("input_column_type"), 1);
        assert_eq!(r.count("boundary_z_dropped"), 1);
        assert_eq!(row.geometry, Some(geo::Geometry::Point(geo::Point::new(3.25, 6.25))));
    }

    #[test]
    fn a_malformed_geometry_fails_the_read_as_a_usage_error() {
        // The geojson crate rejects a one-number position while parsing the
        // Feature itself, so this never reaches `convert`: the whole read
        // fails, naming the file, rather than one row being reported.
        let f = tmp(r#"{"type":"FeatureCollection","features":[
            {"type":"Feature","properties":{"id":"a"},"geometry":{"type":"Point","coordinates":[3.25]}}]}"#);
        let err = read_feature_collection(f.path(), |_, _| Ok(())).unwrap_err();
        assert!(matches!(&err, KilnError::Usage(m) if m.contains("position")), "{err}");
    }

    #[test]
    fn a_feature_collection_streams_every_feature_in_order() {
        let f = tmp(r#"{"name":"layer","features":[
            {"type":"Feature","properties":{"id":"a"},"geometry":null},
            {"type":"Feature","properties":{"id":"b"},"geometry":null},
            {"type":"Feature","properties":{"id":"c"},"geometry":null}],"type":"FeatureCollection"}"#);
        let mut seen = Vec::new();
        read_feature_collection(f.path(), |feature, index| {
            seen.push((index, feature.properties.unwrap()["id"].as_str().unwrap().to_string()));
            Ok(())
        })
        .unwrap();
        assert_eq!(seen, vec![(1, "a".to_string()), (2, "b".to_string()), (3, "c".to_string())]);
    }

    #[test]
    fn a_sink_error_stops_the_read_and_comes_back_unchanged() {
        let f = tmp(r#"{"type":"FeatureCollection","features":[
            {"type":"Feature","properties":{"id":"a"},"geometry":null},
            {"type":"Feature","properties":{"id":"b"},"geometry":null}]}"#);
        let mut calls = 0;
        let err = read_feature_collection(f.path(), |_, _| {
            calls += 1;
            Err(KilnError::Usage("stop".into()))
        })
        .unwrap_err();
        assert!(matches!(&err, KilnError::Usage(m) if m == "stop"));
        assert_eq!(calls, 1);
    }

    #[test]
    fn not_a_collection_is_a_usage_error() {
        let f = tmp(r#"{"type":"Feature","properties":{},"geometry":null}"#);
        let err = read_feature_collection(f.path(), |_, _| Ok(())).unwrap_err();
        assert!(matches!(&err, KilnError::Usage(m) if m.contains("FeatureCollection")), "{err}");
        let f = tmp(r#"[1,2]"#);
        assert!(matches!(read_feature_collection(f.path(), |_, _| Ok(())).unwrap_err(), KilnError::Usage(_)));
    }

    #[test]
    fn feature_lines_are_read_one_per_line() {
        let f = tmp("{\"type\":\"Feature\",\"properties\":{\"id\":\"a\"},\"geometry\":null}\n\n{\"type\":\"Feature\",\"properties\":{\"id\":\"b\"},\"geometry\":null}\n");
        let mut seen = Vec::new();
        read_feature_lines(f.path(), |feature, index| {
            seen.push((index, feature.properties.unwrap()["id"].as_str().unwrap().to_string()));
            Ok(())
        })
        .unwrap();
        assert_eq!(seen.len(), 2);
        assert_eq!(seen[1], (2, "b".to_string()));
        let bad = tmp("{\"nope\":1}\n");
        assert!(matches!(read_feature_lines(bad.path(), |_, _| Ok(())).unwrap_err(), KilnError::Usage(_)));
    }
}
```

- [ ] **Step 3: Run the tests to see them fail**

Add `pub mod geojson;` to `src/diff/mod.rs`.
Run: `cargo test diff::geojson 2>&1 | grep -c 'cannot find'`
Expected: non-zero.

- [ ] **Step 4: Implement**

Insert above the test module:

```rust
use std::fs::File;
use std::io::BufReader;
use std::path::Path;

use geojson::feature::Id;
use geojson::Feature;
use serde::de::{self, DeserializeSeed, Deserializer, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde_json::Value;

use crate::diff::input::{column_from_json, is_writable, InputRow, ID_COLUMN};
use crate::error::{KilnError, Result};
use crate::fhir::ndjson::NdjsonReader;
use crate::geometry::convert;
use crate::report::Report;

/// One Feature -> one row. `line` is the 1-based feature index.
pub fn feature_to_row(f: Feature, line: usize, report: &mut Report) -> InputRow {
    let mut row = InputRow {
        line,
        ..Default::default()
    };
    let props = f.properties.unwrap_or_default();
    row.id = match props.get(ID_COLUMN) {
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        Some(Value::Number(n)) => Some(n.to_string()),
        _ => match f.id {
            Some(Id::String(s)) if !s.is_empty() => Some(s),
            Some(Id::Number(n)) => Some(n.to_string()),
            _ => None,
        },
    };
    let label = row.id.clone().unwrap_or_else(|| format!("<row {line}>"));
    for (name, value) in &props {
        if !is_writable(name) {
            continue;
        }
        match column_from_json(name, value) {
            Ok(v) => {
                row.columns.insert(name.clone(), v);
            }
            Err(msg) => report.add("input_column_type", &label, &format!("{name}: {msg}")),
        }
    }
    if let Some(g) = f.geometry {
        match convert(g) {
            Ok((geom, z_dropped)) => {
                if z_dropped {
                    report.add("boundary_z_dropped", &label, "3D coordinates flattened to 2D");
                }
                row.geometry = Some(geom);
            }
            Err(msg) => report.add("geometry_unparseable", &label, &msg),
        }
    }
    row
}

/// Stream `{"type":"FeatureCollection","features":[...]}` from disk,
/// handing each Feature to `sink` as soon as it is parsed. Keys other
/// than `features` are skipped without being materialised.
pub fn read_feature_collection<F>(path: &Path, mut sink: F) -> Result<()>
where
    F: FnMut(Feature, usize) -> Result<()>,
{
    let file = File::open(path).map_err(|e| KilnError::io(path, e))?;
    let mut de = serde_json::Deserializer::from_reader(BufReader::with_capacity(1 << 20, file));
    let mut state = State {
        sink: &mut sink,
        error: None,
        seen_features: false,
    };
    let outcome = Collection(&mut state).deserialize(&mut de);
    if let Some(e) = state.error.take() {
        return Err(e);
    }
    outcome.map_err(|e| KilnError::Usage(format!("{}: {e}", path.display())))?;
    if !state.seen_features {
        return Err(KilnError::Usage(format!(
            "{}: not a GeoJSON FeatureCollection (no \"features\" array)",
            path.display()
        )));
    }
    Ok(())
}

struct State<'s, F> {
    sink: &'s mut F,
    /// The sink's own error, stashed so it survives serde's error type.
    error: Option<KilnError>,
    seen_features: bool,
}

struct Collection<'a, 's, F>(&'a mut State<'s, F>);
struct Features<'a, 's, F>(&'a mut State<'s, F>);

impl<'de, 'a, 's, F> DeserializeSeed<'de> for Collection<'a, 's, F>
where
    F: FnMut(Feature, usize) -> Result<()>,
{
    type Value = ();
    fn deserialize<D: Deserializer<'de>>(self, d: D) -> std::result::Result<(), D::Error> {
        d.deserialize_map(self)
    }
}

impl<'de, 'a, 's, F> Visitor<'de> for Collection<'a, 's, F>
where
    F: FnMut(Feature, usize) -> Result<()>,
{
    type Value = ();
    fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("a GeoJSON FeatureCollection object")
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> std::result::Result<(), A::Error> {
        while let Some(key) = map.next_key::<String>()? {
            if key == "features" {
                self.0.seen_features = true;
                map.next_value_seed(Features(&mut *self.0))?;
            } else {
                map.next_value::<IgnoredAny>()?;
            }
        }
        Ok(())
    }
}

impl<'de, 'a, 's, F> DeserializeSeed<'de> for Features<'a, 's, F>
where
    F: FnMut(Feature, usize) -> Result<()>,
{
    type Value = ();
    fn deserialize<D: Deserializer<'de>>(self, d: D) -> std::result::Result<(), D::Error> {
        d.deserialize_seq(self)
    }
}

impl<'de, 'a, 's, F> Visitor<'de> for Features<'a, 's, F>
where
    F: FnMut(Feature, usize) -> Result<()>,
{
    type Value = ();
    fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("an array of GeoJSON Features")
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> std::result::Result<(), A::Error> {
        let mut index = 0usize;
        while let Some(feature) = seq.next_element::<Feature>()? {
            index += 1;
            if let Err(e) = (self.0.sink)(feature, index) {
                self.0.error = Some(e);
                return Err(de::Error::custom("stopped by kiln"));
            }
        }
        Ok(())
    }
}

/// One Feature per line (`.geojsonl`, `.geojsons`).
pub fn read_feature_lines<F>(path: &Path, mut sink: F) -> Result<()>
where
    F: FnMut(Feature, usize) -> Result<()>,
{
    let mut index = 0usize;
    for line in NdjsonReader::open(path)? {
        let line = line?;
        index += 1;
        let feature: Feature = serde_json::from_str(&line.text).map_err(|e| {
            KilnError::Usage(format!(
                "{}: line {}: not a GeoJSON Feature: {e}",
                path.display(),
                line.number
            ))
        })?;
        sink(feature, index)?;
    }
    Ok(())
}
```

- [ ] **Step 5: Run the tests**

Run: `cargo test diff::geojson 2>&1 | grep 'test result'`
Expected: `test result: ok. 7 passed`

- [ ] **Step 6: Commit**

```bash
git add src/geometry/geojson.rs src/geometry/mod.rs src/diff/geojson.rs src/diff/mod.rs
git commit -m "diff: streaming GeoJSON readers and feature-to-row"
```

---

## Task 4: GeoParquet reader

**Files:**
- Create: `src/diff/parquet.rs`
- Modify: `src/diff/mod.rs`

- [ ] **Step 1: Write the failing test**

Create `src/diff/parquet.rs` with the test module:

```rust
//! GeoParquet input, one record batch at a time. Cells become JSON so the
//! column coercion is the one GeoJSON uses; the geometry column is found
//! through the `geo` file metadata and read as WKB.

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    use arrow_array::builder::{ListBuilder, StringBuilder};
    use arrow_array::{BinaryArray, Float64Array, RecordBatch, StringArray};
    use arrow_schema::{Field, Schema};
    use parquet::arrow::ArrowWriter;
    use parquet::file::metadata::KeyValue;
    use parquet::file::properties::WriterProperties;

    use crate::diff::input::{ColumnValue, Identifier};
    use crate::geometry::to_wkb;

    fn write_fixture(path: &Path) {
        let mut alias = ListBuilder::new(StringBuilder::new());
        alias.values().append_value("GC");
        alias.append(true);
        alias.append(false);
        let point = to_wkb(&geo::Geometry::Point(geo::Point::new(3.25, 6.25)));
        let schema = Arc::new(Schema::new(vec![
            Field::new("id", DataType::Utf8, true),
            Field::new("name", DataType::Utf8, true),
            Field::new("alias", DataType::List(Arc::new(Field::new("item", DataType::Utf8, true))), true),
            Field::new("identifier", DataType::Utf8, true),
            Field::new("position_longitude", DataType::Float64, true),
            Field::new("admin1_name", DataType::Utf8, true),
            Field::new("shape", DataType::Binary, true),
        ]));
        let batch = RecordBatch::try_new(
            schema.clone(),
            vec![
                Arc::new(StringArray::from(vec![Some("clinic"), None])),
                Arc::new(StringArray::from(vec![Some("Gama Clinic"), Some("x")])),
                Arc::new(alias.finish()),
                Arc::new(StringArray::from(vec![Some(r#"[{"system":"s","value":"v"}]"#), None])),
                Arc::new(Float64Array::from(vec![Some(3.25), None])),
                Arc::new(StringArray::from(vec![Some("Kano"), None])),
                Arc::new(BinaryArray::from(vec![Some(point.as_slice()), Some(b"junk".as_slice())])),
            ],
        )
        .unwrap();
        let geo = r#"{"version":"1.1.0","primary_column":"shape","columns":{"shape":{"encoding":"WKB"}}}"#;
        let props = WriterProperties::builder()
            .set_key_value_metadata(Some(vec![KeyValue::new("geo".into(), geo.to_string())]))
            .build();
        let file = std::fs::File::create(path).unwrap();
        let mut w = ArrowWriter::try_new(file, schema, Some(props)).unwrap();
        w.write(&batch).unwrap();
        w.close().unwrap();
    }

    #[test]
    fn reads_rows_with_columns_and_wkb_geometry() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("edits.parquet");
        write_fixture(&path);
        let mut report = Report::default();
        let mut rows = Vec::new();
        read_geoparquet(&path, &mut report, |row, _| {
            rows.push(row);
            Ok(())
        })
        .unwrap();
        assert_eq!(rows.len(), 2);
        let r = &rows[0];
        assert_eq!(r.id.as_deref(), Some("clinic"));
        assert_eq!(r.line, 1);
        assert_eq!(r.columns.get("name"), Some(&ColumnValue::Text("Gama Clinic".into())));
        assert_eq!(r.columns.get("alias"), Some(&ColumnValue::TextList(vec!["GC".into()])));
        assert_eq!(
            r.columns.get("identifier"),
            Some(&ColumnValue::Identifiers(vec![Identifier { system: Some("s".into()), value: Some("v".into()) }]))
        );
        assert_eq!(r.columns.get("position_longitude"), Some(&ColumnValue::Number(3.25)));
        assert!(!r.columns.contains_key("admin1_name"));
        assert_eq!(r.geometry, Some(geo::Geometry::Point(geo::Point::new(3.25, 6.25))));

        let r = &rows[1];
        assert_eq!(r.id, None);
        assert_eq!(r.columns.get("alias"), Some(&ColumnValue::Null));
        assert_eq!(r.columns.get("position_longitude"), Some(&ColumnValue::Null));
        assert!(r.geometry.is_none());
        assert_eq!(report.count("geometry_unparseable"), 1);
    }

    #[test]
    fn a_file_without_geo_metadata_falls_back_to_a_geometry_column_or_none() {
        let schema = Schema::new(vec![Field::new("geometry", DataType::Binary, true)]);
        assert_eq!(geometry_column_name(None, &schema).as_deref(), Some("geometry"));
        let schema = Schema::new(vec![Field::new("name", DataType::Utf8, true)]);
        assert_eq!(geometry_column_name(None, &schema), None);
        let kv = vec![KeyValue::new("geo".into(), r#"{"primary_column":"shape"}"#.to_string())];
        let schema = Schema::new(vec![Field::new("shape", DataType::Binary, true)]);
        assert_eq!(geometry_column_name(Some(&kv), &schema).as_deref(), Some("shape"));
    }
}
```

- [ ] **Step 2: Run the test to see it fail**

Add `pub mod parquet;` to `src/diff/mod.rs`.
Run: `cargo test diff::parquet 2>&1 | grep -c 'cannot find'`
Expected: non-zero.

- [ ] **Step 3: Implement**

Insert above the test module:

```rust
use std::fs::File;
use std::path::Path;

use arrow_array::cast::AsArray;
use arrow_array::types::{Float32Type, Float64Type, Int32Type, Int64Type};
use arrow_array::{Array, ArrayRef, RecordBatch};
use arrow_schema::{DataType, Schema};
use geo_traits::to_geo::ToGeoGeometry;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::file::metadata::KeyValue;
use serde_json::Value;
use wkb::reader::read_wkb;

use crate::diff::input::{column_from_json, is_writable, InputRow, ID_COLUMN};
use crate::error::{KilnError, Result};
use crate::report::Report;
use crate::write::schema::GEOMETRY_COLUMN;

const BATCH_ROWS: usize = 1024;

/// The sink gets the report back so it can add its own issues while the
/// reader holds the same one.
pub fn read_geoparquet<F>(path: &Path, report: &mut Report, mut sink: F) -> Result<()>
where
    F: FnMut(InputRow, &mut Report) -> Result<()>,
{
    let file = File::open(path).map_err(|e| KilnError::io(path, e))?;
    let builder =
        ParquetRecordBatchReaderBuilder::try_new(file).map_err(|e| KilnError::parquet_at(path, e))?;
    let geometry_column = geometry_column_name(
        builder.metadata().file_metadata().key_value_metadata(),
        builder.schema(),
    );
    let reader = builder
        .with_batch_size(BATCH_ROWS)
        .build()
        .map_err(|e| KilnError::parquet_at(path, e))?;
    let mut index = 0usize;
    for batch in reader {
        let batch = batch?;
        for r in 0..batch.num_rows() {
            index += 1;
            let row = row_from_batch(&batch, r, index, geometry_column.as_deref(), report);
            sink(row, report)?;
        }
    }
    Ok(())
}

/// The GeoParquet `geo` metadata names the primary geometry column; a file
/// without it may still have a column called `geometry`. `None` means an
/// attribute-only edit.
pub fn geometry_column_name(kv: Option<&Vec<KeyValue>>, schema: &Schema) -> Option<String> {
    let from_meta = kv
        .and_then(|kv| kv.iter().find(|k| k.key == "geo"))
        .and_then(|k| k.value.as_deref())
        .and_then(|v| serde_json::from_str::<Value>(v).ok())
        .and_then(|g| g.get("primary_column")?.as_str().map(str::to_string));
    let name = from_meta.unwrap_or_else(|| GEOMETRY_COLUMN.to_string());
    schema.column_with_name(&name).map(|_| name)
}

fn row_from_batch(
    batch: &RecordBatch,
    r: usize,
    index: usize,
    geometry_column: Option<&str>,
    report: &mut Report,
) -> InputRow {
    let mut row = InputRow {
        line: index,
        ..Default::default()
    };
    let schema = batch.schema();
    if let Some((i, _)) = schema.column_with_name(ID_COLUMN) {
        row.id = match cell_to_json(batch.column(i), r) {
            Some(Value::String(s)) if !s.is_empty() => Some(s),
            Some(Value::Number(n)) => Some(n.to_string()),
            _ => None,
        };
    }
    let label = row.id.clone().unwrap_or_else(|| format!("<row {index}>"));
    for (i, field) in schema.fields().iter().enumerate() {
        let name = field.name();
        let col = batch.column(i);
        if Some(name.as_str()) == geometry_column {
            if col.is_null(r) {
                continue;
            }
            match wkb_cell(col, r) {
                Some(bytes) => match read_wkb(bytes).ok().and_then(|w| w.try_to_geometry()) {
                    Some(g) => row.geometry = Some(g),
                    None => report.add("geometry_unparseable", &label, "geometry column is not valid WKB"),
                },
                None => report.add(
                    "geometry_unparseable",
                    &label,
                    &format!("geometry column has type {}, not binary WKB", col.data_type()),
                ),
            }
            continue;
        }
        if !is_writable(name) {
            continue;
        }
        let Some(json) = cell_to_json(col, r) else {
            report.add(
                "input_column_type",
                &label,
                &format!("{name}: unsupported column type {}", col.data_type()),
            );
            continue;
        };
        match column_from_json(name, &json) {
            Ok(v) => {
                row.columns.insert(name.clone(), v);
            }
            Err(msg) => report.add("input_column_type", &label, &format!("{name}: {msg}")),
        }
    }
    row
}

fn wkb_cell(col: &ArrayRef, r: usize) -> Option<&[u8]> {
    match col.data_type() {
        DataType::Binary => Some(col.as_binary::<i32>().value(r)),
        DataType::LargeBinary => Some(col.as_binary::<i64>().value(r)),
        _ => None,
    }
}

fn json_f64(v: f64) -> Value {
    serde_json::Number::from_f64(v).map(Value::Number).unwrap_or(Value::Null)
}

fn list_to_json(items: &ArrayRef) -> Value {
    Value::Array(
        (0..items.len())
            .map(|i| cell_to_json(items, i).unwrap_or(Value::Null))
            .collect(),
    )
}

/// One cell as JSON. `None` means the column's Arrow type is not one diff
/// understands; a null cell is `Some(Value::Null)`.
pub fn cell_to_json(col: &ArrayRef, r: usize) -> Option<Value> {
    if col.is_null(r) {
        return Some(Value::Null);
    }
    Some(match col.data_type() {
        DataType::Utf8 => Value::String(col.as_string::<i32>().value(r).to_string()),
        DataType::LargeUtf8 => Value::String(col.as_string::<i64>().value(r).to_string()),
        DataType::Float64 => json_f64(col.as_primitive::<Float64Type>().value(r)),
        DataType::Float32 => json_f64(col.as_primitive::<Float32Type>().value(r) as f64),
        DataType::Int64 => Value::from(col.as_primitive::<Int64Type>().value(r)),
        DataType::Int32 => Value::from(col.as_primitive::<Int32Type>().value(r)),
        DataType::Boolean => Value::Bool(col.as_boolean().value(r)),
        DataType::List(_) => list_to_json(&col.as_list::<i32>().value(r)),
        DataType::LargeList(_) => list_to_json(&col.as_list::<i64>().value(r)),
        DataType::Struct(_) => {
            let s = col.as_struct();
            let mut o = serde_json::Map::new();
            for (field, child) in s.fields().iter().zip(s.columns()) {
                o.insert(field.name().clone(), cell_to_json(child, r).unwrap_or(Value::Null));
            }
            Value::Object(o)
        }
        _ => return None,
    })
}
```

- [ ] **Step 4: Run the tests**

Run: `cargo test diff::parquet 2>&1 | grep 'test result'`
Expected: `test result: ok. 2 passed`

- [ ] **Step 5: Commit**

```bash
git add src/diff/parquet.rs src/diff/mod.rs
git commit -m "diff: GeoParquet reader"
```

---

## Task 5: Rebuild — writable columns onto the resource

**Files:**
- Create: `src/diff/rebuild.rs`
- Modify: `src/diff/mod.rs`

- [ ] **Step 1: Write the failing tests**

Create `src/diff/rebuild.rs` with the test module:

```rust
//! Apply an edited row to a snapshot resource. Only the writable columns
//! touch the JSON; everything else on the resource is left exactly as the
//! snapshot had it, which is what makes the round trip lossless.

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn row(cols: &[(&str, ColumnValue)]) -> InputRow {
        InputRow {
            id: Some("a".into()),
            columns: cols.iter().map(|(k, v)| (k.to_string(), v.clone())).collect(),
            geometry: None,
            line: 1,
        }
    }

    fn text(s: &str) -> ColumnValue {
        ColumnValue::Text(s.into())
    }

    fn apply(base: Value, cols: &[(&str, ColumnValue)]) -> Value {
        let mut report = Report::default();
        let loc = Location::parse(&base, &mut report);
        rebuild(&base, loc.as_ref(), &row(cols), &mut report)
    }

    #[test]
    fn strings_set_and_null_clears_and_unknown_fields_survive() {
        let base = json!({"resourceType":"Location","id":"a","name":"Old","status":"active","meta":{"versionId":"3"},"mystery":{"k":1}});
        let out = apply(base, &[("name", text("New")), ("status", ColumnValue::Null), ("description", text("d"))]);
        assert_eq!(out["name"], "New");
        assert!(out.get("status").is_none());
        assert_eq!(out["description"], "d");
        assert_eq!(out["meta"]["versionId"], "3");
        assert_eq!(out["mystery"]["k"], 1);
    }

    #[test]
    fn alias_and_identifier_lists_are_replaced_whole() {
        let base = json!({"resourceType":"Location","id":"a","alias":["x"],"identifier":[{"system":"s","value":"1"},{"system":"t","value":"2"}]});
        let out = apply(
            base.clone(),
            &[
                ("alias", ColumnValue::TextList(vec!["y".into(), "z".into()])),
                ("identifier", ColumnValue::Identifiers(vec![Identifier { system: Some("u".into()), value: Some("3".into()) }])),
            ],
        );
        assert_eq!(out["alias"], json!(["y", "z"]));
        assert_eq!(out["identifier"], json!([{"system":"u","value":"3"}]));
        let out = apply(base, &[("alias", ColumnValue::Null), ("identifier", ColumnValue::Null)]);
        assert!(out.get("alias").is_none());
        assert!(out.get("identifier").is_none());
    }

    #[test]
    fn pcode_upserts_into_the_identifier_list_after_it_is_replaced() {
        let base = json!({"resourceType":"Location","id":"a","identifier":[{"system":PCODE_SYSTEM,"value":"OLD"}]});
        let out = apply(base.clone(), &[("pcode", text("NG1"))]);
        assert_eq!(out["identifier"], json!([{"system":PCODE_SYSTEM,"value":"NG1"}]));
        let out = apply(
            base.clone(),
            &[
                ("identifier", ColumnValue::Identifiers(vec![Identifier { system: Some("s".into()), value: Some("v".into()) }])),
                ("pcode", text("NG2")),
            ],
        );
        assert_eq!(out["identifier"], json!([{"system":"s","value":"v"},{"system":PCODE_SYSTEM,"value":"NG2"}]));
        let out = apply(base, &[("pcode", ColumnValue::Null), ("gers_id", text("g"))]);
        assert_eq!(out["identifier"], json!([{"system":GERS_SYSTEM,"value":"g"}]));
        let out = apply(json!({"resourceType":"Location","id":"a"}), &[("gers_id", ColumnValue::Null)]);
        assert!(out.get("identifier").is_none());
    }

    #[test]
    fn references_keep_their_prefix_and_default_to_the_type() {
        let base = json!({"resourceType":"Location","id":"a","partOf":{"reference":"Location/old","display":"Old"},"managingOrganization":{"reference":"http://h/fhir/Organization/o1"}});
        let out = apply(base, &[("part_of", text("new")), ("managing_organization", text("o2"))]);
        assert_eq!(out["partOf"]["reference"], "Location/new");
        assert_eq!(out["partOf"]["display"], "Old");
        assert_eq!(out["managingOrganization"]["reference"], "http://h/fhir/Organization/o2");
        let out = apply(json!({"resourceType":"Location","id":"a"}), &[("part_of", text("p")), ("managing_organization", text("o"))]);
        assert_eq!(out["partOf"]["reference"], "Location/p");
        assert_eq!(out["managingOrganization"]["reference"], "Organization/o");
        let out = apply(json!({"resourceType":"Location","id":"a","partOf":{"reference":"Location/p"}}), &[("part_of", ColumnValue::Null)]);
        assert!(out.get("partOf").is_none());
    }

    #[test]
    fn type_edits_the_first_coding_code_and_keeps_its_system() {
        let base = json!({"resourceType":"Location","id":"a","type":[{"coding":[{"system":"sys","code":"facility"}]},{"coding":[{"system":FACILITY_TYPE_SYSTEM,"code":"phc"}]}]});
        let out = apply(base.clone(), &[("type", text("admin-unit"))]);
        assert_eq!(out["type"][0]["coding"][0], json!({"system":"sys","code":"admin-unit"}));
        assert_eq!(out["type"][1]["coding"][0]["code"], "phc");
        let out = apply(json!({"resourceType":"Location","id":"a"}), &[("type", text("site"))]);
        assert_eq!(out["type"], json!([{"coding":[{"code":"site"}]}]));
        let out = apply(base, &[("type", ColumnValue::Null)]);
        assert_eq!(out["type"], json!([{"coding":[{"system":FACILITY_TYPE_SYSTEM,"code":"phc"}]}]));
    }

    #[test]
    fn physical_type_sets_the_first_coding_or_removes_the_concept() {
        let out = apply(json!({"resourceType":"Location","id":"a"}), &[("physical_type", text("si"))]);
        assert_eq!(out["physicalType"], json!({"coding":[{"code":"si"}]}));
        let out = apply(out, &[("physical_type", ColumnValue::Null)]);
        assert!(out.get("physicalType").is_none());
    }

    #[test]
    fn profile_extensions_upsert_by_url_and_clear() {
        let base = json!({"resourceType":"Location","id":"a","extension":[{"url":"https://example.org/keep","valueString":"k"},{"url":SETTLEMENT_TYPE_EXTENSION_URL,"valueCode":"rural"}]});
        let out = apply(base, &[("settlement_type", text("urban")), ("delivery_strategy", text("fixed"))]);
        assert_eq!(out["extension"][1]["valueCode"], "urban");
        assert_eq!(out["extension"][2], json!({"url":DELIVERY_STRATEGY_EXTENSION_URL,"valueCode":"fixed"}));
        let out = apply(out, &[("settlement_type", ColumnValue::Null), ("delivery_strategy", ColumnValue::Null)]);
        assert_eq!(out["extension"], json!([{"url":"https://example.org/keep","valueString":"k"}]));
        let out = apply(json!({"resourceType":"Location","id":"a"}), &[("settlement_type", ColumnValue::Null)]);
        assert!(out.get("extension").is_none());
    }

    #[test]
    fn facility_level_and_ownership_upsert_codings_by_system() {
        let base = json!({"resourceType":"Location","id":"a","type":[{"coding":[{"code":"facility"}]},{"coding":[{"system":FACILITY_TYPE_SYSTEM,"code":"phc"}]}]});
        let out = apply(base, &[("facility_level", text("hospital")), ("ownership", text("public"))]);
        assert_eq!(out["type"][1]["coding"][0]["code"], "hospital");
        assert_eq!(out["type"][2], json!({"coding":[{"system":OWNERSHIP_SYSTEM,"code":"public"}]}));
        let out = apply(out, &[("facility_level", ColumnValue::Null), ("ownership", ColumnValue::Null)]);
        assert_eq!(out["type"], json!([{"coding":[{"code":"facility"}]}]));
    }

    #[test]
    fn position_columns_set_clear_and_ignore_float_noise() {
        let base = json!({"resourceType":"Location","id":"a","position":{"longitude":3.25,"latitude":6.25}});
        let out = apply(base.clone(), &[("position_longitude", ColumnValue::Number(3.250000004)), ("position_latitude", ColumnValue::Number(6.25))]);
        assert_eq!(out["position"], json!({"longitude":3.25,"latitude":6.25}));
        let out = apply(base.clone(), &[("position_longitude", ColumnValue::Number(4.123456789))]);
        assert_eq!(out["position"], json!({"longitude":4.1234568,"latitude":6.25}));
        let out = apply(base.clone(), &[("position_longitude", ColumnValue::Null), ("position_latitude", ColumnValue::Null)]);
        assert!(out.get("position").is_none());

        let mut report = Report::default();
        let bare = json!({"resourceType":"Location","id":"a"});
        let out = rebuild(&bare, None, &row(&[("position_longitude", ColumnValue::Number(1.0))]), &mut report);
        assert!(out.get("position").is_none());
        assert_eq!(report.count("input_column_type"), 1);
        let out = rebuild(&bare, None, &row(&[("position_longitude", ColumnValue::Number(1.0)), ("position_latitude", ColumnValue::Number(2.0))]), &mut report);
        assert_eq!(out["position"], json!({"longitude":1.0,"latitude":2.0}));
    }
}
```

- [ ] **Step 2: Run the tests to see them fail**

Add `pub mod rebuild;` to `src/diff/mod.rs`.
Run: `cargo test diff::rebuild 2>&1 | grep -c 'cannot find'`
Expected: non-zero.

- [ ] **Step 3: Implement the column application**

Insert above the test module. The geometry hook at the end of `rebuild` is a stub until Task 6.

```rust
use serde_json::{json, Map, Value};

use crate::diff::compare::round_coord;
use crate::diff::input::{ColumnValue, Identifier, InputRow, IDENTIFIER_COLUMN};
use crate::fhir::location::{
    DELIVERY_STRATEGY_EXTENSION_URL, FACILITY_TYPE_SYSTEM, GERS_SYSTEM, OWNERSHIP_SYSTEM,
    PCODE_SYSTEM, SETTLEMENT_TYPE_EXTENSION_URL,
};
use crate::fhir::Location;
use crate::report::Report;

type Object = Map<String, Value>;

/// The resource as it should now be. `base` is the raw snapshot line, or
/// `{"resourceType":"Location","id":..}` for a create; `snapshot` is its
/// parsed form, used for the old geometry and position.
pub fn rebuild(
    base: &Value,
    snapshot: Option<&Location>,
    row: &InputRow,
    report: &mut Report,
) -> Value {
    let mut obj = base.as_object().cloned().unwrap_or_default();
    let id = obj
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("<unknown>")
        .to_string();
    // The whole identifier list first, so pcode / gers_id upsert into it.
    if let Some(v) = row.columns.get(IDENTIFIER_COLUMN) {
        apply_identifier_list(&mut obj, v);
    }
    for (name, value) in &row.columns {
        apply_column(&mut obj, name, value);
    }
    apply_position(&mut obj, row, &id, report);
    if let Some(geom) = &row.geometry {
        apply_geometry(&mut obj, &id, snapshot, geom, report);
    }
    Value::Object(obj)
}

fn text_of(v: &ColumnValue) -> Option<&str> {
    match v {
        ColumnValue::Text(s) => Some(s.as_str()),
        _ => None,
    }
}

fn apply_column(obj: &mut Object, name: &str, value: &ColumnValue) {
    let t = text_of(value);
    match name {
        "name" | "status" | "description" => set_string(obj, name, t),
        "type" => set_first_type_code(obj, t),
        "physical_type" => set_physical_type(obj, t),
        "part_of" => set_reference(obj, "partOf", "Location", t),
        "managing_organization" => set_reference(obj, "managingOrganization", "Organization", t),
        "pcode" => upsert_identifier(obj, PCODE_SYSTEM, t),
        "gers_id" => upsert_identifier(obj, GERS_SYSTEM, t),
        "settlement_type" => upsert_extension_code(obj, SETTLEMENT_TYPE_EXTENSION_URL, t),
        "delivery_strategy" => upsert_extension_code(obj, DELIVERY_STRATEGY_EXTENSION_URL, t),
        "facility_level" => upsert_type_coding(obj, FACILITY_TYPE_SYSTEM, t),
        "ownership" => upsert_type_coding(obj, OWNERSHIP_SYSTEM, t),
        "alias" => match value {
            ColumnValue::TextList(items) => {
                obj.insert("alias".into(), json!(items));
            }
            ColumnValue::Null => {
                obj.remove("alias");
            }
            _ => {}
        },
        // identifier and position_* have their own passes in `rebuild`.
        _ => {}
    }
}

/// `obj[key]` as an array, creating one (or replacing a non-array) first.
fn array_mut<'a>(obj: &'a mut Object, key: &str) -> &'a mut Vec<Value> {
    if !matches!(obj.get(key), Some(Value::Array(_))) {
        obj.insert(key.to_string(), Value::Array(Vec::new()));
    }
    obj.get_mut(key).and_then(Value::as_array_mut).expect("ensured above")
}

/// `obj[key]` as an object, creating one (or replacing a non-object) first.
fn object_mut<'a>(obj: &'a mut Object, key: &str) -> &'a mut Object {
    if !matches!(obj.get(key), Some(Value::Object(_))) {
        obj.insert(key.to_string(), Value::Object(Object::new()));
    }
    obj.get_mut(key).and_then(Value::as_object_mut).expect("ensured above")
}

/// `arr[0]` as an object, inserting an empty one at the front if needed.
fn first_object_mut(arr: &mut Vec<Value>) -> &mut Object {
    if !matches!(arr.first(), Some(Value::Object(_))) {
        arr.insert(0, Value::Object(Object::new()));
    }
    arr[0].as_object_mut().expect("ensured above")
}

fn set_string(obj: &mut Object, key: &str, v: Option<&str>) {
    match v {
        Some(s) => {
            obj.insert(key.to_string(), Value::String(s.to_string()));
        }
        None => {
            obj.remove(key);
        }
    }
}

/// `type` edits the code of the first coding of the first concept and keeps
/// its system. Clearing removes that one coding only: the facility level
/// and ownership concepts share the list.
fn set_first_type_code(obj: &mut Object, code: Option<&str>) {
    let Some(code) = code else {
        if let Some(Value::Array(types)) = obj.get_mut("type") {
            if let Some(Value::Object(first)) = types.first_mut() {
                let now_empty = match first.get_mut("coding").and_then(Value::as_array_mut) {
                    Some(codings) => {
                        if !codings.is_empty() {
                            codings.remove(0);
                        }
                        codings.is_empty()
                    }
                    None => false,
                };
                if now_empty {
                    first.remove("coding");
                }
                if first.is_empty() {
                    types.remove(0);
                }
            }
            if types.is_empty() {
                obj.remove("type");
            }
        }
        return;
    };
    let types = array_mut(obj, "type");
    let first = first_object_mut(types);
    let codings = array_mut(first, "coding");
    let coding = first_object_mut(codings);
    coding.insert("code".into(), Value::String(code.to_string()));
}

fn set_physical_type(obj: &mut Object, code: Option<&str>) {
    let Some(code) = code else {
        obj.remove("physicalType");
        return;
    };
    let concept = object_mut(obj, "physicalType");
    let coding = first_object_mut(array_mut(concept, "coding"));
    coding.insert("code".into(), Value::String(code.to_string()));
}

/// Replace the last path segment of an existing reference, so `Location/x`,
/// `http://host/fhir/Location/x` keep their shape; with no usable existing
/// reference the type is prefixed. Other fields (`display`) are kept.
fn set_reference(obj: &mut Object, key: &str, default_type: &str, id: Option<&str>) {
    let Some(id) = id else {
        obj.remove(key);
        return;
    };
    let prefix = obj
        .get(key)
        .and_then(|r| r.get("reference"))
        .and_then(Value::as_str)
        .and_then(|r| r.rfind('/').map(|i| r[..=i].to_string()));
    let reference = match prefix {
        Some(p) => format!("{p}{id}"),
        None => format!("{default_type}/{id}"),
    };
    object_mut(obj, key).insert("reference".into(), Value::String(reference));
}

fn system_is(entry: &Value, system: &str) -> bool {
    entry.get("system").and_then(Value::as_str) == Some(system)
}

fn upsert_identifier(obj: &mut Object, system: &str, value: Option<&str>) {
    let list = array_mut(obj, "identifier");
    match value {
        Some(v) => match list.iter_mut().find(|i| system_is(i, system)) {
            Some(Value::Object(i)) => {
                i.insert("value".into(), Value::String(v.to_string()));
            }
            _ => list.push(json!({"system": system, "value": v})),
        },
        None => list.retain(|i| !system_is(i, system)),
    }
    if list.is_empty() {
        obj.remove("identifier");
    }
}

fn apply_identifier_list(obj: &mut Object, value: &ColumnValue) {
    match value {
        ColumnValue::Identifiers(items) => {
            let list: Vec<Value> = items.iter().map(identifier_json).collect();
            obj.insert("identifier".into(), Value::Array(list));
        }
        ColumnValue::Null => {
            obj.remove("identifier");
        }
        _ => {}
    }
}

fn identifier_json(i: &Identifier) -> Value {
    let mut o = Object::new();
    if let Some(s) = &i.system {
        o.insert("system".into(), json!(s));
    }
    if let Some(v) = &i.value {
        o.insert("value".into(), json!(v));
    }
    Value::Object(o)
}

fn url_is(entry: &Value, url: &str) -> bool {
    entry.get("url").and_then(Value::as_str) == Some(url)
}

fn upsert_extension_code(obj: &mut Object, url: &str, code: Option<&str>) {
    let exts = array_mut(obj, "extension");
    match code {
        Some(c) => match exts.iter_mut().find(|e| url_is(e, url)) {
            Some(Value::Object(e)) => {
                e.insert("valueCode".into(), Value::String(c.to_string()));
            }
            _ => exts.push(json!({"url": url, "valueCode": c})),
        },
        None => exts.retain(|e| !url_is(e, url)),
    }
    if exts.is_empty() {
        obj.remove("extension");
    }
}

/// A coding under `system` anywhere in `Location.type`: set its code, or
/// add a concept holding it; clearing removes those codings and any
/// concept left empty.
fn upsert_type_coding(obj: &mut Object, system: &str, code: Option<&str>) {
    let types = array_mut(obj, "type");
    match code {
        Some(c) => {
            let found = types
                .iter_mut()
                .filter_map(Value::as_object_mut)
                .flat_map(|concept| {
                    concept
                        .get_mut("coding")
                        .and_then(Value::as_array_mut)
                        .into_iter()
                        .flatten()
                })
                .find(|coding| system_is(coding, system));
            match found {
                Some(Value::Object(coding)) => {
                    coding.insert("code".into(), Value::String(c.to_string()));
                }
                _ => types.push(json!({"coding": [{"system": system, "code": c}]})),
            }
        }
        None => {
            for concept in types.iter_mut().filter_map(Value::as_object_mut) {
                let now_empty = match concept.get_mut("coding").and_then(Value::as_array_mut) {
                    Some(codings) => {
                        codings.retain(|coding| !system_is(coding, system));
                        codings.is_empty()
                    }
                    None => false,
                };
                if now_empty {
                    concept.remove("coding");
                }
            }
            types.retain(|concept| concept.as_object().is_some_and(|o| !o.is_empty()));
        }
    }
    if types.is_empty() {
        obj.remove("type");
    }
}

pub fn position_of(obj: &Object) -> Option<(f64, f64)> {
    let p = obj.get("position")?.as_object()?;
    Some((p.get("longitude")?.as_f64()?, p.get("latitude")?.as_f64()?))
}

fn write_position(obj: &mut Object, lon: f64, lat: f64) {
    let pos = object_mut(obj, "position");
    pos.insert("longitude".into(), json!(lon));
    pos.insert("latitude".into(), json!(lat));
}

/// Writes the rounded position unless it rounds to what is already there,
/// so a float round trip leaves the original numbers untouched.
fn set_position(obj: &mut Object, lon: f64, lat: f64) {
    let (lon, lat) = (round_coord(lon), round_coord(lat));
    if let Some((cx, cy)) = position_of(obj) {
        if round_coord(cx) == lon && round_coord(cy) == lat {
            return;
        }
    }
    write_position(obj, lon, lat);
}

/// `position_longitude` / `position_latitude`: a missing column keeps the
/// existing coordinate; both null clears the position; a single coordinate
/// with nothing to pair it with is reported and skipped.
fn apply_position(obj: &mut Object, row: &InputRow, id: &str, report: &mut Report) {
    let lon = row.columns.get("position_longitude");
    let lat = row.columns.get("position_latitude");
    if lon.is_none() && lat.is_none() {
        return;
    }
    let current = position_of(obj);
    let pick = |col: Option<&ColumnValue>, existing: Option<f64>| match col {
        None => existing,
        Some(ColumnValue::Number(n)) => Some(*n),
        Some(_) => None,
    };
    match (
        pick(lon, current.map(|p| p.0)),
        pick(lat, current.map(|p| p.1)),
    ) {
        (Some(x), Some(y)) => set_position(obj, x, y),
        (None, None) => {
            obj.remove("position");
        }
        _ => report.add(
            "input_column_type",
            id,
            "position needs both position_longitude and position_latitude",
        ),
    }
}

fn apply_geometry(
    _obj: &mut Object,
    _id: &str,
    _snapshot: Option<&Location>,
    _geom: &geo::Geometry<f64>,
    _report: &mut Report,
) {
    // Task 6.
}
```

- [ ] **Step 4: Run the tests**

Run: `cargo test diff::rebuild 2>&1 | grep 'test result'`
Expected: `test result: ok. 9 passed`

- [ ] **Step 5: Commit**

```bash
git add src/diff/rebuild.rs src/diff/mod.rs
git commit -m "diff: apply writable columns to the snapshot resource"
```

---

## Task 6: Rebuild — the geometry rule

**Files:**
- Modify: `src/diff/rebuild.rs`

- [ ] **Step 1: Write the failing tests**

Add to the test module in `src/diff/rebuild.rs`:

```rust
    use base64::Engine;
    use geo::{line_string, polygon};

    const SQUARE: &str = r#"{"type":"Polygon","coordinates":[[[3,6],[4,6],[4,7],[3,7],[3,6]]]}"#;

    fn with_boundary(url: &str, geojson: &str) -> Value {
        let data = base64::engine::general_purpose::STANDARD.encode(geojson);
        json!({"resourceType":"Location","id":"a","name":"A","extension":[
            {"url":"https://example.org/keep","valueString":"k"},
            {"url":url,"valueAttachment":{"contentType":"application/geo+json","data":data}}]})
    }

    fn decoded_boundary(out: &Value) -> Value {
        let ext = out["extension"].as_array().unwrap().iter().find(|e| BOUNDARY_EXTENSION_URLS.contains(&e["url"].as_str().unwrap())).unwrap();
        let bytes = base64::engine::general_purpose::STANDARD.decode(ext["valueAttachment"]["data"].as_str().unwrap()).unwrap();
        serde_json::from_slice(&bytes).unwrap()
    }

    fn geom_row(geom: geo::Geometry<f64>, cols: &[(&str, ColumnValue)]) -> InputRow {
        let mut r = row(cols);
        r.geometry = Some(geom);
        r
    }

    fn square(dx: f64) -> geo::Geometry<f64> {
        geo::Geometry::Polygon(polygon![(x: 3.0 + dx, y: 6.0), (x: 4.0 + dx, y: 6.0), (x: 4.0 + dx, y: 7.0), (x: 3.0 + dx, y: 7.0), (x: 3.0 + dx, y: 6.0)])
    }

    fn apply_geom(base: Value, r: &InputRow, report: &mut Report) -> Value {
        let loc = Location::parse(&base, &mut Report::default());
        rebuild(&base, loc.as_ref(), r, report)
    }

    #[test]
    fn an_unchanged_boundary_with_float_noise_is_not_an_edit() {
        let base = with_boundary(BOUNDARY_EXTENSION_URL, SQUARE);
        let mut report = Report::default();
        let out = apply_geom(base.clone(), &geom_row(square(0.00000001), &[]), &mut report);
        assert_eq!(out, base);
        assert!(report.counts().is_empty());
    }

    #[test]
    fn a_redrawn_boundary_replaces_the_attachment_and_keeps_the_extension_url() {
        let base = with_boundary("http://hl7.org/fhir/StructureDefinition/location-boundary-geojson", SQUARE);
        let mut report = Report::default();
        let out = apply_geom(base, &geom_row(square(0.123456789), &[("name", text("B"))]), &mut report);
        assert_eq!(out["name"], "B");
        assert_eq!(out["extension"][0]["url"], "https://example.org/keep");
        assert_eq!(out["extension"][1]["url"], "http://hl7.org/fhir/StructureDefinition/location-boundary-geojson");
        assert_eq!(out["extension"][1]["valueAttachment"]["contentType"], "application/geo+json");
        let g = decoded_boundary(&out);
        assert_eq!(g["type"], "Polygon");
        assert_eq!(g["coordinates"][0][0][0], 3.1234568);
        assert!(report.counts().is_empty());
    }

    #[test]
    fn a_point_on_a_boundary_row_is_reported_and_other_edits_still_apply() {
        let base = with_boundary(BOUNDARY_EXTENSION_URL, SQUARE);
        let mut report = Report::default();
        let out = apply_geom(base.clone(), &geom_row(geo::Geometry::Point(geo::Point::new(3.5, 6.5)), &[("name", text("B"))]), &mut report);
        assert_eq!(out["name"], "B");
        assert_eq!(out["extension"], base["extension"]);
        assert!(out.get("position").is_none());
        assert_eq!(report.count("geometry_kind_changed"), 1);
    }

    #[test]
    fn a_moved_point_updates_the_position_and_an_equal_one_keeps_the_original_numbers() {
        let base = json!({"resourceType":"Location","id":"a","position":{"longitude":3.25,"latitude":6.25}});
        let mut report = Report::default();
        let out = apply_geom(base.clone(), &geom_row(geo::Geometry::Point(geo::Point::new(3.3, 6.123456789)), &[]), &mut report);
        assert_eq!(out["position"], json!({"longitude":3.3,"latitude":6.1234568}));
        assert!(out.get("extension").is_none());
        let out = apply_geom(base.clone(), &geom_row(geo::Geometry::Point(geo::Point::new(3.250000004, 6.25)), &[]), &mut report);
        assert_eq!(out, base);
        assert!(report.counts().is_empty());
    }

    #[test]
    fn when_position_columns_and_the_geometry_disagree_the_geometry_wins() {
        let base = json!({"resourceType":"Location","id":"a","position":{"longitude":3.25,"latitude":6.25}});
        let mut report = Report::default();
        let cols = [("position_longitude", ColumnValue::Number(9.0)), ("position_latitude", ColumnValue::Number(9.0))];
        let out = apply_geom(base.clone(), &geom_row(geo::Geometry::Point(geo::Point::new(3.25, 6.25)), &cols), &mut report);
        assert_eq!(out, base, "geometry equal to the snapshot restores the original numbers");
        assert_eq!(report.count("position_geometry_disagree"), 1);
        let out = apply_geom(base.clone(), &geom_row(geo::Geometry::Point(geo::Point::new(4.0, 5.0)), &cols), &mut report);
        assert_eq!(out["position"], json!({"longitude":4.0,"latitude":5.0}));
        assert_eq!(report.count("position_geometry_disagree"), 2);

        // Stale but unchanged columns next to a moved geometry: a plain move, no report.
        let stale = [("position_longitude", ColumnValue::Number(3.25)), ("position_latitude", ColumnValue::Number(6.25))];
        let out = apply_geom(base, &geom_row(geo::Geometry::Point(geo::Point::new(4.0, 5.0)), &stale), &mut report);
        assert_eq!(out["position"], json!({"longitude":4.0,"latitude":5.0}));
        assert_eq!(report.count("position_geometry_disagree"), 2);
    }

    #[test]
    fn a_polygon_on_a_point_row_adds_a_boundary() {
        let base = json!({"resourceType":"Location","id":"a","position":{"longitude":3.5,"latitude":6.5}});
        let mut report = Report::default();
        let out = apply_geom(base, &geom_row(square(0.0), &[]), &mut report);
        assert_eq!(out["position"], json!({"longitude":3.5,"latitude":6.5}));
        assert_eq!(out["extension"][0]["url"], BOUNDARY_EXTENSION_URL);
        assert_eq!(decoded_boundary(&out)["type"], "Polygon");
        assert!(report.counts().is_empty());
    }

    #[test]
    fn unsupported_and_invalid_geometries_are_reported() {
        let base = json!({"resourceType":"Location","id":"a"});
        let mut report = Report::default();
        let line = geo::Geometry::LineString(line_string![(x: 0.0, y: 0.0), (x: 1.0, y: 1.0)]);
        let out = apply_geom(base.clone(), &geom_row(line, &[]), &mut report);
        assert!(out.get("extension").is_none());
        assert_eq!(report.count("geometry_unparseable"), 1);

        let bowtie = geo::Geometry::Polygon(polygon![(x: 0.0, y: 0.0), (x: 2.0, y: 2.0), (x: 2.0, y: 0.0), (x: 0.0, y: 2.0), (x: 0.0, y: 0.0)]);
        let out = apply_geom(base, &geom_row(bowtie, &[]), &mut report);
        assert_eq!(decoded_boundary(&out)["type"], "Polygon", "written as is");
        assert_eq!(report.count("geometry_invalid"), 1);
    }
```

Add these imports at the top of the test module, next to `use super::*;` (the geo macros must be imported by name, since `polygon!` expands to other geo macros):

```rust
    use geo::{line_string, polygon};

    use crate::fhir::location::{BOUNDARY_EXTENSION_URL, BOUNDARY_EXTENSION_URLS};
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `cargo test diff::rebuild 2>&1 | grep -E 'test result|panicked' | head -5`
Expected: several of the new tests fail (the geometry stub does nothing).

- [ ] **Step 3: Implement the geometry rule**

Replace the `apply_geometry` stub with:

```rust
/// The attachment as diff writes it: inline base64 of the bare GeoJSON
/// geometry with rounded coordinates, the shape the fixture and the
/// Python wrote.
pub fn boundary_attachment(geom: &Geometry<f64>) -> Value {
    let value = geojson::GeometryValue::from(&round_geometry(geom));
    let text = serde_json::to_string(&geojson::Geometry::new(value))
        .expect("a GeoJSON geometry serialises");
    json!({
        "contentType": GEOJSON_CONTENT_TYPE,
        "data": base64::engine::general_purpose::STANDARD.encode(text),
    })
}

fn replace_boundary(obj: &mut Object, geom: &Geometry<f64>) {
    let attachment = boundary_attachment(geom);
    let exts = array_mut(obj, "extension");
    let existing = exts.iter_mut().find(|e| {
        e.get("url")
            .and_then(Value::as_str)
            .is_some_and(|u| BOUNDARY_EXTENSION_URLS.contains(&u))
    });
    match existing {
        Some(Value::Object(e)) => {
            e.insert("valueAttachment".into(), attachment);
        }
        _ => exts.push(json!({"url": BOUNDARY_EXTENSION_URL, "valueAttachment": attachment})),
    }
}

fn check_validity(geom: &Geometry<f64>, id: &str, report: &mut Report) {
    if let Err(reason) = validity::check(geom) {
        report.add("geometry_invalid", id, &format!("{reason}; written as is"));
    }
}

/// The geometry edits what it came from: the boundary attachment when the
/// snapshot resource has a decoded boundary, the position otherwise.
fn apply_geometry(
    obj: &mut Object,
    id: &str,
    snapshot: Option<&Location>,
    geom: &Geometry<f64>,
    report: &mut Report,
) {
    let is_point = matches!(geom, Geometry::Point(_));
    let is_polygon = matches!(geom, Geometry::Polygon(_) | Geometry::MultiPolygon(_));
    if !is_point && !is_polygon {
        report.add(
            "geometry_unparseable",
            id,
            &format!("unsupported geometry type {}", kind_name(geom)),
        );
        return;
    }
    let old_boundary = snapshot.and_then(|l| match &l.boundary {
        Some(Boundary::Inline(bytes)) => parse_boundary(bytes, id, &mut Report::default()),
        _ => None,
    });
    if let Some(old) = old_boundary {
        if is_point {
            report.add(
                "geometry_kind_changed",
                id,
                "a point was supplied for a Location with a boundary; geometry edit skipped",
            );
            return;
        }
        if wkb_key(&old) == wkb_key(geom) {
            return;
        }
        check_validity(geom, id, report);
        replace_boundary(obj, geom);
        return;
    }
    if is_polygon {
        check_validity(geom, id, report);
        replace_boundary(obj, geom);
        return;
    }
    let Geometry::Point(p) = geom else {
        unreachable!("is_point checked above")
    };
    let (x, y) = (round_coord(p.x()), round_coord(p.y()));
    // `apply_position` has already run: if the position columns moved the
    // point somewhere other than where the geometry puts it, both were
    // edited and they disagree. Stale-but-unchanged columns (every GIS
    // export carries them) are not an edit and never report.
    let original = snapshot.and_then(|l| l.position);
    let after_columns = position_of(obj);
    let rounded = |p: Option<(f64, f64)>| p.map(|(a, b)| (round_coord(a), round_coord(b)));
    if rounded(after_columns) != rounded(original) {
        if let Some((cx, cy)) = after_columns {
            if round_coord(cx) != x || round_coord(cy) != y {
                report.add(
                    "position_geometry_disagree",
                    id,
                    "position columns and the geometry both changed and differ; the geometry was used",
                );
            }
        }
    }
    match original {
        // Equal to the snapshot after rounding: restore its exact numbers.
        Some((ox, oy)) if round_coord(ox) == x && round_coord(oy) == y => write_position(obj, ox, oy),
        _ => write_position(obj, x, y),
    }
}
```

Add to the imports at the top of the file:

```rust
use base64::Engine;
use geo::Geometry;

use crate::diff::compare::{round_geometry, wkb_key};
use crate::fhir::location::{BOUNDARY_EXTENSION_URL, BOUNDARY_EXTENSION_URLS, GEOJSON_CONTENT_TYPE};
use crate::fhir::Boundary;
use crate::geometry::{kind_name, parse_boundary, validity};
```

(merge the `crate::diff::compare` and `crate::fhir::location` lines with the existing ones).

- [ ] **Step 4: Run the tests**

Run: `cargo test diff::rebuild 2>&1 | grep 'test result'`
Expected: `test result: ok. 16 passed`

- [ ] **Step 5: Commit**

```bash
git add src/diff/rebuild.rs
git commit -m "diff: geometry edits go to the boundary or the position"
```

---

## Task 7: The diff command

**Files:**
- Modify: `src/diff/mod.rs`
- Create: `tests/diff.rs`

- [ ] **Step 1: Write the failing binary tests**

Create `tests/diff.rs`:

```rust
//! Binary-level tests for `kiln diff` against a small hand-made snapshot.

use std::path::{Path, PathBuf};
use std::process::Command;

use assert_cmd::assert::Assert;
use assert_cmd::prelude::*;
use base64::Engine;
use serde_json::{json, Value};

const BOUNDARY_EXT: &str =
    "https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson";
const PCODE: &str = "https://icr.healthcampaigns.org/identifiers/pcode";
const SQUARE: &str = r#"{"type":"Polygon","coordinates":[[[3,6],[9,6],[9,12],[3,12],[3,6]]]}"#;

fn b64(s: &str) -> String {
    base64::engine::general_purpose::STANDARD.encode(s)
}

fn decode(s: &str) -> Value {
    let bytes = base64::engine::general_purpose::STANDARD.decode(s).unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

fn ng() -> Value {
    json!({
        "resourceType": "Location", "id": "ng",
        "meta": {"versionId": "1", "lastUpdated": "2026-01-01T00:00:00Z"},
        "name": "Nigeria", "status": "active",
        "type": [{"coding": [{"code": "admin-unit"}]}],
        "identifier": [{"system": PCODE, "value": "NG"}],
        "extension": [
            {"url": "https://example.org/keep", "valueString": "k"},
            {"url": BOUNDARY_EXT, "valueAttachment": {"contentType": "application/geo+json", "data": b64(SQUARE)}}
        ]
    })
}

fn clinic() -> Value {
    json!({
        "resourceType": "Location", "id": "clinic",
        "meta": {"versionId": "2"},
        "name": "Gama Clinic", "status": "active",
        "type": [{"coding": [{"code": "facility"}]}],
        "partOf": {"reference": "Location/ng"},
        "position": {"longitude": 3.25, "latitude": 6.25}
    })
}

/// Writes the two-resource snapshot and returns its directory.
fn snapshot(dir: &Path) -> PathBuf {
    let snap = dir.join("snapshot");
    std::fs::create_dir_all(&snap).unwrap();
    std::fs::write(snap.join("locations.ndjson"), format!("{}\n{}\n", ng(), clinic())).unwrap();
    snap
}

fn feature(props: Value, geometry: Value) -> Value {
    json!({"type": "Feature", "properties": props, "geometry": geometry})
}

fn collection(features: Vec<Value>) -> String {
    json!({"type": "FeatureCollection", "features": features}).to_string()
}

fn square_geometry(noise: f64) -> Value {
    json!({"type": "Polygon", "coordinates": [[[3.0 + noise, 6], [9, 6], [9, 12], [3, 12], [3.0 + noise, 6]]]})
}

fn point(lon: f64, lat: f64) -> Value {
    json!({"type": "Point", "coordinates": [lon, lat]})
}

/// The rows a GIS export of the snapshot would carry, unedited, with float
/// noise in the geometry and stale derived columns.
fn export() -> Vec<Value> {
    vec![
        feature(
            json!({"id": "ng", "name": "Nigeria", "status": "active", "type": "admin-unit", "pcode": "NG",
                   "identifier": "[{\"system\":\"https://icr.healthcampaigns.org/identifiers/pcode\",\"value\":\"NG\"}]",
                   "admin0_name": "Nigeria", "tier": "0", "version_id": "1", "part_of": null,
                   "position_longitude": null, "position_latitude": null}),
            square_geometry(0.00000001),
        ),
        feature(
            json!({"id": "clinic", "name": "Gama Clinic", "status": "active", "type": "facility",
                   "part_of": "ng", "admin0_name": "Nigeria", "tier": "site", "version_id": "2",
                   "position_longitude": 3.25, "position_latitude": 6.25}),
            point(3.250000001, 6.25),
        ),
    ]
}

fn write_input(dir: &Path, name: &str, text: &str) -> PathBuf {
    let p = dir.join(name);
    std::fs::write(&p, text).unwrap();
    p
}

fn diff(snap: &Path, input: &Path, out: &Path) -> Assert {
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["diff", "--snapshot"])
        .arg(snap)
        .arg("--in")
        .arg(input)
        .arg("--out")
        .arg(out)
        .arg("--report")
        .arg(out.with_extension("report.json"))
        .assert()
}

fn stdout(a: &Assert) -> String {
    String::from_utf8_lossy(&a.get_output().stdout).into_owned()
}

fn changes(out: &Path) -> Vec<Value> {
    std::fs::read_to_string(out)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

fn report(out: &Path) -> Value {
    serde_json::from_str(&std::fs::read_to_string(out.with_extension("report.json")).unwrap()).unwrap()
}

#[test]
fn an_unedited_export_yields_no_changes() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let input = write_input(dir.path(), "edits.geojson", &collection(export()));
    let out = dir.path().join("changes.ndjson");
    let a = diff(&snap, &input, &out).success();
    assert!(stdout(&a).contains("0 changed, 2 unchanged, 0 new"), "{}", stdout(&a));
    assert_eq!(changes(&out).len(), 0);
    assert_eq!(report(&out)["counts"], json!({}));
}

#[test]
fn a_rename_emits_one_complete_resource_with_its_version() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let mut rows = export();
    rows[1]["properties"]["name"] = json!("Gama PHC");
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    let a = diff(&snap, &input, &out).success();
    assert!(stdout(&a).contains("1 changed, 1 unchanged, 0 new"), "{}", stdout(&a));
    let got = changes(&out);
    assert_eq!(got.len(), 1);
    let r = &got[0];
    assert_eq!(r["id"], "clinic");
    assert_eq!(r["name"], "Gama PHC");
    assert_eq!(r["meta"]["versionId"], "2");
    assert_eq!(r["partOf"]["reference"], "Location/ng");
    assert_eq!(r["position"], json!({"longitude": 3.25, "latitude": 6.25}));
    assert_eq!(r["type"], clinic()["type"]);
}

#[test]
fn stale_derived_columns_never_cause_a_change() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let mut rows = export();
    rows[1]["properties"]["admin0_name"] = json!("Wrong");
    rows[1]["properties"]["path"] = json!("/x/y");
    rows[1]["properties"]["tier"] = json!("4");
    rows[1]["properties"]["version_id"] = json!("99");
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    let a = diff(&snap, &input, &out).success();
    assert!(stdout(&a).contains("0 changed, 2 unchanged, 0 new"), "{}", stdout(&a));
}

#[test]
fn null_clears_lists_round_trip_and_unknown_content_survives() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let mut rows = export();
    rows[0]["properties"]["status"] = Value::Null;
    rows[0]["properties"]["alias"] = json!(["Naija"]);
    rows[0]["properties"]["identifier"] = json!(format!("[{{\"system\":\"{PCODE}\",\"value\":\"NG2\"}}]"));
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    diff(&snap, &input, &out).success();
    let got = changes(&out);
    assert_eq!(got.len(), 1);
    let r = &got[0];
    assert_eq!(r["id"], "ng");
    assert!(r.get("status").is_none());
    assert_eq!(r["alias"], json!(["Naija"]));
    assert_eq!(r["identifier"], json!([{"system": PCODE, "value": "NG"}]),
        "pcode column, still NG, upserts after the identifier list is replaced");
    assert_eq!(r["extension"][0], json!({"url": "https://example.org/keep", "valueString": "k"}));
    assert_eq!(r["extension"][1]["valueAttachment"]["data"], b64(SQUARE), "boundary bytes untouched");
    assert_eq!(r["meta"]["versionId"], "1");
}

#[test]
fn usage_errors_exit_2() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let out = dir.path().join("changes.ndjson");
    let csv = write_input(dir.path(), "edits.csv", "id,name\n");
    diff(&snap, &csv, &out)
        .failure()
        .code(2)
        .stderr(predicates::str::contains(".parquet"));
    let input = write_input(dir.path(), "edits.geojson", &collection(export()));
    diff(Path::new("/nonexistent"), &input, &out)
        .failure()
        .code(2)
        .stderr(predicates::str::contains("locations.ndjson"));
    diff(&snap, Path::new("/nonexistent/edits.geojson"), &out)
        .failure()
        .code(2)
        .stderr(predicates::str::contains("--in"));
    diff(&snap, &input, dir.path())
        .failure()
        .code(2)
        .stderr(predicates::str::contains("--out"));
}

#[test]
fn a_failed_diff_leaves_no_partial_output() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let input = write_input(dir.path(), "edits.geojson", "[1, 2]");
    let out = dir.path().join("changes.ndjson");
    diff(&snap, &input, &out).failure().code(2);
    assert!(!out.exists());
    assert!(!dir.path().join("changes.ndjson.tmp").exists());
}
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `cargo test --test diff 2>&1 | grep 'test result'`
Expected: `test result: FAILED` with every test failing on the "not implemented" stub.

- [ ] **Step 3: Implement run_diff**

Replace the whole of `src/diff/mod.rs` with:

```rust
//! `kiln diff`: edited GeoJSON or GeoParquet -> FHIR NDJSON of the Locations
//! that changed. Offline. The snapshot is indexed by id and byte offset;
//! the input is streamed; each named resource is re-read, rebuilt from the
//! writable columns, and emitted only when its canonical JSON differs.

pub mod compare;
pub mod geojson;
pub mod input;
pub mod parquet;
pub mod rebuild;

use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::cli::DiffArgs;
use crate::diff::compare::canonical;
use crate::diff::geojson::{feature_to_row, read_feature_collection, read_feature_lines};
use crate::diff::input::InputRow;
use crate::diff::parquet::read_geoparquet;
use crate::diff::rebuild::rebuild;
use crate::error::{KilnError, Result};
use crate::fhir::ndjson::{LineAccess, NdjsonReader};
use crate::fhir::Location;
use crate::report::Report;
use crate::transform::{write_report, SNAPSHOT_FILE};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputFormat {
    FeatureCollection,
    FeatureLines,
    GeoParquet,
}

impl InputFormat {
    pub fn from_path(path: &Path) -> Result<Self> {
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| e.to_ascii_lowercase())
            .unwrap_or_default();
        match ext.as_str() {
            "geojson" | "json" => Ok(Self::FeatureCollection),
            "geojsonl" | "geojsons" => Ok(Self::FeatureLines),
            "parquet" => Ok(Self::GeoParquet),
            _ => Err(KilnError::Usage(format!(
                "--in {}: unknown extension; expected .geojson, .json, .geojsonl, .geojsons or .parquet",
                path.display()
            ))),
        }
    }
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct DiffStats {
    pub changed: usize,
    pub unchanged: usize,
    pub created: usize,
}

/// `id -> (offset, len)` for every resource in the snapshot. Nothing else
/// is retained; a resource is re-read only when an input row names it.
pub fn index_snapshot(path: &Path, report: &mut Report) -> Result<HashMap<String, (u64, usize)>> {
    let mut index = HashMap::new();
    for line in NdjsonReader::open(path)? {
        let line = line?;
        let id = serde_json::from_str::<Value>(&line.text)
            .ok()
            .and_then(|v| v.get("id")?.as_str().map(str::to_string));
        match id {
            Some(id) => {
                if index.contains_key(&id) {
                    report.add(
                        "duplicate_id",
                        &id,
                        &format!("snapshot line {} repeats an earlier id; first kept", line.number),
                    );
                } else {
                    index.insert(id, (line.offset, line.len));
                }
            }
            None => report.add(
                "snapshot_line_unparsed",
                "<unknown>",
                &format!("snapshot line {} is not a resource with an id", line.number),
            ),
        }
    }
    Ok(index)
}

struct Diff<'a> {
    index: HashMap<String, (u64, usize)>,
    access: LineAccess,
    out: BufWriter<File>,
    out_path: &'a Path,
    seen: HashSet<String>,
    stats: DiffStats,
}

impl Diff<'_> {
    fn process(&mut self, row: InputRow, report: &mut Report) -> Result<()> {
        let (id, generated) = match row.id.clone() {
            Some(id) => (id, false),
            None => (uuid::Uuid::new_v4().to_string(), true),
        };
        if !self.seen.insert(id.clone()) {
            report.add(
                "duplicate_id",
                &id,
                &format!("input row {} repeats an earlier row; skipped", row.line),
            );
            return Ok(());
        }
        let existing = self.index.get(&id).copied();
        let (base, snapshot) = match existing {
            Some((offset, len)) => {
                let text = self.access.read_at(offset, len)?;
                let base: Value = serde_json::from_str(&text)?;
                let loc = Location::parse(&base, &mut Report::default());
                (base, loc)
            }
            None => {
                if generated {
                    report.add(
                        "new_location_generated_id",
                        &id,
                        &format!("input row {} has no id; assigned {id}", row.line),
                    );
                } else {
                    report.add(
                        "new_location",
                        &id,
                        &format!("input row {}: id not in the snapshot; emitted as a create", row.line),
                    );
                }
                (json!({"resourceType": "Location", "id": id}), None)
            }
        };
        let rebuilt = rebuild(&base, snapshot.as_ref(), &row, report);
        if existing.is_some() && canonical(&rebuilt) == canonical(&base) {
            self.stats.unchanged += 1;
            return Ok(());
        }
        writeln!(self.out, "{rebuilt}").map_err(|e| KilnError::io(self.out_path, e))?;
        if existing.is_some() {
            self.stats.changed += 1;
        } else {
            self.stats.created += 1;
        }
        Ok(())
    }
}

fn read_input(format: InputFormat, input: &Path, diff: &mut Diff, report: &mut Report) -> Result<()> {
    match format {
        InputFormat::FeatureCollection => read_feature_collection(input, |f, i| {
            let row = feature_to_row(f, i, report);
            diff.process(row, report)
        }),
        InputFormat::FeatureLines => read_feature_lines(input, |f, i| {
            let row = feature_to_row(f, i, report);
            diff.process(row, report)
        }),
        InputFormat::GeoParquet => read_geoparquet(input, report, |row, report| diff.process(row, report)),
    }
}

fn tmp_path(out: &Path) -> PathBuf {
    let mut s = out.as_os_str().to_owned();
    s.push(".tmp");
    PathBuf::from(s)
}

pub fn run_diff(args: &DiffArgs) -> Result<()> {
    let ndjson = args.snapshot.join(SNAPSHOT_FILE);
    if !ndjson.is_file() {
        return Err(KilnError::Usage(format!(
            "snapshot file not found: {}",
            ndjson.display()
        )));
    }
    let format = InputFormat::from_path(&args.input)?;
    if !args.input.is_file() {
        return Err(KilnError::Usage(format!(
            "--in {}: not a file",
            args.input.display()
        )));
    }
    if args.out.is_dir() {
        return Err(KilnError::Usage(format!(
            "--out {}: is a directory",
            args.out.display()
        )));
    }

    let mut report = Report::default();
    let index = index_snapshot(&ndjson, &mut report)?;
    eprintln!("indexed {} snapshot resources", index.len());

    let tmp = tmp_path(&args.out);
    let file = File::create(&tmp)
        .map_err(|e| KilnError::Usage(format!("--out {}: {e}", tmp.display())))?;
    let mut diff = Diff {
        index,
        access: LineAccess::open(&ndjson)?,
        out: BufWriter::new(file),
        out_path: &tmp,
        seen: HashSet::new(),
        stats: DiffStats::default(),
    };
    let outcome = read_input(format, &args.input, &mut diff, &mut report).and_then(|()| {
        diff.out.flush().map_err(|e| KilnError::io(&tmp, e))?;
        let file = diff.out.into_inner().map_err(|e| KilnError::io(&tmp, e.into_error()))?;
        file.sync_all().map_err(|e| KilnError::io(&tmp, e))?;
        std::fs::rename(&tmp, &args.out).map_err(|e| KilnError::io(&args.out, e))
    });
    if let Err(e) = outcome {
        let _ = std::fs::remove_file(&tmp);
        return Err(e);
    }

    println!(
        "{} changed, {} unchanged, {} new -> {}",
        diff.stats.changed,
        diff.stats.unchanged,
        diff.stats.created,
        args.out.display()
    );
    if let Some(path) = &args.report {
        write_report(path, &report)?;
    }
    println!("{}", report.summary());
    Ok(())
}
```

If the compiler complains that `diff.out` is moved inside the closure while `diff` is still borrowed, restructure: run `read_input` first into `let read = ...;`, then `match read { Ok(()) => finish(diff.out, &tmp, &args.out), Err(e) => ... }` with `fn finish(out: BufWriter<File>, tmp: &Path, dest: &Path) -> Result<()>` holding the flush/sync/rename lines. Keep the cleanup of `tmp` on any error.

- [ ] **Step 4: Run the tests**

Run: `cargo test --test diff 2>&1 | grep 'test result'`
Expected: `test result: ok. 6 passed`

Run: `cargo test 2>&1 | grep -E 'test result|warning' | sort | uniq -c`
Expected: every suite ok, no warnings.

- [ ] **Step 5: Commit**

```bash
git add src/diff/mod.rs tests/diff.rs
git commit -m "diff: the command, with atomic output and binary tests"
```

---

## Task 8: diff — geometry, creates, GeoParquet and line inputs

**Files:**
- Modify: `tests/diff.rs`

- [ ] **Step 1: Write the tests**

Append to `tests/diff.rs`:

```rust
#[test]
fn moving_a_point_updates_the_position() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let mut rows = export();
    rows[1]["geometry"] = point(3.3, 6.123456789);
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    diff(&snap, &input, &out).success();
    let got = changes(&out);
    assert_eq!(got.len(), 1);
    assert_eq!(got[0]["position"], json!({"longitude": 3.3, "latitude": 6.1234568}));
    assert!(got[0].get("extension").is_none());
    assert_eq!(report(&out)["counts"], json!({}), "stale position columns are not a disagreement");
}

#[test]
fn drawing_a_polygon_on_a_point_row_adds_a_boundary() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let mut rows = export();
    rows[1]["geometry"] = json!({"type": "Polygon", "coordinates": [[[3, 6], [4, 6], [4, 7], [3, 7], [3, 6]]]});
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    diff(&snap, &input, &out).success();
    let got = changes(&out);
    assert_eq!(got.len(), 1);
    let r = &got[0];
    assert_eq!(r["position"], json!({"longitude": 3.25, "latitude": 6.25}));
    assert_eq!(r["extension"][0]["url"], BOUNDARY_EXT);
    assert_eq!(r["extension"][0]["valueAttachment"]["contentType"], "application/geo+json");
    let g = decode(r["extension"][0]["valueAttachment"]["data"].as_str().unwrap());
    assert_eq!(g["type"], "Polygon");
    assert_eq!(g["coordinates"][0].as_array().unwrap().len(), 5);
}

#[test]
fn a_point_on_a_boundary_row_is_reported_and_skipped() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let mut rows = export();
    rows[0]["geometry"] = point(5.0, 8.0);
    rows[0]["properties"]["name"] = json!("Federal Republic of Nigeria");
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    diff(&snap, &input, &out).success();
    let got = changes(&out);
    assert_eq!(got.len(), 1);
    assert_eq!(got[0]["name"], "Federal Republic of Nigeria");
    assert_eq!(got[0]["extension"][1]["valueAttachment"]["data"], b64(SQUARE));
    assert!(got[0].get("position").is_none());
    assert_eq!(report(&out)["counts"]["geometry_kind_changed"], 1);
}

#[test]
fn a_redrawn_boundary_replaces_the_attachment_with_rounded_coordinates() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let mut rows = export();
    rows[0]["geometry"] = square_geometry(0.123456789);
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    diff(&snap, &input, &out).success();
    let got = changes(&out);
    assert_eq!(got.len(), 1);
    let r = &got[0];
    assert_eq!(r["extension"][0]["url"], "https://example.org/keep");
    assert_eq!(r["extension"][1]["url"], BOUNDARY_EXT);
    let g = decode(r["extension"][1]["valueAttachment"]["data"].as_str().unwrap());
    assert_eq!(g["coordinates"][0][0][0], 3.1234568);
    assert_eq!(r["meta"]["versionId"], "1");
}

#[test]
fn new_rows_become_creates_with_and_without_an_id() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let rows = vec![
        feature(json!({"id": "newsite", "name": "New Site", "part_of": "ng", "type": "facility"}), point(4.0, 7.0)),
        feature(json!({"name": "Nameless", "pcode": "NG9"}), point(4.5, 7.5)),
    ];
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    let a = diff(&snap, &input, &out).success();
    assert!(stdout(&a).contains("0 changed, 0 unchanged, 2 new"), "{}", stdout(&a));
    let got = changes(&out);
    assert_eq!(got.len(), 2);
    assert_eq!(got[0]["id"], "newsite");
    assert_eq!(got[0]["resourceType"], "Location");
    assert!(got[0].get("meta").is_none());
    assert_eq!(got[0]["partOf"]["reference"], "Location/ng");
    assert_eq!(got[0]["type"], json!([{"coding": [{"code": "facility"}]}]));
    assert_eq!(got[0]["position"], json!({"longitude": 4.0, "latitude": 7.0}));
    let generated = got[1]["id"].as_str().unwrap();
    assert_eq!(generated.len(), 36, "a UUID: {generated}");
    assert_eq!(got[1]["identifier"], json!([{"system": PCODE, "value": "NG9"}]));
    let counts = &report(&out)["counts"];
    assert_eq!(counts["new_location"], 1);
    assert_eq!(counts["new_location_generated_id"], 1);
}

#[test]
fn duplicate_input_rows_are_reported_and_skipped() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let mut rows = export();
    let mut again = rows[1].clone();
    again["properties"]["name"] = json!("Second");
    rows[1]["properties"]["name"] = json!("First");
    rows.push(again);
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    diff(&snap, &input, &out).success();
    let got = changes(&out);
    assert_eq!(got.len(), 1);
    assert_eq!(got[0]["name"], "First");
    assert_eq!(report(&out)["counts"]["duplicate_id"], 1);
}

#[test]
fn feature_lines_input_is_accepted() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let mut rows = export();
    rows[1]["properties"]["name"] = json!("Gama PHC");
    let text = rows.iter().map(|r| r.to_string()).collect::<Vec<_>>().join("\n");
    let input = write_input(dir.path(), "edits.geojsonl", &text);
    let out = dir.path().join("changes.ndjson");
    let a = diff(&snap, &input, &out).success();
    assert!(stdout(&a).contains("1 changed, 1 unchanged, 0 new"), "{}", stdout(&a));
}

fn parquet_files(dir: &Path, into: &mut Vec<PathBuf>) {
    for entry in std::fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            parquet_files(&path, into);
        } else if path.extension().is_some_and(|e| e == "parquet") {
            into.push(path);
        }
    }
}

#[test]
fn geoparquet_written_by_transform_round_trips_unchanged() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let dataset = dir.path().join("out");
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["transform", "--snapshot"])
        .arg(&snap)
        .arg("--out")
        .arg(&dataset)
        .assert()
        .success();
    let mut files = Vec::new();
    parquet_files(&dataset.join("locations"), &mut files);
    assert_eq!(files.len(), 2, "one polygon and one point partition: {files:?}");
    for (i, file) in files.iter().enumerate() {
        let out = dir.path().join(format!("changes-{i}.ndjson"));
        let a = diff(&snap, file, &out).success();
        assert!(stdout(&a).contains("0 changed, 1 unchanged, 0 new"), "{}: {}", file.display(), stdout(&a));
        assert_eq!(report(&out)["counts"], json!({}), "{}", file.display());
    }
}
```

- [ ] **Step 2: Run the tests**

Run: `cargo test --test diff 2>&1 | grep -E 'test result|panicked|FAILED'`
Expected: `test result: ok. 14 passed`. If `geoparquet_written_by_transform_round_trips_unchanged` fails on a column, print the failing row's rebuilt JSON against the snapshot line and fix the coercion or the rebuild rule, not the test: the transform's own output must diff clean.

- [ ] **Step 3: Commit**

```bash
git add tests/diff.rs
git commit -m "diff: geometry, create, GeoParquet and line-input tests"
```

---

## Task 9: load — ordering, bundles, capability check

**Files:**
- Create: `src/load/order.rs`, `src/load/bundle.rs`, `src/load/capability.rs`
- Modify: `src/load/mod.rs`

- [ ] **Step 1: Write the failing tests**

Create `src/load/order.rs`:

```rust
//! Parents first: a resource is loaded after the `partOf` parent that is
//! also in this set. References to resources not in the set count as
//! roots. Stable, so siblings keep their input order.

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn loc(id: &str, parent: Option<&str>) -> Value {
        let mut r = json!({"resourceType": "Location", "id": id});
        if let Some(p) = parent {
            r["partOf"] = json!({"reference": format!("Location/{p}")});
        }
        r
    }

    fn ids(v: &[Value]) -> Vec<&str> {
        v.iter().map(|r| r["id"].as_str().unwrap()).collect()
    }

    #[test]
    fn children_come_after_their_ancestors() {
        let input = vec![loc("clinic", Some("ward")), loc("ward", Some("state")), loc("state", None), loc("other", Some("state"))];
        let out = order_parents_first(input).unwrap();
        assert_eq!(ids(&out), vec!["state", "ward", "other", "clinic"]);
    }

    #[test]
    fn external_parents_count_as_roots() {
        let input = vec![loc("b", Some("a")), loc("c", Some("not-here"))];
        let out = order_parents_first(input).unwrap();
        assert_eq!(ids(&out), vec!["b", "c"]);
    }

    #[test]
    fn a_cycle_is_a_usage_error() {
        let input = vec![loc("a", Some("b")), loc("b", Some("a"))];
        let err = order_parents_first(input).unwrap_err();
        assert!(matches!(err, KilnError::Usage(m) if m.contains("cycle") && m.contains("Location/")), "{err}");
    }

    #[test]
    fn resources_without_part_of_or_of_other_types_are_roots() {
        let input = vec![json!({"resourceType": "Organization", "id": "o"}), loc("a", Some("o"))];
        let out = order_parents_first(input).unwrap();
        assert_eq!(ids(&out), vec!["o", "a"]);
    }
}
```

Create `src/load/bundle.rs`:

```rust
//! Transaction bundles of `PUT <Type>/<id>`, `ifMatch` on every entry that
//! has a version to check.

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn if_match_is_a_weak_etag() {
        assert_eq!(if_match("3"), "W/\"3\"");
    }

    #[test]
    fn entries_are_chunked_in_order_with_if_match_only_for_existing_resources() {
        let resources = vec![
            json!({"resourceType": "Location", "id": "a", "meta": {"versionId": "1"}}),
            json!({"resourceType": "Location", "id": "b"}),
            json!({"resourceType": "Organization", "id": "o", "meta": {"versionId": "7"}}),
        ];
        let bundles = plan(resources, 2);
        assert_eq!(bundles.len(), 2);
        assert_eq!(bundles[0].entries.len(), 2);
        assert_eq!(bundles[1].entries.len(), 1);
        let json = bundles[0].to_json();
        assert_eq!(json["resourceType"], "Bundle");
        assert_eq!(json["type"], "transaction");
        assert_eq!(json["entry"][0]["request"], json!({"method": "PUT", "url": "Location/a", "ifMatch": "W/\"1\""}));
        assert_eq!(json["entry"][1]["request"], json!({"method": "PUT", "url": "Location/b"}));
        assert_eq!(json["entry"][0]["resource"]["id"], "a");
        assert_eq!(bundles[1].to_json()["entry"][0]["request"]["url"], "Organization/o");
        assert_eq!(
            bundles[0].describe(),
            "Location/a update@1, Location/b create"
        );
    }
}
```

Create `src/load/capability.rs`:

```rust
//! `PUT` to a new id needs update-as-create. One clear preflight error
//! beats hundreds of identical 404s.

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn capability(types: &[(&str, bool)]) -> Value {
        json!({"resourceType": "CapabilityStatement", "rest": [{"resource":
            types.iter().map(|(t, u)| json!({"type": t, "updateCreate": u})).collect::<Vec<_>>()}]})
    }

    #[test]
    fn collects_the_types_that_advertise_update_create() {
        let got = update_create_types(&capability(&[("Location", true), ("Organization", false)]));
        assert!(got.contains("Location"));
        assert!(!got.contains("Organization"));
        assert!(update_create_types(&json!({"resourceType": "OperationOutcome"})).is_empty());
    }

    #[test]
    fn refuses_only_when_a_create_needs_a_missing_type() {
        let cap = capability(&[("Location", false)]);
        assert!(check_update_create(&cap, &[]).is_ok());
        let err = check_update_create(&cap, &["Location".to_string()]).unwrap_err();
        assert!(matches!(err, KilnError::Usage(m) if m.contains("updateCreate") && m.contains("Location")), "{err}");
        assert!(check_update_create(&capability(&[("Location", true)]), &["Location".to_string()]).is_ok());
    }
}
```

In `src/load/mod.rs` add after the doc comment:

```rust
pub mod bundle;
pub mod capability;
pub mod order;
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `cargo test load:: 2>&1 | grep -c 'cannot find'`
Expected: non-zero.

- [ ] **Step 3: Implement ordering**

Insert above the test module in `src/load/order.rs`:

```rust
use std::collections::HashMap;

use serde_json::Value;

use crate::error::{KilnError, Result};

fn parent_id(resource: &Value) -> Option<&str> {
    let reference = resource.get("partOf")?.get("reference")?.as_str()?;
    let last = reference.rsplit('/').next().unwrap_or(reference);
    (!last.is_empty()).then_some(last)
}

/// Depth of `start` within the set: 0 for a root, parent depth + 1
/// otherwise. Memoised in `depths`; a cycle is a usage error.
fn depth(
    start: usize,
    resources: &[Value],
    by_id: &HashMap<&str, usize>,
    depths: &mut Vec<Option<usize>>,
) -> Result<usize> {
    let mut chain: Vec<usize> = Vec::new();
    let mut current = Some(start);
    let mut base = 0usize;
    while let Some(i) = current {
        if let Some(d) = depths[i] {
            base = d + 1;
            break;
        }
        if chain.contains(&i) {
            let id = resources[i]["id"].as_str().unwrap_or("?");
            return Err(KilnError::Usage(format!(
                "partOf cycle detected involving Location/{id}; fix the hierarchy before loading"
            )));
        }
        chain.push(i);
        current = parent_id(&resources[i]).and_then(|p| by_id.get(p).copied());
    }
    for (offset, &i) in chain.iter().rev().enumerate() {
        depths[i] = Some(base + offset);
    }
    Ok(depths[start].expect("set above"))
}

pub fn order_parents_first(resources: Vec<Value>) -> Result<Vec<Value>> {
    let by_id: HashMap<&str, usize> = resources
        .iter()
        .enumerate()
        .filter_map(|(i, r)| r.get("id")?.as_str().map(|id| (id, i)))
        .collect();
    let mut depths = vec![None; resources.len()];
    let mut keyed: Vec<(usize, usize)> = Vec::with_capacity(resources.len());
    for i in 0..resources.len() {
        keyed.push((depth(i, &resources, &by_id, &mut depths)?, i));
    }
    keyed.sort_by_key(|&(d, i)| (d, i));
    let mut slots: Vec<Option<Value>> = resources.into_iter().map(Some).collect();
    Ok(keyed
        .into_iter()
        .map(|(_, i)| slots[i].take().expect("each index once"))
        .collect())
}
```

- [ ] **Step 4: Implement bundles**

Insert above the test module in `src/load/bundle.rs`:

```rust
use serde_json::{json, Value};

pub struct Entry {
    pub resource: Value,
    pub resource_type: String,
    pub id: String,
    /// `meta.versionId` from the snapshot; `None` is a create.
    pub version: Option<String>,
}

pub struct Bundle {
    pub entries: Vec<Entry>,
}

pub fn if_match(version: &str) -> String {
    format!("W/\"{version}\"")
}

impl Bundle {
    pub fn to_json(&self) -> Value {
        let entries: Vec<Value> = self
            .entries
            .iter()
            .map(|e| {
                let mut request = json!({"method": "PUT", "url": format!("{}/{}", e.resource_type, e.id)});
                if let Some(v) = &e.version {
                    request["ifMatch"] = Value::String(if_match(v));
                }
                json!({"resource": e.resource, "request": request})
            })
            .collect();
        json!({"resourceType": "Bundle", "type": "transaction", "entry": entries})
    }

    /// One line for `--dry-run`: `Location/a update@3, Location/b create`.
    pub fn describe(&self) -> String {
        self.entries
            .iter()
            .map(|e| match &e.version {
                Some(v) => format!("{}/{} update@{v}", e.resource_type, e.id),
                None => format!("{}/{} create", e.resource_type, e.id),
            })
            .collect::<Vec<_>>()
            .join(", ")
    }
}

/// Chunk already-ordered resources into bundles of `batch_size`. Every
/// resource has been validated to carry string `resourceType` and `id`.
pub fn plan(resources: Vec<Value>, batch_size: usize) -> Vec<Bundle> {
    let mut bundles = Vec::new();
    let mut current: Vec<Entry> = Vec::new();
    for resource in resources {
        let entry = Entry {
            resource_type: resource["resourceType"].as_str().unwrap_or("").to_string(),
            id: resource["id"].as_str().unwrap_or("").to_string(),
            version: resource
                .get("meta")
                .and_then(|m| m.get("versionId"))
                .and_then(Value::as_str)
                .map(str::to_string),
            resource,
        };
        current.push(entry);
        if current.len() == batch_size.max(1) {
            bundles.push(Bundle {
                entries: std::mem::take(&mut current),
            });
        }
    }
    if !current.is_empty() {
        bundles.push(Bundle { entries: current });
    }
    bundles
}
```

- [ ] **Step 5: Implement the capability check**

Insert above the test module in `src/load/capability.rs`:

```rust
use std::collections::HashSet;

use serde_json::Value;

use crate::error::{KilnError, Result};

/// Resource types whose `rest.resource[].updateCreate` is true.
pub fn update_create_types(capability: &Value) -> HashSet<String> {
    let mut out = HashSet::new();
    for rest in capability.get("rest").and_then(Value::as_array).into_iter().flatten() {
        for resource in rest.get("resource").and_then(Value::as_array).into_iter().flatten() {
            if resource.get("updateCreate").and_then(Value::as_bool) == Some(true) {
                if let Some(t) = resource.get("type").and_then(Value::as_str) {
                    out.insert(t.to_string());
                }
            }
        }
    }
    out
}

/// `needed` are the types of resources without a `versionId`: those are
/// creates, and `PUT` to a new id needs update-as-create.
pub fn check_update_create(capability: &Value, needed: &[String]) -> Result<()> {
    let supported = update_create_types(capability);
    let mut missing: Vec<&str> = needed
        .iter()
        .map(String::as_str)
        .filter(|t| !supported.contains(*t))
        .collect();
    missing.sort();
    missing.dedup();
    if missing.is_empty() {
        return Ok(());
    }
    Err(KilnError::Usage(format!(
        "the input creates new {} resources but this FHIR server does not advertise update-as-create for them \
         (CapabilityStatement rest.resource.updateCreate); kiln load PUTs resources by id, which needs it. \
         On Google Cloud Healthcare API set enableUpdateCreate=true on the store.",
        missing.join(", ")
    )))
}
```

- [ ] **Step 6: Run the tests**

Run: `cargo test load:: 2>&1 | grep 'test result'`
Expected: `test result: ok. 8 passed`

- [ ] **Step 7: Commit**

```bash
git add src/load/order.rs src/load/bundle.rs src/load/capability.rs src/load/mod.rs
git commit -m "load: parents-first ordering, bundles with ifMatch, updateCreate check"
```

---

## Task 10: POST through the retrying client

**Files:**
- Modify: `src/extract/client.rs`

- [ ] **Step 1: Write the failing tests**

Add to the test module in `src/extract/client.rs`:

```rust
    #[test]
    fn post_json_sends_the_body_with_the_fhir_content_type_and_retries() {
        let server = Server::run();
        server.expect(
            Expectation::matching(all_of![
                request::method_path("POST", "/fhir"),
                request::headers(contains(("content-type", "application/fhir+json"))),
                request::body(json_decoded(eq(serde_json::json!({"resourceType": "Bundle"})))),
            ])
            .times(2)
            .respond_with(cycle![status_code(503), status_code(200).body(r#"{"ok":true}"#)]),
        );
        let got = client(3)
            .post_json(&server.url("/fhir").to_string(), br#"{"resourceType":"Bundle"}"#.to_vec())
            .unwrap();
        assert_eq!(got.body, br#"{"ok":true}"#);
    }

    #[test]
    fn a_201_is_a_success_and_a_412_is_final() {
        let server = Server::run();
        server.expect(
            Expectation::matching(request::method_path("POST", "/created"))
                .respond_with(status_code(201).body("made")),
        );
        server.expect(
            Expectation::matching(request::method_path("POST", "/stale"))
                .times(1)
                .respond_with(status_code(412).body("version conflict")),
        );
        let c = client(3);
        assert_eq!(c.post_json(&server.url("/created").to_string(), b"{}".to_vec()).unwrap().body, b"made");
        let err = c.post_json(&server.url("/stale").to_string(), b"{}".to_vec()).unwrap_err();
        assert!(matches!(err, FetchError::Status { status: 412, ref body } if body == "version conflict"), "{err:?}");
    }
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `cargo test extract::client 2>&1 | grep -c 'no method named'`
Expected: non-zero.

- [ ] **Step 3: Refactor the retry loop and add post_json**

In `src/extract/client.rs`, replace the `get` method with:

```rust
    /// GET with the retry policy. Sleeps between attempts.
    pub fn get(&self, url: &str) -> std::result::Result<Fetched, FetchError> {
        self.send(|| self.http.get(url))
    }

    /// POST a FHIR JSON body with the same retry policy. The body is cloned
    /// per attempt; bundles are small next to a boundary fetch.
    pub fn post_json(&self, url: &str, body: Vec<u8>) -> std::result::Result<Fetched, FetchError> {
        self.send(|| {
            self.http
                .post(url)
                .header(CONTENT_TYPE, "application/fhir+json")
                .body(body.clone())
        })
    }

    /// The retry loop. Any 2xx is a success; 429 and 5xx retry with backoff
    /// (a numeric Retry-After replaces the computed delay); any other status
    /// is final on the first response.
    fn send(
        &self,
        request: impl Fn() -> reqwest::blocking::RequestBuilder,
    ) -> std::result::Result<Fetched, FetchError> {
        let mut last: Option<FetchError> = None;
        for attempt in 1..=self.retries {
            match request().send() {
                Err(e) => {
                    last = Some(FetchError::Transport(e.to_string()));
                    if attempt < self.retries {
                        std::thread::sleep(jittered(backoff_delay(attempt, None)));
                    }
                }
                Ok(resp) => {
                    let status = resp.status().as_u16();
                    if status == 429 || status >= 500 {
                        let ra = parse_retry_after(
                            resp.headers()
                                .get("retry-after")
                                .and_then(|v| v.to_str().ok()),
                        );
                        let delay = backoff_delay(attempt, ra);
                        let delay = if ra.is_some() { delay } else { jittered(delay) };
                        last = Some(FetchError::Status {
                            status,
                            body: read_error_body(resp),
                        });
                        if attempt < self.retries {
                            std::thread::sleep(delay);
                        }
                    } else if !(200..300).contains(&status) {
                        return Err(FetchError::Status {
                            status,
                            body: read_error_body(resp),
                        });
                    } else {
                        return match resp.bytes() {
                            Ok(b) => Ok(Fetched { body: b.to_vec() }),
                            Err(e) => Err(FetchError::Transport(format!("reading body: {e}"))),
                        };
                    }
                }
            }
        }
        Err(last.unwrap_or_else(|| FetchError::Transport("no attempts made".to_string())))
    }
```

Add `CONTENT_TYPE` to the `reqwest::header` import line. Update the module doc comment's first sentence to "The one HTTP client. Bearer auth, FHIR accept header, and the retry policy shared by paging, boundary fetches and load's bundle posts".

- [ ] **Step 4: Run the tests**

Run: `cargo test extract:: 2>&1 | grep 'test result'`
Expected: all extract tests pass, including the two new ones.

Run: `cargo test --test extract 2>&1 | grep 'test result'`
Expected: unchanged, all pass (the 2xx change does not alter any GET behaviour the fake server exercises).

- [ ] **Step 5: Commit**

```bash
git add src/extract/client.rs
git commit -m "client: post_json with the shared retry loop; any 2xx succeeds"
```

---

## Task 11: The load command

**Files:**
- Modify: `src/load/mod.rs`
- Create: `tests/load.rs`

- [ ] **Step 1: Write the failing binary tests**

Create `tests/load.rs`:

```rust
//! Binary-level tests for `kiln load` against a fake FHIR server.

use std::path::{Path, PathBuf};
use std::process::Command;

use assert_cmd::assert::Assert;
use assert_cmd::prelude::*;
use httptest::matchers::ExecutionContext;
use httptest::{all_of, cycle, matchers::*, responders::*, Expectation, Server};
use serde_json::{json, Value};

fn loc(id: &str, version: Option<&str>, parent: Option<&str>) -> Value {
    let mut r = json!({"resourceType": "Location", "id": id, "name": id, "status": "active"});
    if let Some(v) = version {
        r["meta"] = json!({"versionId": v});
    }
    if let Some(p) = parent {
        r["partOf"] = json!({"reference": format!("Location/{p}")});
    }
    r
}

fn write_ndjson(dir: &Path, resources: &[Value]) -> PathBuf {
    let p = dir.join("changes.ndjson");
    let text: String = resources.iter().map(|r| format!("{r}\n")).collect();
    std::fs::write(&p, text).unwrap();
    p
}

fn kiln() -> Command {
    let mut c = Command::cargo_bin("kiln").unwrap();
    c.env_remove("KILN_TOKEN");
    c
}

fn load(server: &Server, input: &Path, extra: &[&str]) -> Assert {
    kiln()
        .args(["load", "--server", &server.url("/fhir").to_string(), "--in"])
        .arg(input)
        .args(extra)
        .assert()
}

fn stdout(a: &Assert) -> String {
    String::from_utf8_lossy(&a.get_output().stdout).into_owned()
}

fn stderr(a: &Assert) -> String {
    String::from_utf8_lossy(&a.get_output().stderr).into_owned()
}

fn ok(body: String) -> ResponseBuilder<String> {
    status_code(200)
        .insert_header("content-type", "application/fhir+json")
        .body(body)
}

fn capability(update_create: bool) -> String {
    json!({"resourceType": "CapabilityStatement", "rest": [{"resource": [
        {"type": "Location", "updateCreate": update_create}]}]})
    .to_string()
}

fn expect_metadata(server: &Server, update_create: bool) {
    server.expect(
        Expectation::matching(request::method_path("GET", "/fhir/metadata"))
            .respond_with(ok(capability(update_create))),
    );
}

fn transaction_response() -> String {
    json!({"resourceType": "Bundle", "type": "transaction-response"}).to_string()
}

type Req = httptest::http::Request<httptest::bytes::Bytes>;

/// Matches a POSTed bundle by a predicate over its JSON.
struct BundleWhere(fn(&Value) -> bool);

impl Matcher<Req> for BundleWhere {
    fn matches(&mut self, req: &Req, _ctx: &mut ExecutionContext) -> bool {
        serde_json::from_slice::<Value>(req.body())
            .map(|v| (self.0)(&v))
            .unwrap_or(false)
    }
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("BundleWhere(..)")
    }
}

fn posted_bundle(pred: fn(&Value) -> bool) -> impl Matcher<Req> {
    all_of![request::method_path("POST", "/fhir"), BundleWhere(pred)]
}

fn any_bundle() -> impl Matcher<Req> {
    request::method_path("POST", "/fhir")
}

#[test]
fn bundles_arrive_parents_first_with_if_match_on_existing_entries() {
    let server = Server::run();
    let dir = tempfile::tempdir().unwrap();
    let input = write_ndjson(
        dir.path(),
        &[
            loc("clinic", Some("2"), Some("ward")),
            loc("ward", Some("1"), Some("state")),
            loc("state", Some("3"), None),
            loc("newsite", None, Some("ward")),
        ],
    );
    expect_metadata(&server, true);
    server.expect(
        Expectation::matching(posted_bundle(|b| {
            b["type"] == "transaction"
                && b["entry"][0]["request"] == json!({"method": "PUT", "url": "Location/state", "ifMatch": "W/\"3\""})
                && b["entry"][1]["request"] == json!({"method": "PUT", "url": "Location/ward", "ifMatch": "W/\"1\""})
                && b["entry"][0]["resource"]["name"] == "state"
        }))
        .times(1)
        .respond_with(ok(transaction_response())),
    );
    server.expect(
        Expectation::matching(posted_bundle(|b| {
            b["entry"][0]["request"] == json!({"method": "PUT", "url": "Location/clinic", "ifMatch": "W/\"2\""})
                && b["entry"][1]["request"] == json!({"method": "PUT", "url": "Location/newsite"})
        }))
        .times(1)
        .respond_with(ok(transaction_response())),
    );
    let a = load(&server, &input, &["--batch-size", "2"]).success();
    assert!(stderr(&a).contains("bundle 2/2 committed (2 resources)"), "{}", stderr(&a));
    assert!(stdout(&a).contains("Loaded 4 resources in 2 bundle(s)"), "{}", stdout(&a));
}

#[test]
fn update_create_is_required_only_when_the_input_creates() {
    let server = Server::run();
    let dir = tempfile::tempdir().unwrap();
    let input = write_ndjson(dir.path(), &[loc("a", Some("1"), None), loc("b", None, None)]);
    expect_metadata(&server, false);
    load(&server, &input, &[])
        .failure()
        .code(2)
        .stderr(predicates::str::contains("updateCreate"));

    let server = Server::run();
    let input = write_ndjson(dir.path(), &[loc("a", Some("1"), None)]);
    expect_metadata(&server, false);
    server.expect(
        Expectation::matching(any_bundle())
            .times(1)
            .respond_with(ok(transaction_response())),
    );
    load(&server, &input, &[]).success();
}

#[test]
fn a_412_names_the_conflicting_ids_and_stops() {
    let server = Server::run();
    let dir = tempfile::tempdir().unwrap();
    let input = write_ndjson(
        dir.path(),
        &[loc("a", Some("1"), None), loc("b", Some("2"), None), loc("c", Some("3"), None)],
    );
    expect_metadata(&server, true);
    server.expect(
        Expectation::matching(any_bundle())
            .times(2)
            .respond_with(cycle![
                ok(transaction_response()),
                status_code(412).body(r#"{"resourceType":"OperationOutcome"}"#),
            ]),
    );
    server.expect(
        Expectation::matching(request::method_path("GET", "/fhir/Location/b"))
            .times(1)
            .respond_with(ok(json!({"resourceType": "Location", "id": "b", "meta": {"versionId": "5"}}).to_string())),
    );
    let a = load(&server, &input, &["--batch-size", "1"]).failure().code(1);
    let err = stderr(&a);
    assert!(err.contains("bundle 2/3 rejected with HTTP 412"), "{err}");
    assert!(err.contains("Location/b (expected version 2, server has 5)"), "{err}");
    assert!(err.contains("bundle 1/3 committed"), "{err}");
    assert!(err.contains("diff again"), "{err}");
}

#[test]
fn a_503_is_retried_and_a_400_aborts_with_the_body() {
    let dir = tempfile::tempdir().unwrap();
    let input = write_ndjson(dir.path(), &[loc("a", Some("1"), None)]);

    let server = Server::run();
    expect_metadata(&server, true);
    server.expect(
        Expectation::matching(any_bundle())
            .times(2)
            .respond_with(cycle![status_code(503), ok(transaction_response())]),
    );
    load(&server, &input, &[]).success();

    let server = Server::run();
    expect_metadata(&server, true);
    server.expect(
        Expectation::matching(any_bundle())
            .times(1)
            .respond_with(status_code(400).body("no such profile")),
    );
    load(&server, &input, &[])
        .failure()
        .code(1)
        .stderr(predicates::str::contains("HTTP 400: no such profile"));
}

#[test]
fn dry_run_posts_nothing_and_prints_the_plan() {
    let server = Server::run();
    let dir = tempfile::tempdir().unwrap();
    let input = write_ndjson(dir.path(), &[loc("clinic", None, Some("state")), loc("state", Some("3"), None)]);
    expect_metadata(&server, true);
    let a = load(&server, &input, &["--dry-run"]).success();
    let out = stdout(&a);
    assert!(out.contains("Dry run: 2 resources in 1 bundle(s)"), "{out}");
    assert!(out.contains("bundle 1/1: Location/state update@3, Location/clinic create"), "{out}");
}

#[test]
fn input_problems_are_usage_errors_before_any_request() {
    let server = Server::run();
    let dir = tempfile::tempdir().unwrap();

    let input = write_ndjson(dir.path(), &[loc("a", Some("1"), Some("b")), loc("b", Some("1"), Some("a"))]);
    load(&server, &input, &[])
        .failure()
        .code(2)
        .stderr(predicates::str::contains("cycle"));

    let server = Server::run();
    let input = write_ndjson(dir.path(), &[loc("a", Some("1"), None), loc("a", Some("1"), None)]);
    load(&server, &input, &[])
        .failure()
        .code(2)
        .stderr(predicates::str::contains("duplicate resource Location/a"));

    let input = write_ndjson(dir.path(), &[json!({"resourceType": "Location", "name": "no id"})]);
    load(&server, &input, &[])
        .failure()
        .code(2)
        .stderr(predicates::str::contains("line 1"));

    let input = write_ndjson(dir.path(), &[loc("a", Some("1"), None)]);
    load(&server, &input, &["--batch-size", "0"])
        .failure()
        .code(2)
        .stderr(predicates::str::contains("--batch-size"));
}
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `cargo test --test load 2>&1 | grep 'test result'`
Expected: `test result: FAILED` (the stub refuses everything with exit 2).

- [ ] **Step 3: Implement run_load**

Replace the whole of `src/load/mod.rs` with:

```rust
//! `kiln load`: FHIR NDJSON -> transaction bundles with `If-Match`. Parents
//! first, a capability preflight, retry through the shared client, and a
//! precise conflict list on 412 or 409. The first failed bundle aborts;
//! committed bundles stay committed, and because every entry is a PUT by
//! id with a version check, re-running is safe.

pub mod bundle;
pub mod capability;
pub mod order;

use std::collections::HashSet;
use std::path::Path;
use std::time::Duration;

use serde_json::Value;

use crate::cli::LoadArgs;
use crate::error::{KilnError, Result};
use crate::extract::client::{FetchError, FhirClient};
use crate::fhir::ndjson::NdjsonReader;
use crate::load::bundle::{plan, Bundle};
use crate::load::capability::check_update_create;
use crate::load::order::order_parents_first;

/// Every line must be an object with string `resourceType` and `id`, and
/// no `<Type>/<id>` may repeat. The whole changeset is held in memory:
/// ordering needs all of it, and it is a changeset, not a registry.
pub fn read_resources(path: &Path) -> Result<Vec<Value>> {
    let mut resources = Vec::new();
    let mut seen = HashSet::new();
    for line in NdjsonReader::open(path)? {
        let line = line?;
        let value: Value = serde_json::from_str(&line.text).map_err(|e| {
            KilnError::Usage(format!("{}: line {}: {e}", path.display(), line.number))
        })?;
        let resource_type = value.get("resourceType").and_then(Value::as_str);
        let id = value.get("id").and_then(Value::as_str);
        let key = match (resource_type, id) {
            (Some(t), Some(i)) if !t.is_empty() && !i.is_empty() => format!("{t}/{i}"),
            _ => {
                return Err(KilnError::Usage(format!(
                    "{}: line {}: not a FHIR resource (needs a resourceType and a non-empty string id)",
                    path.display(),
                    line.number
                )))
            }
        };
        if !seen.insert(key.clone()) {
            return Err(KilnError::Usage(format!(
                "{}: line {}: duplicate resource {key}",
                path.display(),
                line.number
            )));
        }
        resources.push(value);
    }
    Ok(resources)
}

/// Types of the resources that have no `meta.versionId`: the creates.
fn create_types(resources: &[Value]) -> Vec<String> {
    resources
        .iter()
        .filter(|r| {
            r.get("meta")
                .and_then(|m| m.get("versionId"))
                .and_then(Value::as_str)
                .is_none()
        })
        .filter_map(|r| r.get("resourceType")?.as_str().map(str::to_string))
        .collect()
}

/// After a 412 or 409: read each version-checked entry back and name the
/// ones whose server version differs. Standard reads, nothing vendor
/// specific, so the message is exact on any R4 server.
fn probe_conflicts(client: &FhirClient, base: &str, bundle: &Bundle) -> Vec<String> {
    let mut out = Vec::new();
    for e in &bundle.entries {
        let Some(expected) = &e.version else {
            continue;
        };
        let label = format!("{}/{}", e.resource_type, e.id);
        match client.get(&format!("{base}/{label}")) {
            Ok(fetched) => {
                let actual = serde_json::from_slice::<Value>(&fetched.body)
                    .ok()
                    .and_then(|v| v.get("meta")?.get("versionId")?.as_str().map(str::to_string));
                match actual {
                    Some(a) if &a == expected => {}
                    Some(a) => out.push(format!(
                        "{label} (expected version {expected}, server has {a})"
                    )),
                    None => out.push(format!(
                        "{label} (expected version {expected}, server returned no versionId)"
                    )),
                }
            }
            Err(FetchError::Status {
                status: 404 | 410, ..
            }) => out.push(format!(
                "{label} (expected version {expected}, deleted on the server)"
            )),
            Err(err) => out.push(format!("{label} (could not check: {err})")),
        }
    }
    out
}

pub fn run_load(args: &LoadArgs) -> Result<()> {
    if args.batch_size == 0 {
        return Err(KilnError::Usage("--batch-size must be at least 1".into()));
    }
    if !args.input.is_file() {
        return Err(KilnError::Usage(format!(
            "--in {}: not a file",
            args.input.display()
        )));
    }
    let resources = read_resources(&args.input)?;
    if resources.is_empty() {
        println!("Nothing to load: {} is empty", args.input.display());
        return Ok(());
    }
    // Ordering and the cycle check come before any request: a bad input
    // should never cost a network round trip.
    let creates = create_types(&resources);
    let ordered = order_parents_first(resources)?;
    let bundles = plan(ordered, args.batch_size);
    let total: usize = bundles.iter().map(|b| b.entries.len()).sum();

    let base = args.server.trim_end_matches('/').to_string();
    let client = FhirClient::new(
        args.token.clone(),
        args.retries,
        Duration::from_secs(args.timeout),
    )?;

    // Preflight: also the cheapest check that the URL and token work.
    let metadata_url = format!("{base}/metadata");
    let capability: Value = match client.get(&metadata_url) {
        Ok(f) => serde_json::from_slice(&f.body).map_err(|e| {
            KilnError::Environment(format!(
                "capability preflight: {metadata_url} returned non-JSON: {e}"
            ))
        })?,
        Err(e) => {
            return Err(KilnError::Environment(format!(
                "capability preflight failed: {e} for {metadata_url}; check the server URL and token"
            )))
        }
    };
    check_update_create(&capability, &creates)?;

    if args.dry_run {
        println!(
            "Dry run: {total} resources in {} bundle(s) for {base}; nothing posted",
            bundles.len()
        );
        for (i, b) in bundles.iter().enumerate() {
            println!("bundle {}/{}: {}", i + 1, bundles.len(), b.describe());
        }
        return Ok(());
    }

    let count = bundles.len();
    for (i, b) in bundles.iter().enumerate() {
        let n = i + 1;
        let body = b.to_json().to_string().into_bytes();
        match client.post_json(&base, body) {
            Ok(_) => eprintln!(
                "bundle {n}/{count} committed ({} resources)",
                b.entries.len()
            ),
            Err(FetchError::Status {
                status: status @ (409 | 412),
                body,
            }) => {
                let conflicts = probe_conflicts(&client, &base, b);
                let detail = if conflicts.is_empty() {
                    format!("server response: {body}")
                } else {
                    format!(
                        "{} conflict(s):\n  {}",
                        conflicts.len(),
                        conflicts.join("\n  ")
                    )
                };
                return Err(KilnError::Environment(format!(
                    "bundle {n}/{count} rejected with HTTP {status}; nothing in it was written. {detail}\n\
                     These resources changed on the server after the snapshot was taken: \
                     run extract, re-apply the edit, and diff again."
                )));
            }
            Err(e) => {
                return Err(KilnError::Environment(format!(
                    "bundle {n}/{count} failed: {e}; bundles before it were committed, re-running is safe"
                )))
            }
        }
    }
    println!("Loaded {total} resources in {count} bundle(s) to {base}");
    Ok(())
}
```

- [ ] **Step 4: Run the tests**

Run: `cargo test --test load 2>&1 | grep -E 'test result|panicked'`
Expected: `test result: ok. 6 passed`

Run: `cargo test 2>&1 | grep -E 'test result|warning' | sort | uniq -c`
Expected: every suite ok, no warnings.

Run: `cargo clippy --all-targets 2>&1 | grep -E '^(warning|error)' | sort | uniq -c`
Expected: nothing new compared with `git stash; cargo clippy --all-targets; git stash pop` on the branch base (the crate is not clippy-clean historically; only fix what this plan introduced).

- [ ] **Step 5: Commit**

```bash
git add src/load/mod.rs tests/load.rs
git commit -m "load: the command, with preflight, If-Match bundles and conflict probe"
```

---

## Task 12: README and spec status

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-09-05-diff-load-design.md`

- [ ] **Step 1: Update the README**

In `README.md`:

1. The Status callout near line 15: `transform`, `inspect`, `extract`, `run`, `diff` and `load` are implemented in Rust; the Python package under `python/` still provides `bake` and `bake-points`. Delete the words saying `diff` and `load` are in progress.

2. In "Commands", replace the two lines for diff and load with:

```
kiln diff      --snapshot DIR --in EDITS --out CHANGES.ndjson [--report FILE]
kiln load      --server URL [--token T] --in CHANGES.ndjson
               [--dry-run] [--batch-size 100] [--retries 3] [--timeout SECS]
```

3. In "Round trip: diff and load", section "diff", after the paragraph beginning "Input is GeoJSON or GeoParquet", replace the rest of the section with:

```markdown
The input is detected by extension: `.geojson` or `.json` for a
FeatureCollection, read one feature at a time so a whole-country export
never sits in memory; `.geojsonl` or `.geojsons` for one Feature per line;
`.parquet` for GeoParquet, read one record batch at a time. The row's id is
the `id` column, or the Feature id when the column is absent. A row with an
id the snapshot does not have becomes a new Location; a row with no id at
all is given a UUID and reported as `new_location_generated_id`.

For each row, diff finds the snapshot resource by id and rebuilds what the
resource *should* now be:

1. Start from the snapshot's complete resource.
2. Apply every **writable** column that is present in the input. A column
   that is absent leaves the field alone. A column that is present and
   empty removes the field: GIS exports carry every column, so an empty
   `description` has to mean "clear it" or a cleared field could never
   round trip. `identifier` is replaced as a whole list, then `pcode` and
   `gers_id` upsert their entry into it. A list column may arrive as a JSON
   string, which is how GDAL exports nested fields.
3. Apply the geometry to what it came from. A row whose snapshot resource
   has a boundary gets its boundary attachment replaced when the polygon
   differs; a row without one gets its `position` moved when the point
   differs. Drawing a polygon on a point row adds a boundary. Drawing a
   point on a boundary row is reported as `geometry_kind_changed` and
   skipped, since erasing a boundary that way is not a plausible intent.
   Comparison is on the WKB after rounding coordinates to seven decimals,
   so a float round trip through a GIS tool does not register as an edit,
   and coordinates are written back rounded the same way. Invalid polygons
   are reported as `geometry_invalid` and written as they are.
4. Ignore every **derived** column. A stale `admin1_name` in the input never
   causes a change. If the position columns and a point geometry were both
   edited and disagree, the geometry wins and `position_geometry_disagree`
   is reported.
5. Compare the rebuilt resource to the original. If nothing changed, skip it.

What comes out is plain FHIR NDJSON containing only the resources that
changed, each complete, each carrying the `meta` it was based on, with a
new resource carrying none. You can inspect it, validate it against the
profile, or hand it to someone else before anything is sent. `--report`
writes the issues found as JSON in the same shape as transform's report;
the summary is always printed. The kinds are `duplicate_id`,
`new_location`, `new_location_generated_id`, `input_column_type` (a
writable column holding a value of the wrong type, ignored for that row),
`geometry_unparseable`, `geometry_kind_changed`, `geometry_invalid`,
`position_geometry_disagree`, `boundary_z_dropped` and
`snapshot_line_unparsed`.
```

4. In section "load", replace everything after the command block with:

```markdown
Load reads any NDJSON of FHIR resources with a `resourceType` and an `id`,
orders them parents first by `partOf`, groups them into transaction bundles
of `PUT <Type>/<id>`, and posts them with retry and backoff. Before the
first bundle it fetches the server's capability statement, which also
proves the URL and token work; if the input creates any new resource, the
server must advertise update-as-create for that type, because `PUT` to a
new id needs it and one clear error beats hundreds of identical 404s.

Every entry whose resource carries a `meta.versionId` is sent with
`ifMatch` set to that version. If the resource was changed on the server
after the snapshot was taken, the server answers 412 (some answer 409),
nothing in that bundle is written, and kiln reads each version-checked
resource in the bundle back to name exactly which ids conflicted and what
version the server has now. The run stops there; bundles before it stay
committed, and re-running the same file is safe because every entry is a
PUT by id with a version check. The fix is to extract again, re-apply the
edit, and diff again. This is what makes it safe for a GIS user to edit a
copy that might be a day old.

`--dry-run` runs the preflight and prints one line per bundle, naming each
entry as `create` or `update@<version>`, without posting anything.

Load does not update the snapshot. The server is the authority on what was
stored; the next extract brings the snapshot up to date.
```

5. In "Repository layout", change the two `(in progress)` lines to:

```
    diff/        input rows from GeoJSON and GeoParquet, resource rebuild, canonical compare
    load/        parents-first ordering, transaction bundles with ifMatch, capability preflight
```

and add `diff.rs, load.rs` after `fixtures/snapshot/` under `tests/`. Change the `python/` line to `the Python package: bake and bake-points (extract and load are superseded by the Rust binary)`.

6. In "What kiln does not do", the bullet "Validate against the profile" already describes diff correctly; leave it.

- [ ] **Step 2: Update the spec status**

In `docs/superpowers/specs/2026-09-05-diff-load-design.md`, change the `**Status:**` line to: `Implemented on branch \`rust-diff-load\` (plan \`docs/superpowers/plans/2026-09-05-rust-diff-load.md\`).` If any decision changed during execution, add it to that line the way the plan 2 spec records its amendments.

- [ ] **Step 3: Commit**

```bash
git add README.md docs/superpowers/specs/2026-09-05-diff-load-design.md
git commit -m "README: document diff and load as implemented"
```

---

## Self-review

- **Spec coverage.** Input formats and streaming: Tasks 3, 4, 7. Id source and new rows: Tasks 3, 4, 7, 8. Absent vs null: Task 2 (coercion), Task 5 (removal). Geometry rule, kind change, polygon on point, disagreement, rounding, attachment shape: Task 6, tested end to end in Task 8. `identifier` then `pcode`/`gers_id`: Task 5. `type`, `facility_level`, `ownership`, references with prefix: Task 5. Report and summary: Task 7. Atomic output: Task 7. Load input validation, preflight only refusing on creates, ordering, `ifMatch`, conflict probe, first-failure abort, dry run: Tasks 9 to 11. `post_json` and 2xx: Task 10. Dependencies: Task 0. README: Task 12. The spec's memory measurement (a 200,000 feature GeoJSON) is not a task: record it once by hand in the Task 12 README edit if you run it; the streaming design is what the spec asks for and Task 3's visitor is that design.
- **Consistency.** `read_geoparquet`'s sink takes `(InputRow, &mut Report)` in Task 4 and Task 7. `apply_geometry` has no `row` parameter in Tasks 5 and 6. `convert` is re-exported from `crate::geometry` in Task 3 and used in Task 3 only. `Bundle::describe` output format is the same in Task 9's unit test and Task 11's dry-run test. `FetchError::Status` body capping (500 chars) applies to the 400 message in Task 11's test; the test body is short.
- **Known compiler risks, with the fix already in the step:** the closure moving `diff.out` in Task 7 (fallback given). Test-only imports in Tasks 3 and 4 live inside the test modules.
- **httptest note.** An `Expectation` without `.times(..)` must be hit exactly once or the server panics on drop, and an unexpected request fails the test too. Task 11's tests only register the metadata expectation when the command reaches the preflight.
