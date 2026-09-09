//! `kiln population`: population denominators for admin units from a raster.
//!
//! Reads the snapshot with the same pass-one index `transform` uses, so the
//! hierarchy (and its report) come for free; sums the raster under every
//! admin unit at `--level` with the pixel-centroid rule; rolls the sums up
//! the partOf chain so every admin ancestor gets a calculated total; and
//! writes one `ICRTargetPopulation` Group per unit as FHIR NDJSON for
//! `kiln load`. Group ids are deterministic (`pop-SOURCE-YEAR-LOCATIONID`),
//! so re-running replaces the same resources. Locations are never touched.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::time::Duration;

use crate::cli::PopulationArgs;
use crate::error::{KilnError, Result};
use crate::fhir::group::{group_id, target_population_group, TargetPopulation, MAX_ID_LEN};
use crate::fhir::ndjson::LineAccess;
use crate::fhir::{Boundary, Location};
use crate::geometry::parse_boundary;
use crate::index::hierarchy::Hierarchy;
use crate::index::{build_index, IndexRecord};
use crate::raster::fetch;
use crate::raster::geotiff::GeoTiff;
use crate::raster::zonal::{raster_total, zonal_sum, ZonalSum};
use crate::report::Report;
use crate::snapshot::Snapshot;
use crate::transform::write_report;

/// One unit's figure: measured from the raster at `--level`, or rolled up
/// from measured descendants.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct UnitTotal {
    pub sum: f64,
    pub pixels: u64,
    /// Rolled up from children rather than measured directly.
    pub calculated: bool,
    /// A measured descendant had no usable boundary, so this under-counts.
    pub incomplete: bool,
}

/// Admin-unit ancestors of record `i`, nearest first. Non-admin nodes on
/// the chain (a facility's supervisory area, say) are skipped, not stopped at.
fn admin_ancestors(hierarchy: &Hierarchy, i: usize) -> Vec<usize> {
    let mut out = Vec::new();
    let mut cur = hierarchy.get(i).and_then(|info| info.parent);
    while let Some(n) = cur {
        let idx = n.index();
        let info = hierarchy.get(idx);
        if info.and_then(|f| f.admin_level).is_some() {
            out.push(idx);
        }
        cur = info.and_then(|f| f.parent);
    }
    out
}

/// Fold one measured unit (or a unit that could not be measured) into every
/// admin ancestor's rolled-up total.
pub fn roll_up(
    totals: &mut BTreeMap<usize, UnitTotal>,
    hierarchy: &Hierarchy,
    i: usize,
    measured: Option<ZonalSum>,
) {
    for anc in admin_ancestors(hierarchy, i) {
        let t = totals.entry(anc).or_insert(UnitTotal {
            calculated: true,
            ..UnitTotal::default()
        });
        match measured {
            Some(z) => {
                t.sum += z.sum;
                t.pixels += z.pixels;
            }
            None => t.incomplete = true,
        }
    }
}

/// The unit's boundary summed against the raster; `None` (after reporting)
/// when there is nothing usable to measure. Parsing here re-reads the line
/// pass one already indexed, so parse noise goes to a scratch report and
/// only this command's own kinds reach `report`.
fn measure(
    lines: &mut LineAccess,
    rec: &IndexRecord,
    raster: &mut GeoTiff,
    report: &mut Report,
) -> Result<Option<ZonalSum>> {
    let text = lines.read_at(rec.offset, rec.len)?;
    let value: serde_json::Value = serde_json::from_str(&text)?;
    let mut scratch = Report::default();
    let Some(loc) = Location::parse(&value, &mut scratch) else {
        return Ok(None);
    };
    let geom = match &loc.boundary {
        Some(Boundary::Inline(bytes)) => {
            let g = parse_boundary(bytes, &loc.id, &mut scratch);
            if g.is_none() {
                report.add(
                    "no_boundary",
                    &loc.id,
                    "boundary did not parse; no population measured",
                );
            }
            g
        }
        Some(Boundary::Url(url)) => {
            report.add(
                "no_boundary",
                &loc.id,
                &format!("boundary is an unresolved url ({url}); run extract to inline it"),
            );
            None
        }
        None => {
            report.add(
                "no_boundary",
                &loc.id,
                "admin unit has no boundary; no population measured",
            );
            None
        }
    };
    let Some(geom) = geom else {
        return Ok(None);
    };
    match zonal_sum(raster, &geom) {
        Ok(z) => Ok(Some(z)),
        Err(KilnError::Usage(msg)) => {
            report.add(
                "no_boundary",
                &loc.id,
                &format!("{msg}; no population measured"),
            );
            Ok(None)
        }
        Err(e) => Err(e),
    }
}

fn valid_source_code(code: &str) -> bool {
    !code.is_empty() && code.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
}

pub fn run_population(args: &PopulationArgs) -> Result<()> {
    let snapshot = Snapshot::new(&args.snapshot);
    let ndjson = snapshot.locations();
    if !ndjson.is_file() {
        return Err(KilnError::Usage(format!(
            "snapshot file not found: {}",
            ndjson.display()
        )));
    }
    if args.out.is_dir() {
        return Err(KilnError::Usage(format!(
            "--out {}: is a directory",
            args.out.display()
        )));
    }
    if !valid_source_code(&args.source) {
        return Err(KilnError::Usage(format!(
            "--source must be a code of letters, digits and hyphens, got {:?}",
            args.source
        )));
    }

    let index = build_index(&ndjson, None)?;
    let mut report = index.report;
    let level = i8::try_from(args.level)
        .map_err(|_| KilnError::Usage(format!("--level {} is out of range", args.level)))?;
    let targets: Vec<usize> = (0..index.records.len())
        .filter(|&i| index.hierarchy.get(i).and_then(|f| f.admin_level) == Some(level))
        .collect();
    if targets.is_empty() {
        return Err(KilnError::Usage(format!(
            "no admin units at level {} in {}",
            args.level,
            ndjson.display()
        )));
    }

    let cache_dir = args.cache_dir.clone().unwrap_or_else(|| snapshot.rasters());
    if fetch::is_url(&args.raster) {
        eprintln!("raster: fetching {}", args.raster);
    }
    let loaded = fetch::load(
        &args.raster,
        &cache_dir,
        args.retries,
        Duration::from_secs(args.timeout),
    )?;
    for e in &loaded.cache_errors {
        report.add("cache_error", &args.raster, e);
    }
    println!(
        "Raster {} ({:.1} MB{})",
        loaded.label,
        loaded.bytes.len() as f64 / 1e6,
        if loaded.cached { ", from cache" } else { "" }
    );
    let mut raster = GeoTiff::open(loaded.bytes, &loaded.label)?;
    let grand = raster_total(&mut raster)?;

    let mut lines = LineAccess::open(&ndjson)?;
    let mut totals: BTreeMap<usize, UnitTotal> = BTreeMap::new();
    for &i in &targets {
        let measured = measure(&mut lines, &index.records[i], &mut raster, &mut report)?;
        if let Some(z) = measured {
            totals.insert(
                i,
                UnitTotal {
                    sum: z.sum,
                    pixels: z.pixels,
                    calculated: false,
                    incomplete: false,
                },
            );
        }
        roll_up(&mut totals, &index.hierarchy, i, measured);
    }

    let source_text = format!(
        "{}; pixel-centroid zonal sum over admin level {}",
        loaded.label, args.level
    );
    let tmp = args.out.with_extension("ndjson.tmp");
    let file = File::create(&tmp).map_err(|e| KilnError::io(&tmp, e))?;
    let mut out = BufWriter::new(file);
    let (mut measured_n, mut rolled_n) = (0usize, 0usize);
    for (&i, t) in &totals {
        let rec = &index.records[i];
        if t.pixels == 0 {
            report.add(
                if t.calculated {
                    "rollup_empty"
                } else {
                    "no_pixels"
                },
                &rec.id,
                "no raster pixel with data has its centre inside; no Group written",
            );
            continue;
        }
        if t.incomplete {
            report.add(
                "rollup_incomplete",
                &rec.id,
                "a descendant at the measured level had no usable boundary; this total under-counts",
            );
        }
        let id = group_id(&args.source, args.year, &rec.id);
        if id.len() > MAX_ID_LEN {
            report.add(
                "group_id_too_long",
                &rec.id,
                &format!("{id} exceeds {MAX_ID_LEN} characters; no Group written"),
            );
            continue;
        }
        let group = target_population_group(&TargetPopulation {
            location_id: &rec.id,
            location_name: rec.name.as_deref(),
            count: t.sum.round() as u64,
            year: args.year,
            source_code: &args.source,
            source_text: &source_text,
            calculated: t.calculated,
            planning_denominator: args.planning_denominator,
        });
        serde_json::to_writer(&mut out, &group)?;
        out.write_all(b"\n").map_err(|e| KilnError::io(&tmp, e))?;
        if t.calculated {
            rolled_n += 1;
        } else {
            measured_n += 1;
        }
    }
    out.flush().map_err(|e| KilnError::io(&tmp, e))?;
    drop(out);
    std::fs::rename(&tmp, &args.out).map_err(|e| KilnError::io(&args.out, e))?;

    let assigned: f64 = totals
        .values()
        .filter(|t| !t.calculated)
        .map(|t| t.sum)
        .sum();
    let share = if grand.sum > 0.0 {
        100.0 * assigned / grand.sum
    } else {
        0.0
    };
    println!(
        "Wrote {} Groups to {} ({measured_n} measured at level {}, {rolled_n} rolled up)",
        measured_n + rolled_n,
        args.out.display(),
        args.level
    );
    println!(
        "Raster total {:.0}; assigned to level-{} units {:.0} ({share:.1}%)",
        grand.sum, args.level, assigned
    );
    println!("{}", report.summary());
    if let Some(path) = &args.report {
        write_report(path, &report)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::hierarchy::resolve_hierarchy;

    fn admin(id: &str, part_of: Option<&str>) -> IndexRecord {
        IndexRecord {
            id: id.to_string(),
            part_of: part_of.map(str::to_string),
            type_code: Some("admin-unit".to_string()),
            ..IndexRecord::default()
        }
    }

    #[test]
    fn roll_up_sums_into_every_admin_ancestor_and_flags_gaps() {
        // ng ← kano ← (lga-a, lga-b); ng ← lagos ← lga-c ; clinic (not admin) under lga-a
        let records = vec![
            admin("ng", None),
            admin("kano", Some("ng")),
            admin("lga-a", Some("kano")),
            admin("lga-b", Some("kano")),
            admin("lagos", Some("ng")),
            admin("lga-c", Some("lagos")),
            IndexRecord {
                id: "clinic".into(),
                part_of: Some("lga-a".into()),
                type_code: Some("facility".into()),
                ..IndexRecord::default()
            },
        ];
        let mut report = Report::default();
        let hierarchy = resolve_hierarchy(&records, &mut report);
        assert_eq!(hierarchy.get(2).unwrap().admin_level, Some(2));
        assert_eq!(hierarchy.get(6).unwrap().admin_level, None);

        let mut totals = BTreeMap::new();
        let z = |sum: f64, pixels: u64| Some(ZonalSum { sum, pixels });
        roll_up(&mut totals, &hierarchy, 2, z(10.0, 1));
        roll_up(&mut totals, &hierarchy, 3, z(20.0, 2));
        roll_up(&mut totals, &hierarchy, 5, None);

        let kano = totals[&1];
        assert_eq!(
            (kano.sum, kano.pixels, kano.calculated, kano.incomplete),
            (30.0, 3, true, false)
        );
        let lagos = totals[&4];
        assert_eq!(
            (lagos.sum, lagos.pixels, lagos.calculated, lagos.incomplete),
            (0.0, 0, true, true)
        );
        let ng = totals[&0];
        assert_eq!(
            (ng.sum, ng.pixels, ng.calculated, ng.incomplete),
            (30.0, 3, true, true)
        );
        assert!(
            !totals.contains_key(&2),
            "measured units are the caller's to insert"
        );
        assert!(!totals.contains_key(&6));
    }

    #[test]
    fn source_codes_are_restricted_to_id_safe_characters() {
        assert!(
            valid_source_code("worldpop")
                && valid_source_code("grid3")
                && valid_source_code("census-projection")
        );
        assert!(
            !valid_source_code("") && !valid_source_code("world pop") && !valid_source_code("a/b")
        );
    }
}
