//! `kiln load`: FHIR NDJSON -> transaction bundles with If-Match.

use crate::cli::LoadArgs;
use crate::error::{KilnError, Result};

pub fn run_load(_args: &LoadArgs) -> Result<()> {
    Err(KilnError::Usage("load is not implemented yet".into()))
}
