# Facility Organizations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** kiln extracts, projects and round-trips the Organization paired with every facility Location: the parquet gains the NHFR codes and type labels, a facility edit rebuilds both resources, a new facility row creates the pair, and load posts Organizations before the Locations that reference them.

**Architecture:** Organizations go through the same extract phases as Locations (page, merge, own watermark) into `organizations.ndjson`. Transform indexes that file by id and byte offset and, in pass two, seeks the Organization for any row whose Location names one, filling six new columns. Diff indexes it the same way and rebuilds the Organization beside the Location from the mirrored and Organization-only columns, emitting each only if it changed. Load sorts non-Location resources first.

**Tech Stack:** Existing crate; no new dependencies.

Spec: `docs/superpowers/specs/2026-09-05-facility-organization-design.md`. Where this plan and the spec disagree, the spec wins; fix the plan.

---

## Existing APIs you will use

- `src/fhir/location.rs`: `Identifier { system, value }`, `Location::parse`, private helpers `str_field`, `coding_code_by_system(node, system)`, `first_coding_code`. Task 0 makes `str_field` and `coding_code_by_system` `pub(crate)`.
- `src/fhir/ndjson.rs`: `NdjsonReader::open(path)` yielding `Line { number, offset, len, text }`; `LineAccess::open(path)`, `read_at(offset, len)`.
- `src/snapshot/mod.rs`: `Snapshot { dir }` with path methods; `State` (serde) with `read`/`write`.
- `src/snapshot/merge.rs`: `merge(snap, notes, full, lookup, failures, report) -> MergeStats { total, added, updated, watermark }`; private `merge_into`.
- `src/extract/page.rs`: `page_locations(client, server, since, incoming, report) -> PageResult { notes: Vec<PageNote { id, last_updated, boundary_url }>, pages }`.
- `src/extract/mod.rs`: `run_extract`, private `run_phases(args, snap, since)`.
- `src/index/mod.rs`: `IndexRecord`; `src/index/build.rs`: `build_index(ndjson, country_override) -> Index { records, hierarchy, order, report, read }`.
- `src/write/schema.rs`: `output_schema()`, `OutputRow`, `RowBatch::{new, push, finish}`, `identifier_fields()`; `src/write/dataset.rs`: `write_dataset(ndjson, &mut index, out_dir, keys, row_group_size)`, private `write_partitions`, `load_location`.
- `src/diff/mod.rs`: `index_snapshot(path, report)`, `Diff { index, access, out, out_path, seen, stats }`, `Diff::process`, `read_input`, `run_diff`; `src/diff/rebuild.rs`: `rebuild`, private `set_string`, `upsert_identifier`, `upsert_type_coding`, `array_mut`, `object_mut`, `apply_identifier_list`, `system_is`, `text_of`.
- `src/load/order.rs`: `order_parents_first(Vec<Value>)`.
- Tests: `tests/extract.rs` helpers `loc`, `bundle`, `kiln`, `extract`, `extract_at`, `state`, `lines`, `ok`, `full_search`, `since_search`; `tests/diff.rs` helpers `snapshot`, `ng`, `clinic`, `export`, `feature`, `collection`, `point`, `write_input`, `diff`, `changes`, `report`, `stdout`; `tests/load.rs` helpers `loc`, `write_ndjson`, `load`, `expect_metadata`, `posted_bundle`, `any_bundle`, `transaction_response`.

## File structure

```
src/
  fhir/organization.rs      Organization::parse, NHFR systems, concept_text_by_system   (new)
  fhir/location.rs          pub(crate) str_field, coding_code_by_system
  fhir/mod.rs               + pub mod organization; re-export Organization
  snapshot/mod.rs           organizations paths, State.organization_watermark/_count
  snapshot/index.rs         index_by_id(path, report, unparsed_kind)                     (new)
  snapshot/merge.rs         MergeFiles; merge_file(); merge() wraps it for Locations
  extract/page.rs           page_resources(resource_type, ..); page_locations wraps it
  extract/mod.rs            Organization phase after the Location merge; state fields
  index/mod.rs              IndexRecord.managing_organization
  index/build.rs            fill it
  write/schema.rs           six new columns
  write/dataset.rs          Organization lookup in pass two; write_dataset(.., organizations: Option<&Path>)
  transform.rs              pass the organizations path when the file exists
  diff/mod.rs               organization index and access; pair handling in process()
  diff/rebuild.rs           rebuild_organization, new_organization, pub(crate) helpers
  load/order.rs             Organizations first
tests/
  extract.rs                expect_organizations helper in every test; two new tests
  transform.rs              organization columns test
  diff.rs                   pair tests
  load.rs                   ordering test
README.md, docs/qgis.md, the spec status line
```

---

## Task 0: Organization parsing

**Files:**
- Create: `src/fhir/organization.rs`
- Modify: `src/fhir/location.rs`, `src/fhir/mod.rs`

- [x] **Step 1: Write the failing tests**

Create `src/fhir/organization.rs` with the test module:

```rust
//! One FHIR Organization, flattened into the fields kiln projects onto its
//! facility's row. The ICR facility pairing (`ICRFacilityOrganization`)
//! puts the registry codes and the human-readable type labels here.

#[cfg(test)]
mod tests {
    use super::*;

    const ORG: &str = r#"{"resourceType":"Organization","id":"org-1","active":true,"name":"Siyi Health Post",
        "meta":{"versionId":"7","lastUpdated":"2026-08-06T02:59:22Z"},
        "identifier":[{"system":"https://icr.healthcampaigns.org/identifiers/nga-nhfr-code","value":"05/08/1/1/1/0061"},
                      {"system":"https://icr.healthcampaigns.org/identifiers/nga-nhfr-uid","value":"21526030"}],
        "type":[{"coding":[{"system":"http://terminology.hl7.org/CodeSystem/organization-type","code":"prov"}]},
                {"coding":[{"system":"https://icr.healthcampaigns.org/CodeSystem/icr-facility-type-cs","code":"primary"}],"text":"Health Post"},
                {"coding":[{"system":"https://icr.healthcampaigns.org/CodeSystem/icr-ownership-cs","code":"public"}],"text":"Local Government"}]}"#;

    #[test]
    fn parses_the_pairing_fields() {
        let mut report = Report::default();
        let org = Organization::parse(&serde_json::from_str(ORG).unwrap(), &mut report).unwrap();
        assert_eq!(org.id, "org-1");
        assert_eq!(org.version_id.as_deref(), Some("7"));
        assert_eq!(org.name.as_deref(), Some("Siyi Health Post"));
        assert_eq!(org.identifier.len(), 2);
        assert_eq!(org.nhfr_code.as_deref(), Some("05/08/1/1/1/0061"));
        assert_eq!(org.nhfr_uid.as_deref(), Some("21526030"));
        assert_eq!(org.facility_level.as_deref(), Some("primary"));
        assert_eq!(org.ownership.as_deref(), Some("public"));
        assert_eq!(org.facility_level_text.as_deref(), Some("Health Post"));
        assert_eq!(org.ownership_text.as_deref(), Some("Local Government"));
        assert!(org.fhir_json.starts_with("{\"resourceType\":\"Organization\""));
        assert!(report.counts().is_empty());
    }

    #[test]
    fn missing_id_and_wrong_type_are_rejected() {
        let mut report = Report::default();
        assert!(Organization::parse(&serde_json::json!({"resourceType":"Organization"}), &mut report).is_none());
        assert_eq!(report.count("missing_id"), 1);
        assert!(Organization::parse(&serde_json::json!({"resourceType":"Location","id":"x"}), &mut report).is_none());
        assert_eq!(report.count("malformed_field"), 1);
    }

    #[test]
    fn text_is_read_from_the_concept_holding_the_system() {
        let v = serde_json::json!([{"coding":[{"system":"a","code":"1"}],"text":"A"},{"coding":[{"system":"b","code":"2"}]}]);
        assert_eq!(concept_text_by_system(Some(&v), "a").as_deref(), Some("A"));
        assert_eq!(concept_text_by_system(Some(&v), "b"), None);
        assert_eq!(concept_text_by_system(None, "a"), None);
    }
}
```

- [x] **Step 2: Run the tests to see them fail**

In `src/fhir/mod.rs` add `pub mod organization;` and change the re-export to `pub use location::{Boundary, Location};` plus `pub use organization::Organization;`.
Run: `cargo test --bin kiln fhir::organization 2>&1 | grep -c 'cannot find'`
Expected: non-zero.

- [x] **Step 3: Implement**

In `src/fhir/location.rs` change `fn str_field(` to `pub(crate) fn str_field(` and `fn coding_code_by_system(` to `pub(crate) fn coding_code_by_system(`.

Insert above the test module in `src/fhir/organization.rs`:

```rust
use serde_json::Value;

use crate::fhir::location::{
    coding_code_by_system, str_field, Identifier, FACILITY_TYPE_SYSTEM, OWNERSHIP_SYSTEM,
};
use crate::report::Report;

pub const NHFR_CODE_SYSTEM: &str = "https://icr.healthcampaigns.org/identifiers/nga-nhfr-code";
pub const NHFR_UID_SYSTEM: &str = "https://icr.healthcampaigns.org/identifiers/nga-nhfr-uid";
pub const ORGANIZATION_TYPE_SYSTEM: &str = "http://terminology.hl7.org/CodeSystem/organization-type";

#[derive(Debug, Clone, Default)]
pub struct Organization {
    pub id: String,
    pub version_id: Option<String>,
    pub last_updated: Option<String>,
    pub name: Option<String>,
    pub identifier: Vec<Identifier>,
    pub nhfr_code: Option<String>,
    pub nhfr_uid: Option<String>,
    pub facility_level: Option<String>,
    pub ownership: Option<String>,
    pub facility_level_text: Option<String>,
    pub ownership_text: Option<String>,
    /// The resource as JSON, key order preserved, untouched.
    pub fhir_json: String,
}

/// `text` of the first CodeableConcept (in a concept or a list of them)
/// that holds a coding under `system`.
pub fn concept_text_by_system(node: Option<&Value>, system: &str) -> Option<String> {
    let concepts: Vec<&Value> = match node? {
        Value::Array(items) => items.iter().collect(),
        other => vec![other],
    };
    concepts
        .into_iter()
        .find(|concept| {
            concept
                .get("coding")
                .and_then(Value::as_array)
                .is_some_and(|codings| {
                    codings
                        .iter()
                        .any(|c| c.get("system").and_then(Value::as_str) == Some(system))
                })
        })
        .and_then(|concept| concept.get("text")?.as_str().map(str::to_string))
}

impl Organization {
    /// Flatten one resource. Returns None (after reporting) if it has no
    /// usable id or is not an Organization.
    pub fn parse(resource: &Value, report: &mut Report) -> Option<Organization> {
        let Value::Object(obj) = resource else {
            report.add("malformed_field", "<unknown>", "Organization resource is not an object");
            return None;
        };
        let id = match obj.get("id") {
            Some(Value::String(s)) if !s.is_empty() => s.clone(),
            _ => {
                report.add("missing_id", "<unknown>", "Organization resource has no id");
                return None;
            }
        };
        if let Some(rt) = obj.get("resourceType").and_then(Value::as_str) {
            if rt != "Organization" {
                report.add(
                    "malformed_field",
                    &id,
                    &format!("resourceType is {rt}, not Organization"),
                );
                return None;
            }
        }
        let mut org = Organization {
            id: id.clone(),
            name: str_field(obj, "name"),
            ..Default::default()
        };
        if let Some(Value::Object(meta)) = obj.get("meta") {
            org.version_id = str_field(meta, "versionId");
            org.last_updated = str_field(meta, "lastUpdated");
        }
        if let Some(Value::Array(items)) = obj.get("identifier") {
            for item in items {
                if let Value::Object(ident) = item {
                    org.identifier.push(Identifier {
                        system: str_field(ident, "system"),
                        value: str_field(ident, "value"),
                    });
                } else {
                    report.add("malformed_field", &id, "identifier entry is not an object");
                }
            }
        }
        let by_system = |system: &str| {
            org.identifier
                .iter()
                .find(|i| i.system.as_deref() == Some(system))
                .and_then(|i| i.value.clone())
        };
        org.nhfr_code = by_system(NHFR_CODE_SYSTEM);
        org.nhfr_uid = by_system(NHFR_UID_SYSTEM);
        org.facility_level = coding_code_by_system(obj.get("type"), FACILITY_TYPE_SYSTEM);
        org.ownership = coding_code_by_system(obj.get("type"), OWNERSHIP_SYSTEM);
        org.facility_level_text = concept_text_by_system(obj.get("type"), FACILITY_TYPE_SYSTEM);
        org.ownership_text = concept_text_by_system(obj.get("type"), OWNERSHIP_SYSTEM);
        org.fhir_json = resource.to_string();
        Some(org)
    }
}
```

- [x] **Step 4: Run the tests**

Run: `cargo test --bin kiln fhir:: 2>&1 | grep 'test result'`
Expected: all fhir tests pass including the 3 new ones.

- [x] **Step 5: Commit**

```bash
git add src/fhir/organization.rs src/fhir/location.rs src/fhir/mod.rs
git commit -m "fhir: Organization parsing with the NHFR codes and type labels"
```

---

## Task 1: Snapshot paths, state fields, and a shared id index

**Files:**
- Modify: `src/snapshot/mod.rs`
- Create: `src/snapshot/index.rs`
- Modify: `src/diff/mod.rs` (use the shared index)

- [x] **Step 1: Write the failing tests**

In `src/snapshot/mod.rs` add to the test module:

```rust
    #[test]
    fn organization_paths_and_optional_state_fields() {
        let s = Snapshot::new(std::path::Path::new("/x"));
        assert_eq!(s.organizations(), std::path::PathBuf::from("/x/organizations.ndjson"));
        assert_eq!(s.organizations_tmp(), std::path::PathBuf::from("/x/organizations.ndjson.tmp"));
        assert_eq!(s.incoming_organizations(), std::path::PathBuf::from("/x/.incoming-organizations.ndjson"));

        // A plan 2 state file has no organization fields and must still load.
        let old = r#"{"server":"s","watermark":"2026-01-01T00:00:00Z","count":1,"kiln_version":"0.2.0","completed_at":"2026-01-01T00:00:00Z"}"#;
        let st: State = serde_json::from_str(old).unwrap();
        assert_eq!(st.organization_watermark, None);
        assert_eq!(st.organization_count, None);
        let text = serde_json::to_string(&st).unwrap();
        assert!(!text.contains("organization"), "absent fields stay absent: {text}");
    }
```

Create `src/snapshot/index.rs` with the test module:

```rust
//! `id -> (offset, len)` over one NDJSON file. Nothing else is retained; a
//! resource is re-read only when something names it.

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn indexes_ids_keeps_the_first_duplicate_and_reports_unparsed_lines() {
        let mut f = tempfile::NamedTempFile::new().unwrap();
        writeln!(f, r#"{{"id":"a","n":1}}"#).unwrap();
        writeln!(f, "not json").unwrap();
        writeln!(f, r#"{{"id":"a","n":2}}"#).unwrap();
        writeln!(f, r#"{{"id":"b"}}"#).unwrap();
        let mut report = Report::default();
        let index = index_by_id(f.path(), &mut report, "organization_line_unparsed").unwrap();
        assert_eq!(index.len(), 2);
        assert_eq!(index["a"], (0, 16));
        assert_eq!(report.count("duplicate_id"), 1);
        assert_eq!(report.count("organization_line_unparsed"), 1);
    }
}
```

- [x] **Step 2: Run the tests to see them fail**

Add `pub mod index;` to `src/snapshot/mod.rs`.
Run: `cargo test --bin kiln snapshot:: 2>&1 | grep -c 'cannot find\|no method\|no field'`
Expected: non-zero.

- [x] **Step 3: Implement**

In `src/snapshot/mod.rs` add constants and methods:

```rust
pub const ORGANIZATIONS_FILE: &str = "organizations.ndjson";
pub const INCOMING_ORGANIZATIONS_FILE: &str = ".incoming-organizations.ndjson";
```

and in `impl Snapshot`:

```rust
    pub fn organizations(&self) -> PathBuf {
        self.dir.join(ORGANIZATIONS_FILE)
    }
    pub fn organizations_tmp(&self) -> PathBuf {
        self.dir.join(format!("{ORGANIZATIONS_FILE}.tmp"))
    }
    pub fn incoming_organizations(&self) -> PathBuf {
        self.dir.join(INCOMING_ORGANIZATIONS_FILE)
    }
```

Add to `State`, after `count`:

```rust
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub organization_watermark: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub organization_count: Option<usize>,
```

Every place that constructs `State { .. }` (in `src/extract/mod.rs` and in tests under `src/snapshot/mod.rs` and `tests/`) gets `organization_watermark: None, organization_count: None,` for now; Task 4 fills them in extract.

Insert above the test module in `src/snapshot/index.rs`:

```rust
use std::collections::HashMap;
use std::path::Path;

use serde_json::Value;

use crate::error::Result;
use crate::fhir::ndjson::NdjsonReader;
use crate::report::Report;

/// Index one NDJSON file by resource id. A repeated id keeps the first
/// line and is reported as `duplicate_id`; a line that is not an object
/// with a string id is reported under `unparsed_kind` and skipped.
pub fn index_by_id(
    path: &Path,
    report: &mut Report,
    unparsed_kind: &str,
) -> Result<HashMap<String, (u64, usize)>> {
    let mut index = HashMap::new();
    for line in NdjsonReader::open(path)? {
        let line = line?;
        let id = serde_json::from_str::<Value>(&line.text)
            .ok()
            .and_then(|v| v.get("id")?.as_str().map(str::to_string));
        match id {
            Some(id) => match index.entry(id) {
                std::collections::hash_map::Entry::Occupied(e) => report.add(
                    "duplicate_id",
                    e.key(),
                    &format!("line {} repeats an earlier id; first kept", line.number),
                ),
                std::collections::hash_map::Entry::Vacant(e) => {
                    e.insert((line.offset, line.len));
                }
            },
            None => report.add(
                unparsed_kind,
                "<unknown>",
                &format!("line {} is not a resource with an id", line.number),
            ),
        }
    }
    Ok(index)
}
```

In `src/diff/mod.rs` delete `index_snapshot` and replace its one call with `index_by_id(&ndjson, &mut report, "snapshot_line_unparsed")`, importing `crate::snapshot::index::index_by_id`. Drop the now-unused `HashMap`/`Value` imports if the compiler says so (`HashMap` is still used by `Diff`).

- [x] **Step 4: Run the tests**

Run: `cargo test 2>&1 | grep -E 'test result|^error' | sort | uniq -c`
Expected: every suite ok (the diff binary tests still pass with the shared index).

- [x] **Step 5: Commit**

```bash
git add src/snapshot/mod.rs src/snapshot/index.rs src/diff/mod.rs src/extract/mod.rs
git commit -m "snapshot: organization paths, optional state fields, shared id index"
```

---

## Task 2: Paging any resource type

**Files:**
- Modify: `src/extract/page.rs`

- [x] **Step 1: Write the failing test**

Add to the test module in `src/extract/page.rs`:

```rust
    #[test]
    fn page_resources_walks_the_named_type() {
        let server = Server::run();
        server.expect(
            Expectation::matching(request::method_path("GET", "/fhir/Organization"))
                .times(1)
                .respond_with(status_code(200).body(bundle(
                    &[serde_json::json!({"resourceType":"Organization","id":"org-1","meta":{"lastUpdated":"2026-01-01T00:00:00Z"}})],
                    None,
                ))),
        );
        let dir = tempfile::tempdir().unwrap();
        let incoming = dir.path().join("inc.ndjson");
        let client = FhirClient::new(None, 1, std::time::Duration::from_secs(5)).unwrap();
        let mut report = Report::default();
        let got = page_resources(&client, &server.url("/fhir").to_string(), "Organization", None, &incoming, &mut report).unwrap();
        assert_eq!(got.notes.len(), 1);
        assert_eq!(got.notes[0].id, "org-1");
        assert_eq!(got.notes[0].boundary_url, None);
        assert!(std::fs::read_to_string(&incoming).unwrap().contains("\"id\":\"org-1\""));
    }
```

- [x] **Step 2: Run the test to see it fail**

Run: `cargo test --bin kiln page_resources_walks 2>&1 | grep -c 'cannot find'`
Expected: `1` or more.

- [x] **Step 3: Implement**

In `src/extract/page.rs`, rename the existing function to `page_resources` with a `resource_type: &str` parameter after `server`, build the URL from it, and add the old name as a wrapper:

```rust
pub fn page_locations(
    client: &FhirClient,
    server: &str,
    since: Option<&str>,
    incoming: &Path,
    report: &mut Report,
) -> Result<PageResult> {
    page_resources(client, server, "Location", since, incoming, report)
}

/// Walk `<server>/<resource_type>` with `_count` paging, appending every
/// resource to `incoming` and noting what the merge needs. Boundary URLs
/// are noted for any type; only Locations carry them.
pub fn page_resources(
    client: &FhirClient,
    server: &str,
    resource_type: &str,
    since: Option<&str>,
    incoming: &Path,
    report: &mut Report,
) -> Result<PageResult> {
    let base = server.trim_end_matches('/');
    let mut url = Url::parse(&format!("{base}/{resource_type}"))
        .map_err(|_| KilnError::Usage(format!("--server is not a valid URL: {server}")))?;
    // ... the existing body from here on, unchanged ...
```

- [x] **Step 4: Run the tests**

Run: `cargo test --bin kiln extract::page 2>&1 | grep 'test result'`
Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add src/extract/page.rs
git commit -m "extract: page any resource type"
```

---

## Task 3: Merging into any snapshot file

**Files:**
- Modify: `src/snapshot/merge.rs`

- [x] **Step 1: Write the failing test**

Add to the test module in `src/snapshot/merge.rs` (it already has helpers that write an incoming file and build `PageNote`s; follow the shape of the nearest existing test for setup):

```rust
    #[test]
    fn merge_file_upserts_organizations_without_touching_locations() {
        let dir = tempfile::tempdir().unwrap();
        let snap = Snapshot::new(dir.path());
        std::fs::write(snap.locations(), "{\"id\":\"loc\"}\n").unwrap();
        std::fs::write(
            snap.organizations(),
            "{\"resourceType\":\"Organization\",\"id\":\"org-a\",\"meta\":{\"lastUpdated\":\"2026-01-01T00:00:00Z\"},\"name\":\"old\"}\n",
        )
        .unwrap();
        std::fs::write(
            snap.incoming_organizations(),
            "{\"resourceType\":\"Organization\",\"id\":\"org-a\",\"meta\":{\"lastUpdated\":\"2026-01-02T00:00:00Z\"},\"name\":\"new\"}\n{\"resourceType\":\"Organization\",\"id\":\"org-b\",\"meta\":{\"lastUpdated\":\"2026-01-03T00:00:00Z\"}}\n",
        )
        .unwrap();
        let notes = vec![
            PageNote { id: "org-a".into(), last_updated: Some("2026-01-02T00:00:00Z".into()), boundary_url: None },
            PageNote { id: "org-b".into(), last_updated: Some("2026-01-03T00:00:00Z".into()), boundary_url: None },
        ];
        let mut report = Report::default();
        let files = MergeFiles::organizations(&snap);
        let stats = merge_file(&files, &notes, false, &|_| None, &HashMap::new(), &mut report).unwrap();
        assert_eq!((stats.total, stats.added, stats.updated), (2, 1, 1));
        assert_eq!(stats.watermark.as_deref(), Some("2026-01-03T00:00:00Z"));
        let text = std::fs::read_to_string(snap.organizations()).unwrap();
        assert!(text.contains("\"name\":\"new\""));
        assert!(!text.contains("old"));
        assert_eq!(std::fs::read_to_string(snap.locations()).unwrap(), "{\"id\":\"loc\"}\n");
        assert!(!snap.incoming_organizations().exists());
        assert!(!snap.organizations_tmp().exists());
    }
```

- [x] **Step 2: Run the test to see it fail**

Run: `cargo test --bin kiln merge_file_upserts 2>&1 | grep -c 'cannot find'`
Expected: non-zero.

- [x] **Step 3: Implement**

In `src/snapshot/merge.rs` add, above `merge`:

```rust
/// The three files one merge touches, plus the report kind for a line in
/// the existing file that cannot be parsed.
pub struct MergeFiles {
    pub existing: std::path::PathBuf,
    pub tmp: std::path::PathBuf,
    pub incoming: std::path::PathBuf,
    pub unparsed_kind: &'static str,
}

impl MergeFiles {
    pub fn locations(snap: &Snapshot) -> Self {
        Self {
            existing: snap.locations(),
            tmp: snap.locations_tmp(),
            incoming: snap.incoming(),
            unparsed_kind: "snapshot_line_unparsed",
        }
    }
    pub fn organizations(snap: &Snapshot) -> Self {
        Self {
            existing: snap.organizations(),
            tmp: snap.organizations_tmp(),
            incoming: snap.incoming_organizations(),
            unparsed_kind: "organization_line_unparsed",
        }
    }
}

/// `merge` for any snapshot file. Boundary inlining only ever triggers on
/// notes that carry a `boundary_url`, which Organizations never do.
pub fn merge_file(
    files: &MergeFiles,
    notes: &[PageNote],
    full: bool,
    lookup: &dyn Fn(&str) -> Option<Vec<u8>>,
    failures: &HashMap<String, String>,
    report: &mut Report,
) -> Result<MergeStats> {
    let input = MergeInput {
        notes,
        full,
        lookup,
        failures,
    };
    let result = merge_into(files, &input, report);
    if result.is_err() {
        let _ = std::fs::remove_file(&files.tmp);
    }
    result
}
```

Change `merge` to `merge_file(&MergeFiles::locations(snap), notes, full, lookup, failures, report)`. Change `merge_into`'s signature to `(files: &MergeFiles, input: &MergeInput, report: &mut Report)` and inside it replace `snap.locations()` with `files.existing`, `snap.incoming()` with `files.incoming`, `tmp` with `&files.tmp`, `"snapshot_line_unparsed"` with `files.unparsed_kind`, and the directory fsync's `snap.dir` with `files.existing.parent()` (skip when `None`). The `let tmp = ...` in `merge` goes away.

- [x] **Step 4: Run the tests**

Run: `cargo test --bin kiln snapshot::merge 2>&1 | grep 'test result'`
Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add src/snapshot/merge.rs
git commit -m "snapshot: merge_file for any snapshot file"
```

---

## Task 4: The Organization phase in extract

**Files:**
- Modify: `src/extract/mod.rs`
- Modify: `tests/extract.rs`

- [x] **Step 1: Write the failing tests**

In `tests/extract.rs` add two helpers after `since_search`:

```rust
/// `GET <base>/Organization?_lastUpdated=ge<since>`.
fn org_since_search(since: &str) -> impl Matcher<Req> {
    all_of![
        request::method_path("GET", "/fhir/Organization"),
        request::query(url_decoded(contains((
            "_lastUpdated",
            format!("ge{since}")
        )))),
    ]
}

/// Every extract pages Organizations after Locations. Tests about
/// Locations answer that search with an empty bundle, any number of times.
fn expect_no_organizations(server: &Server) {
    server.expect(
        Expectation::matching(request::method_path("GET", "/fhir/Organization"))
            .times(0..)
            .respond_with(ok(bundle(vec![], None))),
    );
}

fn org(id: &str, updated: &str, name: &str) -> Value {
    json!({"resourceType": "Organization", "id": id, "active": true, "name": name,
           "meta": {"lastUpdated": updated}})
}

fn org_lines(snapshot: &Path) -> Vec<Value> {
    std::fs::read_to_string(snapshot.join("organizations.ndjson"))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}
```

Add `expect_no_organizations(&server);` as the first line after every `let server = Server::run();` in the existing tests (there are about twenty; `grep -n 'Server::run()' tests/extract.rs` lists them; the `run_*` tests included). Two tests name their servers differently: `server_mismatch_is_a_usage_error_and_full_repoints` (`a`, `b`) and `circuit_breaker_aborts_and_keeps_the_old_snapshot` (`good`, `bad`); call the helper on each of those too.

Append two tests:

```rust
#[test]
fn organizations_are_paged_after_locations_with_their_own_watermark() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    server.expect(
        Expectation::matching(full_search())
            .times(1)
            .respond_with(ok(bundle(vec![loc("a", "2026-01-01T00:00:00Z", None)], None))),
    );
    server.expect(
        Expectation::matching(all_of![
            request::method_path("GET", "/fhir/Organization"),
            request::query(url_decoded(not(contains(key("_lastUpdated"))))),
        ])
        .times(1)
        .respond_with(ok(bundle(vec![org("org-a", "2026-02-01T00:00:00Z", "A")], None))),
    );
    let a = extract(&server, snap.path(), &[]).success();
    assert!(stdout(&a).contains("organizations: 1 resources, 1 new, 0 updated"), "{}", stdout(&a));
    assert_eq!(org_lines(snap.path()).len(), 1);
    let st = state(snap.path());
    assert_eq!(st["watermark"], "2026-01-01T00:00:00Z", "the Location watermark is its own");
    assert_eq!(st["organization_watermark"], "2026-02-01T00:00:00Z");
    assert_eq!(st["organization_count"], 1);

    // Incremental: each type asks since its own watermark.
    server.expect(
        Expectation::matching(since_search("2026-01-01T00:00:00Z"))
            .times(1)
            .respond_with(ok(bundle(vec![], None))),
    );
    server.expect(
        Expectation::matching(org_since_search("2026-02-01T00:00:00Z"))
            .times(1)
            .respond_with(ok(bundle(vec![org("org-b", "2026-02-02T00:00:00Z", "B")], None))),
    );
    extract(&server, snap.path(), &[]).success();
    let orgs = org_lines(snap.path());
    assert_eq!(orgs.len(), 2);
    assert_eq!(orgs[1]["id"], "org-b");
    assert_eq!(state(snap.path())["organization_watermark"], "2026-02-02T00:00:00Z");
    assert_eq!(state(snap.path())["count"], 1);
}

#[test]
fn a_snapshot_without_organizations_fetches_them_in_full() {
    let server = Server::run();
    let snap = tempfile::tempdir().unwrap();
    // A plan 2 snapshot: locations and a state file with no organization fields.
    std::fs::write(snap.path().join("locations.ndjson"), format!("{}\n", loc("a", "2026-01-01T00:00:00Z", None))).unwrap();
    std::fs::write(
        snap.path().join("state.json"),
        json!({"server": server.url("/fhir").to_string(), "watermark": "2026-01-01T00:00:00Z", "count": 1,
               "kiln_version": "0.2.0", "completed_at": "2026-01-01T00:00:00Z"}).to_string(),
    )
    .unwrap();
    server.expect(
        Expectation::matching(since_search("2026-01-01T00:00:00Z"))
            .times(1)
            .respond_with(ok(bundle(vec![], None))),
    );
    server.expect(
        Expectation::matching(all_of![
            request::method_path("GET", "/fhir/Organization"),
            request::query(url_decoded(not(contains(key("_lastUpdated"))))),
        ])
        .times(1)
        .respond_with(ok(bundle(vec![org("org-a", "2026-02-01T00:00:00Z", "A")], None))),
    );
    extract(&server, snap.path(), &[]).success();
    assert_eq!(org_lines(snap.path()).len(), 1);
    assert_eq!(state(snap.path())["organization_watermark"], "2026-02-01T00:00:00Z");
}
```

- [x] **Step 2: Run the tests to see them fail**

Run: `cargo test --test extract 2>&1 | grep -E 'test result|panicked' | head -5`
Expected: the two new tests fail (no Organization request is made; the expectation with `.times(1)` is unmet).

- [x] **Step 3: Implement**

In `src/extract/mod.rs`:

1. Change `run_phases` to take `org_since: Option<&str>` as a fourth parameter. In `run_extract`, after computing `since`, compute:

```rust
    // Organizations have their own watermark. A snapshot from before they
    // were extracted has no file and no watermark, so they are fetched in
    // full while Locations stay incremental.
    let org_since: Option<String> = if args.full {
        None
    } else if let Some(s) = &args.since {
        Some(s.clone())
    } else if snap.organizations().exists() {
        state.as_ref().and_then(|st| st.organization_watermark.clone())
    } else {
        None
    };
    let result = run_phases(args, &snap, since.as_deref(), org_since.as_deref());
    if result.is_err() {
        let _ = std::fs::remove_file(snap.incoming());
        let _ = std::fs::remove_file(snap.incoming_organizations());
    }
    result
```

2. In `run_phases`, after the `--refresh` stale-cache block and before `let new_state`, add:

```rust
    // Organizations: page and merge, no boundary phase. This runs after the
    // Location merge so a failure here still leaves a consistent Location
    // snapshot behind, with the state file untouched until both succeed.
    let org_paged = page_resources(
        &client,
        &args.server,
        "Organization",
        org_since,
        &snap.incoming_organizations(),
        &mut report,
    )?;
    eprintln!(
        "paged {} organizations over {} page(s)",
        org_paged.notes.len(),
        org_paged.pages
    );
    let org_stats = merge_file(
        &MergeFiles::organizations(snap),
        &org_paged.notes,
        org_since.is_none(),
        &|_| None,
        &HashMap::new(),
        &mut report,
    )?;
```

3. Fill the state: `organization_watermark: org_stats.watermark.clone(), organization_count: Some(org_stats.total),` and after the `snapshot:` println add:

```rust
    println!(
        "organizations: {} resources, {} new, {} updated, watermark {}",
        org_stats.total,
        org_stats.added,
        org_stats.updated,
        org_stats.watermark.as_deref().unwrap_or("none"),
    );
```

Imports: `use crate::extract::page::{page_locations, page_resources};` and `use crate::snapshot::merge::{merge, merge_file, MergeFiles};`.

- [x] **Step 4: Run the tests**

Run: `cargo test --test extract 2>&1 | grep -E 'test result|panicked'`
Expected: `test result: ok. 23 passed`

- [x] **Step 5: Commit**

```bash
git add src/extract/mod.rs tests/extract.rs
git commit -m "extract: page and merge Organizations with their own watermark"
```

---

## Task 5: Organization columns in the schema

**Files:**
- Modify: `src/index/mod.rs`, `src/index/build.rs`
- Modify: `src/write/schema.rs`

- [x] **Step 1: Write the failing test**

In `src/write/schema.rs`, extend `schema_has_the_documented_columns_in_order`:

```rust
        let pos = |n: &str| names.iter().position(|x| *x == n).unwrap();
        assert!(pos("ownership") < pos("nhfr_code"));
        assert!(pos("nhfr_code") < pos("nhfr_uid"));
        assert!(pos("nhfr_uid") < pos("organization_identifier"));
        assert!(pos("organization_identifier") < pos("facility_level_text"));
        assert!(pos("facility_level_text") < pos("ownership_text"));
        assert!(pos("overlays_admin_unit_ids") < pos("organization_json"));
        assert!(pos("organization_json") < pos("country"));
```

and in `batch_round_trips_a_row`, set on the row `nhfr_code: Some("05/08".into()), organization_identifier: vec![(Some("s".into()), Some("v".into()))], organization_json: Some("{}".into())` and assert after `finish` that column `nhfr_code` at row 0 is `"05/08"` and `organization_json` is `"{}"` (the test already downcasts string columns by name; copy that pattern).

- [x] **Step 2: Run the test to see it fail**

Run: `cargo test --bin kiln write::schema 2>&1 | grep -E 'no field|panicked' | head -3`
Expected: compile error on the new fields.

- [x] **Step 3: Implement**

`src/index/mod.rs`: add `pub managing_organization: Option<String>,` to `IndexRecord` after `type_code`. `src/index/build.rs`: add `managing_organization: loc.managing_organization,` where the record is built.

`src/write/schema.rs`:

- In `output_schema`, after `utf8("ownership"),` insert:

```rust
        utf8("nhfr_code"),
        utf8("nhfr_uid"),
        Field::new(
            "organization_identifier",
            DataType::List(Arc::new(Field::new(
                "item",
                DataType::Struct(identifier_fields()),
                true,
            ))),
            true,
        ),
        utf8("facility_level_text"),
        utf8("ownership_text"),
```

and in the `fields.extend([...])` block insert `utf8("organization_json"),` before `Field::new("country", ...)`.

- In `OutputRow`, after `ownership`:

```rust
    pub nhfr_code: Option<String>,
    pub nhfr_uid: Option<String>,
    pub organization_identifier: Vec<(Option<String>, Option<String>)>,
    pub facility_level_text: Option<String>,
    pub ownership_text: Option<String>,
```

and after `overlays_admin_unit_ids`: `pub organization_json: Option<String>,`.

- In `RowBatch`, after `ownership: StringBuilder,`:

```rust
    nhfr_code: StringBuilder,
    nhfr_uid: StringBuilder,
    organization_identifier: ListBuilder<StructBuilder>,
    facility_level_text: StringBuilder,
    ownership_text: StringBuilder,
```

and after `overlays`: `organization_json: StringBuilder,`. Initialise them in `new` (`ListBuilder::new(StructBuilder::from_fields(identifier_fields(), 0))` for the list). Factor the identifier push into a helper and use it for both lists:

```rust
fn push_identifiers(b: &mut ListBuilder<StructBuilder>, items: &[(Option<String>, Option<String>)]) {
    let sb = b.values();
    for (system, value) in items {
        sb.field_builder::<StringBuilder>(0)
            .unwrap()
            .append_option(system.as_deref());
        sb.field_builder::<StringBuilder>(1)
            .unwrap()
            .append_option(value.as_deref());
        sb.append(true);
    }
    b.append(true);
}
```

In `push`, after `self.ownership.append_option(...)`:

```rust
        self.nhfr_code.append_option(r.nhfr_code.as_deref());
        self.nhfr_uid.append_option(r.nhfr_uid.as_deref());
        push_identifiers(&mut self.organization_identifier, &r.organization_identifier);
        self.facility_level_text
            .append_option(r.facility_level_text.as_deref());
        self.ownership_text.append_option(r.ownership_text.as_deref());
```

and after `push_list(&mut self.overlays, ...)`: `self.organization_json.append_option(r.organization_json.as_deref());`. In `finish`, push the five arrays after `self.ownership.finish()` and `self.organization_json.finish()` right after `self.overlays.finish()`, in the same order as the schema.

- [x] **Step 4: Run the tests**

Run: `cargo test --bin kiln write:: 2>&1 | grep 'test result'`
Expected: all pass. `cargo test --test transform 2>&1 | grep 'test result'` also passes: new columns are null.

- [x] **Step 5: Commit**

```bash
git add src/index/mod.rs src/index/build.rs src/write/schema.rs
git commit -m "transform: organization columns in the schema"
```

---

## Task 6: Filling the columns in pass two

**Files:**
- Modify: `src/write/dataset.rs`, `src/transform.rs`
- Modify: `tests/transform.rs`

- [x] **Step 1: Write the failing test**

Append to `tests/transform.rs`:

```rust
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
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["transform", "--snapshot"])
        .arg(&snap)
        .arg("--out")
        .arg(&out)
        .assert()
        .success();

    let nhfr = column(&out, "nhfr_code");
    assert_eq!(nhfr["paired"].as_deref(), Some("05/08/1"));
    assert_eq!(nhfr["lonely"], None);
    assert_eq!(nhfr["clinic"], None, "a facility with no managingOrganization");
    assert_eq!(column(&out, "facility_level_text")["paired"].as_deref(), Some("Health Post"));
    assert_eq!(column(&out, "ownership_text")["paired"].as_deref(), Some("Local Government"));
    assert!(column(&out, "organization_json")["paired"].as_deref().unwrap().contains("\"id\":\"org-paired\""));

    let report: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(out.join("_report.json")).unwrap()).unwrap();
    assert_eq!(report["counts"]["organization_missing"], 1);
    assert!(report["counts"].get("organization_name_mismatch").is_none());

    // A renamed Organization is drift, reported once.
    std::fs::write(snap.join("organizations.ndjson"), format!("{}\n", organization("org-paired", "Renamed"))).unwrap();
    Command::cargo_bin("kiln").unwrap().args(["transform", "--snapshot"]).arg(&snap).arg("--out").arg(&out).assert().success();
    let report: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(out.join("_report.json")).unwrap()).unwrap();
    assert_eq!(report["counts"]["organization_name_mismatch"], 1);

    // No organizations file at all: columns null, nothing reported.
    std::fs::remove_file(snap.join("organizations.ndjson")).unwrap();
    Command::cargo_bin("kiln").unwrap().args(["transform", "--snapshot"]).arg(&snap).arg("--out").arg(&out).assert().success();
    assert_eq!(column(&out, "nhfr_code")["paired"], None);
    let report: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(out.join("_report.json")).unwrap()).unwrap();
    assert!(report["counts"].get("organization_missing").is_none());
}
```

Add `use std::path::Path;` to the top of `tests/transform.rs` if it is not already imported (it is: `use std::path::Path;` is the first line).

- [x] **Step 2: Run the test to see it fail**

Run: `cargo test --test transform facility_rows 2>&1 | grep -E 'panicked|test result' | head -3`
Expected: fails on `nhfr["paired"]` being `None`.

- [x] **Step 3: Implement**

In `src/write/dataset.rs`:

1. `write_dataset` gains a last parameter `organizations: Option<&Path>` and passes it to `write_partitions`, which gains the same.
2. At the top of `write_partitions`, after `let mut lines = LineAccess::open(ndjson)?;`:

```rust
    // The Organization side of the facility pairing, when the snapshot has
    // it. Indexed by id; each row's Organization is read on demand.
    let mut organizations = match organizations {
        Some(path) => Some((
            index_by_id(path, report, "organization_line_unparsed")?,
            LineAccess::open(path)?,
        )),
        None => None,
    };
```

3. In the row loop, after `let Some(info) = hierarchy.get(i) else { ... };` and before the parent check, add:

```rust
            let org = match (&rec.managing_organization, organizations.as_mut()) {
                (Some(org_id), Some((index, access))) => match index.get(org_id) {
                    Some(&(offset, len)) => {
                        let text = access.read_at(offset, len)?;
                        let value: serde_json::Value = serde_json::from_str(&text)?;
                        Organization::parse(&value, &mut Report::default())
                    }
                    None => {
                        report.add(
                            "organization_missing",
                            &rec.id,
                            &format!("managingOrganization {org_id} is not in organizations.ndjson"),
                        );
                        None
                    }
                },
                _ => None,
            };
            if let Some(o) = &org {
                if o.name.is_some() && o.name != loc.name {
                    report.add(
                        "organization_name_mismatch",
                        &rec.id,
                        &format!(
                            "Location is named {:?} but Organization {} is named {:?}",
                            loc.name.as_deref().unwrap_or(""),
                            o.id,
                            o.name.as_deref().unwrap_or("")
                        ),
                    );
                }
            }
```

4. In the `OutputRow { .. }` literal add:

```rust
                nhfr_code: org.as_ref().and_then(|o| o.nhfr_code.clone()),
                nhfr_uid: org.as_ref().and_then(|o| o.nhfr_uid.clone()),
                organization_identifier: org
                    .as_ref()
                    .map(|o| o.identifier.iter().map(|i| (i.system.clone(), i.value.clone())).collect())
                    .unwrap_or_default(),
                facility_level_text: org.as_ref().and_then(|o| o.facility_level_text.clone()),
                ownership_text: org.as_ref().and_then(|o| o.ownership_text.clone()),
                organization_json: org.as_ref().map(|o| o.fhir_json.clone()),
```

Imports: `use crate::fhir::Organization;` and `use crate::snapshot::index::index_by_id;`. Every existing `write_dataset(...)` call in the unit tests of `dataset.rs` gets `, None` appended.

In `src/transform.rs`, before calling `write_dataset`:

```rust
    let organizations = args.snapshot.join(ORGANIZATIONS_FILE);
    let organizations = organizations.is_file().then_some(organizations.as_path());
```

and pass `organizations` as the last argument; import `crate::snapshot::ORGANIZATIONS_FILE`.

- [x] **Step 4: Run the tests**

Run: `cargo test 2>&1 | grep -E 'test result|^error' | sort | uniq -c`
Expected: every suite ok, including the new transform test.

- [x] **Step 5: Commit**

```bash
git add src/write/dataset.rs src/transform.rs tests/transform.rs
git commit -m "transform: fill the organization columns from the snapshot"
```

---

## Task 7: diff rebuilds the pair

**Files:**
- Modify: `src/diff/input.rs`, `src/diff/rebuild.rs`, `src/diff/mod.rs`, `src/fhir/location.rs`

- [x] **Step 1: Write the failing unit tests**

In `src/diff/input.rs`, extend `writable_columns_are_the_readme_second_group`:

```rust
        assert!(is_writable("nhfr_code"));
        assert!(is_writable("facility_level_text"));
        assert!(is_writable("organization_identifier"));
        assert!(!is_writable("organization_json"));
```

and add to `identifier_accepts_objects_or_a_json_string_of_them`:

```rust
        assert_eq!(column_from_json("organization_identifier", &list), want);
```

In `src/diff/rebuild.rs` add to the test module:

```rust
    fn org_base() -> Value {
        json!({"resourceType":"Organization","id":"org-a","active":true,"name":"A","meta":{"versionId":"9"},
            "identifier":[{"system":NHFR_CODE_SYSTEM,"value":"05/08/1"}],
            "type":[{"coding":[{"system":ORGANIZATION_TYPE_SYSTEM,"code":"prov"}]},
                    {"coding":[{"system":FACILITY_TYPE_SYSTEM,"code":"primary"}],"text":"Health Post"}]})
    }

    fn snapshot_location() -> Location {
        Location::parse(
            &json!({"resourceType":"Location","id":"a","name":"A","status":"active","managingOrganization":{"reference":"Organization/org-a"}}),
            &mut Report::default(),
        )
        .unwrap()
    }

    #[test]
    fn mirrored_columns_reach_the_organization_only_when_edited() {
        let mut report = Report::default();
        let loc = snapshot_location();
        // Unedited name and status, and a facility level the Location never had: no change.
        let same = row(&[("name", text("A")), ("status", text("active")), ("facility_level", ColumnValue::Null)]);
        assert_eq!(rebuild_organization(&org_base(), &same, Some(&loc), &mut report), org_base());
        // Edited name and status.
        let edited = row(&[("name", text("B")), ("status", text("inactive"))]);
        let out = rebuild_organization(&org_base(), &edited, Some(&loc), &mut report);
        assert_eq!(out["name"], "B");
        assert_eq!(out["active"], false);
        assert_eq!(out["meta"]["versionId"], "9");
        // Facility level edited on the row: the Organization's coding follows.
        let out = rebuild_organization(&org_base(), &row(&[("facility_level", text("secondary"))]), Some(&loc), &mut report);
        assert_eq!(out["type"][1]["coding"][0]["code"], "secondary");
        assert_eq!(out["type"][1]["text"], "Health Post", "the label is untouched");
    }

    #[test]
    fn organization_only_columns_apply_directly() {
        let mut report = Report::default();
        let loc = snapshot_location();
        let r = row(&[
            ("nhfr_code", text("05/08/2")),
            ("nhfr_uid", text("999")),
            ("facility_level_text", text("Clinic")),
            ("ownership_text", text("Private")),
        ]);
        let out = rebuild_organization(&org_base(), &r, Some(&loc), &mut report);
        assert_eq!(out["identifier"], json!([{"system":NHFR_CODE_SYSTEM,"value":"05/08/2"},{"system":NHFR_UID_SYSTEM,"value":"999"}]));
        assert_eq!(out["type"][1]["text"], "Clinic");
        assert_eq!(report.count("input_column_type"), 1, "no ownership coding to label");
        let out = rebuild_organization(&org_base(), &row(&[("facility_level_text", ColumnValue::Null)]), Some(&loc), &mut report);
        assert!(out["type"][1].get("text").is_none());
        let out = rebuild_organization(
            &org_base(),
            &row(&[("organization_identifier", ColumnValue::Identifiers(vec![Identifier { system: Some("s".into()), value: Some("v".into()) }]))]),
            Some(&loc),
            &mut report,
        );
        assert_eq!(out["identifier"], json!([{"system":"s","value":"v"}]));
    }

    #[test]
    fn a_new_organization_carries_the_pairing_shape() {
        let mut report = Report::default();
        let r = row(&[("name", text("New Site")), ("facility_level", text("primary")), ("facility_level_text", text("Health Post")), ("nhfr_code", text("05/09"))]);
        let out = new_organization("org-new", &r, &mut report);
        assert_eq!(out["resourceType"], "Organization");
        assert_eq!(out["id"], "org-new");
        assert_eq!(out["active"], true);
        assert_eq!(out["name"], "New Site");
        assert_eq!(out["type"][0]["coding"][0]["code"], "prov");
        assert_eq!(out["type"][1], json!({"coding":[{"system":FACILITY_TYPE_SYSTEM,"code":"primary"}],"text":"Health Post"}));
        assert_eq!(out["identifier"], json!([{"system":NHFR_CODE_SYSTEM,"value":"05/09"}]));
        assert!(out.get("meta").is_none());
    }
```

Add to the test module imports: `use crate::fhir::organization::{NHFR_CODE_SYSTEM, NHFR_UID_SYSTEM, ORGANIZATION_TYPE_SYSTEM};`.

- [x] **Step 2: Run the tests to see them fail**

Run: `cargo test --bin kiln diff:: 2>&1 | grep -cE 'cannot find|panicked'`
Expected: non-zero.

- [x] **Step 3: Implement the columns and the Organization rebuild**

`src/diff/input.rs`: extend `TEXT_COLUMNS` to 17 entries by appending `"nhfr_code", "nhfr_uid", "facility_level_text", "ownership_text"`, and replace the single identifier column with `pub const IDENTIFIER_COLUMNS: [&str; 2] = ["identifier", "organization_identifier"];` keeping `IDENTIFIER_COLUMN = "identifier"` for the Location rebuild. `is_writable` and `column_from_json` test membership in `IDENTIFIER_COLUMNS`.

`src/fhir/location.rs`: make `strip_reference` `pub(crate)`.

`src/diff/rebuild.rs`, above the test module:

```rust
/// True when the row carries `column` with a value different from what
/// the snapshot Location has. An unedited export repeats every column;
/// only a real edit may reach the Organization, so drift between the two
/// resources is never "corrected" by accident.
fn edited(row: &InputRow, column: &str, current: Option<&str>) -> bool {
    match row.columns.get(column) {
        None => false,
        Some(ColumnValue::Null) => current.is_some(),
        Some(ColumnValue::Text(t)) => Some(t.as_str()) != current,
        Some(_) => false,
    }
}

/// `text` on the concept in `type` that holds a coding under `system`.
fn set_concept_text(
    obj: &mut Object,
    system: &str,
    text: Option<&str>,
    id: &str,
    column: &str,
    report: &mut Report,
) {
    let concept = obj
        .get_mut("type")
        .and_then(Value::as_array_mut)
        .and_then(|types| {
            types.iter_mut().filter_map(Value::as_object_mut).find(|c| {
                c.get("coding")
                    .and_then(Value::as_array)
                    .is_some_and(|cs| cs.iter().any(|x| system_is(x, system)))
            })
        });
    match (concept, text) {
        (Some(c), Some(t)) => {
            c.insert("text".into(), Value::String(t.to_string()));
        }
        (Some(c), None) => {
            c.remove("text");
        }
        (None, Some(_)) => report.add(
            "input_column_type",
            id,
            &format!("{column}: the Organization has no coding under {system} to label; ignored"),
        ),
        (None, None) => {}
    }
}

/// The Organization half of a facility row. `name`, `status`, facility
/// level and ownership mirror from the Location columns when edited (or
/// always, for a create); the NHFR codes, the type labels and the whole
/// `organization_identifier` list are Organization-only and apply as given.
pub fn rebuild_organization(
    base: &Value,
    row: &InputRow,
    snapshot: Option<&Location>,
    report: &mut Report,
) -> Value {
    let mut obj = base.as_object().cloned().unwrap_or_default();
    let id = obj
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("<unknown>")
        .to_string();
    let mirror = |column: &str, current: Option<&str>| snapshot.is_none() || edited(row, column, current);
    if let Some(v) = row.columns.get("organization_identifier") {
        apply_identifier_list(&mut obj, v);
    }
    for (name, value) in &row.columns {
        let t = text_of(value);
        match name.as_str() {
            "name" if mirror("name", snapshot.and_then(|l| l.name.as_deref())) => set_string(&mut obj, "name", t),
            "status" if mirror("status", snapshot.and_then(|l| l.status.as_deref())) => {
                if let Some(s) = t {
                    obj.insert("active".into(), Value::Bool(s == "active"));
                }
            }
            "facility_level" if mirror("facility_level", snapshot.and_then(|l| l.facility_level.as_deref())) => {
                upsert_type_coding(&mut obj, FACILITY_TYPE_SYSTEM, t)
            }
            "ownership" if mirror("ownership", snapshot.and_then(|l| l.ownership.as_deref())) => {
                upsert_type_coding(&mut obj, OWNERSHIP_SYSTEM, t)
            }
            "facility_level_text" => set_concept_text(&mut obj, FACILITY_TYPE_SYSTEM, t, &id, name, report),
            "ownership_text" => set_concept_text(&mut obj, OWNERSHIP_SYSTEM, t, &id, name, report),
            "nhfr_code" => upsert_identifier(&mut obj, NHFR_CODE_SYSTEM, t),
            "nhfr_uid" => upsert_identifier(&mut obj, NHFR_UID_SYSTEM, t),
            _ => {}
        }
    }
    Value::Object(obj)
}

/// The Organization for a new facility row: the pairing shape bake used,
/// then the row's columns on top.
pub fn new_organization(org_id: &str, row: &InputRow, report: &mut Report) -> Value {
    let base = json!({
        "resourceType": "Organization",
        "id": org_id,
        "active": true,
        "type": [{"coding": [{"system": ORGANIZATION_TYPE_SYSTEM, "code": "prov", "display": "Healthcare Provider"}]}],
    });
    rebuild_organization(&base, row, None, report)
}
```

Imports: `use crate::fhir::organization::{NHFR_CODE_SYSTEM, NHFR_UID_SYSTEM, ORGANIZATION_TYPE_SYSTEM};`. Note `Location` in `rebuild.rs` must expose `status`, `facility_level`, `ownership`: it does (they are fields of `Location`).

- [x] **Step 4: Run the unit tests**

Run: `cargo test --bin kiln diff:: 2>&1 | grep 'test result'`
Expected: all pass.

- [x] **Step 5: Wire the pair into the command**

In `src/diff/mod.rs`:

1. `DiffStats` gains `pub locations: usize, pub organizations: usize` (resources written).
2. `Diff` gains `organizations: Option<(HashMap<String, (u64, usize)>, LineAccess)>`.
3. Replace the body of `process` from `let rebuilt = rebuild(...)` to the end with:

```rust
        let mut rebuilt = rebuild(&base, snapshot.as_ref(), &row, report);
        let is_create = existing.is_none();
        let mut emitted = 0usize;

        // The Organization half, when the snapshot has Organizations.
        if let Some((org_index, org_access)) = self.organizations.as_mut() {
            let org_ref = rebuilt
                .get("managingOrganization")
                .and_then(|m| m.get("reference"))
                .and_then(Value::as_str)
                .and_then(strip_reference);
            match org_ref {
                Some(org_id) => match org_index.get(&org_id) {
                    Some(&(offset, len)) => {
                        let text = org_access.read_at(offset, len)?;
                        let org_base: Value = serde_json::from_str(&text)?;
                        let org_new = rebuild_organization(&org_base, &row, snapshot.as_ref(), report);
                        if canonical(&org_new) != canonical(&org_base) {
                            writeln!(self.out, "{org_new}").map_err(|e| KilnError::io(self.out_path, e))?;
                            self.stats.organizations += 1;
                            emitted += 1;
                        }
                    }
                    None => report.add(
                        "organization_missing",
                        &id,
                        &format!("managingOrganization {org_id} is not in organizations.ndjson; Organization not updated"),
                    ),
                },
                None if is_create && row.columns.get("type") == Some(&ColumnValue::Text("facility".into())) => {
                    let org_id = format!("org-{id}");
                    let org_new = new_organization(&org_id, &row, report);
                    writeln!(self.out, "{org_new}").map_err(|e| KilnError::io(self.out_path, e))?;
                    self.stats.organizations += 1;
                    emitted += 1;
                    if let Some(obj) = rebuilt.as_object_mut() {
                        obj.insert(
                            "managingOrganization".into(),
                            json!({"reference": format!("Organization/{org_id}")}),
                        );
                    }
                }
                None => {}
            }
        }

        if is_create || canonical(&rebuilt) != canonical(&base) {
            writeln!(self.out, "{rebuilt}").map_err(|e| KilnError::io(self.out_path, e))?;
            self.stats.locations += 1;
            emitted += 1;
        }
        if is_create {
            self.stats.created += 1;
        } else if emitted > 0 {
            self.stats.changed += 1;
        } else {
            self.stats.unchanged += 1;
        }
        Ok(())
```

4. In `run_diff`, after indexing the snapshot:

```rust
    let org_path = args.snapshot.join(ORGANIZATIONS_FILE);
    let organizations = if org_path.is_file() {
        let index = index_by_id(&org_path, &mut report, "organization_line_unparsed")?;
        eprintln!("indexed {} organizations", index.len());
        Some((index, LineAccess::open(&org_path)?))
    } else {
        eprintln!("no organizations.ndjson in the snapshot: organisation columns are ignored and no Organizations are written");
        None
    };
```

pass it into `Diff { organizations, .. }`, and after the `N changed` line print:

```rust
    println!(
        "Location: {}, Organization: {}",
        stats.locations, stats.organizations
    );
```

Imports: `crate::diff::input::ColumnValue`, `crate::diff::rebuild::{new_organization, rebuild, rebuild_organization}`, `crate::fhir::location::strip_reference`, `crate::snapshot::ORGANIZATIONS_FILE`.

- [x] **Step 6: Run the tests**

Run: `cargo test 2>&1 | grep -E 'test result|^error' | sort | uniq -c`
Expected: every suite ok. The diff binary tests still pass because their snapshot has no `organizations.ndjson` yet; Task 9 adds it.

- [x] **Step 7: Commit**

```bash
git add src/diff/input.rs src/diff/rebuild.rs src/diff/mod.rs src/fhir/location.rs
git commit -m "diff: rebuild the Organization beside its facility Location"
```

---

## Task 8: load posts Organizations first

**Files:**
- Modify: `src/load/order.rs`
- Modify: `tests/load.rs`

- [x] **Step 1: Write the failing tests**

In `src/load/order.rs` add to the test module:

```rust
    #[test]
    fn organizations_come_before_every_location() {
        let input = vec![
            loc("clinic", Some("ward")),
            json!({"resourceType": "Organization", "id": "org-clinic"}),
            loc("ward", None),
        ];
        let out = order_parents_first(input).unwrap();
        assert_eq!(ids(&out), vec!["org-clinic", "ward", "clinic"]);
    }
```

In `tests/load.rs` append:

```rust
#[test]
fn organizations_are_posted_before_their_locations() {
    let server = Server::run();
    let dir = tempfile::tempdir().unwrap();
    let mut clinic = loc("clinic", Some("2"), None);
    clinic["managingOrganization"] = json!({"reference": "Organization/org-clinic"});
    let org = json!({"resourceType": "Organization", "id": "org-clinic", "name": "clinic", "meta": {"versionId": "5"}});
    let input = write_ndjson(dir.path(), &[clinic, org]);
    expect_metadata(&server, true);
    server.expect(
        Expectation::matching(posted_bundle(|b| {
            b["entry"][0]["request"] == json!({"method": "PUT", "url": "Organization/org-clinic", "ifMatch": "W/\"5\""})
                && b["entry"][1]["request"]["url"] == "Location/clinic"
        }))
        .times(1)
        .respond_with(ok(transaction_response())),
    );
    load(&server, &input, &[]).success();
}
```

- [x] **Step 2: Run the tests to see them fail**

Run: `cargo test organizations_ 2>&1 | grep -E 'test result|panicked' | head -4`
Expected: both fail on ordering.

- [x] **Step 3: Implement**

In `src/load/order.rs`, change the sort to put non-Locations first:

```rust
    let is_location = |i: usize| resources[i].get("resourceType").and_then(Value::as_str) == Some("Location");
    let mut keyed: Vec<(bool, usize, usize)> = Vec::with_capacity(resources.len());
    for i in 0..resources.len() {
        keyed.push((is_location(i), depth(i, &resources, &by_id, &mut depths)?, i));
    }
    keyed.sort_by_key(|&(l, d, i)| (l, d, i));
```

adjusting the `map(|(_, i)| ..)` to `map(|(_, _, i)| ..)`, and update the module doc: "Organizations (any non-Location) first, then Locations parents first: a Location's managingOrganization must exist before the Location is written."

- [x] **Step 4: Run the tests**

Run: `cargo test organizations_ 2>&1 | grep 'test result'`
Expected: both pass; `cargo test --test load` all pass.

- [x] **Step 5: Commit**

```bash
git add src/load/order.rs tests/load.rs
git commit -m "load: Organizations before the Locations that reference them"
```

---

## Task 9: diff binary tests for the pair

**Files:**
- Modify: `tests/diff.rs`

- [x] **Step 1: Give the test snapshot an Organization**

In `tests/diff.rs`:

- Add constants `const NHFR_CODE: &str = "https://icr.healthcampaigns.org/identifiers/nga-nhfr-code";` and `const FACILITY_TYPE: &str = "https://icr.healthcampaigns.org/CodeSystem/icr-facility-type-cs";`.
- In `clinic()` add `"managingOrganization": {"reference": "Organization/org-clinic"}`.
- Add:

```rust
fn org_clinic() -> Value {
    json!({
        "resourceType": "Organization", "id": "org-clinic", "active": true, "name": "Gama Clinic",
        "meta": {"versionId": "9"},
        "identifier": [{"system": NHFR_CODE, "value": "05/08/1"}],
        "type": [
            {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/organization-type", "code": "prov"}]},
            {"coding": [{"system": FACILITY_TYPE, "code": "primary"}], "text": "Health Post"}
        ]
    })
}
```

- In `snapshot()` also write `organizations.ndjson` with `org_clinic()`.
- In `export()`, the clinic row gains the organisation columns an export carries: `"nhfr_code": "05/08/1", "nhfr_uid": null, "facility_level": null, "facility_level_text": "Health Post", "ownership_text": null, "organization_identifier": "[{\"system\":\"https://icr.healthcampaigns.org/identifiers/nga-nhfr-code\",\"value\":\"05/08/1\"}]"`.

Update the existing assertions that change shape:

- `a_rename_emits_one_complete_resource_with_its_version`: expect `1 changed, 1 unchanged, 0 new` still, `got.len() == 2`, `got[0]["resourceType"] == "Organization"` with `name` "Gama PHC" and `meta.versionId` "9", and the Location assertions on `got[1]`.
- `new_rows_become_creates_with_and_without_an_id`: `got.len() == 3`; `got[0]` is `Organization` `org-newsite` with `active == true` and `name` "New Site"; `got[1]` is the `newsite` Location with `managingOrganization.reference == "Organization/org-newsite"`; `got[2]` is the nameless row (no `type` column, so no pair). The stdout line still says `2 new`.
- `duplicate_input_rows_are_reported_and_skipped`: `got.len() == 2` and the Location (`got[1]`) is named "First".

- [x] **Step 2: Add the pair tests**

```rust
#[test]
fn nhfr_code_edits_touch_the_organization_only() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let mut rows = export();
    rows[1]["properties"]["nhfr_code"] = json!("05/08/2");
    rows[1]["properties"]["facility_level_text"] = json!("Clinic");
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    let a = diff(&snap, &input, &out).success();
    assert!(stdout(&a).contains("Location: 0, Organization: 1"), "{}", stdout(&a));
    let got = changes(&out);
    assert_eq!(got.len(), 1);
    assert_eq!(got[0]["resourceType"], "Organization");
    assert_eq!(got[0]["identifier"][0]["value"], "05/08/2");
    assert_eq!(got[0]["type"][1]["text"], "Clinic");
    assert_eq!(got[0]["type"][1]["coding"][0]["code"], "primary", "an unedited facility_level never reaches the Organization");
}

#[test]
fn retiring_a_facility_deactivates_its_organization() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let mut rows = export();
    rows[1]["properties"]["status"] = json!("inactive");
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    diff(&snap, &input, &out).success();
    let got = changes(&out);
    assert_eq!(got.len(), 2);
    assert_eq!(got[0]["active"], false);
    assert_eq!(got[1]["status"], "inactive");
}

#[test]
fn a_new_settlement_row_stays_a_single_resource() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    let rows = vec![feature(json!({"id": "village", "name": "Village", "type": "settlement", "part_of": "ng"}), point(4.0, 7.0))];
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    diff(&snap, &input, &out).success();
    let got = changes(&out);
    assert_eq!(got.len(), 1);
    assert_eq!(got[0]["resourceType"], "Location");
    assert!(got[0].get("managingOrganization").is_none());
}

#[test]
fn without_an_organizations_file_diff_is_location_only() {
    let dir = tempfile::tempdir().unwrap();
    let snap = snapshot(dir.path());
    std::fs::remove_file(snap.join("organizations.ndjson")).unwrap();
    let mut rows = export();
    rows[1]["properties"]["name"] = json!("Gama PHC");
    let input = write_input(dir.path(), "edits.geojson", &collection(rows));
    let out = dir.path().join("changes.ndjson");
    let a = diff(&snap, &input, &out).success();
    assert!(String::from_utf8_lossy(&a.get_output().stderr).contains("no organizations.ndjson"));
    let got = changes(&out);
    assert_eq!(got.len(), 1);
    assert_eq!(got[0]["resourceType"], "Location");
}
```

`geoparquet_written_by_transform_round_trips_unchanged` needs no change: it must still print `0 changed` for both files even though the facility row now carries organisation columns and the Organization has a facility-level coding the Location lacks.

- [x] **Step 3: Run the tests**

Run: `cargo test --test diff 2>&1 | grep -E 'test result|panicked|FAILED'`
Expected: `test result: ok. 18 passed`. If the round-trip test fails, the `edited` rule in `rebuild_organization` is wrong; fix it, not the test.

- [x] **Step 4: Commit**

```bash
git add tests/diff.rs
git commit -m "diff: binary tests for the facility pair"
```

---

## Task 10: Docs, spec status, real data

**Files:**
- Modify: `README.md`, `docs/qgis.md`, `docs/superpowers/specs/2026-09-05-facility-organization-design.md`

- [x] **Step 1: README**

1. "The snapshot" layout block: add `organizations.ndjson     one Organization per line, for the facility pairing` after `locations.ndjson`, and after the `state.json` paragraph add: "Organizations are paged after Locations with their own watermark, `organization_watermark` in `state.json`, and merged the same way. A snapshot written before kiln extracted Organizations has no file and no watermark, so the next run fetches them in full while Locations stay incremental."
2. "Columns", writable group: add rows for `nhfr_code`, `nhfr_uid` (promoted from the Organization's identifiers by system), `organization_identifier` (list of struct), `facility_level_text`, `ownership_text` (the `text` of the Organization's type concepts); lossless fallback: add `organization_json`. Add one paragraph after the table: "A facility in the ICR registry is a Location paired with an Organization (`org-<location id>`) that carries the registry codes and the institution's name and type. Rows whose Location names a managing organisation that is in the snapshot carry its fields; the Location's own `facility_level` and `ownership` codings are what the row shows for those two, and a Location whose Organization is missing is reported as `organization_missing`, a name that differs as `organization_name_mismatch`."
3. "Round trip", diff step 2: add "`name`, `status`, `facility_level` and `ownership` on a facility row apply to both resources when edited; the NHFR codes, the type labels and `organization_identifier` apply to the Organization only. A new `type = facility` row creates both, the Organization first." Report kinds list: add `organization_missing`.
4. "load": add "Organizations are posted before Locations, so a new pair resolves within the run."
5. Report kinds in "The report": add `organization_missing`, `organization_name_mismatch` under Dataset; extract's list gains `organization_line_unparsed`.

- [x] **Step 2: docs/qgis.md**

In step 3 ("Edit"), add a bullet: "**Facilities are two resources.** Renaming a facility, retiring it, or changing its level or ownership updates both its Location and its Organization; the NHFR code, uid and the type labels live on the Organization only. A new facility row creates both."

- [x] **Step 3: Spec status**

Change the spec's `**Status:**` line to `Implemented on branch \`facility-organizations\` (plan \`docs/superpowers/plans/2026-09-05-facility-organizations.md\`). Amendment: mirrored columns (name, status, facility_level, ownership) reach the Organization only when the row's value differs from the snapshot Location's, so an unedited export never rewrites an Organization that has drifted from its Location.`

- [x] **Step 4: Real data**

With the token from `CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.14 gcloud auth print-access-token`:

```sh
export KILN_TOKEN=...
S=https://healthcare.googleapis.com/v1/projects/icr-registry/locations/us-west1/datasets/ICR/fhirStores/icr-demo/fhir
./target/debug/kiln extract --server $S --snapshot data/icr-demo/snapshot        # fetches Organizations in full
./target/debug/kiln transform --snapshot data/icr-demo/snapshot --out data/icr-demo/out
./target/debug/kiln diff --snapshot data/icr-demo/snapshot --in 'data/icr-demo/out/locations/country=NGA/geom_type=point/type=facility/part-0.parquet' --out data/icr-demo/rt.ndjson
```

Expected: `organizations: 1468 resources`; the transform report shows exactly one `organization_name_mismatch` (facility `a621fa4f-...`, renamed earlier without its Organization); the diff prints `0 changed`. Record the three numbers in the PR description. Do not load anything.

- [x] **Step 5: Commit**

```bash
git add README.md docs/qgis.md docs/superpowers/specs/2026-09-05-facility-organization-design.md
git commit -m "README, qgis.md: document the facility Organization pairing"
```

---

## Self-review

- **Spec coverage.** Extract with own watermark and the no-file case: Task 4. Columns, join, `organization_missing`, `organization_name_mismatch`, no-file silence: Tasks 5, 6. Mirroring rules, Organization-only columns, pair creation for `type = facility`, Organization emitted first, per-resource counts, no-file warning: Task 7, tested in Task 9. Load ordering: Task 8. Docs: Task 10. The spec's `organization_line_unparsed` kind: Tasks 1, 3, 6, 7.
- **Amendment recorded in Task 10:** mirrored columns apply to the Organization only when edited relative to the snapshot Location. The spec said "apply to both"; an unedited export would otherwise rewrite drifted Organizations, and the transform round trip proves the rule.
- **Type consistency.** `index_by_id(path, report, kind)` in Tasks 1, 6, 7. `MergeFiles::organizations(snap)` and `merge_file(&files, notes, full, lookup, failures, report)` in Tasks 3, 4. `page_resources(client, server, type, since, incoming, report)` in Tasks 2, 4. `rebuild_organization(base, row, snapshot: Option<&Location>, report)` and `new_organization(org_id, row, report)` in Tasks 7, 9. `write_dataset(.., organizations: Option<&Path>)` as the last parameter in Task 6 and its tests. `DiffStats { changed, unchanged, created, locations, organizations }` in Task 7 and the `Location: a, Organization: b` line in Task 9.
- **httptest note.** Every existing extract test must call `expect_no_organizations` or the new Organization search is an unexpected request and the server panics on drop.
