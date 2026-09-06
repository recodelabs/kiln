# kiln diff and load — design (plan 3 of the Rust rewrite)

**Date:** 2026-09-05
**Status:** Draft, awaiting review before the implementation plan is written.
**Builds on:** README.md ("Round trip: diff and load", "Columns", "What is writable"), plan 1 (`transform`, `inspect`, PR #8), plan 2 (`extract`, `run`, PR #9)

## Problem

The Rust binary produces the GeoParquet dataset and keeps the snapshot up to
date, but the round trip the README promises does not exist yet: an edit made
in QGIS or DuckDB has no way back to the FHIR server. The Python package has a
`load` that PUTs whole NDJSON files with no version check and no `diff` at
all. Editing a stale copy of the registry is only safe when every write is
version checked, and only useful when the operator can see exactly which
resources will change before anything is sent.

## Goals

- `kiln diff` turns an edited GeoJSON or GeoParquet file into plain FHIR
  NDJSON containing only the Locations that actually changed, each complete
  and carrying the `meta.versionId` it was based on.
- Only the README's writable columns flow back; derived columns are ignored
  even when stale; everything kiln does not model survives untouched via
  `fhir_json`.
- A float round trip through a GIS tool is not an edit: geometry compares on
  WKB after rounding to seven decimals.
- `kiln load` sends the changes as transaction bundles, parents first, with
  `If-Match` on every existing resource, a capability preflight, retry and
  backoff, and a clear list of conflicting ids on 412.
- Bounded memory in `diff`: the snapshot is indexed by id and offset, never
  loaded; the input is read one feature or one record batch at a time.
- No network in `diff`; nothing but the network in `load`. Same split as
  extract and transform.
- Generic FHIR R4: transaction bundles, `If-Match`, `CapabilityStatement`.

## Non-goals

- Deleting resources. A row missing from the input is not a deletion.
- Validating against the ICR profile. The NDJSON checkpoint exists so a
  validator can be run on it.
- Reading CSV, Shapefile, GeoPackage or any other input. GeoJSON and
  GeoParquet cover QGIS, DuckDB and Python.
- Writing derived columns anywhere. `admin1_name` on a row is never written.
- Updating the snapshot after a load. The next extract does that.
- Partial application of a failed bundle, or continuing past one.

## Decisions taken

These are the choices the README leaves open. They were taken as defaults
while drafting; change any of them here before the plan is written.

| Question | Decision |
|---|---|
| Input formats | `.geojson` / `.json` (FeatureCollection, streamed one feature at a time), `.geojsonl` / `.geojsons` (one Feature per line), `.parquet` (GeoParquet, one record batch at a time). Anything else is a usage error. |
| Where a row's id comes from | The `id` property or column. A `Feature.id` is used when the property is absent. |
| Row with no id | New Location with a generated UUID v4 id, reported as `new_location_generated_id`. |
| Row with an id not in the snapshot | New Location with that id, reported as `new_location`. |
| Column absent vs null | Absent: untouched. Present and null (or empty string): the FHIR field is removed. GIS exports carry every column, so null must mean "clear" or a cleared name could never round trip. |
| Which geometry a `geometry` edit changes | The one it came from: the boundary attachment when the snapshot resource has a decoded boundary, `position` when it does not. |
| Point drawn on a polygon row | Reported as `geometry_kind_changed` and the geometry edit is skipped; other column edits on the row still apply. Erasing a boundary by drawing a point is not a plausible intent. |
| Polygon drawn on a point row | A boundary attachment is added (inline base64 GeoJSON, ICR extension URL). Adding a boundary to a site is a legitimate edit. |
| `position_*` columns and a point geometry both edited and disagreeing | The geometry wins; reported as `position_geometry_disagree`. |
| Coordinates written back | Rounded to seven decimals, same as the comparison. |
| Replaced boundary attachment | Replaces the whole `valueAttachment` with `{contentType, data}`; a URL attachment becomes inline. The extension URL already on the resource is kept; a new extension uses the ICR URL. |
| `pcode` / `gers_id` vs `identifier` | `identifier` is applied first (whole list replaced), then `pcode` and `gers_id` upsert their system's entry. |
| `type`, `facility_level`, `ownership` | `type` edits the code of the first coding of the first `Location.type` concept, keeping its system. `facility_level` and `ownership` upsert the coding whose system is the ICR facility-type or ownership system, adding a concept if none exists, removing it when cleared. |
| `part_of`, `managing_organization` | Bare id in; the reference keeps the original's prefix (`Location/`, `Organization/`, or an absolute URL base) when replacing its last segment, otherwise `Location/<id>` and `Organization/<id>`. |
| Diff report | Summary printed; written to `--report FILE` when given. Report kinds below. |
| Load input | Any NDJSON of FHIR resources with `resourceType` and `id`, not only diff output, so baked files load too. Ordering uses `partOf` where present. |
| Load preflight | Always GETs `/metadata` (it also validates URL and token). Refuses only when the input holds a resource without `meta.versionId` and the server does not advertise `updateCreate` for its type. |
| Conflict reporting | On a bundle rejected with 412 or 409, kiln GETs `<Type>/<id>` for each entry that carried `If-Match` and lists the ids whose server `versionId` differs, then exits 1. Standard reads, no OperationOutcome parsing. |
| Failure policy | First failed bundle aborts the run, as in the Python: the remaining bundles are not attempted. Bundles already committed stay committed; re-running is safe because every entry is a PUT by id with `If-Match`. |
| `--dry-run` | Runs the preflight and prints one line per bundle: index, entry count, and each `id` with `create` or `update@<version>`. Posts nothing. |
| Dependencies | `uuid` (v4) for generated ids. Parquet reading uses the `parquet` crate's Arrow reader already in the tree; WKB reading uses `wkb`. No other additions. |

## CLI

```
kiln diff --snapshot DIR --in EDITS --out CHANGES.ndjson [--report FILE]
kiln load --server URL [--token T] --in CHANGES.ndjson
          [--dry-run] [--batch-size 100] [--retries 3] [--timeout 300]
```

- `--in` for diff is detected by extension (table above).
- `--out` is written to a sibling temporary file and renamed into place, so
  a failed diff never leaves a half written changes file.
- `--token` falls back to `$KILN_TOKEN`; `--retries` and `--timeout` mean
  what they mean for extract.
- `--batch-size` is entries per transaction bundle; `0` is a usage error.

## diff

### 1. Index the snapshot

One sequential pass over `snapshot/locations.ndjson` with `NdjsonReader`,
keeping `id -> (offset, len)` in a map. A duplicate id keeps the first
occurrence and is reported `duplicate_id`, matching transform. Nothing else
is retained: the resource is re-read and parsed with `Location::parse` only
when an input row names it.

### 2. Read the input, one row at a time

Every reader yields the same `InputRow`:

```
InputRow {
  id:        Option<String>,
  columns:   Map<String, ColumnValue>,   // only the writable columns, keyed by README name
  geometry:  Option<geo::Geometry<f64>>, // already 2D, unrounded
  line:      usize,                      // feature index or row index, for messages
}
```

`ColumnValue` is `Null`, `Text(String)`, `Number(f64)`, `TextList(Vec<String>)`,
or `Identifiers(Vec<Identifier>)`. Each reader coerces its native types into
these:

- **GeoJSON:** properties are JSON. A string that parses as JSON of the
  expected shape is accepted for list and identifier columns, because GDAL
  flattens nested fields to JSON text on export. The Feature geometry goes
  through `geometry::parse_boundary` rules (Z dropped, reported).
- **GeoParquet:** Arrow columns. `alias` as `List<Utf8>`, `identifier` as
  `List<Struct{system,value}>`, a `Utf8` column holding JSON for either is
  also accepted. The geometry column is the one named in the GeoParquet
  `geo` metadata's `primary_column`, else `geometry`; WKB only. A file with
  no geometry column is an attribute-only edit.
- A column of the wrong type in either format is reported
  `input_column_type` with the column name and the row is processed without
  it.

The FeatureCollection reader is a `serde` visitor over the `features` array
that hands each element to the row builder as it is parsed, so a whole
country export never sits in memory at once.

### 3. Rebuild the resource

For a row whose id is in the snapshot, start from the snapshot resource as
JSON (`fhir_json` semantics: the decoded boundary extension removed). Apply
each present writable column per the mapping in the Decisions table.
Geometry is handled as decided above; a boundary edit re-inserts a boundary
extension carrying the new attachment. A row with no geometry, or a null
one, makes no geometry change. `position_longitude` and `position_latitude`
always map to `Location.position` whatever the row's geometry kind; both
null clears it, one null is reported `input_column_type` and skipped. For a row not in the snapshot, start
from `{"resourceType": "Location", "id": ...}` and apply the same rules.

Derived columns, `version_id`, `last_updated` and any unknown column are
ignored without a report; QGIS exports all of them and reporting each would
drown the report.

### 4. Compare and emit

The rebuilt resource is compared to the snapshot resource as canonical JSON
(sorted keys, no whitespace) after the boundary attachment on both sides is
replaced by the rounded WKB of its geometry, so base64 layout and float
noise do not register. Geometry rounding is seven decimals on every
coordinate. If equal, the row is skipped and counted as `unchanged`.

Otherwise the rebuilt resource is written as one NDJSON line, complete,
with `meta.versionId` exactly as the snapshot had it (new resources have no
`meta`). The boundary attachment in the output is inline base64 GeoJSON of
the bare geometry, as the fixture and the Python wrote it. Output order is
input order.

Diff prints: `N changed, M unchanged, K new` plus the report summary, and
exits 0 even when every row was reported, unless the input could not be
read at all.

### Report kinds

| kind | meaning |
|---|---|
| `duplicate_id` | id repeated in the snapshot (first kept) or in the input (later rows reported and skipped) |
| `new_location` | input id not in the snapshot; emitted as a create |
| `new_location_generated_id` | input row with no id; UUID assigned, detail carries the row index and the id |
| `input_column_type` | a writable column with a value of the wrong type; column ignored for that row |
| `geometry_kind_changed` | point supplied for a boundary row; geometry edit skipped |
| `geometry_unparseable` | input geometry that could not be read (bad WKB, unsupported GeoJSON); geometry edit skipped |
| `position_geometry_disagree` | point row where `position_*` and the geometry both changed and differ; geometry used |
| `boundary_z_dropped`, `geometry_invalid` | as in transform, on the input geometry; invalid geometry is still written, per "report, don't repair" |
| `snapshot_line_unparsed` | a snapshot line that is not a Location with an id; skipped |

## load

### 1. Validate the input

Stream the NDJSON. Every line must be a JSON object with a non-empty string
`resourceType` and `id`; anything else is a usage error naming the line.
Duplicate `<Type>/<id>` is a usage error. The whole file is read into memory:
it is a changeset, not a registry, and ordering needs all of it. `meta.versionId`
per resource is kept as the expected version.

### 2. Preflight

`GET <server>/metadata` through `FhirClient`. Non-200 or non-JSON is an
environment error. If any resource lacks `versionId`, every such resource's
type must have `rest.resource[].updateCreate == true`, else a usage error
naming the types and the setting.

### 3. Order and bundle

Parents first by `partOf` depth within the set, external parents counting as
roots, stable within a depth so siblings keep input order; a cycle is a usage
error naming an id. The ported Python algorithm. Chunk into bundles of
`--batch-size` entries:

```json
{"resourceType":"Bundle","type":"transaction","entry":[
  {"resource": {...}, "request": {"method":"PUT","url":"Location/<id>","ifMatch":"W/\"<version>\""}}
]}
```

`ifMatch` is present only when the resource has a `versionId`.

### 4. Post

`FhirClient` gains `post_json(url, body)` with the same retry policy as `get`:
connection errors, 429 and 5xx retried with backoff and `Retry-After`; any
other status final. A 2xx commits the bundle and prints `bundle i/n
committed (k resources)`. 412 or 409 triggers the conflict probe described
above, then an environment error listing the conflicting ids and telling the
operator to extract, re-apply, and diff again. Any other 4xx is an
environment error with the capped response body.

`--dry-run` stops after step 3 and prints the plan.

## Errors

Usage (exit 2): unreadable snapshot, unknown input extension, unwritable
`--out`, invalid NDJSON line or missing `resourceType`/`id` in load input,
duplicate ids in load input, `partOf` cycle, `--batch-size 0`,
update-as-create not supported for a create.

Environment (exit 1): connection failure after retries, non-200 metadata,
bundle rejected, version conflict, disk errors.

Reports (exit 0): everything per row in diff.

## Testing

Binary-level tests with `assert_cmd`, `tempfile`, `httptest` for load, plus
unit tests per module.

diff, against `tests/fixtures/snapshot` and hand written inputs:

1. A GeoJSON export with no edits produces an empty changes file and
   `unchanged` equals the row count, including when coordinates carry float
   noise in the eighth decimal.
2. Renaming one Location yields one line, complete, with the original
   `versionId`, and every other field of the snapshot resource present.
3. Editing a stale `admin1_name` alone yields no change.
4. Moving a point row's geometry updates `position`; drawing a polygon on a
   point row adds a boundary; a point on a polygon row is reported and
   skipped while a name edit on the same row still applies.
5. Redrawing a polygon replaces the attachment; the output decodes to the
   new geometry rounded to seven decimals; an extension kiln does not know
   survives.
6. `identifier`, `pcode`, `alias`, `part_of`, `managing_organization`,
   `settlement_type`, `facility_level`, `ownership`: each round trips, and
   null clears.
7. A row with no id gets a UUID and is reported; a row with an unknown id is
   a create with no `meta`.
8. GeoParquet input, produced by `transform` itself from the fixture, then
   edited with the `parquet` crate in the test: same outcomes as GeoJSON.
   GeoJSON with JSON-string `identifier` is accepted.
9. Streaming: a 200,000 feature GeoJSON generated on the fly diffs with
   peak RSS well under the file size (recorded once, not asserted in CI).

load, against an `httptest` server:

1. Two bundles of `--batch-size 2` over five resources arrive parents
   first, each existing entry carrying `ifMatch` with its version, the new
   one without.
2. A server whose CapabilityStatement lacks `updateCreate` is refused when
   the input holds a create and accepted when it does not.
3. A 412 on bundle two: bundle one was committed, the probe GETs each entry
   of bundle two, the error lists exactly the ids whose versions differ,
   exit 1.
4. 503 then 200 succeeds; 400 aborts with the body.
5. `--dry-run` posts nothing and prints the plan.
6. Cycle and duplicate ids are usage errors before any request.

Unit tests: column coercion from JSON and Arrow, reference prefix
preservation, the identifier upsert, canonical comparison with rounding,
parents-first ordering, bundle chunking, `ifMatch` formatting, CapabilityStatement
parsing.

## Files

```
src/
  cli.rs               + DiffArgs, LoadArgs, Command::{Diff, Load}
  main.rs              + mod diff; mod load
  diff/
    mod.rs             run_diff: index, read, rebuild, compare, emit
    input.rs           InputRow, ColumnValue, coercion, extension dispatch
    geojson.rs         streaming FeatureCollection and line readers
    parquet.rs         GeoParquet record batch reader
    rebuild.rs         writable column -> FHIR field application
    compare.rs         canonical JSON with rounded WKB, seven-decimal rounding
  load/
    mod.rs             run_load: validate, preflight, order, bundle, post
    order.rs           parents-first ordering, cycle detection
    bundle.rs          transaction bundle construction, ifMatch
    capability.rs      CapabilityStatement updateCreate check
  extract/client.rs    + post_json
tests/
  diff.rs
  load.rs
```

Dependency added: `uuid = { version = "1", features = ["v4"] }`.

## Open items deferred

- A `--validate` hook calling a FHIR validator on diff output (README
  "Future extensions").
- Continuing past a failed bundle with a per-bundle report, if operators
  turn out to want partial loads.
- CSV input for attribute-only edits.
