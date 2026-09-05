//! One Parquet file per partition. Native GEOMETRY logical type comes from
//! the `geoarrow.wkb` extension on the geometry field (parquet's `geospatial`
//! feature); GeoParquet 1.1 `geo` metadata and the bbox column are written
//! for readers that predate the logical type. Matches what the spike in
//! docs/superpowers/spikes/2026-09-05-rust-native-geoparquet produced.

use std::fs::File;
use std::path::Path;

use arrow_array::Array;
use parquet::arrow::ArrowWriter;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::metadata::KeyValue;
use parquet::file::properties::WriterProperties;

use crate::error::{KilnError, Result};
use crate::write::schema::{output_schema, RowBatch, BBOX_COLUMN, GEOMETRY_COLUMN};

pub struct PartitionStats {
    pub rows: usize,
    pub row_groups: usize,
}

pub struct PartitionWriter {
    writer: ArrowWriter<File>,
    bbox: [f64; 4],
    rows: usize,
    row_groups: usize,
}

impl PartitionWriter {
    pub fn create(path: &Path, row_group_size: usize) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| KilnError::io(parent, e))?;
        }
        let file = File::create(path).map_err(|e| KilnError::io(path, e))?;
        let props = WriterProperties::builder()
            .set_compression(Compression::ZSTD(ZstdLevel::default()))
            .set_max_row_group_size(row_group_size.max(1))
            .build();
        let writer = ArrowWriter::try_new(file, output_schema(), Some(props))?;
        Ok(Self {
            writer,
            bbox: [f64::MAX, f64::MAX, f64::MIN, f64::MIN],
            rows: 0,
            row_groups: 0,
        })
    }

    /// Flush the batch as one row group. Callers fill a batch to
    /// `row_group_size` and call this; the last partial batch is fine.
    pub fn write(&mut self, batch: &mut RowBatch) -> Result<()> {
        if batch.rows == 0 {
            return Ok(());
        }
        let rb = batch.finish()?;
        let bbox = rb.column_by_name(BBOX_COLUMN).expect("schema has bbox");
        let bbox = bbox
            .as_any()
            .downcast_ref::<arrow_array::StructArray>()
            .expect("bbox is a struct");
        for (i, name) in ["xmin", "ymin", "xmax", "ymax"].iter().enumerate() {
            let col = bbox
                .column_by_name(name)
                .expect("bbox field")
                .as_any()
                .downcast_ref::<arrow_array::Float64Array>()
                .expect("f64");
            for v in col.iter().flatten() {
                if i < 2 {
                    self.bbox[i] = self.bbox[i].min(v)
                } else {
                    self.bbox[i] = self.bbox[i].max(v)
                }
            }
        }
        self.rows += rb.num_rows();
        self.row_groups += 1;
        self.writer.write(&rb)?;
        self.writer.flush()?;
        Ok(())
    }

    /// Append the `geo` metadata and close the file.
    pub fn finish(mut self, geometry_types: &[String]) -> Result<PartitionStats> {
        let bbox = if self.rows == 0 {
            serde_json::Value::Null
        } else {
            serde_json::json!(self.bbox)
        };
        let geo = serde_json::json!({
            "version": "1.1.0",
            "primary_column": GEOMETRY_COLUMN,
            "columns": {
                GEOMETRY_COLUMN: {
                    "encoding": "WKB",
                    "geometry_types": geometry_types,
                    "bbox": bbox,
                    "covering": {"bbox": {
                        "xmin": [BBOX_COLUMN, "xmin"], "ymin": [BBOX_COLUMN, "ymin"],
                        "xmax": [BBOX_COLUMN, "xmax"], "ymax": [BBOX_COLUMN, "ymax"]}}
                }
            }
        });
        self.writer
            .append_key_value_metadata(KeyValue::new("geo".into(), geo.to_string()));
        self.writer.close()?;
        Ok(PartitionStats {
            rows: self.rows,
            row_groups: self.row_groups,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::write::schema::{OutputRow, RowBatch};
    use parquet::basic::LogicalType;
    use parquet::file::reader::{FileReader, SerializedFileReader};

    fn row(id: &str, x: f64, y: f64) -> OutputRow {
        let mut wkb = vec![1u8];
        wkb.extend_from_slice(&1u32.to_le_bytes());
        wkb.extend_from_slice(&x.to_le_bytes());
        wkb.extend_from_slice(&y.to_le_bytes());
        OutputRow {
            id: id.into(),
            tier: "site".into(),
            path: format!("/{id}"),
            country: "NG".into(),
            geom_type: "point".into(),
            lon: x,
            lat: y,
            wkb,
            bbox: [x, y, x, y],
            fhir_json: "{}".into(),
            ..Default::default()
        }
    }

    #[test]
    fn writes_native_geometry_type_with_stats_and_geo_metadata() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("part-0.parquet");
        let mut writer = PartitionWriter::create(&path, 2).unwrap();
        let mut batch = RowBatch::new();
        for (i, (x, y)) in [(3.0, 6.0), (3.5, 6.5), (50.0, 50.0)].iter().enumerate() {
            batch.push(&row(&format!("r{i}"), *x, *y));
            if batch.rows == 2 {
                writer.write(&mut batch).unwrap();
            }
        }
        writer.write(&mut batch).unwrap();
        let stats = writer.finish(&["Point".to_string()]).unwrap();
        assert_eq!(stats.rows, 3);
        assert_eq!(stats.row_groups, 2);

        let reader = SerializedFileReader::new(std::fs::File::open(&path).unwrap()).unwrap();
        let meta = reader.metadata();
        let geom_col = meta
            .file_metadata()
            .schema_descr()
            .columns()
            .iter()
            .find(|c| c.name() == "geometry")
            .unwrap();
        assert!(
            matches!(geom_col.logical_type(), Some(LogicalType::Geometry(_))),
            "{:?}",
            geom_col.logical_type()
        );
        let rg0 = meta
            .row_group(0)
            .columns()
            .iter()
            .find(|c| c.column_path().string() == "geometry")
            .unwrap();
        let bbox = rg0.geo_statistics().unwrap().bounding_box().unwrap();
        assert_eq!((bbox.get_xmin(), bbox.get_xmax()), (3.0, 3.5));
        let geo = meta
            .file_metadata()
            .key_value_metadata()
            .unwrap()
            .iter()
            .find(|kv| kv.key == "geo")
            .unwrap();
        let geo: serde_json::Value = serde_json::from_str(geo.value.as_ref().unwrap()).unwrap();
        assert_eq!(geo["primary_column"], "geometry");
        assert_eq!(
            geo["columns"]["geometry"]["bbox"],
            serde_json::json!([3.0, 6.0, 50.0, 50.0])
        );
        assert!(
            geo["columns"]["geometry"].get("crs").is_none(),
            "crs key must be absent, not null"
        );
        assert_eq!(
            geo["columns"]["geometry"]["geometry_types"],
            serde_json::json!(["Point"])
        );
    }

    #[test]
    fn empty_writer_finishes_with_null_bbox() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("empty.parquet");
        let writer = PartitionWriter::create(&path, 10).unwrap();
        let stats = writer.finish(&[]).unwrap();
        assert_eq!(stats.rows, 0);
        let reader = SerializedFileReader::new(std::fs::File::open(&path).unwrap()).unwrap();
        let geo = reader
            .metadata()
            .file_metadata()
            .key_value_metadata()
            .unwrap()
            .iter()
            .find(|kv| kv.key == "geo")
            .unwrap();
        let geo: serde_json::Value = serde_json::from_str(geo.value.as_ref().unwrap()).unwrap();
        assert!(geo["columns"]["geometry"]["bbox"].is_null());
    }

    #[test]
    fn duckdb_reads_the_written_partition_if_available() {
        if std::process::Command::new("duckdb")
            .arg("--version")
            .output()
            .is_err()
        {
            eprintln!("duckdb not on PATH, skipping");
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("duckdb-check.parquet");
        let mut writer = PartitionWriter::create(&path, 10).unwrap();
        let mut batch = RowBatch::new();
        for (i, (x, y)) in [(3.0, 6.0), (3.5, 6.5), (50.0, 50.0)].iter().enumerate() {
            batch.push(&row(&format!("r{i}"), *x, *y));
        }
        writer.write(&mut batch).unwrap();
        writer.finish(&["Point".to_string()]).unwrap();

        let output = std::process::Command::new("duckdb")
            .args([
                "-csv",
                "-c",
                &format!("SELECT count(*) FROM '{}'", path.display()),
            ])
            .output()
            .unwrap();
        let stdout = String::from_utf8_lossy(&output.stdout);
        eprintln!("duckdb output: {stdout}");
        assert!(stdout.contains('3'), "expected count 3, got: {stdout}");
    }
}
