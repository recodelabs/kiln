//! `kiln transform`: pass one, pass two, report.

use std::path::Path;

use crate::cli::TransformArgs;
use crate::error::{KilnError, Result};
use crate::index::build_index;
use crate::index::partition::parse_keys;
use crate::write::dataset::write_dataset;

pub const SNAPSHOT_FILE: &str = "locations.ndjson";

pub fn run_transform(args: &TransformArgs) -> Result<()> {
    let ndjson = args.snapshot.join(SNAPSHOT_FILE);
    if !ndjson.is_file() {
        return Err(KilnError::Usage(format!(
            "snapshot file not found: {}",
            ndjson.display()
        )));
    }
    let keys = parse_keys(&args.partition_by)?;
    if args.row_group_size == 0 {
        return Err(KilnError::Usage(
            "--row-group-size must be at least 1".into(),
        ));
    }

    let mut index = build_index(&ndjson, args.country.as_deref())?;
    eprintln!(
        "indexed {} resources, {} with geometry and hierarchy",
        index.read,
        index.order.len()
    );

    // A stale report must never describe a dataset it does not match:
    // remove it before writing, rewrite it only on success.
    let report_path = args.out.join("_report.json");
    if report_path.exists() {
        std::fs::remove_file(&report_path).map_err(|e| KilnError::io(&report_path, e))?;
    }

    let written = write_dataset(&ndjson, &mut index, &args.out, &keys, args.row_group_size)?;

    write_report(&report_path, &index.report)?;
    let rows: usize = written.iter().map(|w| w.rows).sum();
    println!(
        "Wrote {rows} rows across {} partitions to {}",
        written.len(),
        args.out.display()
    );
    println!("{}", index.report.summary());
    Ok(())
}

pub fn write_report(path: &Path, report: &crate::report::Report) -> Result<()> {
    let text = serde_json::to_string_pretty(&report.to_json())?;
    std::fs::write(path, text).map_err(|e| KilnError::io(path, e))
}
