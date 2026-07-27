# kiln — FHIR Locations to GeoParquet

**Date:** 2026-07-27
**Status:** Approved design, ready for planning

## Problem

Location data lives in a FHIR store as `Location` resources: an admin hierarchy
expressed through `partOf` chains, terminal sites carrying lat/lng points, and
boundary polygons attached as GeoJSON. None of it is queryable by the geospatial
tools people actually use.

kiln exports those Locations to partitioned GeoParquet with proper spatial
indexing, so they can be read by DuckDB, QGIS, geopq-workbench, or anything else
that speaks the format — and later baked into PMTiles.

## Goals

- Export FHIR `Location` resources to GeoParquet with native Parquet geometry
  types, a per-row covering bbox, and spatially clustered row groups.
- Resolve `partOf` hierarchies into both a materialized path and denormalized
  ancestor columns.
- Handle point geometry (`Location.position`) and polygon geometry (the
  `location-boundary-geojson` extension) in one dataset.
- Report data-quality problems rather than silently dropping rows.

## Scope

**One country per run.** Expected volume is 100k–1M Locations. Households are
not exported as Locations — they are referenced and joined downstream — which is
what keeps the count in that range.

Single-country scope is load-bearing for the design: it makes everything fit in
memory, so the pipeline is a straightforward in-process transform rather than an
out-of-core one.

`country` is still kept as a partition key even though it is constant within a
run. It costs one directory level and means separate country exports can be
dropped into a shared tree and read as one dataset later — geopq-workbench and
DuckDB both surface `key=value` path segments as queryable columns.

## Non-goals (v1)

- **PMTiles baking.** Deferred. The schema is designed so baking is additive.
- **Bulk `$export`.** Deferred; the NDJSON seam makes it a drop-in third source.
- **Multi-country runs.** Out of scope, but the partition layout does not
  preclude it.
- **Writing back to FHIR.** kiln is read-only.

## Verified environment findings

Tested on the target machine on 2026-07-27. These drive the tooling decisions
below; re-verify if the toolchain changes.

### Who can write what

| Writer | `geo` version | Native `GEOMETRY` | Covering bbox |
|---|---|---|---|
| `duckdb COPY` (1.5.5) | 1.0.0 | ❌ WKB | ❌ |
| `gdf.to_parquet(write_covering_bbox=True)` | 1.0.0 | ❌ WKB | ✅ |
| `gdf.to_parquet(schema_version="1.1.0", write_covering_bbox=True)` | 1.1.0 | ❌ WKB | ✅ |
| **`ogr2ogr -lco USE_PARQUET_GEO_TYPES=YES`** (GDAL 3.13 / libarrow 24) | **1.1.0 + native** | **✅** | **✅** |

**GDAL is the only writer available that emits native Parquet geometry types.**
Everything else is a staging format. This is why the pipeline ends in `ogr2ogr`
regardless of what precedes it.

DuckDB's output was unchanged at 60k rows, above its
`geometry_minimum_shredding_size` threshold — the 1.0 result is not a
small-input artifact.

### Other verified facts

| Finding | Evidence |
|---|---|
| GDAL preserves input row order when `SORT_BY_BBOX=NO`, so a sort applied upstream survives the finalize pass | first-5 ids identical before/after rewrite |
| DuckDB reads GDAL's native-geometry output without issue | `select count(*), ST_GeometryType(...)` → `60000, POINT` |
| shapely 2.1.2 provides `from_geojson`, `make_valid`, `point_on_surface`; geopandas 1.1.4 provides `GeoSeries.hilbert_distance` | `hasattr` probe + round-trip |
| geopandas emits a stray `__index_level_0__` column unless `index=False` | schema inspection |

### Why not DuckDB

DuckDB was evaluated and dropped. The pipeline has eight stages; Python is
mandatory at stages 1–2 (HTTP paging, per-row boundary resolution) and GDAL is
mandatory at stage 8 (native geometry types). DuckDB could only ever occupy the
middle, so it is never an alternative to Python — only an addition to it.

At single-country scale its two strongest arguments evaporate: out-of-core
processing is unnecessary, and `PARTITION_BY` saves a `groupby` over roughly a
dozen groups. What remains is the recursive CTE, which is not worth a dependency
and a Python↔SQL handoff for what is a ~40-line dict walk — one that also
produces better diagnostics, since it can report *which* id dangles and *what*
the cycle path was.

Separately, FHIR's shape is hostile to SQL: reaching
`extension[url=…].valueAttachment.data` means `json_extract` plus filtering plus
unnesting, versus a dict lookup in Python.

## Source profile

Built against [ICRLocation](https://icr.healthcampaigns.org/StructureDefinition-ICRLocation.html).
Profile-specific details are isolated in `profile.py` so other profiles can be
supported without reshaping the pipeline.

- Boundary extension: `https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson`,
  `valueAttachment` with `contentType` pinned to `application/geo+json`. Data
  arrives **either** inline base64 (`data`) **or** by reference (`url`).
- `identifier:pcode` — OCHA P-code, system `https://icr.healthcampaigns.org/identifiers/pcode`
- `identifier:gers` — Overture GERS id, system `https://icr.healthcampaigns.org/identifiers/overture-gers`
- `type` — nine values: admin-unit, settlement, facility, school,
  community-distribution-point, temporary-post, household, supervisory-area,
  operational-area
- `physicalType` — jurisdiction | site | building | household
- `overlaysAdminUnit` (0..*) — a **non-tree** many-to-many relation; operational
  areas deliberately cross admin boundaries
- `settlementType`, `deliveryStrategy` — carried through as attributes

The profile has **no explicit admin-level element**. Level is derived (below).

## Architecture

Three commands over one seam. The seam is NDJSON of raw FHIR Location resources.

```
kiln extract    FHIR server ──paged /Location──> locations.ndjson
kiln transform  locations.ndjson ──python/shapely──> staging ──ogr2ogr──> out/**.parquet + _report.json
kiln run        extract, then transform
kiln inspect    out/ ──> partition / row / geometry summary
```

`transform` never touches the network; `extract` knows nothing about GeoParquet.
This is what makes the geo half testable from fixtures and lets `$export` slot in
later as a third front-end without changing anything downstream.

## Schema

One table. Every output file is homogeneous in geometry type.

| Group | Columns |
|---|---|
| identity | `id`, `name`, `status`, `loc_type`, `physical_type` |
| join keys | `pcode`, `gers_id`, `identifiers` (list of `{system, value}` — retains the rest) |
| hierarchy | `parent_id`, `depth`, `admin_level` (null off-tree), `path`, `ancestor_ids[]`, `admin0..4_name`, `admin0..4_code`, `country` |
| domain | `settlement_type`, `delivery_strategy`, `overlays_admin_unit_ids[]` |
| geometry | `geometry` (native, EPSG:4326), `geom_type`, `lon`, `lat`, `geometry_bbox` |
| provenance | `last_updated`, `source_server`, `extracted_at` |

Two decisions inside this are load-bearing:

**`overlaysAdminUnit` is an array column, not a sidecar table.** It is a genuine
many-to-many and cannot share the `partOf` columns, but a list column preserves
the single-table design and Parquet handles lists natively.

**A Location with both a point and a boundary produces one row, not two.**
`geometry` takes the polygon; `lon`/`lat` carry the point. This is the settlement
case that ruled out a two-table layout, and it resolves without duplication.
When only a polygon exists, `lon`/`lat` come from `point_on_surface` — **not**
centroid, because a centroid can fall outside a crescent-shaped district and put
a label in the neighbouring one.

## Hierarchy resolution

A plain dict walk in `hierarchy.py`. Build `id → parent_id`, then walk upward
from each node, memoizing ancestor chains. Depth-capped at 12.

- `admin_level` = depth counting **only** `type = admin-unit` nodes. Non-admin
  types get `NULL`.
- Ancestor columns resolve to the **nearest admin-unit ancestor at each level**,
  not to hop count. A clinic whose `partOf` points at a settlement still gets
  `admin2 = Nassarawa`.
- Ragged trees are expected: a three-level country and a five-level country both
  work. Level N is **not** guaranteed comparable across countries — a documented
  limitation, revisited if cross-country rollups become a requirement.
  `resolve_level()` is a single function to keep that change cheap.

Failure modes are handled, not crashed on, and each records enough detail to act
on:

- Dangling `partOf` → node becomes a root; report records the orphan id and the
  missing parent id.
- Depth cap reached → report records the full cycle path; node excluded from
  output.

## Geometry resolution

**Point** — `Location.position` → `Point(longitude, latitude)`. FHIR orders
these longitude-first; this is a classic silent bug and gets an explicit test.

**Polygon** — `extension[boundary].valueAttachment`:

1. Verify `contentType == application/geo+json`.
2. `data` present → base64-decode.
3. `url` present → fetch; if it resolves to a `Binary` resource, base64-decode
   `Binary.data`.
4. Normalize: GeoJSON may arrive as a `Feature`, a `FeatureCollection`, or a
   bare geometry. A multi-feature collection folds to MultiPolygon with a
   warning.
5. `shapely.from_geojson`, then `shapely.make_valid` on invalid rings (counted).

`geom_type` is `point` or `polygon`; MultiPolygon folds into `polygon`.

## Partitioning and write

```
out/locations/country=NG/geom_type=polygon/tier=1/part-0.parquet
out/locations/country=NG/geom_type=polygon/tier=site/part-0.parquet
out/locations/country=NG/geom_type=point/tier=site/part-0.parquet
```

`tier` = `admin_level` where it exists, else the literal `site`. Hive
partitioning on a nullable int produces `__HIVE_DEFAULT_PARTITION__`
directories, which are miserable to work with; `tier` avoids that while
`admin_level` remains a real int column for filtering.

With one country in scope this yields roughly a dozen partitions — two geometry
types by around six tiers.

### Pass 1 — build and stage

Records assemble into a `GeoDataFrame`, then:

```python
gdf = gdf.iloc[gdf.hilbert_distance().argsort()]        # spatial clustering
keys = ["country", "geom_type", "tier"]                 # --partition-by
for values, part in gdf.groupby(keys, sort=False):
    part.to_parquet(staging_path, index=False)          # index=False is required
```

Hilbert bounds default to the dataset envelope, which for a single country gives
far better resolution than world bounds — `hilbert_distance` returns a uint,
and spreading that over the whole globe is roughly 600 m at the equator.

`index=False` is not optional: geopandas otherwise emits a stray
`__index_level_0__` column that survives into the final output.

Staging output is GeoParquet 1.0/1.1 WKB and is disposable.

### Pass 2 — finalize

One `ogr2ogr` per staging partition file:

```
-lco USE_PARQUET_GEO_TYPES=YES     # native GEOMETRY + 1.1 metadata (dual-emit)
-lco WRITE_COVERING_BBOX=YES       # per-row bbox -> row-level pushdown
-lco SORT_BY_BBOX=NO               # preserve the Hilbert order from pass 1
-lco COMPRESSION=ZSTD
-lco ROW_GROUP_SIZE=20000          # default; --row-group-size overrides
```

Uses **system GDAL via subprocess**, not a linked bundled build. This is
deliberate: pyogrio ships its own GDAL that may lag and may not expose
`USE_PARQUET_GEO_TYPES`. Shelling to the system binary removes the version-skew
risk entirely. kiln probes `ogr2ogr --version` and the Parquet driver's creation
options at startup, failing fast with a clear message if either is inadequate.

Default row-group size is 20,000 rows rather than GDAL's 65,536. Smaller groups
mean finer-grained pruning, which is the point of sorting spatially in the first
place; polygon rows are also far larger than the tabular rows GDAL's default
assumes. `inspect` reports the resulting group count and average size so this can
be tuned against real data.

kiln names the final files `part-0.parquet`.

`--geo-types both|only|legacy` maps to `USE_PARQUET_GEO_TYPES=YES|ONLY|NO` for
consumers that are not yet current.

### Small-partition guard

Three-level partitioning over one small country can produce a pile of 4 KB
files, which is slower than not partitioning at all. `transform` warns when a
partition falls below a row threshold. `--partition-by` is configurable.

## What makes the output good

Beyond format version, six properties are what actually make GeoParquet fast to
query. Items 2 and 3 matter most in practice and are the most commonly skipped.

1. Native geometry types, with WKB + 1.1 metadata as the compatible floor
2. **Covering bbox column** — gives row-level spatial pushdown
3. **Spatial sort** (Hilbert) — a bbox query hits few row groups, not all
4. Tuned row groups — ~20k rows, not GDAL's 65k default, for polygons
5. Hive partitioning on low-cardinality keys only
6. ZSTD compression

## Validation report

`out/_report.json` plus a human-readable summary. Counts and affected ids for:

- orphans (dangling `partOf`), with the missing parent id
- cycles (depth cap reached), with the cycle path
- duplicate pcodes
- Locations with no geometry at all
- boundary attachments that failed to fetch or parse
- geometries repaired by `make_valid`
- **facilities whose point does not fall inside their nearest admin ancestor's
  polygon** — in microplanning this is usually a real data error, and it is
  nearly free once both geometries are in one table (shapely `STRtree`)

## CLI

```
kiln extract --server URL --token TOK [--since DATE] --out locations.ndjson
kiln transform --in locations.ndjson --out out/ [--geo-types both|only|legacy]
               [--partition-by country,geom_type,tier] [--row-group-size N]
kiln run --server URL --token TOK --out out/
kiln inspect out/
```

Token falls back to `$KILN_TOKEN`, matching tokempic's convention.

## Module layout

```
kiln/
  pyproject.toml              uv, ruff, pytest
  src/kiln/
    cli.py                    argument parsing, command dispatch
    extract.py                paged /Location fetch, bearer auth, `next` links,
                              url-referenced boundary resolution
    profile.py                ICR mapping: extension URLs, identifier systems
    hierarchy.py              dict walk: level, path, ancestors
    geometry.py               position + boundary-geojson -> shapely geometry
    write.py                  hilbert sort, partition loop, ogr2ogr finalize
    report.py                 validation checks and report emission
    bake.py                   PMTiles (deferred, stub)
  tests/
    fixtures/                 synthetic NDJSON
```

Dependencies: `shapely`, `geopandas`, `pyarrow`, `httpx`; system GDAL ≥ 3.13.

## Testing

A synthetic fixture with a deliberately ragged tree, carrying one of each
pathology:

- inline base64 boundary
- url-referenced boundary, and a Binary-resolved one
- Location with both position and boundary
- missing geometry entirely
- orphan (dangling `partOf`)
- cycle
- duplicate pcode
- point outside its parent polygon
- swapped lon/lat

Output assertions read Parquet metadata directly: `geo` key present, version,
`covering` declared, `geometry_types` homogeneous per file, native `Geometry`
logical type on the geometry column, no `__index_level_0__`, expected partition
directories.

**Acceptance check beyond pytest:** open the output in
[geopq-workbench](https://github.com/gsueur/geopq-workbench). Its quality gate
checks exactly what matters here — spatial index present, row-group bbox
overlap, sensible row-group sizes — and its row-group-bbox map overlay makes
clustering visible: a well-sorted file shows a neat mosaic, a badly sorted one
shows boxes covering everything. It also opens hive-partitioned directories as a
single layer with `key=value` segments as queryable columns, which validates the
partition layout end to end.

## Risks and open items

| Risk | Mitigation |
|---|---|
| GDAL not installed on a target machine | Startup probe with a clear error; document the requirement |
| `url`-referenced boundaries make extraction slow or partially fail | Concurrency limit, retry with backoff; failures recorded in the report rather than aborting the run. **Riskiest unknown** — if most boundaries are external references, extraction becomes N+1 fetches. Check against real ICR data early |
| Level not comparable across countries | Documented limitation; `resolve_level()` seam for a pcode or config strategy later |
| Volume grows past ~1M rows | In-memory design would need revisiting. The FHIR parsing and hierarchy walk stay valid; only the sort and write stage would change |
| Two-pass write costs an extra pass over the data | Accepted; staging is local and disposable |

## Future

**PMTiles.** GDAL 3.13 has a read/write PMTiles driver, and tippecanoe 2.79 is
available for multi-zoom generalization. The `tier` partition maps naturally to
tile zoom ranges — admin0/1 at low zoom, sites at high zoom — so baking is a
selection over existing partitions rather than a reshape.

**Overture join.** `gers_id` is a first-class column specifically so Locations
can be joined to Overture division data without a fuzzy match.

**Multi-country.** The `country=` partition key already accommodates merging
separate runs into one tree.
