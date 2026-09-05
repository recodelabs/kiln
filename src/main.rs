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
    // reqwest is built with `rustls-no-provider`, so no crypto provider is
    // installed by default; without this, the first HTTPS request panics.
    rustls::crypto::ring::default_provider()
        .install_default()
        .expect("install rustls ring provider");

    let cli = cli::Cli::parse();
    let result: error::Result<()> = match cli.command {
        cli::Command::Transform(args) => transform::run_transform(&args),
        cli::Command::Inspect(args) => inspect::run_inspect(&args),
        cli::Command::Extract(_) => Err(error::KilnError::Usage(
            "extract: not implemented yet".into(),
        )),
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
