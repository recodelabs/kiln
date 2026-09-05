//! Pass one: stream the snapshot, keep an IndexRecord per Location, resolve
//! the hierarchy, choose country and tier, and produce the output order
//! (retained records sorted by partition then Hilbert key).

use std::collections::HashMap;
use std::path::Path;

use crate::fhir::ndjson::NdjsonReader;
use crate::fhir::{Boundary, Location};
use crate::geometry;
use crate::index::hierarchy::{resolve_hierarchy, Hierarchy};
use crate::index::hilbert::hilbert_key;
use crate::index::IndexRecord;
use crate::report::Report;

pub const UNKNOWN_COUNTRY: &str = "unknown";

pub struct Index {
    /// Every parsed resource, in file order. The hierarchy is indexed by position here.
    pub records: Vec<IndexRecord>,
    pub hierarchy: Hierarchy,
    /// Indices into `records` of the rows that will be written, in output order.
    pub order: Vec<u32>,
    pub report: Report,
    /// Resources read from the file, including ones that failed to parse.
    pub read: usize,
}

pub fn build_index(ndjson: &Path, country_override: Option<&str>) -> crate::error::Result<Index> {
    let mut report = Report::default();
    let mut records: Vec<IndexRecord> = Vec::new();
    let mut read = 0usize;

    for line in NdjsonReader::open(ndjson)? {
        let line = line?;
        read += 1;
        let value: serde_json::Value = match serde_json::from_str(&line.text) {
            Ok(v) => v,
            Err(err) => {
                report.add(
                    "malformed_field",
                    &format!("line {}", line.number),
                    &err.to_string(),
                );
                continue;
            }
        };
        let Some(loc) = Location::parse(&value, &mut report) else {
            continue;
        };
        if let Some(Boundary::Url(url)) = &loc.boundary {
            report.add(
                "boundary_unresolved_url",
                &loc.id,
                &format!("boundary is a url reference ({url}); run kiln extract to inline it"),
            );
        }
        let geometry = geometry::summarize(&loc, &mut report);
        records.push(IndexRecord {
            id: loc.id,
            part_of: loc.part_of,
            name: loc.name,
            pcode: loc.pcode,
            type_code: loc.type_code,
            offset: line.offset,
            len: line.len,
            geometry,
            position: loc.position,
            ..Default::default()
        });
    }

    // Resolved over every parsed record, including ones with no geometry:
    // a geometry-less district is still somebody's parent.
    let hierarchy = resolve_hierarchy(&records, &mut report);

    let retained: Vec<u32> = (0..records.len())
        .filter(|&i| hierarchy.get(i).is_some() && records[i].geometry.is_some())
        .map(|i| i as u32)
        .collect();

    let mut extent = [f64::MAX, f64::MAX, f64::MIN, f64::MIN];
    for &i in &retained {
        let b = records[i as usize].geometry.unwrap().bbox;
        extent = [
            extent[0].min(b[0]),
            extent[1].min(b[1]),
            extent[2].max(b[2]),
            extent[3].max(b[3]),
        ];
    }

    let mut by_pcode: HashMap<String, Vec<String>> = HashMap::new();
    for &i in &retained {
        let i = i as usize;
        let country = match (country_override, hierarchy.country(&records, i)) {
            (Some(c), _) => c.to_string(),
            (None, Some(c)) => c,
            (None, None) => {
                report.add(
                    "no_country",
                    &records[i].id,
                    "no admin-unit ancestor carries a pcode; filed under 'unknown'",
                );
                UNKNOWN_COUNTRY.to_string()
            }
        };
        let tier = match hierarchy.get(i).and_then(|info| info.admin_level) {
            Some(level) => level.to_string(),
            None => "site".to_string(),
        };
        let b = records[i].geometry.unwrap().bbox;
        let hilbert = hilbert_key([(b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0], extent);
        let rec = &mut records[i];
        rec.country = country;
        rec.tier = tier;
        rec.hilbert = hilbert;
        if let Some(p) = &rec.pcode {
            by_pcode.entry(p.clone()).or_default().push(rec.id.clone());
        }
    }

    let mut dups: Vec<(&String, &Vec<String>)> =
        by_pcode.iter().filter(|(_, ids)| ids.len() > 1).collect();
    dups.sort();
    for (pcode, ids) in dups {
        let mut ids = ids.clone();
        ids.sort();
        report.add(
            "duplicate_pcode",
            &ids.join(", "),
            &format!("pcode {pcode} claimed by {} Locations", ids.len()),
        );
    }

    let mut order = retained;
    order.sort_by(|&a, &b| {
        let ra = &records[a as usize];
        let rb = &records[b as usize];
        (
            ra.country.as_str(),
            ra.geometry.unwrap().kind.as_str(),
            ra.hilbert,
        )
            .cmp(&(
                rb.country.as_str(),
                rb.geometry.unwrap().kind.as_str(),
                rb.hilbert,
            ))
    });

    Ok(Index {
        records,
        hierarchy,
        order,
        report,
        read,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn fixture() -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/snapshot/locations.ndjson")
    }

    fn idx(index: &Index, id: &str) -> usize {
        index
            .records
            .iter()
            .position(|r| r.id == id)
            .unwrap_or_else(|| panic!("no record {id}"))
    }

    #[test]
    fn indexes_the_fixture_snapshot() {
        let index = build_index(&fixture(), None).unwrap();
        assert_eq!(index.read, 12);
        assert_eq!(
            index.records.len(),
            12,
            "every parsed resource stays addressable"
        );
        let retained: Vec<&str> = index
            .order
            .iter()
            .map(|&i| index.records[i as usize].id.as_str())
            .collect();
        // dropped: remote (url boundary, no position), ghost (no geometry), cyc-a, cyc-b (cycle)
        assert_eq!(retained.len(), 8, "{retained:?}");
        assert!(!retained.contains(&"ghost"));
        assert!(!retained.contains(&"remote"));
        assert!(!retained.contains(&"cyc-a"));
        let clinic = idx(&index, "clinic");
        assert_eq!(index.records[clinic].country, "NG");
        assert_eq!(index.records[clinic].tier, "site");
        assert_eq!(
            index.hierarchy.admin_names(&index.records, clinic)[1].as_deref(),
            Some("Kano")
        );
        let orphan = idx(&index, "orphan");
        assert_eq!(index.records[orphan].country, "unknown");
        assert_eq!(index.report.count("no_country"), 1);
        assert_eq!(index.report.count("boundary_unresolved_url"), 1);
        assert_eq!(index.report.count("duplicate_pcode"), 1);
        assert_eq!(index.report.count("cycle"), 2);
        assert_eq!(index.report.count("orphan"), 1);
        assert_eq!(index.report.count("no_geometry"), 2, "ghost and remote");
        // Sorted by partition then Hilbert: (country, geom kind) is non-decreasing along `order`.
        let keys: Vec<(String, &str)> = index
            .order
            .iter()
            .map(|&i| {
                let r = &index.records[i as usize];
                (r.country.clone(), r.geometry.unwrap().kind.as_str())
            })
            .collect();
        let mut sorted = keys.clone();
        sorted.sort();
        assert_eq!(keys, sorted);
        assert!(index
            .order
            .iter()
            .all(|&i| index.records[i as usize].len > 0));
    }

    #[test]
    fn country_override_wins() {
        let index = build_index(&fixture(), Some("XX")).unwrap();
        assert!(index
            .order
            .iter()
            .all(|&i| index.records[i as usize].country == "XX"));
        assert_eq!(index.report.count("no_country"), 0);
    }

    #[test]
    fn empty_snapshot_yields_empty_order() {
        let f = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(f.path(), b"{\"resourceType\":\"Location\",\"id\":\"x\"}\n").unwrap();
        let index = build_index(f.path(), None).unwrap();
        assert_eq!(index.records.len(), 1);
        assert!(index.order.is_empty());
        assert_eq!(index.report.count("no_geometry"), 1);
    }
}
