mod cli;
mod error;
mod fhir;
mod geometry;
mod index;
mod report;
mod transform;
mod write;

use clap::Parser;

fn main() {
    let cli = cli::Cli::parse();
    let result: error::Result<()> = match cli.command {
        cli::Command::Transform(args) => transform::run_transform(&args),
        cli::Command::Inspect(_) => Err(error::KilnError::Usage(
            "inspect: not implemented yet".into(),
        )),
    };
    if let Err(err) = result {
        eprintln!("kiln: {err}");
        std::process::exit(err.exit_code());
    }
}
