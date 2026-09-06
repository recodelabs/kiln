# Facility Organizations — design (plan 4 of the Rust rewrite)

**Date:** 2026-09-05
**Status:** Draft, awaiting review before the implementation plan is written.
**Builds on:** plans 1–3 (all six commands on `main`), the mCSD facility pairing the Python `bake-points --paired-org` created in the registry.

## Problem

In the ICR registry every facility is two resources: a `Location` for the
place and an `Organization` for the institution, paired by id
(`org-<location id>`) and linked by `Location.managingOrganization`. The
Organization carries the national facility register codes (NHFR code and
uid), the institution's name, and its type codings with free-text labels;
the Location duplicates the facility level and ownership codings so map
layers can show them without a join. This is the mCSD pattern the profile
`ICRFacilityOrganization` documents.

kiln reads and writes the Location half only. Consequences today, verified
against the `icr-demo` store:

- The parquet has no NHFR code or uid, so facilities cannot be joined to
  DHIS2, NHFR extracts or microplans.
- A rename through QGIS updates the Location and leaves the Organization
  with the old name.
- A new facility row creates a Location with no Organization, which
  breaks the profile and the pairing convention.

## Goals

- Facility rows carry the Organization's registry codes and type labels,
  editable like any other writable column.
- A facility edit rebuilds both resources; only the halves that changed
  are emitted, each with its own version.
- A new facility row creates the pair.
- Retiring a facility retires both: `status` on the Location mirrors to
  `active` on the Organization.
- Nothing changes for settlements and admin units, which are single
  resources.
- A snapshot without Organizations (one made by the Python extract, or
  from a server that has none) keeps working: organisation columns are
  empty and the Location-only behaviour is unchanged.

## Non-goals

- Organization hierarchies (`Organization.partOf`) or Organizations that
  are not a facility's managing organisation. They are extracted and kept
  in the snapshot but not projected.
- Editing an Organization through anything other than its facility row.
- Fetching other resource types.

## Decisions

| Question | Decision |
|---|---|
| Which Locations get the join | Any Location with `managingOrganization` whose target is in the snapshot. In practice facilities. |
| Which rows create a pair | New rows with `type = facility`. Other new rows stay single. The user's answer 2026-09-05. |
| Pair id convention | `org-<location id>`, as bake did. A new facility gets a UUID Location id and `org-<uuid>`. |
| Name mirroring | `name` edits go to both resources. A snapshot where the two names differ is reported as `organization_name_mismatch`; the parquet shows the Location's name. |
| Status mirroring | `status = active` ⇒ `Organization.active = true`; any other status ⇒ `false`. Confirmed by the user. |
| Facility level, ownership | Coded values edit both resources' `type` codings by system, as Location does today. |
| Type text labels | `facility_level_text` and `ownership_text` columns, from `Organization.type[].text` of the concept holding that coding; editable, Organization only. Confirmed by the user. |
| Registry codes | `organization_identifier` list column (whole-list replace, like `identifier`), plus `nhfr_code` and `nhfr_uid` promoted by system and upserting back, like `pcode`. |
| Lossless fallback | `organization_json`: the complete Organization resource, like `fhir_json`. |
| Missing Organization | Reported once per facility as `organization_missing` when `organizations.ndjson` exists; not reported at all when it does not. Organisation columns null either way. |
| Extract | Pages `Organization` after `Location` with the same paging code, into `organizations.ndjson`; own watermark and count in `state.json`. No boundary phase. |
| Load ordering | All Organizations before all Locations, then parents first among Locations. Same bundle when the batch allows; ordering alone guarantees the reference resolves. |
| Editing an Organization not paired by id | Mirroring follows `managingOrganization`, whatever the Organization's id. The `org-` convention is only used when creating. |

## The snapshot

```
snapshot/
  locations.ndjson
  organizations.ndjson     one Organization per line, as the server returned it
  state.json               + organization_watermark, organization_count
  ...
```

`state.json` keeps `watermark` and `count` for Locations so existing
snapshots and the Python-written ones stay readable; the two new fields are
optional. An incremental extract asks
`Organization?_lastUpdated=ge<organization_watermark>` and merges by id
exactly as Locations are merged, minus boundary inlining. A snapshot
without `organizations.ndjson` is extracted in full on the next run for
Organizations only.

## Columns added

All on every row; null for rows with no joined Organization.

| column | from | writable |
|---|---|---|
| `nhfr_code` | `Organization.identifier` under the NHFR code system | yes, upserts |
| `nhfr_uid` | `Organization.identifier` under the NHFR uid system | yes, upserts |
| `organization_identifier` | `Organization.identifier`, list of `{system, value}` | yes, whole list |
| `facility_level_text` | `text` of the `Organization.type` concept holding the facility-type coding | yes |
| `ownership_text` | `text` of the `Organization.type` concept holding the ownership coding | yes |
| `organization_json` | the complete Organization | no |

`managing_organization` stays as it is. `organization_name` is deliberately
not a column: the names are meant to be equal, and drift is a report, not
a column.

## Transform

Pass one indexes `organizations.ndjson` into `id -> (offset, len)` when the
file exists, and each `IndexRecord` keeps the Location's
`managing_organization`. Pass two, when building a row whose record has a
managing organisation in the index, seeks the Organization line, parses it
with a new `Organization::parse` (name, active, identifiers, type concepts
with text, raw JSON), and fills the new columns. Memory grows by one map
entry per Organization; the Organization line is read once per facility
row, in Hilbert order, which is random access on a file of short lines.

Report kinds added: `organization_missing`, `organization_name_mismatch`,
`organization_line_unparsed` (in extract's merge, as for Locations).

## Diff

The snapshot index gains the Organization map. For an input row whose
Location has `managingOrganization` resolving to an Organization in the
snapshot, diff rebuilds both:

1. The Location as today.
2. The Organization from its raw line: `name` from the `name` column,
   `active` from `status`, facility level and ownership codings by system,
   the two text columns onto the matching concepts' `text`,
   `organization_identifier` replaced whole, then `nhfr_code` / `nhfr_uid`
   upserted.

Each resource is compared to its own snapshot line and emitted only if
changed, Organization line before Location line. A new `type = facility`
row emits the Organization (`org-<id>`, `active: true`, `name`, `prov`
type coding, identifiers and codings from the columns) followed by the
Location with `managingOrganization` set.

Columns that mirror to both (`name`, `status`, `facility_level`,
`ownership`) are applied to both; a row can therefore produce one, two or
zero lines. Counts print as `N changed, M unchanged, K new` over rows, and
a second line `Location: a, Organization: b` over resources.

Without `organizations.ndjson`, diff behaves as plan 3: Location only, and
one warning line saying organisation columns were ignored.

## Load

`order_parents_first` sorts Organizations (any resource without
`resourceType = Location`) before Locations, stable, then Locations parents
first as today. Nothing else changes: bundles, `ifMatch`, the conflict
probe and update-as-create all already work per resource type.

## Errors and reports

No new error classes. New report kinds: `organization_missing`,
`organization_name_mismatch`, `organization_line_unparsed`, and in diff
`organization_missing` when a facility row's Organization cannot be found
for mirroring.

## Testing

Extract, against the fake server: Organizations paged after Locations with
their own watermark; a snapshot without `organizations.ndjson` triggers a
full Organization fetch; `state.json` round-trips both watermarks; an older
`state.json` without the new fields still loads.

Transform, on a hand-made snapshot with two facilities, one paired and one
whose Organization is missing, plus an admin unit: the new columns are
filled, null, and null respectively; `organization_missing` fires once;
`organization_name_mismatch` fires when the names differ; a snapshot
without `organizations.ndjson` reports nothing and fills nulls.

Diff: renaming a facility emits two lines with the right versions;
changing `nhfr_code` emits the Organization only; moving the point emits
the Location only; `status = inactive` emits both with `active: false`; a
new facility row emits the pair in order; a new settlement row emits one
resource; transform's own output diffs clean, including the new columns.

Load: an input mixing Organizations and Locations posts Organizations
first, verified by the fake server.

Real data: `icr-demo` extract, transform and a clean round trip, then a
`load --dry-run` of a two-line rename.

## Files

```
src/
  fhir/organization.rs        Organization::parse, NHFR system constants
  extract/mod.rs, page.rs     resource type parameter; second phase for Organization
  snapshot/mod.rs             organizations.ndjson path, state fields
  snapshot/merge.rs           merge without boundary inlining for Organizations
  index/build.rs, mod.rs      organization index, managing_organization on the record
  write/schema.rs, dataset.rs new columns, Organization lookup in pass two
  diff/mod.rs, rebuild.rs     organization index, rebuild_organization, pair creation
  load/order.rs               Organizations first
tests/                        extract.rs, transform.rs, diff.rs, load.rs additions
```

## Open items deferred

- `organization_name` as a column, if someone wants to see drift in the
  table rather than the report.
- Organizations for settlements or admin units, if the profile ever pairs
  them.
- Editing an Organization's `partOf` or its other fields; today they ride
  along unchanged in `organization_json`.
