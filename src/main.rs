mod cli;
mod error;
mod extract;
mod fhir;
mod geometry;
mod index;
mod inspect;
mod report;
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
        cli::Command::Run(args) => {
            let _ = args.transform_args();
            Err(error::KilnError::Usage("run: not implemented yet".into()))
        }
    };
    if let Err(err) = result {
        eprintln!("kiln: {err}");
        std::process::exit(err.exit_code());
    }
}
