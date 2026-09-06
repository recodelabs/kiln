//! `kiln load`: FHIR NDJSON -> transaction bundles with If-Match.

pub mod bundle;
pub mod capability;
pub mod order;

use crate::cli::LoadArgs;
use crate::error::{KilnError, Result};

pub fn run_load(_args: &LoadArgs) -> Result<()> {
    Err(KilnError::Usage("load is not implemented yet".into()))
}
