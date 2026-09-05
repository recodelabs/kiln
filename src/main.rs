mod cli;
mod error;
mod fhir;
mod geometry;
mod index;
mod inspect;
mod report;
mod transform;
mod write;

use clap::Parser;

fn main() {
    let cli = cli::Cli::parse();
    let result: error::Result<()> = match cli.command {
        cli::Command::Transform(args) => transform::run_transform(&args),
        cli::Command::Inspect(args) => inspect::run_inspect(&args),
    };
    if let Err(err) = result {
        eprintln!("kiln: {err}");
        std::process::exit(err.exit_code());
    }
}
