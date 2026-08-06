# kiln — GeoJSON admin boundaries into FHIR (`bake` + `load`)

**Date:** 2026-08-05
**Status:** Approved design, ready for planning

## Problem

Admin-boundary data arrives as GeoJSON — e.g. GRID3's Nigeria operational
wards (`data/GRID3_NGA_operational_wards_v3_0_*.geojson`: 323 Bauchi-state
ward Polygons across 20 LGAs, EPSG:4326). The FHIR store kiln exports from
has no way to receive it: kiln today is export-only (FHIR → GeoParquet).

This adds the import direction: one `Location` resource per admin unit,
profiled to ICRLocation, with the geometry inlined as a GeoJSON boundary
attachment and the admin hierarchy expressed through `partOf` — so the
existing export pipeline (and everything downstream of the store) sees a
complete georegistry.

The 2026-07-27 spec listed "writing back to FHIR" as a non-goal. This spec
deliberately reverses that: kiln becomes the round-trip tool for FHIR geo
data.

## Goals

- Convert a one-feature-class GeoJSON file (all features at the same admin
  level) into ICRLocation-profiled `Location` NDJSON, minting the ancestor
  levels found in feature properties.
- Load that NDJSON into a FHIR store idempotently: re-runs upsert in place,
  never duplicate.
- Keep the source→FHIR mapping fully definable at the CLI per admin level
  (which property is the name, which is the code, which is the alias
  source).
- Report data-quality problems per feature rather than aborting; abort only
  on systematic failure (kiln's existing Report philosophy).

## Scope

One country, one source file, one feature level per run. Ancestor levels
(state, LGA) are minted from feature properties — names and identifiers
only, no boundary geometry. Expected volume: hundreds to ~10k features
(full-country GRID3 ward files). Everything fits in memory, same as the
export pipeline.

## Non-goals (v1)

- **Boundary geometry for minted parents** (dissolving ward polygons into
  LGA polygons). Parents can be enriched by a later run over an LGA-level
  source file — same tool, different `--level` flags.
- **Multi-level source files** (e.g. a file mixing state and LGA features).
- **GERS / P-code conflation.** Identifiers beyond the source's own codes
  are backfilled later per the IG's enrichment lifecycle.
- **Deletes.** Removing admin units that disappeared from a newer source
  file is manual.
- **Profile validation.** `bake` output is structurally profiled but not
  run through a FHIR validator; the NDJSON checkpoint exists precisely so a
  sample can be validated externally when wanted.

## CLI

Two subcommands mirroring the export pipeline's offline/network split:

```bash
# Offline: GeoJSON -> Location NDJSON
uv run kiln bake \
  --in data/GRID3_NGA_operational_wards_v3_0_*.geojson \
  --country "Nigeria=NGA" \
  --level state=state:statecode \
  --level lga=lga \
  --level ward=ward \
  --alias lga=lga_alt_names \
  --alias ward=ward_alt_names \
  --out wards.ndjson

# Network: NDJSON -> FHIR store
uv run kiln load \
  --server https://healthcare.googleapis.com/v1/projects/.../fhir \
  --token "$(gcloud auth print-access-token)" \
  --in wards.ndjson
```

`--token` falls back to `$KILN_TOKEN`, matching `extract`. No combined
`bake`+`load` shortcut in v1 (the NDJSON checkpoint is the point).

### Level mapping

- `--country NAME=CODE` — the admin0 root. `CODE` (e.g. `NGA`) seeds the
  slug and the country identifier.
- `--level <levelname>=<name-property>[:<code-property>]`, repeatable,
  **ordered**: the flag sequence is the hierarchy below the country. The
  last `--level` is the feature level (carries the geometry); earlier
  levels are minted by grouping features on their name/code properties.
- `--alias <levelname>=<property>` — optional; a non-empty value becomes
  `Location.alias` (split on `;` if the source packs several).
- `--code-system URI` — identifier system for source codes and composite
  identifiers. Default: `https://icr.healthcampaigns.org/identifiers/national-admin-code`.

## Resource construction (per admin unit)

| FHIR element | Value |
| --- | --- |
| `id` | Slug path: code where the level has one, slugified name otherwise — `nga`, `nga-ba`, `nga-ba-alkaleri`, `nga-ba-alkaleri-east` |
| `meta.profile` | `https://icr.healthcampaigns.org/StructureDefinition/ICRLocation` |
| `name` | Level's name property |
| `alias` | From `--alias`, when present and non-empty |
| `status` | `active` |
| `type` | `admin-unit` (ICR location-type CodeSystem) |
| `physicalType` | `jdn` (Jurisdiction) |
| `partOf` | `Location/<parent slug>`; absent on the country root |
| `identifier` | Source code under `--code-system` when the level has a code property; otherwise the slug path as a composite identifier under the same system. Every admin unit gets ≥1 identifier (IG rule). |
| boundary extension | Feature level only: ICR extension URL, `valueAttachment` with `contentType: application/geo+json`, `data` = base64 of the feature's geometry object (RFC 7946: geometry only, no Feature wrapper) |

Slugging: lowercase, ASCII-fold, non-alphanumerics → `-`, collapsed. A slug
collision between two distinct units under the same parent is a fatal bake
error (not a Report issue): proceeding would silently merge two wards.

Geometry handling in `bake`: verify CRS is EPSG:4326 (accept a missing
`crs` member — that is the GeoJSON default — and the `EPSG:4326` /
`CRS84` spellings; anything else is fatal), validate with shapely, fix
ring orientation to RFC 7946 winding on the way out. An invalid or empty
geometry is a Report issue (`geometry_invalid`); the unit is still emitted
**without** a boundary rather than dropped — identity and hierarchy are
worth having even when one polygon is broken.

### Extension-URL policy

`bake` **writes** the ICR-minted boundary extension URL (what the deployed
IG and the store's existing consumers expect). `profile.py`'s read path
(used by `extract`/`transform`) gains a two-URL tuple so it **reads** both
the ICR URL and the HL7 canonical
(`http://hl7.org/fhir/StructureDefinition/location-boundary-geojson`),
making a later IG switch a constant swap with no stranded data.

## Load semantics

- Preflight: fetch the store's CapabilityStatement and check
  `rest.resource[Location].updateCreate`. If absent/false, fail
  immediately with a message naming the store setting (Google Healthcare
  API: `enableUpdateCreate`) — never half-load.
- Transaction bundles of 100, each entry `PUT Location/<id>`. Batches are
  ordered parents-first (country → states → LGAs → wards) so even a
  referential-integrity-enforcing store accepts them.
- Retry/backoff on 429/5xx reusing `extract`'s retry helpers (Retry-After
  honoured, sleeps clamped); a failed bundle after retries aborts with the
  server's OperationOutcome — no per-resource skipping at this layer,
  since a bundle-level failure is systematic, not a data problem.
- Summary line to stderr in `extract`'s style:
  `loaded: 345 upserted, 0 failed`.

## Architecture

```
GeoJSON ──bake──▶ locations.ndjson ──load──▶ FHIR store ──extract/transform──▶ GeoParquet
        (offline)                  (network)
```

- `src/kiln/bake.py` — offline, pure. Parses features, applies the level
  mapping, builds the hierarchy, emits resources. All FHIR-shape knowledge
  delegated to `profile.py`.
- `src/kiln/profile.py` — gains `build_location(...)`, the write-side twin
  of `shred()`; boundary-URL read tuple. Profile knowledge stays in this
  one module (its existing contract).
- `src/kiln/load.py` — network. Capability preflight, bundle assembly,
  PUT with retry. Only `extract.py` and `load.py` touch the network.
- `src/kiln/cli.py` — two new subcommands; `write_ndjson`/`read_ndjson`
  reused as-is.

## Error handling

Per-feature data problems → `Report`, run continues: missing name at any
level (`missing_field` — the feature is skipped, since it cannot be placed
in the hierarchy), invalid geometry (`geometry_invalid`, unit emitted
boundary-less), empty alias property (ignored, not reported).

Fatal (abort before writing anything): unreadable/non-FeatureCollection
input, wrong CRS, slug collision, a `--level` property missing from every
feature (a mapping typo, not a data problem).

`load` fatal: failed preflight, bundle failure after retries. `load` never
partially retries inside a bundle; re-running the whole load is safe by
construction (PUT upserts).

## Testing

- Unit: `bake` mapping/slugging/geometry fixes and `profile.build_location`
  from a small synthetic GeoJSON fixture; `load` bundle assembly, ordering,
  preflight and retry against a mock transport (kiln already tests
  `extract` this way).
- Round-trip integration (offline, the key test): `bake` the fixture, feed
  the NDJSON to the existing `transform`, assert the GeoParquet comes out
  with the right `admin0..admin3` names/codes and ward polygons. Proves
  the two directions agree without a server.
- Round-trip of the read tuple: a resource written with the HL7 canonical
  boundary URL still shreds correctly.
- Live smoke test against the real store stays manual:
  `bake` → `load` → `kiln run` → `kiln inspect`.

## Worked example (the Bauchi file)

323 ward features → 323 ward Locations with boundaries + 20 minted LGAs +
1 state + 1 country = **345 resources**, ~14MB inline geometry, 4 bundles.
State gets identifier `BA` from `statecode`; LGAs and wards get composite
slug identifiers (GRID3 v3 export carries no LGA/ward codes); P-codes and
GERS IDs backfill later without profile changes.
