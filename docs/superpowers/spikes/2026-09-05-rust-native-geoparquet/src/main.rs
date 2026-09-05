//! Spike: write GeoParquet with native Parquet GEOMETRY logical type from pure Rust.
//! Mirrors what `ogr2ogr -lco USE_PARQUET_GEO_TYPES=YES -lco WRITE_COVERING_BBOX=YES` emits:
//! WKB column typed as Parquet GEOMETRY (with geospatial statistics), a bbox struct column,
//! and GeoParquet 1.1 "geo" file metadata for older readers.
use std::fs::File;
use std::sync::Arc;

use arrow_array::{ArrayRef, BinaryArray, Float64Array, RecordBatch, StringArray, StructArray};
use arrow_schema::{DataType, Field, Fields, Schema};
use parquet::arrow::ArrowWriter;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::metadata::KeyValue;
use parquet::file::properties::WriterProperties;

const GEOARROW_EXT_NAME: &str = "ARROW:extension:name";
const GEOARROW_EXT_META: &str = "ARROW:extension:metadata";

fn wkb_point(x: f64, y: f64) -> Vec<u8> {
    let mut b = Vec::with_capacity(21);
    b.push(1); // little endian
    b.extend_from_slice(&1u32.to_le_bytes()); // Point
    b.extend_from_slice(&x.to_le_bytes());
    b.extend_from_slice(&y.to_le_bytes());
    b
}

/// Axis-aligned square with lower-left (x, y) and side s, closed ring, CCW.
fn wkb_box(x: f64, y: f64, s: f64) -> Vec<u8> {
    let ring = [(x, y), (x + s, y), (x + s, y + s), (x, y + s), (x, y)];
    let mut b = Vec::new();
    b.push(1);
    b.extend_from_slice(&3u32.to_le_bytes()); // Polygon
    b.extend_from_slice(&1u32.to_le_bytes()); // one ring
    b.extend_from_slice(&(ring.len() as u32).to_le_bytes());
    for (px, py) in ring {
        b.extend_from_slice(&px.to_le_bytes());
        b.extend_from_slice(&py.to_le_bytes());
    }
    b
}

struct Row {
    id: &'static str,
    name: &'static str,
    wkb: Vec<u8>,
    bbox: (f64, f64, f64, f64),
}

fn main() {
    // Same shapes as kiln's test fixture, plus a few far-away points so row-group
    // statistics differ and spatial pruning is observable.
    let rows = vec![
        Row { id: "ng", name: "Nigeria", wkb: wkb_box(3.0, 6.0, 6.0), bbox: (3.0, 6.0, 9.0, 12.0) },
        Row { id: "kano", name: "Kano", wkb: wkb_box(3.0, 6.0, 3.0), bbox: (3.0, 6.0, 6.0, 9.0) },
        Row { id: "nassarawa", name: "Nassarawa", wkb: wkb_box(3.0, 6.0, 1.0), bbox: (3.0, 6.0, 4.0, 7.0) },
        Row { id: "gama", name: "Gama", wkb: wkb_box(3.1, 6.1, 0.4), bbox: (3.1, 6.1, 3.5, 6.5) },
        Row { id: "clinic", name: "Gama Clinic", wkb: wkb_point(3.25, 6.25), bbox: (3.25, 6.25, 3.25, 6.25) },
        Row { id: "stray", name: "Stray Post", wkb: wkb_point(50.0, 50.0), bbox: (50.0, 50.0, 50.0, 50.0) },
        Row { id: "far1", name: "Far One", wkb: wkb_point(120.0, -30.0), bbox: (120.0, -30.0, 120.0, -30.0) },
        Row { id: "far2", name: "Far Two", wkb: wkb_point(121.0, -31.0), bbox: (121.0, -31.0, 121.0, -31.0) },
        Row { id: "far3", name: "Far Three", wkb: wkb_box(-100.0, 40.0, 2.0), bbox: (-100.0, 40.0, -98.0, 42.0) },
    ];

    // geoarrow.wkb extension field with an explicit lon/lat CRS. parquet-geospatial maps
    // OGC:CRS84 to the Parquet default (no crs written) and anything unset to "srid:0",
    // so the CRS must be set here or the logical type would say "unknown CRS".
    let geom_meta = serde_json::json!({"crs": "OGC:CRS84", "crs_type": "authority_code"});
    let geom_field = Field::new("geometry", DataType::Binary, true).with_metadata(
        [
            (GEOARROW_EXT_NAME.to_string(), "geoarrow.wkb".to_string()),
            (GEOARROW_EXT_META.to_string(), geom_meta.to_string()),
        ]
        .into(),
    );
    let bbox_fields: Fields = vec![
        Field::new("xmin", DataType::Float64, false),
        Field::new("ymin", DataType::Float64, false),
        Field::new("xmax", DataType::Float64, false),
        Field::new("ymax", DataType::Float64, false),
    ]
    .into();
    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Utf8, false),
        Field::new("name", DataType::Utf8, false),
        geom_field,
        Field::new("bbox", DataType::Struct(bbox_fields.clone()), false),
    ]));

    let id: ArrayRef = Arc::new(StringArray::from_iter_values(rows.iter().map(|r| r.id)));
    let name: ArrayRef = Arc::new(StringArray::from_iter_values(rows.iter().map(|r| r.name)));
    let geometry: ArrayRef = Arc::new(BinaryArray::from_iter_values(rows.iter().map(|r| r.wkb.as_slice())));
    let bbox: ArrayRef = Arc::new(StructArray::new(
        bbox_fields,
        vec![
            Arc::new(Float64Array::from_iter_values(rows.iter().map(|r| r.bbox.0))),
            Arc::new(Float64Array::from_iter_values(rows.iter().map(|r| r.bbox.1))),
            Arc::new(Float64Array::from_iter_values(rows.iter().map(|r| r.bbox.2))),
            Arc::new(Float64Array::from_iter_values(rows.iter().map(|r| r.bbox.3))),
        ],
        None,
    ));
    let batch = RecordBatch::try_new(schema.clone(), vec![id, name, geometry, bbox]).unwrap();

    // GeoParquet 1.1 file-level metadata, for readers that predate the logical type.
    let geo = serde_json::json!({
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": ["Point", "Polygon"],
                "bbox": [-100.0, -31.0, 121.0, 50.0],
                "covering": {"bbox": {
                    "xmin": ["bbox", "xmin"], "ymin": ["bbox", "ymin"],
                    "xmax": ["bbox", "xmax"], "ymax": ["bbox", "ymax"]}}
            }
        }
    });

    let props = WriterProperties::builder()
        .set_compression(Compression::ZSTD(ZstdLevel::default()))
        .set_max_row_group_size(3) // tiny groups so per-group geo stats are visible
        .set_key_value_metadata(Some(vec![KeyValue::new("geo".into(), geo.to_string())]))
        .build();

    let out = std::env::args().nth(1).unwrap_or_else(|| "spike.parquet".into());
    let file = File::create(&out).unwrap();
    let mut writer = ArrowWriter::try_new(file, schema, Some(props)).unwrap();
    writer.write(&batch).unwrap();
    let meta = writer.close().unwrap();

    println!("wrote {} ({} rows, {} row groups)", out, meta.file_metadata().num_rows(), meta.num_row_groups());
    let parquet_schema = meta.file_metadata().schema_descr();
    for col in parquet_schema.columns() {
        println!("  column {:<14} physical={:?} logical={:?}", col.path(), col.physical_type(), col.logical_type());
    }
    for (i, rg) in meta.row_groups().iter().enumerate() {
        for cc in rg.columns() {
            if cc.column_path().string() == "geometry" {
                println!("  rg{} geometry geo_statistics={:?}", i, cc.geo_statistics().map(|s| s.bounding_box().map(|b| (b.get_xmin(), b.get_ymin(), b.get_xmax(), b.get_ymax()))));
            }
        }
    }
}
