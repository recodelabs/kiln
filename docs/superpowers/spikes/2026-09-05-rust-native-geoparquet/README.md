# Spike: native-typed GeoParquet from pure Rust (2026-09-05)

Question: can a Rust kiln write the same GeoParquet that `ogr2ogr -lco
USE_PARQUET_GEO_TYPES=YES -lco WRITE_COVERING_BBOX=YES` writes, so the Rust
port can drop the GDAL dependency?

Answer: yes. `cargo run --release -- spike.parquet` writes a file with

- `geometry` as Parquet `GEOMETRY` logical type, default (lon/lat) CRS
- per-row-group geospatial statistics (bbox + geometry types)
- a `bbox` struct column and GeoParquet 1.1 `geo` metadata for older readers

Verified on this file: GDAL 3.13.3 (`ogrinfo -spat` prunes correctly, CRS read as
WGS 84), DuckDB 1.5.5 core and spatial (`parquet_metadata` shows `geo_bbox` and
`geo_types` per row group), pyarrow 25 and GeoPandas 1.1.4 (CRS OGC:CRS84).
`parquet_schema` output is identical to the GDAL-written equivalent.

Dependencies: `arrow-array`, `arrow-schema`, `parquet` 59 with the `geospatial`
feature. No GDAL, no geoarrow crates.

Gotchas found:

- The `geoparquet` crate's `GeoParquetRecordBatchEncoder` re-emits the WKB field
  with empty extension metadata, dropping the CRS, and `parquet` then labels the
  column `crs="srid:0"` (unknown). Build the `geoarrow.wkb` field directly and
  set `{"crs":"OGC:CRS84","crs_type":"authority_code"}` on it; `parquet` maps
  that to the Parquet default.
- Omit the `crs` key in the `geo` column metadata rather than writing `null`:
  GeoPandas reads an explicit `null` as no CRS, and an absent key as CRS84.
