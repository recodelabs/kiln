//! `kiln diff`: edited GeoJSON or GeoParquet -> FHIR NDJSON of changed Locations.

pub mod compare;
pub mod geojson;
pub mod input;

use crate::cli::DiffArgs;
use crate::error::{KilnError, Result};

pub fn run_diff(_args: &DiffArgs) -> Result<()> {
    Err(KilnError::Usage("diff is not implemented yet".into()))
}
