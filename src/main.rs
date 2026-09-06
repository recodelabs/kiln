mod cells;
mod cli;
mod diff;
mod error;
mod extract;
mod fhir;
mod geometry;
mod index;
mod inspect;
mod load;
mod report;
mod run;
mod snapshot;
mod transform;
mod write;

use clap::Parser;

fn main() {
    let cli = cli::Cli::parse();
    let result: error::Result<()> = match cli.command {
        cli::Command::Transform(args) => transform::run_transform(&args),
        cli::Command::Inspect(args) => inspect::run_inspect(&args),
        cli::Command::Extract(args) => extract::run_extract(&args),
        cli::Command::Run(args) => run::run(&args),
        cli::Command::Diff(args) => diff::run_diff(&args),
        cli::Command::Load(args) => load::run_load(&args),
        cli::Command::Index(args) => cells::run_index(&args),
    };
    if let Err(err) = result {
        eprintln!("kiln: {err}");
        std::process::exit(err.exit_code());
    }
}
