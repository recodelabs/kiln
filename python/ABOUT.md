# What kiln is for

## The problem

A FHIR server stores places as `Location` resources — one JSON document each,
linked to their parent by reference:

```json
{
  "resourceType": "Location",
  "id": "clinic",
  "name": "Gama Clinic",
  "type": [{ "coding": [{ "code": "facility" }] }],
  "partOf": { "reference": "Location/gama" },
  "position": { "longitude": 3.25, "latitude": 6.25 }
}
```

That record does not say which country, region or district the clinic is in. To
find out, you follow `partOf` to `gama`, then its `partOf`, and so on until you
reach the top. Every question you might reasonably ask — *how many clinics are
in Kano state?*, *draw me the districts with their facilities* — means walking
that chain for every record, in application code.

No GIS tool does this. QGIS, DuckDB, deck.gl and PMTiles all want a **flat
table with a geometry column**. FHIR gives you a linked list of documents.

## What kiln does

It turns one country's `Location` resources into a single flat geospatial
table, with the hierarchy already resolved.

```
FHIR server  ──▶  locations.ndjson  ──▶  out/locations/**/*.parquet
             extract              transform
```

Here is the same clinic after `kiln transform`, alongside its neighbours:

```
       id        name   loc_type  admin_level tier admin0_name admin1_name admin2_name geom_type   lon   lat
   clinic Gama Clinic   facility          NaN site     Nigeria        Kano   Nassarawa     point  3.25  6.25
       ng     Nigeria admin-unit          0    0       Nigeria         NaN         NaN   polygon  6.00  9.00
     kano        Kano admin-unit          1    1       Nigeria        Kano         NaN   polygon  4.50  7.50
nassarawa   Nassarawa admin-unit          2    2       Nigeria        Kano   Nassarawa   polygon  3.50  6.50
     gama        Gama settlement        NaN  site      Nigeria        Kano   Nassarawa   polygon  3.20  6.20
```

The clinic row now carries `Nigeria / Kano / Nassarawa`. None of that was in its
JSON. That is the core of what kiln does.

Note `gama` is a settlement, not an admin unit — the clinic's `partOf` points at
it, not at a district. kiln walks *past* it to find the nearest real admin
ancestor at each level. Real registries are ragged like this everywhere.

## The three things it gets right

**1. Hierarchy.** Every row knows its full ancestry — as denormalized columns
(`admin0_name` … `admin4_code`) for filtering and joining, and as a path
(`/ng/kano/nassarawa/gama/clinic`) for tree operations. Broken chains are
reported rather than silently dropped: dangling parents, cycles, and nodes
orphaned by a cycle upstream each get their own diagnostic.

**2. Points and polygons in one table.** District boundaries are polygons.
Clinics are points. Both live in the same table with the same columns, and every
row also carries `lon`/`lat` — for a polygon that is a guaranteed-inside
representative point, so you can label it on a map without computing anything.

A settlement that has *both* a boundary and a GPS point produces **one** row,
not two: the polygon as geometry, the point as `lon`/`lat`.

**3. Output that is actually fast to query.** Files are partitioned by country,
geometry type and admin level, sorted along a Hilbert curve so nearby places sit
together on disk, and carry a per-row bounding box. A map that asks for one
district reads a few kilobytes instead of the whole file — including over HTTP,
without downloading it.

## What you do with it

```sql
-- DuckDB, straight off the files
SELECT admin1_name, count(*)
FROM 'out/locations/**/*.parquet'
WHERE loc_type = 'facility'
GROUP BY admin1_name;
```

Open the directory in QGIS or [geopq-workbench](https://github.com/gsueur/geopq-workbench).
Join to Overture Maps on the `gers_id` column. Bake vector tiles from the
polygon partitions. Load it into deck.gl. It is an ordinary GeoParquet dataset —
nothing about it is kiln-specific once written.

## Why it is a pipeline and not one command

`extract` touches the network; `transform` never does. They are joined by a
plain NDJSON file you can inspect, keep, diff, or hand to someone else.

That split exists because the two halves fail in different ways. A FHIR server
being slow or a token expiring has nothing to do with whether your geometry is
valid. Splitting them means you fetch once and can re-run the conversion as
often as you like, and it means the whole geospatial half is testable from a
file with no server involved.

## Why two passes to write a file

geopandas writes the staging file; `ogr2ogr` rewrites it into the final one.
This looks redundant and is not: **GDAL is the only tool available that writes
native Parquet geometry types**, which is what lets other tools skip whole
chunks of the file when answering a spatial query. This was measured, not
assumed — DuckDB writes GeoParquet 1.0, geopandas writes 1.1, only GDAL writes
the native types. Hence the hand-off.

## What it does not do

- **One country per run.** Everything is held in memory; the design targets
  100k–1M locations. Separate runs can be merged into one directory tree later.
- **It does not fix your data.** It reports problems — duplicate p-codes,
  facilities whose GPS point falls outside their own district, boundaries that
  will not parse — into `_report.json`, and keeps going. Deciding what to do
  about them is your job.
- **It does not make tiles.** PMTiles is a natural next step and the partition
  layout was designed with it in mind, but kiln stops at GeoParquet.
- **It does not write back to FHIR.** Read-only.

## Requirements

Python 3.11+, and **system GDAL 3.13 or newer** — checked at startup, with a
clear error if it is missing. See [README.md](README.md) for installation,
command reference, the full column list, and the data-quality report kinds.
