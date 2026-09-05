# kiln

**A bridge between a FHIR Location registry and modern GIS tools.**

kiln turns the `Location` resources in a FHIR server into a GeoParquet dataset
that QGIS, DuckDB, GeoPandas and web maps can read directly and fast, and
carries edits made in those tools back into the FHIR server as ordinary FHIR
updates. FHIR stays the master registry. The parquet is a derived, disposable
projection of it.

It ships as a single static binary with no runtime dependencies: no Python, no
GDAL, no database. It is designed to run on a laptop in a district office.

> **Status.** This README describes the Rust rewrite of kiln, which is being
> built now. The current Python implementation, which the rewrite replaces for
> the export and load paths, is documented in [README-python.md](README-python.md).
> The Python `bake` and `bake-points` commands (GeoJSON and CSV into FHIR) stay
> in Python for now and live under `python/`.

---

## Contents

- [Why kiln exists](#why-kiln-exists)
- [What kiln does](#what-kiln-does)
- [The workflow](#the-workflow)
- [Commands](#commands)
- [The snapshot](#the-snapshot)
- [The output dataset](#the-output-dataset)
- [How transform works: the two pass design](#how-transform-works-the-two-pass-design)
- [Round trip: diff and load](#round-trip-diff-and-load)
- [Reading the output](#reading-the-output)
- [Design decisions](#design-decisions)
- [What kiln does not do](#what-kiln-does-not-do)
- [Future extensions](#future-extensions)
- [Building](#building)
- [Repository layout](#repository-layout)

---

## Why kiln exists

Public health campaign systems built on FHIR keep their places, from the
country down to the village clinic, as `Location` resources. That is the right
home for them. A Location is linked to everything else in the registry: the
organisations that run it, the campaigns that target it, the people and
households recorded in it. It has identifiers from several systems, a version
history, and a defined profile.

It is also, for GIS purposes, close to unusable.

A FHIR server is a document store. Each Location is one JSON document, linked to
its parent by a `partOf` reference. To answer *how many clinics are in Kano
state* you follow `partOf` from every clinic up the chain, in application code,
one HTTP request at a time. Boundary polygons are base64 encoded GeoJSON inside
an attachment extension. There is no spatial index, no spatial query, no way to
ask for everything inside a bounding box. HAPI's Postgres tables are not a GIS
schema you can point PostGIS at, and hosted FHIR stores expose no SQL at all.

Meanwhile every GIS tool wants the same thing: a flat table with a geometry
column, ideally in a format it can read over HTTP without downloading all of it.
GeoParquet is that format. QGIS opens it. DuckDB queries it in place, from a
laptop or from a bucket. GeoPandas loads it. A web map can fetch only the row
groups that intersect the viewport.

kiln is the translation between those two worlds, in both directions.

### Why a projection and not a query layer

The alternative is to put a spatial API in front of the FHIR server. That means
a running service, a second copy of the data that has to stay in sync, and a
custom protocol every client has to learn. A file on disk or in a bucket is
simpler in every way: no server, nothing to keep alive, every GIS tool already
speaks it, and it can be rebuilt from the FHIR server at any time. If the
parquet is wrong, delete it and run kiln again. Nothing is lost.

### Why a single binary

kiln's users include people setting up a country deployment with limited
connectivity, on machines with a few gigabytes of memory, without a Python or
GDAL toolchain and without the ability to install one. The original Python
kiln needed Python 3.11, a package manager, and a system GDAL built against a
recent Arrow, because at the time GDAL was the only writer of the native
Parquet geometry type. That is no longer true. The Rust Parquet implementation
writes it directly, verified against GDAL, DuckDB and pyarrow (see
[docs/superpowers/spikes/2026-09-05-rust-native-geoparquet](docs/superpowers/spikes/2026-09-05-rust-native-geoparquet)).
So kiln can be one file you copy onto a machine and run.

---

## What kiln does

Given a FHIR server, kiln produces this:

```
FHIR server ──extract──▶ snapshot/ ──transform──▶ out/
                          (NDJSON)               (GeoParquet, partitioned)
```

and, when someone edits the data in a GIS tool, this:

```
edits.geojson ──diff (against snapshot)──▶ changes.ndjson ──load──▶ FHIR server
```

The transform is where the value is. Take a clinic as the FHIR server stores it:

```json
{
  "resourceType": "Location",
  "id": "clinic",
  "name": "Gama Clinic",
  "status": "active",
  "type": [{ "coding": [{ "code": "facility" }] }],
  "partOf": { "reference": "Location/gama" },
  "position": { "longitude": 3.25, "latitude": 6.25 },
  "identifier": [{ "system": "https://icr.example/identifiers/pcode", "value": "NG001002001" }]
}
```

Nothing in that record says which state or district the clinic is in. After
kiln, the same clinic is one row in a table alongside its neighbours:

```
id         name          type        admin_level  tier   admin0_name  admin1_name  admin2_name  geom_type
clinic     Gama Clinic   facility    -            site   Nigeria      Kano         Nassarawa    point
gama       Gama          settlement  -            site   Nigeria      Kano         Nassarawa    polygon
nassarawa  Nassarawa     admin-unit  2            2      Nigeria      Kano         Nassarawa    polygon
kano       Kano          admin-unit  1            1      Nigeria      Kano         -            polygon
ng         Nigeria       admin-unit  0            0      Nigeria      -            -            polygon
```

The clinic's row carries `Nigeria / Kano / Nassarawa`. kiln walked the
`partOf` chain for every record, skipped past `gama` because a settlement is
not an admin unit, and wrote the nearest real admin ancestor at each level as a
plain column. Every row also has a full ancestry path, a geometry, a
representative point, and the complete original FHIR resource as JSON, so
nothing is lost in the projection.

That table is what GIS tools want. Everything else in kiln exists to produce it
reliably, quickly, incrementally, and in a form that can be edited and pushed
back.

---

## The workflow

A typical cycle, for one country:

1. **Extract.** Pull Location resources from the FHIR server into a local
   snapshot. The first run fetches everything. Later runs fetch only what
   changed since the last run and merge it in.
2. **Transform.** Build the GeoParquet dataset from the snapshot. This is
   offline and fast, and can be re-run as often as you like.
3. **Publish.** Copy the output directory to wherever people read it: a shared
   drive, an object storage bucket, a static web server. This step is outside
   kiln; the output is plain files.
4. **Use.** Open in QGIS. Query with DuckDB. Join to campaign data. Draw a map.
5. **Edit.** Move a facility, correct a name, fix a ward boundary, add a new
   site. Do this in QGIS or any tool that can write GeoJSON or GeoParquet.
6. **Diff.** Compare the edited file to the snapshot. kiln works out which
   Locations actually changed and writes them as complete FHIR resources.
7. **Load.** Send the changed resources to the FHIR server, with a version
   check so an edit made on the server in the meantime is never overwritten.
8. **Extract again.** The snapshot picks up the new versions, and the cycle
   continues.

Steps 1 and 2 can run on a schedule or in response to a change notification.
Steps 5 to 7 happen when someone has something to fix.

---

## Commands

All commands are subcommands of one binary.

```
kiln extract   --server URL [--token T] --snapshot DIR [--full] [--concurrency N] [--retries N]
kiln transform --snapshot DIR --out DIR [--country CC] [--row-group-size N] [--partition-by KEYS]
kiln run       --server URL [--token T] --snapshot DIR --out DIR [...]
kiln inspect   --out DIR
kiln diff      --snapshot DIR --in EDITS --out CHANGES.ndjson
kiln load      --server URL [--token T] --in CHANGES.ndjson [--dry-run] [--batch-size N]
```

`--token` falls back to `$KILN_TOKEN`. Any FHIR R4 server that supports
standard search with `_lastUpdated` and transaction bundles works; kiln does
not depend on any vendor's extensions.

`extract` and `load` touch the network. `transform`, `inspect` and `diff`
never do. That split is deliberate: a slow server or an expired token has
nothing to do with whether your geometry is valid, and keeping them apart means
the whole geospatial half is testable from files with no server involved.

---

## The snapshot

The snapshot is kiln's local copy of the registry and the unit of state
between runs.

```
snapshot/
  locations.ndjson     one Location resource per line, boundaries inlined
  state.json           server URL, watermark, resource count, kiln version
  boundaries/          content addressed cache of fetched boundary attachments
```

**`locations.ndjson`** holds every Location as the server returned it, with
one change: a boundary attachment that referred to a URL has been fetched and
inlined as base64 data, so the file is self contained. It is plain NDJSON. You
can `grep` it, `jq` it, diff two of them, or hand one to someone else.

**`state.json`** records the watermark: the latest `meta.lastUpdated` seen in
the snapshot. The next extract asks the server for
`Location?_lastUpdated=gt<watermark>` and merges the result by id: new ids are
appended, existing ids are replaced. The merge rewrites the file, which is a
sequential pass and takes seconds even for a large country.

**`boundaries/`** is the boundary cache carried over from the Python kiln.
Admin boundaries change rarely, and a registry can have tens of thousands of
them, so every successfully fetched attachment is stored under a hash of its
URL and served from disk on later runs. A run that is killed halfway through a
long fetch resumes where it left off. Only successes are cached, so a
temporary outage does not become a permanent one.

### Incremental extract and its limits

Incremental extract makes the routine case fast: a registry that changed by a
few hundred resources since yesterday costs a few hundred resource fetches,
not a hundred thousand.

It has one blind spot. A resource that was **deleted** on the server does not
appear in any search result, so an incremental extract cannot see it go. Two
rules follow:

- Locations should be retired, not deleted. Set `status` to `inactive`. That
  is better registry practice anyway, because other resources may still refer
  to them.
- `kiln extract --full` discards the snapshot and fetches everything. Run it
  occasionally, or whenever you suspect drift.

---

## The output dataset

`transform` writes one GeoParquet dataset per snapshot:

```
out/
  locations/
    country=NG/
      geom_type=polygon/part-0.parquet
      geom_type=point/part-0.parquet
  _report.json
```

Partitioned by country and geometry type only. Within each file, rows are
sorted along a Hilbert curve so that places near each other on the ground sit
near each other on disk, and every row group carries its bounding box in the
Parquet geospatial statistics. A reader asking for one district touches a few
kilobytes, including over HTTP with range requests.

The geometry column uses the native Parquet `GEOMETRY` logical type, with the
GeoParquet 1.1 file metadata and a `bbox` column alongside for readers that
predate the logical type. GDAL 3.12+, QGIS, DuckDB 1.5+, pyarrow 21+ and
GeoPandas all read it; older readers fall back to the metadata.

### Columns

One row per Location. Columns are named after the FHIR path they come from
wherever one exists, so you can guess the column from the resource and the
resource from the column. They fall into four groups, and the group tells you
whether an edit to the column will flow back to FHIR.

**Identity and version. Read only.**

| column | from |
|---|---|
| `id` | `Location.id` |
| `version_id` | `Location.meta.versionId` |
| `last_updated` | `Location.meta.lastUpdated` |

**Writable FHIR content. Edits flow back through `diff`.**

| column | from |
|---|---|
| `name` | `Location.name` |
| `alias` | `Location.alias`, list of string |
| `status` | `Location.status` |
| `description` | `Location.description` |
| `type` | first `Location.type.coding.code` |
| `physical_type` | `Location.physicalType.coding.code` |
| `part_of` | `Location.partOf.reference`, as a bare id |
| `managing_organization` | `Location.managingOrganization.reference`, as a bare id |
| `identifier` | `Location.identifier`, list of struct `{system, value}` |
| `position_longitude`, `position_latitude` | `Location.position` |
| `geometry` | the boundary attachment extension, as a polygon; or the position, as a point |
| `pcode`, `gers_id` | promoted from `identifier` by system, for convenience |
| `settlement_type`, `delivery_strategy`, `facility_level`, `ownership` | the ICR profile extensions |

A Location with both a boundary and a position is one row: the polygon is the
geometry, and the position is kept in its own columns.

**Derived hierarchy. Rebuilt on every transform, ignored by `diff`.**

| column | meaning |
|---|---|
| `depth` | number of `partOf` hops to the root |
| `admin_level` | 0 for country, 1 for the first subdivision, and so on; null for sites |
| `tier` | `0` to `4` for admin units, `site` for everything else |
| `path` | `/ng/kano/nassarawa/gama/clinic` |
| `ancestor_ids` | list of ids from the root down |
| `admin0_name` … `admin4_name`, `admin0_code` … `admin4_code` | nearest admin ancestor at each level |
| `overlays_admin_unit_ids` | admin units this operational area overlaps, when it is not in the tree |
| `country` | ISO code, from the level 0 ancestor |
| `geom_type` | `polygon` or `point` |
| `lon`, `lat` | a representative point guaranteed inside the geometry, for labelling |
| `bbox` | struct of `xmin, ymin, xmax, ymax` |

**Lossless fallback.**

| column | content |
|---|---|
| `fhir_json` | the complete Location resource as JSON text, with the boundary attachment removed |

The last column is the guarantee that the projection loses nothing. Any field
kiln does not model as a column is still there, queryable with DuckDB's JSON
functions, and `diff` uses it as the base when reconstructing an edited
resource, so an extension kiln has never heard of survives a round trip
through QGIS untouched.

### The report

`_report.json` records what transform found and could not or would not fix:
dangling parents, cycles, nodes orphaned by a cycle upstream, duplicate
p-codes, boundaries that did not parse, invalid polygons, facilities whose GPS
point falls outside their own district. Each has a code, a count, and the
affected ids. kiln keeps going past all of them. Deciding what to do about
them is the registry owner's job, and the report is how they find out.

---

## How transform works: the two pass design

The Python kiln held everything in memory: every resource, every parsed
polygon, all at once in a dataframe. That is fine on a workstation and not
fine on a two gigabyte laptop with a country of detailed ward boundaries.

The Rust kiln reads the snapshot twice and never holds the geometries.

### Pass one: index

Read `locations.ndjson` line by line. For each resource, parse it and keep a
small record:

- the ids and text needed for the hierarchy: id, parent id, name, codes,
  type, status, the promoted extension values
- the **byte offset and length** of the line in the file
- the geometry's **kind and bounding box**, computed by parsing the boundary
  once and immediately discarding the parsed shape
- any diagnostics found while parsing

Then drop everything else. The record is on the order of a hundred bytes plus
its strings. A million Locations index into a few hundred megabytes at most,
and the polygons, which are where the real bulk is, are not resident.

All of kiln's logic runs on this index:

1. **Hierarchy.** Follow `partOf` from every node to the root, with
   memoisation so shared ancestor chains are walked once. Detect dangling
   parents, cycles, and nodes orphaned by a cycle. Assign depth, tier, path,
   ancestor list, and the nearest admin ancestor at each level, walking past
   settlements and operational areas that sit in the tree but are not admin
   units.
2. **Country.** From the level 0 ancestor, or the `--country` override.
3. **Data quality.** Duplicate p-codes, missing geometry, unresolved boundary
   URLs.
4. **Sort order.** A Hilbert curve key from the centre of each bounding box,
   over the country's extent. Sort the index by partition, then by key.

### Pass two: write

Walk the sorted index. Keep one Parquet writer open per partition, created on
first use. For each record:

1. Seek to its byte offset and re-read the one line.
2. Parse the resource again, this time keeping the geometry.
3. Validate the polygon, compute the representative point, encode to WKB.
4. Build the row and append it to the partition's current batch.
5. Release the geometry.

Batches flush to a row group at the configured size. The one check that needs
two geometries at once, a point outside its parent's polygon, fetches the
parent by seeking to *its* offset; parents are few and checked repeatedly, so
a small cache of parent polygons is kept, bounded by count.

Peak memory is the index plus one batch per open partition plus the parent
cache. It does not grow with polygon size. The cost is reading the file twice,
and the second read is in sorted order rather than file order, so it is
seek-heavy. On an SSD this is invisible. On a spinning disk it is still far
cheaper than swapping.

### Atomic output

Each partition is written to a temporary file and renamed into place. The
whole dataset directory is then swapped in one rename, with the previous
dataset kept as a backup until the swap completes. A crash at any point
leaves either the old dataset or the new one, never a mixture and never a
truncated file that a reader might mistake for a complete one.

A failed transform leaves the previous dataset intact but removes
`_report.json`. A report describing a dataset it does not match is worse
than none: silence is at least honest about not knowing.

### Geometry handling

GeoJSON is parsed with the `geojson` crate into `geo` types. Validity is
checked and reported. It is **not** repaired: a self intersecting ward
boundary is a registry problem, and silently fixing it would hide that from
the people who need to know. The representative point is a `geo` interior
point. WKB is encoded directly. There is no GEOS and no GDAL anywhere in the
binary.

---

## Round trip: diff and load

Editing happens in GIS tools because that is where the good editing tools are.
QGIS can move a point, redraw a boundary, or edit a name in a table view, and
it can do it against the kiln parquet loaded as a layer. What it cannot do is
talk FHIR. `diff` and `load` close that gap.

### diff

```
kiln diff --snapshot snapshot/ --in edits.geojson --out changes.ndjson
```

Input is GeoJSON or GeoParquet, detected by extension, with an `id` column.
GeoJSON is what QGIS exports most naturally; GeoParquet suits DuckDB and
Python users. Rows with no `id` are treated as new Locations, given a
generated id, and reported.

For each row, diff finds the snapshot resource by id and rebuilds what the
resource *should* now be:

1. Start from the snapshot's full resource (`fhir_json`).
2. Apply every **writable** column from the input on top of it: name, status,
   identifiers, position, parent, and so on.
3. If the input geometry differs from the snapshot geometry, replace the
   boundary attachment. Comparison is on the WKB after rounding coordinates
   to seven decimals, so a float round trip through a GIS tool does not
   register as an edit.
4. Ignore every **derived** column. A stale `admin1_name` in the input never
   causes a change.
5. Compare the rebuilt resource to the original. If nothing changed, skip it.

What comes out is plain FHIR NDJSON containing only the resources that
changed, each complete, each carrying the `meta.versionId` it was based on.
You can inspect it, validate it against the profile, or hand it to someone
else before anything is sent.

### load

```
kiln load --server URL --token T --in changes.ndjson [--dry-run]
```

Load orders resources parents first, groups them into transaction bundles of
`PUT Location/<id>`, and posts them with retry and backoff. Before the first
bundle it checks the server's capability statement for update-as-create,
because `PUT` to a new id needs it and one clear error beats hundreds of
identical 404s.

Every entry carries `If-Match` with the versionId from the snapshot. If the
resource was changed on the server after the snapshot was taken, the server
answers 412, kiln reports which ids conflicted, and nothing in that bundle is
written. The fix is to extract again, re-apply the edit, and diff again. This
is what makes it safe for a GIS user to edit a copy that might be a day old.

`--dry-run` runs the preflight and prints the bundle plan without posting.

Load does not update the snapshot. The server is the authority on what was
stored; the next extract brings the snapshot up to date.

### What is writable

Only FHIR content is writable: the columns in the second group above. The
hierarchy columns are computed and there is nothing to write them to. To move
a facility to a different district, change `part_of`. To rename a state,
change `name` on the state's row; every descendant's `admin1_name` will follow
on the next transform.

---

## Reading the output

The output is an ordinary GeoParquet dataset. Nothing about it is kiln
specific once written.

**DuckDB**, straight off the files, no extension needed for the geometry type:

```sql
SELECT admin1_name, count(*)
FROM 'out/locations/**/*.parquet'
WHERE type = 'facility'
GROUP BY admin1_name;
```

With the spatial extension, spatial predicates prune row groups using the
native statistics:

```sql
LOAD spatial;
SELECT id, name
FROM 'out/locations/**/*.parquet'
WHERE ST_Intersects(geometry, ST_MakeEnvelope(3, 6, 4, 7));
```

A field kiln did not promote to a column is still reachable:

```sql
SELECT id, fhir_json->>'$.extension[0].url' FROM 'out/locations/**/*.parquet';
```

**QGIS** opens the directory or a single partition file as a vector layer.

**GeoPandas** reads it with `read_parquet`.

**Vector tiles.** kiln does not make tiles. Two paths that start from its
output:

- On demand: DuckDB's `ST_AsMVT` cuts a Mapbox Vector Tile per request from
  the parquet, behind a tiny tile server or in the browser with DuckDB WASM.
- Pre baked: tippecanoe over the polygon and point partitions produces a
  PMTiles archive for fully static hosting.

Both are a few lines of glue outside kiln, and both are rebuilt from the same
parquet after every transform.

For a production deployment the likely tile server is
[Martin](https://maplibre.org/martin/), a MapLibre project written in Rust.
It serves PMTiles from local files or over HTTP, so it can front the
tippecanoe output directly, and it has a GeoParquet source backed by DuckDB
that would cut tiles from kiln's parquet with no bake step at all. That
source is marked unstable at the time of writing; the PMTiles path is the
one to rely on until it settles.

---

## Design decisions

**FHIR is the master; parquet is a projection.** The parquet can always be
rebuilt. It is never the source of truth and never edited in place as the
record of what is true. This keeps FHIR's version history, references, and
profile validation as the single authority.

**Incremental extract, full transform.** Extract is incremental because
network is the slow part. Transform is always full because the alternative is
worse than it looks: renaming one state changes the derived columns of every
row beneath it, so partial parquet updates would have to track change
amplification through the hierarchy. A full transform from the snapshot is a
sequential pass that takes seconds to minutes and has no such state.

**Two passes over the snapshot instead of everything in memory.** Explained
above. The design targets machines with a few gigabytes, and polygons are
where the bytes are.

**Native Parquet geometry type plus legacy metadata.** The native type gives
readers per row group pruning. The GeoParquet 1.1 metadata and the `bbox`
column keep older readers working. Writing both costs a few bytes per row.

**Column names follow FHIR paths.** `status`, `part_of`,
`position_longitude`, `identifier`. Someone who knows the resource can find
the column, and someone who knows the column can find the field.

**`fhir_json` in every row.** The projection is lossless. Whatever kiln does
not model still survives, is still queryable, and still round trips.

**Report, don't repair.** Invalid geometry, duplicate codes, broken chains are
recorded and the run continues. Fixing them silently would hide registry
problems from the people whose job is to fix them.

**Version checked writes.** `If-Match` on every load is what makes editing a
stale copy safe. Without it, a GIS user working from yesterday's export could
overwrite today's server side correction without either party noticing.

**Generic FHIR.** Standard search, standard paging, standard transaction
bundles, standard `If-Match`. kiln has no dependency on any particular
server's extensions, so the same binary works against HAPI, a hosted FHIR
store, or anything else that implements R4.

**No GDAL, no GEOS, no Python at runtime.** The deployment constraint drives
this. Everything kiln needs from those libraries turned out to be available
in pure Rust, at the cost of not offering geometry repair, which kiln did not
want to offer anyway.

---

## What kiln does not do

- **Make tiles.** See above. Tippecanoe and DuckDB do this well from kiln's
  output.
- **Repair geometry.** Reported, never fixed.
- **Delete resources.** Retire them with `status = inactive`.
- **Merge countries.** One snapshot and one dataset per country. Separate
  outputs can be placed under one directory tree by hand, since the partition
  key includes the country.
- **Import external data.** Turning a GRID3 ward file or a CSV of facilities
  into FHIR resources is the job of `bake` and `bake-points`, which remain in
  the Python package under `python/` for now.
- **Validate against the profile.** `diff` produces structurally correct
  resources; running them through a FHIR validator before load is the
  operator's call, and the NDJSON checkpoint exists so that is easy.

---

## Future extensions

Things that fit the design and are not in it yet, roughly in the order they
are likely to matter.

**Change notifications.** Today extract is run on a schedule or by hand. A
FHIR server that supports Subscriptions, or any outboard notification of
resource changes, could trigger `kiln run` so the parquet is never more than a
minute behind the registry. The watermark logic already supports this; only
the trigger is missing.

**Publishing.** A `--publish` target that copies the finished dataset to an
object storage bucket after the atomic swap, so the whole pipeline is one
command. Readers over HTTP would see either the old or the new dataset, never
a partial one.

**Multi country datasets.** Merge several snapshots into one dataset with
`country` as the top level partition, for a regional or global view. The
partition layout already accommodates it; what is missing is the merge
command and a decision about how the report combines.

**`bake` and `bake-points` in Rust.** Porting the import side of the Python
kiln, so the whole toolkit is one binary. The GeoJSON and CSV parsing are
straightforward; `--dissolve-parents` needs polygon union, which is available
in pure Rust but is the piece to prototype first.

**Tile baking as an optional step.** If a Rust tiler matures to the point of
matching tippecanoe's generalisation quality, `kiln tiles` could become an
optional subcommand. Until then, tippecanoe is the right tool and kiln stays
out of the way.

**Other resource types.** The same projection idea applies to any resource
with a spatial or hierarchical shape: `Organization` hierarchies, `Group`
households with a location, campaign `CarePlan` targets. The snapshot and
transform machinery is resource agnostic; the column mapping is not, and each
type would need its own.

**Validation hook.** An optional call to a FHIR validator on `diff` output
before `load`, so profile violations are caught locally rather than by the
server.

**Geometry repair as an explicit opt in.** If a deployment wants kiln to fix
invalid polygons rather than report them, a `--repair` flag could do so. That
would mean taking on GEOS or a pure Rust equivalent, and it would need to be
clearly marked in the report which geometries were altered.

---

## Building

Requires a stable Rust toolchain.

```sh
cargo build --release
./target/release/kiln --help
```

Release binaries are built in CI for macOS (arm64, x86_64), Linux (x86_64 and
aarch64, statically linked against musl) and Windows (x86_64), as single
files with no runtime dependencies.

Tests:

```sh
cargo test
```

Integration tests read the fixture snapshot under `tests/fixtures/`, run a
transform, and verify the output with DuckDB, which must be on `PATH` for
those tests only. DuckDB is never linked into kiln.

---

## Repository layout

```
kiln/
  Cargo.toml
  src/
    main.rs, cli.rs
    fhir/        Location parsing, profile extensions, NDJSON read and write
    snapshot/    state.json, merge by id, watermark
    extract/     FHIR client, paging, boundary fetch, boundary cache
    index/       the pass one record, hierarchy, Hilbert key, partition choice
    geometry/    GeoJSON parse, validity, interior point, WKB
    write/       Parquet writers, geo metadata, atomic swap
    report.rs
    diff/        GeoJSON and GeoParquet readers, resource reconstruction
    load/        bundles, If-Match, capability preflight, retry
  tests/
    fixtures/
  python/        the Python package: bake and bake-points
  docs/
    superpowers/ design specs, plans, and spikes
```
