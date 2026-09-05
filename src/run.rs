//! `kiln run`: extract then transform, with the snapshot as the handoff.

use crate::cli::RunArgs;
use crate::error::Result;
use crate::extract::run_extract;
use crate::transform::run_transform;

pub fn run(args: &RunArgs) -> Result<()> {
    run_extract(&args.extract)?;
    run_transform(&args.transform_args())
}
