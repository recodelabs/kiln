use std::path::PathBuf;

use clap::{Parser, Subcommand};

pub const DEFAULT_ROW_GROUP_SIZE: usize = 20_000;
pub const DEFAULT_PARTITION_BY: &str = "country,geom_type";

#[derive(Parser, Debug)]
#[command(name = "kiln", version, about = "Bridge between a FHIR Location registry and GeoParquet")]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Subcommand, Debug)]
pub enum Command {
    /// Convert a snapshot into a partitioned GeoParquet dataset (offline)
    Transform(TransformArgs),
    /// Summarise a written dataset
    Inspect(InspectArgs),
}

#[derive(clap::Args, Debug, Clone)]
pub struct TransformArgs {
    /// Snapshot directory containing locations.ndjson
    #[arg(long)]
    pub snapshot: PathBuf,
    /// Output directory; the dataset is written to OUT/locations
    #[arg(long)]
    pub out: PathBuf,
    /// Override the country code derived from the level-0 admin unit
    #[arg(long)]
    pub country: Option<String>,
    /// Rows per Parquet row group
    #[arg(long, default_value_t = DEFAULT_ROW_GROUP_SIZE)]
    pub row_group_size: usize,
    /// Comma-separated partition keys: any of country, geom_type, tier, type
    #[arg(long, default_value = DEFAULT_PARTITION_BY)]
    pub partition_by: String,
}

#[derive(clap::Args, Debug, Clone)]
pub struct InspectArgs {
    /// Output directory previously written by transform
    #[arg(long)]
    pub out: PathBuf,
}
