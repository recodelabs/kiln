mod cli;
mod error;
mod report;

use clap::Parser;

fn main() {
    let cli = cli::Cli::parse();
    let result: error::Result<()> = match cli.command {
        cli::Command::Transform(_) => Err(error::KilnError::Usage(
            "transform: not implemented yet".into(),
        )),
        cli::Command::Inspect(_) => Err(error::KilnError::Usage(
            "inspect: not implemented yet".into(),
        )),
    };
    if let Err(err) = result {
        eprintln!("kiln: {err}");
        std::process::exit(err.exit_code());
    }
}
