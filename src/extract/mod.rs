//! `kiln extract`: page, fetch, merge. See the spec for the phase contracts.

pub mod boundary;
pub mod cache;
pub mod client;
pub mod page;

use std::collections::HashMap;
use std::time::Duration;

use crate::cli::ExtractArgs;
use crate::error::{KilnError, Result};
use crate::extract::boundary::{fetch_boundaries, FetchOptions};
use crate::extract::cache::Cache;
use crate::extract::client::FhirClient;
use crate::extract::page::page_locations;
use crate::report::Report;
use crate::snapshot::instant::{format_utc, parse_instant};
use crate::snapshot::merge::merge;
use crate::snapshot::{same_server, Snapshot, State};
use crate::transform::write_report;

/// Fetch Locations into the snapshot, incrementally unless `--full`.
///
/// Nothing before the merge touches `locations.ndjson` or `state.json`, so a
/// failure in the page or fetch phase leaves the previous snapshot intact.
pub fn run_extract(args: &ExtractArgs) -> Result<()> {
    if args.no_cache && (args.refresh || args.cache_dir.is_some()) {
        return Err(KilnError::Usage(
            "--no-cache cannot be combined with --refresh or --cache-dir".into(),
        ));
    }
    if args.concurrency == 0 {
        return Err(KilnError::Usage("--concurrency must be at least 1".into()));
    }
    if let Some(s) = &args.since {
        if parse_instant(s).is_none() {
            return Err(KilnError::Usage(format!(
                "--since {s}: not a FHIR instant (e.g. 2026-01-02T03:04:05Z)"
            )));
        }
    }
    let snap = Snapshot::new(&args.snapshot);
    std::fs::create_dir_all(&snap.dir).map_err(|e| KilnError::io(&snap.dir, e))?;

    // Mode: full, or incremental from --since or the stored watermark. The
    // snapshot belongs to one server: any incremental run against a
    // different one -- including one pinned with --since -- would silently
    // mix two registries, so it is a usage error. --full repoints.
    let state = State::read(&snap.state_path())?;
    if !args.full {
        if let Some(st) = &state {
            if !same_server(&st.server, &args.server) {
                return Err(KilnError::Usage(format!(
                    "snapshot was extracted from {} but --server is {}; pass --full to repoint it",
                    st.server, args.server
                )));
            }
        }
    }
    let since: Option<String> = if args.full {
        None
    } else if let Some(s) = &args.since {
        Some(s.clone())
    } else {
        state.as_ref().and_then(|st| st.watermark.clone())
    };
    let full = since.is_none();
    eprintln!(
        "{}",
        match &since {
            Some(s) => format!("incremental extract since {s}"),
            None => "full extract".to_string(),
        }
    );

    let client = FhirClient::new(
        args.token.clone(),
        args.retries,
        Duration::from_secs(args.timeout),
    )?;
    let mut report = Report::default();

    let paged = page_locations(
        &client,
        &args.server,
        since.as_deref(),
        &snap.incoming(),
        &mut report,
    )?;
    // Reported here rather than inside the pager so every phase summary is
    // printed by the orchestrator; the pager returns the moment it is done,
    // so this still lands before the (much longer) fetch phase starts.
    eprintln!(
        "paged {} resources over {} page(s)",
        paged.notes.len(),
        paged.pages
    );

    let mut urls: Vec<String> = paged
        .notes
        .iter()
        .filter_map(|n| n.boundary_url.clone())
        .collect();
    urls.sort();
    urls.dedup();
    let cache = if args.no_cache {
        None
    } else {
        Some(Cache::new(
            &args.cache_dir.clone().unwrap_or_else(|| snap.boundaries()),
        ))
    };
    let opts = FetchOptions {
        concurrency: args.concurrency,
        cache: cache.clone(),
        refresh: args.refresh,
        max_consecutive_failures: args.max_consecutive_failures,
    };
    let summary = fetch_boundaries(&client, &urls, &opts, &mut report)?;

    // The merge reads boundary bytes back by URL: from memory with
    // --no-cache (or after a cache write failed), from the cache otherwise.
    let in_memory: HashMap<String, Vec<u8>> = summary.in_memory;
    let lookup = |url: &str| -> Option<Vec<u8>> {
        if let Some(b) = in_memory.get(url) {
            return Some(b.clone());
        }
        cache.as_ref().and_then(|c| c.read(url).ok().flatten())
    };
    let stats = merge(
        &snap,
        &paged.notes,
        full,
        &lookup,
        &summary.failures,
        &mut report,
    )?;

    let new_state = State {
        server: args.server.trim_end_matches('/').to_string(),
        watermark: stats.watermark.clone(),
        count: stats.total,
        kiln_version: env!("CARGO_PKG_VERSION").to_string(),
        completed_at: format_utc(std::time::SystemTime::now()),
    };
    new_state.write(&snap.state_path())?;
    write_report(&snap.report_path(), &report)?;

    println!(
        "snapshot: {} resources, {} new, {} updated, watermark {}",
        stats.total,
        stats.added,
        stats.updated,
        stats.watermark.as_deref().unwrap_or("none"),
    );
    println!("{}", report.summary());
    Ok(())
}
