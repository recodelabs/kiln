use std::path::PathBuf;

use clap::{Parser, Subcommand};

pub const DEFAULT_ROW_GROUP_SIZE: usize = 20_000;
pub const DEFAULT_PARTITION_BY: &str = "country,geom_type";
pub const DEFAULT_CONCURRENCY: usize = 8;
pub const DEFAULT_RETRIES: usize = 3;
pub const DEFAULT_MAX_CONSECUTIVE_FAILURES: usize = 50;

#[derive(Parser, Debug)]
#[command(
    name = "kiln",
    version,
    about = "Bridge between a FHIR Location registry and GeoParquet"
)]
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
    /// Fetch Locations from a FHIR server into a snapshot (incremental)
    Extract(ExtractArgs),
    /// Extract then transform
    Run(RunArgs),
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

#[derive(clap::Args, Debug, Clone)]
pub struct ExtractArgs {
    /// Base URL of the FHIR server (Location is appended)
    #[arg(long)]
    pub server: String,
    /// Bearer token; falls back to $KILN_TOKEN
    #[arg(long, env = "KILN_TOKEN", hide_env_values = true)]
    pub token: Option<String>,
    /// Snapshot directory to create or update
    #[arg(long)]
    pub snapshot: PathBuf,
    /// Ignore the existing snapshot and watermark; fetch everything
    #[arg(long)]
    pub full: bool,
    /// Use this instant instead of the stored watermark for this run
    #[arg(long)]
    pub since: Option<String>,
    /// Boundary fetch worker threads
    #[arg(long, default_value_t = DEFAULT_CONCURRENCY)]
    pub concurrency: usize,
    /// Attempts per request
    #[arg(long, default_value_t = DEFAULT_RETRIES)]
    pub retries: usize,
    /// Abort after this many consecutive boundary failures with no success yet; 0 disables
    #[arg(long, default_value_t = DEFAULT_MAX_CONSECUTIVE_FAILURES)]
    pub max_consecutive_failures: usize,
    /// Neither read nor write the boundary cache (holds fetched boundaries in memory)
    #[arg(long)]
    pub no_cache: bool,
    /// Skip cache reads but still write fetched boundaries to it
    #[arg(long)]
    pub refresh: bool,
    /// Boundary cache directory (default: SNAPSHOT/boundaries)
    #[arg(long)]
    pub cache_dir: Option<PathBuf>,
}

#[derive(clap::Args, Debug, Clone)]
pub struct RunArgs {
    #[command(flatten)]
    pub extract: ExtractArgs,
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

impl RunArgs {
    pub fn transform_args(&self) -> TransformArgs {
        TransformArgs {
            snapshot: self.extract.snapshot.clone(),
            out: self.out.clone(),
            country: self.country.clone(),
            row_group_size: self.row_group_size,
            partition_by: self.partition_by.clone(),
        }
    }
}
