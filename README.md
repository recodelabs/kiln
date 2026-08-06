# kiln

**New here? Read [ABOUT.md](ABOUT.md) first** — what kiln is for, with a worked example. This file is the command reference.

Export FHIR `Location` resources to partitioned GeoParquet — native Parquet
geometry types, per-row covering bbox, Hilbert-sorted row groups.

## Requirements

- Python 3.11+
- **System GDAL 3.13+** with a Parquet driver built against libarrow >= 21
  (`brew install gdal`). kiln probes for `ogr2ogr`/`ogrinfo` and the
  `USE_PARQUET_GEO_TYPES` capability at the start of `transform` and fails
  with a clear message if either is missing or too old.

  The pipeline is two-pass because of this: `frame.py` stages the data as a
  plain GeoDataFrame (WKB), and `ogr2ogr` is what rewrites each staged file
  into the final output — it is the only writer available that emits native
  Parquet geometry types rather than a GeoJSON/WKB workaround.

## Install

```bash
uv sync
```

## Usage

One country per run.

```bash
# Fetch Locations, resolving any url-referenced boundary attachments
uv run kiln extract \
  --server https://healthcare.googleapis.com/v1/projects/.../fhir \
  --token "$(gcloud auth print-access-token)" \
  --out locations.ndjson

# Convert to partitioned GeoParquet (offline — no network)
uv run kiln transform --in locations.ndjson --out out/

# Or both at once
uv run kiln run --server ... --token ... --out out/

# Check what was written
uv run kiln inspect --out out/
```

Every command below is written as `uv run kiln ...` for the same reason: `uv sync`
alone does not put `kiln` on `PATH`. If you'd rather invoke it bare, activate the
venv first (`source .venv/bin/activate`) or `uv pip install -e .` into an
already-active environment.

`--token` falls back to `$KILN_TOKEN`.

### `transform` is offline

`transform` never makes a network call. A boundary attachment that carries a
`url` instead of inline `data` cannot be resolved by `transform` alone — it
is left geometry-less and reported as `boundary_unresolved_url`. Run `kiln
extract` first (or use `kiln run`); that is the step that fetches
url-referenced boundaries and inlines them as base64 `data` before anything
is written to NDJSON.

### Boundary fetching is concurrent, with retry

A registry can carry tens of thousands of url-referenced boundary
attachments. `extract` fetches them on a pool of worker threads (default 8,
`--concurrency`) sharing one HTTP client, instead of one at a time — with
retry and exponential backoff (default 3 attempts, `--retries`) for
connection errors, `5xx`, and `429`. A `429` or `5xx` honours a
`Retry-After` header if the server sends one, in the numeric-seconds form
(`Retry-After: 7`) — the HTTP-date form (`Retry-After: Wed, 21 Oct 2026
07:28:00 GMT`) is not parsed and falls back to exponential backoff instead.
A `404` (or any other non-retryable `4xx`) is *not* retried — it will still
be missing on the third attempt, so retrying it only multiplies the wait —
and is reported once as `boundary_fetch_failed`, same as before. Every
sleep is capped at 30s **and floored at 0s**, so neither a pathological
huge `Retry-After` nor a negative one can stall or crash the run. One
boundary's fetch failing never aborts the others; progress (`resolved
1200/50000 boundaries (14 failed)`) prints to stderr for runs large enough
to matter, so a multi-hour fetch isn't silent throughout.

A server that is entirely unreachable (wrong token, bad `--server`, host
down) is a different problem from a handful of dead boundary URLs: retrying
every one of tens of thousands of Locations for up to a minute each before
giving up would take hours to fail. `--max-consecutive-failures` (default
50, `0` to disable) aborts the run early if that many boundary fetches in a
row fail *and nothing has succeeded yet in the whole run* — the "zero
successes" condition is what keeps a registry with a genuine scattering of
dead URLs (a data problem) from ever tripping it, no matter how those
failures happen to be distributed.

### Fetched boundaries are cached on disk

Admin boundaries change rarely — once a year at most — so `extract` caches
every successfully-fetched boundary attachment under `--cache-dir` (default
`~/.cache/kiln/boundaries/`), keyed on a sha256 hash of its URL. A second
run over the same registry reads the cache instead of the network and is
correspondingly fast; a run that gets killed halfway through a large fetch
picks up where it left off next time instead of re-fetching everything,
as a side effect of the same mechanism.

The key is the URL alone — it deliberately does not include the auth
token. That's a caveat, not a bug, but an unusual one: if you point two
different servers (or two tokens with different access grants) at the
*same* `--cache-dir`, and both happen to expose the same URL but would
return different bytes for it per-token, the first one fetched wins and the
second server silently gets served the first server's cached bytes instead
of its own. This needs a fairly odd deployment to hit (shared cache
directory, colliding URLs, token-dependent responses), but if that
describes your setup, use a separate `--cache-dir` per server/token.

Only successful fetches are cached — a `404` today may be a working URL
tomorrow, so caching a failure would turn a transient outage into a
permanent one. `--no-cache` disables the cache entirely for one run
(neither read nor written); `--refresh` ignores whatever is already
cached and re-fetches everything, but still writes the fresh results back
so the cache is warm again afterward. A cache problem — an unreadable or
corrupt entry, an unwritable `--cache-dir` — is reported as `cache_error`
and treated as a miss for that one boundary; it never aborts the run,
same as a boundary that fails to fetch outright.

Each entry is two files, `<hash>.bin` (the raw fetched bytes) and
`<hash>.meta.json` (`url`, `fetched_at`, and a `sha256` of `.bin`, checked
on every read to catch a truncated or corrupted file before it's used).
The metadata file is what makes the cache directory debuggable instead of
just a pile of anonymous hex-named files — `grep` it for a URL to find
which entry it maps to and when it was fetched. Both files are written
temp-file-then-`os.replace`, same as `write.py`'s partition writes, so two
worker threads racing to fill the same cache entry can't leave behind a
half-written file for a later run to read as if it were complete.

Alongside the existing fetch progress, `extract` prints a one-line summary
to stderr, e.g. `boundaries: 4820 cached, 180 fetched, 3 failed`. Unlike the
periodic progress ticker (which only appears for 50+ boundaries), this
summary line always prints, even for a run with a handful of boundaries or
none at all (`boundaries: 0 cached, 0 fetched, 0 failed`) — it's the only
way to tell a fast run apart from one that just had nothing to do, and that
distinction matters most exactly on the small runs the ticker skips.

### `--in` must be NDJSON (or a single-line Bundle), not pretty-printed JSON

`read_ndjson` streams the input line by line — deliberately, so a
multi-gigabyte file with base64-inlined boundaries is never held in memory
twice over. That means a **pretty-printed, multi-line** FHIR Bundle (e.g.
`json.dumps(bundle, indent=2)`, or anything a text editor's "format
document" produced) is not readable input: every line fails to parse as its
own JSON value. `transform` detects this specific shape up front (the first
non-blank line being just `{` or `[`, with nothing else on it) and exits
immediately with status 2 and a clear message, rather than reporting a pile
of `malformed_field` issues for every line and exiting 0 as if it had
legitimately resolved zero rows. That distinction matters because
`transform` fully owns `out/locations/` — a silently "successful" empty run
would otherwise replace a user's previous good export with nothing, for an
input kiln simply cannot read. Compact the file first if you hit this, e.g.
`jq -c . input.json > input.ndjson`.

A single-line Bundle (exactly what `kiln extract`/`write_ndjson` produce) is
still supported — only a Bundle reformatted across multiple lines afterward
is not.

## Importing admin boundaries

The reverse direction: a one-level admin GeoJSON file (all features at the
same level, e.g. GRID3 wards) becomes ICRLocation-profiled resources in the
store, ancestors minted from feature properties.

```bash
# Offline: GeoJSON -> Location NDJSON (inspect/validate before loading)
uv run kiln bake \
  --in data/GRID3_NGA_operational_wards_v3_0.geojson \
  --country "Nigeria=NGA" \
  --level state=state:statecode \
  --level lga=lga \
  --level ward=ward \
  --alias lga=lga_alt_names --alias ward=ward_alt_names \
  --out wards.ndjson

# Network: NDJSON -> FHIR store (idempotent -- re-runs upsert in place)
uv run kiln load \
  --server https://healthcare.googleapis.com/v1/projects/.../fhir \
  --token "$(gcloud auth print-access-token)" \
  --in wards.ndjson
```

`--level` flags are ordered: the sequence is the hierarchy below the
country, and the last one is the feature level that carries the geometry.
Each flag is `LEVEL=NAME_PROP[:CODE_PROP]` — which property holds the
unit's name, and (optionally) its code. Units get slug-path ids
(`nga-ba-alkaleri-alkaleri-east`) built from codes where the source has
them, names otherwise, so loading is `PUT`-idempotent.

`kiln load` requires the store to support update-as-create (on Google
Healthcare API: `enableUpdateCreate=true`); it checks the store's
CapabilityStatement up front and refuses to half-load. A bundle failure
aborts the run — re-running the whole load is always safe.

Data problems (a feature missing its ward name, an invalid polygon) are
reported and skipped; mapping problems (a `--level` property that matches
nothing, two units slugging to the same id, a non-WGS84 CRS) abort before
anything is written.

### Facility and other point sites

`kiln bake-points` does the same for point features from a CSV — health
facilities, schools, any site — linking each row into an already-baked
admin registry by name (names and aliases, slug-normalized, so `Tama/Daye`
matches `Tama Daye`). Rows whose ward doesn't resolve link to the deepest
ancestor that does and are reported as `parent_unresolved`.

```bash
uv run kiln bake-points \
  --in facilities.csv \
  --admin wards.ndjson \
  --type facility \
  --name-col facility_name --lat-col latitude --lon-col longitude \
  --id-col globalid \
  --parent state=state --parent lga=lga --parent ward=ward \
  --identifier "https://icr.healthcampaigns.org/identifiers/nga-nhfr-code=nhfr_facility_code" \
  --where state=Bauchi \
  --out facilities.ndjson
```

Sites come out `type` = the given ICR location-type code, `physicalType` =
`si` Site, with `position` from the lat/lon columns — then `kiln load`
upserts them like any other Location NDJSON.

By default only the feature level carries geometry — the minted ancestors
(state, LGA) are boundary-less until an authoritative file for their level
is loaded. Pass `--dissolve-parents` to give every ancestor a *derived*
boundary instead: the union of its children's polygons. Derived boundaries
are exactly consistent with the child tiling (rollups and containment
checks line up), but they are not authoritative cartography — loading an
official boundary file later upserts over them, since ids are stable.

## Verified example

Running `transform` against the test fixture (`tests/fixtures/locations.ndjson`,
12 Locations exercising several of the failure modes below) produces:

```
$ uv run kiln transform --in tests/fixtures/locations.ndjson --out out/
Wrote 8 rows across 6 partitions to out
Issues found:
  boundary_unresolved_url: 1
  cycle: 2
  duplicate_pcode: 1
  no_country: 1
  no_geometry: 2
  orphan: 1
  point_outside_parent: 1
  small_partition: 6
```

```
$ uv run kiln inspect --out out/
8 rows across 6 partitions (84.9KB)

  locations/country=NG/geom_type=point/tier=site/part-0.parquet
    rows=2 row_groups=1 (min=2 avg=2 max=2) size=14.1KB
    geo=1.1.0 covering=True types=Point
  locations/country=NG/geom_type=polygon/tier=0/part-0.parquet
    rows=1 row_groups=1 (min=1 avg=1 max=1) size=14.1KB
    geo=1.1.0 covering=True types=Polygon
  locations/country=NG/geom_type=polygon/tier=1/part-0.parquet
    rows=1 row_groups=1 (min=1 avg=1 max=1) size=14.3KB
    geo=1.1.0 covering=True types=Polygon
  locations/country=NG/geom_type=polygon/tier=2/part-0.parquet
    rows=2 row_groups=1 (min=2 avg=2 max=2) size=14.6KB
    geo=1.1.0 covering=True types=Polygon
  locations/country=NG/geom_type=polygon/tier=site/part-0.parquet
    rows=1 row_groups=1 (min=1 avg=1 max=1) size=14.1KB
    geo=1.1.0 covering=True types=Polygon
  locations/country=unknown/geom_type=point/tier=site/part-0.parquet
    rows=1 row_groups=1 (min=1 avg=1 max=1) size=13.7KB
    geo=1.1.0 covering=True types=Point
```

Of the 12 input Locations, only 8 rows are written. The other 4 are omitted
and each has a `no_geometry` (or, for the cycle members, `cycle`) entry in
`_report.json`: the two cycle members (`cyc-a`, `cyc-b`), one Location with
a url-only boundary and no fallback position (`remote`), and one Location
with no position and no boundary at all (`ghost`). Everything else survives
but some of it is flagged: `dup` and `nassarawa` both survive and are
reported for sharing a pcode; `stray` survives and is reported for its point
falling outside its parent polygon; `orphan` survives, filed under
`country=unknown` because its dangling `partOf` leaves it with no admin
ancestor to derive a country from.

## Output layout

```
out/locations/country=NG/geom_type=polygon/tier=0/part-0.parquet
out/locations/country=NG/geom_type=polygon/tier=1/part-0.parquet
out/locations/country=NG/geom_type=point/tier=site/part-0.parquet
out/_report.json
```

`tier` is the admin level (as a string) for admin-units, `site` for
everything else — see [Schema](#schema) for why it's a separate column from
`admin_level`. Every file holds a single geometry type.

`transform` fully owns `out/locations/`: every run's output *replaces* it as
one atomic unit, so every successful run's output is an exact reflection of
that run's input, including the case where it resolves zero rows. This is
what makes re-running `transform` (or `run`) into the same `--out` — the
normal nightly-refresh workflow — safe: without it, a changed
`--partition-by` key set (a `--country` change, an admin level disappearing
from source, all polygons failing to resolve) would leave the previous
run's partitions behind, and DuckDB/geopq-workbench would read the old and
new rows as one indistinguishable layer.

The replacement is atomic, not clear-then-write: every partition is written
into a hidden staging directory (`out/.locations.tmp/`) first, and only once
*every* partition has finalized successfully is that staging directory
swapped in for `out/locations/`. If any partition fails partway through —
`ogr2ogr` crashing, a disk filling up — the staging directory is discarded
and the previous `out/locations/`, if any, is left completely untouched, not
a mix of old and new partitions. `out/_report.json` is deleted at the same
point the write is attempted, before anything else, so a failed run never
leaves behind a report that still describes an old, unrelated successful
run as if it were current; on success it's rewritten to describe exactly
what's now on disk. (A run that fails *before* attempting the write at all —
a bad `--partition-by` column, a missing GDAL — never touches either the
dataset or the report, same as before.)

If `out/locations` is a symlink, `transform` refuses to run through it and
exits with a clear error rather than crashing on the delete.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--country` | derived from the root admin-unit's pcode | Override the country partition value |
| `--geo-types` | `both` | `both` = native Parquet geometry type, plus GeoParquet 1.1 sidecar metadata (`geo` key: version, geometry types, covering bbox). `only` = native geometry type with **no** GeoParquet sidecar metadata at all — `kiln inspect` and any other metadata-based reader will report `geo=None`, `covering=False`, empty `types=` for a perfectly valid file, so don't reach for `only` unless every downstream reader speaks native Arrow geometry types directly. `legacy` = plain WKB (no native geometry type), but *with* the GeoParquet 1.1 sidecar metadata — the most broadly compatible option |
| `--partition-by` | `country,geom_type,tier` | Hive partition keys. A null value (e.g. partitioning by the nullable `admin_level`) is written to its own `key=null` directory rather than dropping those rows. A value containing a `/` or other filesystem-unsafe character (e.g. a pcode used as `--partition-by country`) is sanitized for the directory name and reported as `partition_value_sanitized`; the underlying data is untouched. If two distinct values sanitize (or otherwise render) to the *same* directory segment — `"A/B"` and `"A_B"` both become `A_B`; `None` and the literal string `"null"` both become `null` — the second one gets a short hash suffix appended (e.g. `A_B~e466256d`) so they never collide on disk, and this is reported as `partition_value_collision`. An unknown column name exits with status 2 instead of a traceback. Partitioning by an integer column (e.g. the raw `admin_level` rather than the string `tier`) is accepted and written correctly, but breaks reading the output back with `pyarrow.dataset(..., partitioning="hive")` — see [Known reader gotchas](#known-reader-gotchas) |
| `--row-group-size` | `20000` | Rows per Parquet row group |

`kiln extract` also takes `--server` (required), `--token`, `--since`
(`_lastUpdated=gt<since>`) and `--out`, plus the options below for
resolving url-referenced boundary attachments. `kiln run` takes the union of
`extract`'s and `transform`'s options and writes the intermediate NDJSON to
`<out>/locations.ndjson`.

| Option | Default | Description |
| --- | --- | --- |
| `--concurrency` | `8` | Worker threads fetching url-referenced boundary attachments concurrently, sharing one HTTP client |
| `--retries` | `3` | Attempts per boundary fetch before giving up, with exponential backoff. Retries connection errors, `5xx`, and `429` (honouring `Retry-After` if present); a `404` or other non-retryable `4xx` is never retried |
| `--cache-dir` | `~/.cache/kiln/boundaries/` | Local disk cache for fetched boundary attachments, keyed on a sha256 hash of each boundary's URL. A warm cache turns a second run over the same registry into a near-instant, no-network operation, and lets a killed-halfway run resume without re-fetching what it already has |
| `--no-cache` | off | Disable the boundary cache entirely for this run — neither read nor write it |
| `--refresh` | off | Ignore existing cache entries and re-fetch every boundary over the network, but still write the fresh results back so the cache is warm again afterward |
| `--max-consecutive-failures` | `50` | Abort (`kiln extract` exits with status 2) if this many boundary fetches in a row fail with zero successes anywhere in the run — a signal of a systematic problem (server unreachable, wrong token, bad base URL), not a handful of bad boundary URLs. `0` disables this check |

## Schema

33 columns, one table, one row per Location that resolves. Every output
file also carries a `geometry_bbox` struct column (added by `ogr2ogr` for
the GeoParquet covering-bbox), which is not part of this 33.

| Group | Column | Meaning |
| --- | --- | --- |
| identity | `id` | FHIR `Location.id` |
| identity | `name` | `Location.name` |
| identity | `status` | `Location.status` |
| identity | `loc_type` | `Location.type[0].coding[0].code` (e.g. `admin-unit`, `settlement`, `facility`) |
| identity | `physical_type` | `Location.physicalType[0].coding[0].code` |
| join keys | `pcode` | Identifier value for the pcode system — the code used to join to other datasets |
| join keys | `gers_id` | Identifier value for the Overture GERS system |
| join keys | `identifiers` | Full `[{system, value}]` identifier list (superset of `pcode`/`gers_id`) |
| hierarchy | `parent_id` | `Location.partOf`, reference stripped to a bare id |
| hierarchy | `depth` | Distance from the root of this Location's `partOf` chain (root = 0) |
| hierarchy | `admin_level` | Depth counting only `admin-unit` ancestors (root admin-unit = 0); **NULL for anything that is not itself an `admin-unit`** |
| hierarchy | `tier` | `admin_level` as a string, or the literal `"site"`; always a string (see below) |
| hierarchy | `path` | `/`-joined ids from root to this Location, inclusive |
| hierarchy | `ancestor_ids` | Ids from root to parent, excluding self |
| hierarchy | `admin0_name` … `admin4_name` | Name of the nearest `admin-unit` ancestor at each of 5 levels |
| hierarchy | `admin0_code` … `admin4_code` | pcode of the same ancestors |
| hierarchy | `country` | pcode of the root admin-unit ancestor, or the `--country` override; `"unknown"` if no ancestor carries a pcode |
| domain | `settlement_type` | `settlement-type` extension value |
| domain | `delivery_strategy` | `delivery-strategy` extension value |
| domain | `overlays_admin_unit_ids` | Ids of admin-units this Location's catchment overlays (many-to-many, from the `overlays-admin-unit` extension) |
| geometry | `geometry` | Point or polygon geometry — see [CRS](#crs) below |
| geometry | `geom_type` | `"point"` or `"polygon"` (MultiPolygon folds into `"polygon"`) |
| geometry | `lon`, `lat` | Representative point: `Location.position` if present, else `point_on_surface` of the polygon (not centroid — a centroid can fall outside a crescent-shaped district) |
| provenance | `last_updated` | `Location.meta.lastUpdated` |

`admin_level` is a nullable `Int64` so it stays a real, filterable integer
column. `tier` exists as a separate string column because Hive-partitioning
on a nullable int produces `__HIVE_DEFAULT_PARTITION__` directories for the
null rows, which most readers (including geopq-workbench) handle badly.

### `identifiers` changes type on write

`identifiers` is `list<struct<system, value>>` in the staged frame. `ogr2ogr`
converts it to a JSON-string Arrow extension type in the final output — the
data survives and is human-readable, but a consumer reading the Parquet file
directly gets a JSON string column, not native struct access. `ancestor_ids`
and `overlays_admin_unit_ids` are plain `list<string>` and come through
unchanged.

## CRS

The output CRS is **`OGC:CRS84`, not `EPSG:4326`.** `frame.py` builds the
staged frame as EPSG:4326; `ogr2ogr` retags the finalized output as
`OGC:CRS84`. Same datum, same longitude-then-latitude axis order — the
coordinate values are identical and correct — but the CRS objects are not
interchangeable in code:

```python
>>> import pyproj
>>> pyproj.CRS("EPSG:4326") == pyproj.CRS("OGC:CRS84")
False
>>> pyproj.CRS("EPSG:4326").equals(pyproj.CRS("OGC:CRS84"), ignore_axis_order=True)
True
```

A naive `crs == "EPSG:4326"` check on the output will fail. Compare with
`.equals(..., ignore_axis_order=True)`, or just don't compare CRS objects by
identity.

## Data quality

`out/_report.json` has a `counts` summary, a per-issue `issues` list
(`kind`, `location_id`, `detail`), and a `truncated` map. Some issues
describe a row that still made it into the output (e.g. `duplicate_pcode`);
others describe a row that was **omitted** — if you're counting rows
against your source count, the gap is explained here. For every
`boundary_*` decode failure, the boundary is simply discarded, not the
Location: if `Location.position` is also present, the row still comes
through as a point. Only when neither a boundary nor a position survives
does the row disappear, and that always additionally raises `no_geometry`
on the same Location — grep the report for that id to see which applies.

`issues` retains at most 1000 entries per kind — a systematic fault (every
row using the `(0, 0)` default coordinate, a swapped lat/lon) can otherwise
raise one issue per row and produce a report larger than the dataset it
describes. `counts` is always exact regardless of the cap; it's the number
to act on. `truncated` maps `kind -> count omitted from issues` for any kind
that hit the cap, and is empty when nothing did; `summary()`'s printed
output likewise only mentions the cap when it actually triggers.

| Kind | Row omitted? | Meaning |
| --- | --- | --- |
| `missing_id` | yes | The FHIR resource had no `id` (or a non-string one); dropped before any other processing |
| `malformed_field` | usually no | A field documented as 0..\* or a specific shape arrived as something else (e.g. a string where a list was expected); the field is ignored and the Location keeps going. The one exception: a non-string `id` reports `malformed_field` *and* drops the resource entirely. A malformed NDJSON line (invalid JSON) is also reported this way, with the 1-based line number in `detail`, and that line alone is skipped |
| `orphan` | no | `partOf` references an id not present in the input; the Location is treated as a root |
| `cycle` | yes | This Location is itself part of a `partOf` cycle |
| `unreachable_ancestor` | yes | This Location's ancestor chain passes through a cycle elsewhere (collateral damage, not a cycle member itself) |
| `too_deep` | yes | The chain to the root exceeds `MAX_DEPTH` (12) |
| `no_country` | no | No admin-unit ancestor carries a pcode; the row is filed under `country=unknown` |
| `duplicate_pcode` | no | The same pcode is claimed by more than one Location |
| `no_geometry` | yes | No `position` and no usable boundary — the final, definitive "this row is missing" signal |
| `geometry_unexpected_type` | yes | A boundary parsed to a real geometry, but not a Point/Polygon/MultiPolygon, or the polygon was empty after repair — unlike the `boundary_*` kinds below, there is no fallback to `position` here |
| `geometry_repaired` | no | An invalid polygon ring was fixed by `shapely.make_valid` |
| `point_outside_parent` | no | A site's `(lon, lat)` falls outside its nearest polygon ancestor — in microplanning this is nearly always a real data error |
| `boundary_bad_content_type` | only if no `position` | Boundary attachment `contentType` wasn't `application/geo+json` |
| `boundary_bad_base64` | only if no `position` | Boundary attachment `data` wasn't valid base64 |
| `boundary_empty` | only if no `position` | Boundary extension present but had neither `data` nor `url` |
| `boundary_unparseable` | only if no `position` | Boundary GeoJSON couldn't be parsed, wasn't an object, or had no geometry |
| `boundary_multi_feature` | no | A `FeatureCollection` had more than one feature; the geometries were unioned into one and used as-is |
| `boundary_fetch_failed` | n/a (raised in `extract`) | `kiln extract` could not fetch a url-referenced boundary (network/HTTP error, or a `Binary` resource with bad/missing data); the attachment is left as-is, so `transform` will separately report `boundary_unresolved_url` for it |
| `boundary_unresolved_url` | only if no `position` | `transform` saw a url-only boundary it can't fetch offline; run `kiln extract` first |
| `cache_error` | n/a (raised in `extract`) | The on-disk boundary cache (`--cache-dir`) hit a problem for this one boundary — an unreadable/corrupt cache entry, or an unwritable cache directory. Treated as a cache miss: the boundary is (re-)fetched over the network as if it hadn't been cached at all, so this never stops the boundary itself from resolving |
| `small_partition` | no | A written partition has fewer than `MIN_PARTITION_ROWS` (100) rows |
| `partition_value_sanitized` | no | A `--partition-by` value contained a `/` or other filesystem-unsafe character (e.g. a pcode used as `--partition-by country`) and was rewritten for the directory name; the underlying data column is untouched |
| `partition_value_collision` | no | Two distinct `--partition-by` values rendered to the *same* directory segment (e.g. `"A/B"` and `"A_B"` both sanitizing to `A_B`; `None` and the literal string `"null"` both rendering as `null`) — the second was given a short hash suffix (e.g. `A_B~e466256d`) so both are written to separate files instead of one silently overwriting the other |

## Checking the output

[geopq-workbench](https://github.com/gsueur/geopq-workbench) opens the output
directory as a single layer and has a quality gate for exactly the properties
kiln targets — spatial index present, row-group clustering, sensible row-group
size

## Known reader gotchas

- **Partitioning by an integer column breaks hive-partitioned reads.**
  `--partition-by` on a column that also survives into the row data itself
  as a genuine integer (e.g. the raw `admin_level`, as opposed to the
  string `tier` that exists specifically to avoid this) writes correctly,
  but `pyarrow.dataset(path, partitioning="hive")` — what `kiln inspect`
  and most other readers use — infers the partition column's type from the
  *directory name* (a string) and the *in-file* column's type (`Int64`)
  independently, and refuses to reconcile them:
  `ArrowTypeError: ... incompatible types ...`. This is pre-existing (not
  introduced by this change) and not fixed here; use `tier`, or another
  string column, for hive partitioning, and reserve integer columns like
  `admin_level`/`depth` for in-file filtering only.s.
