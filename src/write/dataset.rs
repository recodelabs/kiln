//! Pass two: walk the index's output order, seek each line, build rows, write
//! one file per partition into a staging directory, then swap it in atomically.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use geo::Intersects;

use crate::error::{KilnError, Result};
use crate::fhir::ndjson::LineAccess;
use crate::fhir::Location;
use crate::geometry::{self, GeomKind};
use crate::index::partition::{segment, Claims, PartitionKey};
use crate::index::{Index, IndexRecord};
use crate::report::Report;
use crate::write::parquet::PartitionWriter;
use crate::write::schema::{OutputRow, RowBatch};

pub const DATASET_DIR: &str = "locations";
const STAGING_DIR: &str = ".locations.tmp";
const BACKUP_DIR: &str = ".locations.bak";
const MIN_PARTITION_ROWS: usize = 100;
const PARENT_CACHE_MAX: usize = 256;

pub struct WrittenPartition {
    pub path: PathBuf,
    /// `key=value/key=value` as written under the dataset directory.
    pub partition: String,
    pub rows: usize,
    pub row_groups: usize,
}

/// Re-read and parse one record's line. Diagnostics were reported in pass
/// one; this parses with a throwaway report so nothing is counted twice.
fn load_location(lines: &mut LineAccess, rec: &IndexRecord) -> Result<Option<Location>> {
    let text = lines.read_at(rec.offset, rec.len)?;
    let value: serde_json::Value = serde_json::from_str(&text)?;
    let mut silent = Report::default();
    Ok(Location::parse(&value, &mut silent))
}

/// Bounded cache of parent polygons for the point-outside-parent check,
/// keyed by record index.
struct ParentCache {
    polygons: HashMap<u32, Option<geo::Geometry<f64>>>,
}

impl ParentCache {
    fn get(
        &mut self,
        idx: u32,
        records: &[IndexRecord],
        lines: &mut LineAccess,
    ) -> Result<Option<geo::Geometry<f64>>> {
        if let Some(cached) = self.polygons.get(&idx) {
            return Ok(cached.clone());
        }
        if self.polygons.len() >= PARENT_CACHE_MAX {
            self.polygons.clear();
        }
        let mut silent = Report::default();
        let geom = load_location(lines, &records[idx as usize])?
            .and_then(|loc| geometry::build(&loc, &mut silent))
            .filter(|g| g.kind == GeomKind::Polygon)
            .map(|g| g.geometry);
        self.polygons.insert(idx, geom.clone());
        Ok(geom)
    }
}

/// The nearest ancestor (walking parent links) that has a polygon geometry.
fn nearest_polygon_ancestor(index: &Index, i: usize) -> Option<u32> {
    let mut cur = index.hierarchy.get(i)?.parent;
    while let Some(p) = cur {
        let pi = p.index();
        if index.records[pi].geometry.map(|g| g.kind) == Some(GeomKind::Polygon) {
            return Some(pi as u32);
        }
        cur = index.hierarchy.get(pi)?.parent;
    }
    None
}

fn recover_incomplete_swap(dataset: &Path, backup: &Path, staging: &Path) -> Result<()> {
    if backup.exists() && !dataset.exists() {
        std::fs::rename(backup, dataset).map_err(|e| KilnError::io(backup, e))?;
    } else if backup.exists() {
        std::fs::remove_dir_all(backup).map_err(|e| KilnError::io(backup, e))?;
    }
    if staging.exists() {
        std::fs::remove_dir_all(staging).map_err(|e| KilnError::io(staging, e))?;
    }
    Ok(())
}

fn swap_in(dataset: &Path, backup: &Path, staging: &Path) -> Result<()> {
    if dataset.exists() {
        std::fs::rename(dataset, backup).map_err(|e| KilnError::io(dataset, e))?;
    }
    std::fs::rename(staging, dataset).map_err(|e| KilnError::io(staging, e))?;
    if backup.exists() {
        std::fs::remove_dir_all(backup).map_err(|e| KilnError::io(backup, e))?;
    }
    Ok(())
}

pub fn write_dataset(
    ndjson: &Path,
    index: &mut Index,
    out_dir: &Path,
    keys: &[PartitionKey],
    row_group_size: usize,
) -> Result<Vec<WrittenPartition>> {
    std::fs::create_dir_all(out_dir).map_err(|e| KilnError::io(out_dir, e))?;
    let dataset = out_dir.join(DATASET_DIR);
    let backup = out_dir.join(BACKUP_DIR);
    let staging = out_dir.join(STAGING_DIR);
    if dataset.is_symlink() {
        return Err(KilnError::Usage(format!(
            "{} is a symlink; kiln will not replace it",
            dataset.display()
        )));
    }
    recover_incomplete_swap(&dataset, &backup, &staging)?;
    std::fs::create_dir(&staging).map_err(|e| KilnError::io(&staging, e))?;

    let written = match write_partitions(ndjson, index, &staging, keys, row_group_size) {
        Ok(w) => w,
        Err(e) => {
            let _ = std::fs::remove_dir_all(&staging);
            return Err(e);
        }
    };
    swap_in(&dataset, &backup, &staging)?;
    Ok(written
        .into_iter()
        .map(|w| WrittenPartition {
            path: dataset.join(
                w.path
                    .strip_prefix(&staging)
                    .expect("written under staging"),
            ),
            ..w
        })
        .collect())
}

fn write_partitions(
    ndjson: &Path,
    index: &mut Index,
    staging: &Path,
    keys: &[PartitionKey],
    row_group_size: usize,
) -> Result<Vec<WrittenPartition>> {
    // Group records by their partition values, preserving the index's
    // (country, geom_type, Hilbert) order within each group so every
    // partition stays spatially clustered whatever keys were chosen.
    let mut groups: Vec<(Vec<String>, Vec<u32>)> = Vec::new();
    for &i in &index.order {
        let rec = &index.records[i as usize];
        let values: Vec<String> = keys.iter().map(|k| k.value(rec).to_string()).collect();
        match groups.iter_mut().find(|(v, _)| *v == values) {
            Some((_, idxs)) => idxs.push(i),
            None => groups.push((values, vec![i])),
        }
    }

    let mut lines = LineAccess::open(ndjson)?;
    let mut parent_cache = ParentCache {
        polygons: HashMap::new(),
    };
    // One claims table per (parent path, key): a segment only needs to be
    // unique among siblings in the same directory.
    let mut claims: HashMap<(Vec<String>, &'static str), Claims> = HashMap::new();
    let mut report = Report::default();
    let mut written = Vec::new();

    for (values, idxs) in &groups {
        let mut segments: Vec<String> = Vec::new();
        for (key, value) in keys.iter().zip(values) {
            let ctx = (segments.clone(), key.name());
            let seg = segment(
                key.name(),
                value,
                claims.entry(ctx).or_default(),
                &mut report,
            );
            segments.push(format!("{}={seg}", key.name()));
        }
        let partition = segments.join("/");
        if idxs.len() < MIN_PARTITION_ROWS {
            report.add(
                "small_partition",
                &partition,
                &format!(
                    "{} rows is below MIN_PARTITION_ROWS={MIN_PARTITION_ROWS}",
                    idxs.len()
                ),
            );
        }
        let path = segments
            .iter()
            .fold(staging.to_path_buf(), |p, s| p.join(s))
            .join("part-0.parquet");
        let mut writer = PartitionWriter::create(&path, row_group_size)?;
        let mut batch = RowBatch::new();
        let mut geometry_types: Vec<String> = Vec::new();

        for &i in idxs {
            let i = i as usize;
            let rec = &index.records[i];
            let Some(loc) = load_location(&mut lines, rec)? else {
                continue;
            };
            let mut silent = Report::default();
            let Some(geom) = geometry::build(&loc, &mut silent) else {
                continue;
            };
            let Some(info) = index.hierarchy.get(i) else {
                continue;
            };

            if let Some(parent_idx) = nearest_polygon_ancestor(index, i) {
                if let Some(parent) = parent_cache.get(parent_idx, &index.records, &mut lines)? {
                    let pt = geo::Point::new(geom.lon, geom.lat);
                    if !parent.intersects(&pt) {
                        report.add(
                            "point_outside_parent",
                            &rec.id,
                            &format!(
                                "({}, {}) falls outside its nearest admin ancestor",
                                geom.lon, geom.lat
                            ),
                        );
                    }
                }
            }

            let type_name = match &geom.geometry {
                geo::Geometry::Point(_) => "Point",
                geo::Geometry::Polygon(_) => "Polygon",
                geo::Geometry::MultiPolygon(_) => "MultiPolygon",
                _ => "Geometry",
            };
            if !geometry_types.iter().any(|t| t == type_name) {
                geometry_types.push(type_name.to_string());
            }

            let row = OutputRow {
                id: loc.id.clone(),
                version_id: loc.version_id.clone(),
                last_updated: loc.last_updated.clone(),
                name: loc.name.clone(),
                alias: loc.alias.clone(),
                status: loc.status.clone(),
                description: loc.description.clone(),
                type_code: loc.type_code.clone(),
                physical_type: loc.physical_type.clone(),
                part_of: loc.part_of.clone(),
                managing_organization: loc.managing_organization.clone(),
                identifier: loc
                    .identifier
                    .iter()
                    .map(|ident| (ident.system.clone(), ident.value.clone()))
                    .collect(),
                position: loc.position,
                pcode: loc.pcode.clone(),
                gers_id: loc.gers_id.clone(),
                settlement_type: loc.settlement_type.clone(),
                delivery_strategy: loc.delivery_strategy.clone(),
                facility_level: loc.facility_level.clone(),
                ownership: loc.ownership.clone(),
                depth: i32::from(info.depth),
                admin_level: info.admin_level.map(i32::from),
                tier: rec.tier.clone(),
                path: index.hierarchy.path(&index.records, i),
                ancestor_ids: index.hierarchy.ancestor_ids(&index.records, i),
                admin_names: index.hierarchy.admin_names(&index.records, i),
                admin_codes: index.hierarchy.admin_codes(&index.records, i),
                overlays_admin_unit_ids: loc.overlays_admin_unit_ids.clone(),
                country: rec.country.clone(),
                geom_type: geom.kind.as_str().to_string(),
                lon: geom.lon,
                lat: geom.lat,
                wkb: geom.wkb(),
                bbox: geom.bbox,
                fhir_json: loc.fhir_json,
            };
            batch.push(&row);
            if batch.rows >= row_group_size {
                writer.write(&mut batch)?;
            }
        }
        writer.write(&mut batch)?;
        let stats = writer.finish(&geometry_types)?;
        written.push(WrittenPartition {
            path,
            partition,
            rows: stats.rows,
            row_groups: stats.row_groups,
        });
    }

    for issue in report.issues {
        index
            .report
            .add(&issue.kind, &issue.location_id, &issue.detail);
    }
    Ok(written)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::build_index;
    use crate::index::partition::parse_keys;
    use std::path::Path;

    fn fixture() -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/snapshot/locations.ndjson")
    }

    #[test]
    fn writes_one_file_per_partition_and_swaps_atomically() {
        let out = tempfile::tempdir().unwrap();
        let mut index = build_index(&fixture(), None).unwrap();
        let keys = parse_keys("country,geom_type").unwrap();
        let written = write_dataset(&fixture(), &mut index, out.path(), &keys, 3).unwrap();
        let rel: Vec<String> = written
            .iter()
            .map(|w| {
                w.path
                    .strip_prefix(out.path())
                    .unwrap()
                    .to_string_lossy()
                    .replace('\\', "/")
            })
            .collect();
        assert!(
            rel.contains(&"locations/country=NG/geom_type=polygon/part-0.parquet".to_string()),
            "{rel:?}"
        );
        assert!(
            rel.contains(&"locations/country=NG/geom_type=point/part-0.parquet".to_string()),
            "{rel:?}"
        );
        assert!(
            rel.contains(&"locations/country=unknown/geom_type=point/part-0.parquet".to_string()),
            "{rel:?}"
        );
        assert_eq!(written.iter().map(|w| w.rows).sum::<usize>(), 8);
        assert_eq!(
            index.report.count("point_outside_parent"),
            1,
            "stray is outside nassarawa"
        );
        assert_eq!(index.report.count("small_partition"), 3);
        assert!(!out.path().join(".locations.tmp").exists());
        assert!(!out.path().join(".locations.bak").exists());

        // Second run replaces the dataset and leaves no backup behind.
        let mut index = build_index(&fixture(), None).unwrap();
        write_dataset(&fixture(), &mut index, out.path(), &keys, 3).unwrap();
        assert!(!out.path().join(".locations.bak").exists());
        assert!(out
            .path()
            .join("locations/country=NG/geom_type=polygon/part-0.parquet")
            .exists());
    }

    #[test]
    fn partition_by_tier_groups_non_consecutive_rows() {
        let out = tempfile::tempdir().unwrap();
        let mut index = build_index(&fixture(), None).unwrap();
        let keys = parse_keys("tier").unwrap();
        let written = write_dataset(&fixture(), &mut index, out.path(), &keys, 3).unwrap();
        let mut parts: Vec<&str> = written.iter().map(|w| w.partition.as_str()).collect();
        parts.sort();
        assert_eq!(parts, vec!["tier=0", "tier=1", "tier=2", "tier=site"]);
        assert_eq!(
            written
                .iter()
                .find(|w| w.partition == "tier=2")
                .unwrap()
                .rows,
            2,
            "nassarawa and dup"
        );
    }

    #[test]
    fn empty_index_writes_an_empty_dataset_dir() {
        let out = tempfile::tempdir().unwrap();
        let snapshot = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(
            snapshot.path(),
            b"{\"resourceType\":\"Location\",\"id\":\"x\"}\n",
        )
        .unwrap();
        let mut index = build_index(snapshot.path(), None).unwrap();
        let keys = parse_keys("country,geom_type").unwrap();
        let written = write_dataset(snapshot.path(), &mut index, out.path(), &keys, 3).unwrap();
        assert!(written.is_empty());
        assert!(out.path().join("locations").is_dir());
    }

    #[test]
    fn a_leftover_backup_from_a_crashed_swap_is_recovered() {
        let out = tempfile::tempdir().unwrap();
        // Simulate: live dataset missing, backup present (crash between the two renames).
        let backup = out.path().join(".locations.bak");
        std::fs::create_dir_all(backup.join("country=NG")).unwrap();
        std::fs::write(backup.join("country=NG/marker"), b"old").unwrap();
        let mut index = build_index(&fixture(), None).unwrap();
        let keys = parse_keys("country,geom_type").unwrap();
        write_dataset(&fixture(), &mut index, out.path(), &keys, 3).unwrap();
        assert!(!backup.exists());
        assert!(
            !out.path().join("locations/country=NG/marker").exists(),
            "the recovered old dataset was then replaced by the new one"
        );
        assert!(out
            .path()
            .join("locations/country=NG/geom_type=polygon/part-0.parquet")
            .exists());
    }
}
