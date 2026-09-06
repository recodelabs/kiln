//! `kiln diff`: edited GeoJSON or GeoParquet -> FHIR NDJSON of the Locations
//! that changed. Offline. The snapshot is indexed by id and byte offset;
//! the input is streamed; each named resource is re-read, rebuilt from the
//! writable columns, and emitted only when its canonical JSON differs.

pub mod compare;
pub mod geojson;
pub mod input;
pub mod parquet;
pub mod rebuild;

use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::cli::DiffArgs;
use crate::diff::compare::canonical;
use crate::diff::geojson::{feature_to_row, read_feature_collection, read_feature_lines};
use crate::diff::input::InputRow;
use crate::diff::parquet::read_geoparquet;
use crate::diff::rebuild::rebuild;
use crate::error::{KilnError, Result};
use crate::fhir::ndjson::{LineAccess, NdjsonReader};
use crate::fhir::Location;
use crate::report::Report;
use crate::transform::{write_report, SNAPSHOT_FILE};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputFormat {
    FeatureCollection,
    FeatureLines,
    GeoParquet,
}

impl InputFormat {
    pub fn from_path(path: &Path) -> Result<Self> {
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| e.to_ascii_lowercase())
            .unwrap_or_default();
        match ext.as_str() {
            "geojson" | "json" => Ok(Self::FeatureCollection),
            "geojsonl" | "geojsons" => Ok(Self::FeatureLines),
            "parquet" => Ok(Self::GeoParquet),
            _ => Err(KilnError::Usage(format!(
                "--in {}: unknown extension; expected .geojson, .json, .geojsonl, .geojsons or .parquet",
                path.display()
            ))),
        }
    }
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct DiffStats {
    pub changed: usize,
    pub unchanged: usize,
    pub created: usize,
}

/// `id -> (offset, len)` for every resource in the snapshot. Nothing else
/// is retained; a resource is re-read only when an input row names it.
pub fn index_snapshot(path: &Path, report: &mut Report) -> Result<HashMap<String, (u64, usize)>> {
    let mut index = HashMap::new();
    for line in NdjsonReader::open(path)? {
        let line = line?;
        let id = serde_json::from_str::<Value>(&line.text)
            .ok()
            .and_then(|v| v.get("id")?.as_str().map(str::to_string));
        match id {
            Some(id) => {
                if index.contains_key(&id) {
                    report.add(
                        "duplicate_id",
                        &id,
                        &format!("snapshot line {} repeats an earlier id; first kept", line.number),
                    );
                } else {
                    index.insert(id, (line.offset, line.len));
                }
            }
            None => report.add(
                "snapshot_line_unparsed",
                "<unknown>",
                &format!("snapshot line {} is not a resource with an id", line.number),
            ),
        }
    }
    Ok(index)
}

struct Diff<'a> {
    index: HashMap<String, (u64, usize)>,
    access: LineAccess,
    out: BufWriter<File>,
    out_path: &'a Path,
    seen: HashSet<String>,
    stats: DiffStats,
}

impl Diff<'_> {
    fn process(&mut self, row: InputRow, report: &mut Report) -> Result<()> {
        let (id, generated) = match row.id.clone() {
            Some(id) => (id, false),
            None => (uuid::Uuid::new_v4().to_string(), true),
        };
        if !self.seen.insert(id.clone()) {
            report.add(
                "duplicate_id",
                &id,
                &format!("input row {} repeats an earlier row; skipped", row.line),
            );
            return Ok(());
        }
        let existing = self.index.get(&id).copied();
        let (base, snapshot) = match existing {
            Some((offset, len)) => {
                let text = self.access.read_at(offset, len)?;
                let base: Value = serde_json::from_str(&text)?;
                let loc = Location::parse(&base, &mut Report::default());
                (base, loc)
            }
            None => {
                if generated {
                    report.add(
                        "new_location_generated_id",
                        &id,
                        &format!("input row {} has no id; assigned {id}", row.line),
                    );
                } else {
                    report.add(
                        "new_location",
                        &id,
                        &format!(
                            "input row {}: id not in the snapshot; emitted as a create",
                            row.line
                        ),
                    );
                }
                (json!({"resourceType": "Location", "id": id}), None)
            }
        };
        let rebuilt = rebuild(&base, snapshot.as_ref(), &row, report);
        if existing.is_some() && canonical(&rebuilt) == canonical(&base) {
            self.stats.unchanged += 1;
            return Ok(());
        }
        writeln!(self.out, "{rebuilt}").map_err(|e| KilnError::io(self.out_path, e))?;
        if existing.is_some() {
            self.stats.changed += 1;
        } else {
            self.stats.created += 1;
        }
        Ok(())
    }
}

fn read_input(
    format: InputFormat,
    input: &Path,
    diff: &mut Diff,
    report: &mut Report,
) -> Result<()> {
    match format {
        InputFormat::FeatureCollection => read_feature_collection(input, |f, i| {
            let row = feature_to_row(f, i, report);
            diff.process(row, report)
        }),
        InputFormat::FeatureLines => read_feature_lines(input, |f, i| {
            let row = feature_to_row(f, i, report);
            diff.process(row, report)
        }),
        InputFormat::GeoParquet => {
            read_geoparquet(input, report, |row, report| diff.process(row, report))
        }
    }
}

fn tmp_path(out: &Path) -> PathBuf {
    let mut s = out.as_os_str().to_owned();
    s.push(".tmp");
    PathBuf::from(s)
}

/// Flush, fsync and rename the finished changes file into place.
fn finish(out: BufWriter<File>, tmp: &Path, dest: &Path) -> Result<()> {
    let mut out = out;
    out.flush().map_err(|e| KilnError::io(tmp, e))?;
    let file = out
        .into_inner()
        .map_err(|e| KilnError::io(tmp, e.into_error()))?;
    file.sync_all().map_err(|e| KilnError::io(tmp, e))?;
    std::fs::rename(tmp, dest).map_err(|e| KilnError::io(dest, e))
}

pub fn run_diff(args: &DiffArgs) -> Result<()> {
    let ndjson = args.snapshot.join(SNAPSHOT_FILE);
    if !ndjson.is_file() {
        return Err(KilnError::Usage(format!(
            "snapshot file not found: {}",
            ndjson.display()
        )));
    }
    let format = InputFormat::from_path(&args.input)?;
    if !args.input.is_file() {
        return Err(KilnError::Usage(format!(
            "--in {}: not a file",
            args.input.display()
        )));
    }
    if args.out.is_dir() {
        return Err(KilnError::Usage(format!(
            "--out {}: is a directory",
            args.out.display()
        )));
    }

    let mut report = Report::default();
    let index = index_snapshot(&ndjson, &mut report)?;
    eprintln!("indexed {} snapshot resources", index.len());

    let tmp = tmp_path(&args.out);
    let file = File::create(&tmp)
        .map_err(|e| KilnError::Usage(format!("--out {}: {e}", tmp.display())))?;
    let mut diff = Diff {
        index,
        access: LineAccess::open(&ndjson)?,
        out: BufWriter::new(file),
        out_path: &tmp,
        seen: HashSet::new(),
        stats: DiffStats::default(),
    };
    let read = read_input(format, &args.input, &mut diff, &mut report);
    let Diff { out, stats, .. } = diff;
    if let Err(e) = read.and_then(|()| finish(out, &tmp, &args.out)) {
        let _ = std::fs::remove_file(&tmp);
        return Err(e);
    }

    println!(
        "{} changed, {} unchanged, {} new -> {}",
        stats.changed,
        stats.unchanged,
        stats.created,
        args.out.display()
    );
    if let Some(path) = &args.report {
        write_report(path, &report)?;
    }
    println!("{}", report.summary());
    Ok(())
}
