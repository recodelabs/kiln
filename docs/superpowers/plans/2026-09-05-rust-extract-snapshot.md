# kiln extract and the snapshot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `kiln extract` fetches Location resources from any FHIR R4 server into the snapshot directory `transform` reads, incrementally by watermark, with boundaries resolved through a content addressed cache; `kiln run` chains extract and transform.

**Architecture:** Three phases through the cache. *Page* walks the search and appends raw resources to `.incoming.ndjson` while noting ids, timestamps and boundary URLs. *Fetch* resolves the distinct boundary URLs on a worker pool into `boundaries/`, with retry, backoff, a circuit breaker and deterministic reporting. *Merge* streams the old snapshot and the incoming file into a new `locations.ndjson`, inlining boundary bytes from the cache, then writes `state.json` with the new watermark. Nothing holds the registry in memory.

**Tech Stack:** Existing crate (Rust stable, clap 4, serde_json, sha2, base64, thiserror) plus `reqwest` 0.13 (`blocking`, `rustls-tls`, no default features). Dev: `httptest` 0.16 for an in-process fake FHIR server.

Spec: `docs/superpowers/specs/2026-09-05-extract-snapshot-design.md`. Where this plan and the spec disagree, the spec wins.

---

## Existing APIs you will use

- `src/error.rs`: `KilnError::{Usage(String), Io{path,source}, Json, ..}`, `KilnError::io(path, e)`, `exit_code()` (Usage → 2, else 1), `type Result<T>`.
- `src/report.rs`: `Report::add(kind, location_id, detail)`, `count`, `to_json`, `summary`.
- `src/transform.rs`: `run_transform(&TransformArgs) -> Result<()>`, `write_report(&Path, &Report)`, `SNAPSHOT_FILE = "locations.ndjson"`.
- `src/fhir/ndjson.rs`: `NdjsonReader::open(&Path)` yielding `Result<Line { number, offset, len, text }>` (skips blanks, BOM aware).
- `src/fhir/location.rs`: `BOUNDARY_EXTENSION_URLS`, `GEOJSON_CONTENT_TYPE`.
- `src/cli.rs`: `Cli`, `Command::{Transform, Inspect}`, `TransformArgs { snapshot, out, country, row_group_size, partition_by }`, `InspectArgs`.

## File structure

```
src/
  cli.rs               + ExtractArgs, RunArgs, Command::{Extract, Run}
  main.rs              + mod extract; mod snapshot; mod run; wiring
  snapshot/
    mod.rs             Snapshot paths, State read/write (atomic), constants
    instant.rs         parse and compare FHIR instants, format UTC now
    merge.rs           streaming upsert with boundary inlining, watermark
  extract/
    mod.rs             run_extract: mode decision, phases, summaries, report file
    client.rs          FhirClient: bearer header, get with retry/backoff, Retry-After
    cache.rs           content addressed boundary cache
    page.rs            search paging into .incoming.ndjson + PageNote per line
    boundary.rs        worker pool, ordered outcomes, breaker, progress, Binary decode
  run.rs               kiln run
tests/
  extract.rs           binary-level tests against an httptest server
```

---

## Task 0: Dependencies and CLI surface

**Files:** `Cargo.toml`, `src/cli.rs`, `src/main.rs`

- [x] **Step 1: Add dependencies**

In `[dependencies]`:
```toml
reqwest = { version = "0.13", default-features = false, features = ["blocking", "rustls-tls"] }
```
In `[dev-dependencies]`:
```toml
httptest = "0.16"
```

- [x] **Step 2: Add the argument structs to src/cli.rs**

```rust
pub const DEFAULT_CONCURRENCY: usize = 8;
pub const DEFAULT_RETRIES: usize = 3;
pub const DEFAULT_MAX_CONSECUTIVE_FAILURES: usize = 50;

#[derive(clap::Args, Debug, Clone)]
pub struct ExtractArgs {
    /// Base URL of the FHIR server (Location is appended)
    #[arg(long)]
    pub server: String,
    /// Bearer token; falls back to $KILN_TOKEN
    #[arg(long, env = "KILN_TOKEN")]
    pub token: Option<String>,
    /// Snapshot directory to create or update
    #[arg(long)]
    pub snapshot: PathBuf,
    /// Ignore the existing snapshot and watermark; fetch everything
    #[arg(long)]
    pub full: bool,
    /// Use this instant instead of the stored watermark for this run
    #[arg(long)]
    pub since: Option<String>,
    /// Boundary fetch worker threads
    #[arg(long, default_value_t = DEFAULT_CONCURRENCY)]
    pub concurrency: usize,
    /// Attempts per request
    #[arg(long, default_value_t = DEFAULT_RETRIES)]
    pub retries: usize,
    /// Abort after this many consecutive boundary failures with no success yet; 0 disables
    #[arg(long, default_value_t = DEFAULT_MAX_CONSECUTIVE_FAILURES)]
    pub max_consecutive_failures: usize,
    /// Neither read nor write the boundary cache (holds fetched boundaries in memory)
    #[arg(long)]
    pub no_cache: bool,
    /// Skip cache reads but still write fetched boundaries to it
    #[arg(long)]
    pub refresh: bool,
    /// Boundary cache directory (default: SNAPSHOT/boundaries)
    #[arg(long)]
    pub cache_dir: Option<PathBuf>,
    /// Total timeout per HTTP request, in seconds (added after the Task 2 review)
    #[arg(long, default_value_t = 300)]
    pub timeout: u64,
}

#[derive(clap::Args, Debug, Clone)]
pub struct RunArgs {
    #[command(flatten)]
    pub extract: ExtractArgs,
    /// Output directory; the dataset is written to OUT/locations
    #[arg(long)]
    pub out: PathBuf,
    /// Override the country code derived from the level-0 admin unit
    #[arg(long)]
    pub country: Option<String>,
    /// Rows per Parquet row group
    #[arg(long, default_value_t = DEFAULT_ROW_GROUP_SIZE)]
    pub row_group_size: usize,
    /// Comma-separated partition keys: any of country, geom_type, tier, type
    #[arg(long, default_value = DEFAULT_PARTITION_BY)]
    pub partition_by: String,
}

impl RunArgs {
    pub fn transform_args(&self) -> TransformArgs {
        TransformArgs {
            snapshot: self.extract.snapshot.clone(),
            out: self.out.clone(),
            country: self.country.clone(),
            row_group_size: self.row_group_size,
            partition_by: self.partition_by.clone(),
        }
    }
}
```

Add to `Command`:
```rust
    /// Fetch Locations from a FHIR server into a snapshot (incremental)
    Extract(ExtractArgs),
    /// Extract then transform
    Run(RunArgs),
```

- [x] **Step 3: Stub the commands in src/main.rs**

Add `mod extract; mod run; mod snapshot;` once those modules exist (Tasks 1 and 7 create them; until then wire the arms to `Err(KilnError::Usage("extract: not implemented yet".into()))` so the crate builds).

- [x] **Step 4: Verify** `cargo build`, `kiln extract --help` lists every flag, `kiln run --help` lists both sets. `cargo clippy --all-targets -- -D warnings` (unused `RunArgs::transform_args` is dead until Task 8; if it warns, call it from the stub arm: `let _ = args.transform_args();`).

- [x] **Step 5: Commit** `git commit -m "Add extract and run argument surface; add reqwest and httptest"`

---

## Task 1: Snapshot paths, state file, instants

**Files:** `src/snapshot/mod.rs`, `src/snapshot/instant.rs`, `src/main.rs` (`mod snapshot;`)

- [x] **Step 1: Tests for instant.rs**

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_fhir_instants_with_fraction_and_offset() {
        assert_eq!(parse_instant("2026-01-02T03:04:05Z"), Some((1767323045, 0)));
        assert_eq!(parse_instant("2026-01-02T03:04:05.123Z"), Some((1767323045, 123_000_000)));
        assert_eq!(parse_instant("2026-01-02T04:04:05+01:00"), Some((1767323045, 0)));
        assert_eq!(parse_instant("2026-01-02"), None);
        assert_eq!(parse_instant("garbage"), None);
    }

    #[test]
    fn later_picks_the_greater_instant_and_ignores_unparseable() {
        assert_eq!(later(None, "2026-01-01T00:00:00Z"), Some("2026-01-01T00:00:00Z".to_string()));
        assert_eq!(later(Some("2026-01-01T00:00:00Z"), "2025-12-31T23:59:59Z"), Some("2026-01-01T00:00:00Z".to_string()));
        assert_eq!(later(Some("2026-01-01T00:00:00Z"), "2026-01-01T01:00:00+01:00"), Some("2026-01-01T00:00:00Z".to_string()), "equal instants keep the first");
        assert_eq!(later(Some("2026-01-01T00:00:00Z"), "nope"), Some("2026-01-01T00:00:00Z".to_string()));
    }

    #[test]
    fn formats_utc_now_as_an_instant() {
        let s = format_utc(std::time::UNIX_EPOCH + std::time::Duration::from_secs(1767323045));
        assert_eq!(s, "2026-01-02T03:04:05Z");
        assert!(parse_instant(&format_utc(std::time::SystemTime::now())).is_some());
    }
}
```

(Verify the epoch value 1767323045 for 2026-01-02T03:04:05Z with `date -u -j -f "%Y-%m-%dT%H:%M:%SZ" 2026-01-02T03:04:05Z +%s` on macOS; fix the constant if it differs.)

- [x] **Step 2: Implement instant.rs**

```rust
//! FHIR `instant` values: parse to (seconds, nanos) since the epoch for
//! comparison, and format the current time. No calendar crate: the civil
//! date arithmetic is Howard Hinnant's days-from-civil.

use std::time::{SystemTime, UNIX_EPOCH};

fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u64;
    let mp = (m as u64 + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d as u64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe as i64 - 719468
}

fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// `YYYY-MM-DDThh:mm:ss[.fraction](Z|±hh:mm)` -> (unix seconds, nanoseconds).
pub fn parse_instant(s: &str) -> Option<(i64, u32)> {
    let b = s.as_bytes();
    if b.len() < 20 || b[4] != b'-' || b[7] != b'-' || b[10] != b'T' || b[13] != b':' || b[16] != b':' {
        return None;
    }
    let num = |r: std::ops::Range<usize>| s.get(r)?.parse::<i64>().ok();
    let (y, mo, d) = (num(0..4)?, num(5..7)? as u32, num(8..10)? as u32);
    let (h, mi, sec) = (num(11..13)?, num(14..16)?, num(17..19)?);
    if !(1..=12).contains(&mo) || !(1..=31).contains(&d) || h > 23 || mi > 59 || sec > 60 {
        return None;
    }
    let mut i = 19;
    let mut nanos = 0u32;
    if b.get(i) == Some(&b'.') {
        let start = i + 1;
        let mut end = start;
        while end < b.len() && b[end].is_ascii_digit() {
            end += 1;
        }
        if end == start {
            return None;
        }
        let digits = &s[start..end];
        let scaled: String = format!("{digits:0<9}").chars().take(9).collect();
        nanos = scaled.parse().ok()?;
        i = end;
    }
    let offset = match b.get(i) {
        Some(b'Z') if i + 1 == b.len() => 0i64,
        Some(sign @ (b'+' | b'-')) if i + 6 == b.len() && b[i + 3] == b':' => {
            let oh = num(i + 1..i + 3)?;
            let om = num(i + 4..i + 6)?;
            let secs = oh * 3600 + om * 60;
            if *sign == b'+' { secs } else { -secs }
        }
        _ => return None,
    };
    let days = days_from_civil(y, mo, d);
    Some((days * 86400 + h * 3600 + mi * 60 + sec - offset, nanos))
}

/// The greater of a current best and a candidate; unparseable candidates lose.
pub fn later(current: Option<&str>, candidate: &str) -> Option<String> {
    let Some(c) = parse_instant(candidate) else { return current.map(str::to_string) };
    match current.and_then(|s| parse_instant(s).map(|p| (s, p))) {
        Some((s, p)) if p >= c => Some(s.to_string()),
        _ => Some(candidate.to_string()),
    }
}

pub fn format_utc(t: SystemTime) -> String {
    let secs = t.duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    let days = secs.div_euclid(86400);
    let rem = secs.rem_euclid(86400);
    let (y, m, d) = civil_from_days(days);
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", rem / 3600, (rem % 3600) / 60, rem % 60)
}
```

- [x] **Step 3: Tests for snapshot/mod.rs**

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_are_under_the_snapshot_dir() {
        let s = Snapshot::new(std::path::Path::new("/x"));
        assert_eq!(s.locations(), std::path::PathBuf::from("/x/locations.ndjson"));
        assert_eq!(s.state_path(), std::path::PathBuf::from("/x/state.json"));
        assert_eq!(s.incoming(), std::path::PathBuf::from("/x/.incoming.ndjson"));
        assert_eq!(s.boundaries(), std::path::PathBuf::from("/x/boundaries"));
    }

    #[test]
    fn state_round_trips_and_is_absent_when_missing() {
        let dir = tempfile::tempdir().unwrap();
        let s = Snapshot::new(dir.path());
        assert!(State::read(&s.state_path()).unwrap().is_none());
        let state = State { server: "https://f/fhir".into(), watermark: Some("2026-01-01T00:00:00Z".into()), count: 3, kiln_version: "0.2.0".into(), completed_at: "2026-01-01T00:00:01Z".into() };
        state.write(&s.state_path()).unwrap();
        assert_eq!(State::read(&s.state_path()).unwrap().unwrap(), state);
        assert!(!dir.path().join("state.json.tmp").exists());
    }

    #[test]
    fn corrupt_state_is_a_usage_error() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("state.json"), "{nope").unwrap();
        let err = State::read(&dir.path().join("state.json")).unwrap_err();
        assert_eq!(err.exit_code(), 2);
    }

    #[test]
    fn servers_compare_without_trailing_slash() {
        assert!(same_server("https://f/fhir/", "https://f/fhir"));
        assert!(!same_server("https://f/fhir", "https://g/fhir"));
    }
}
```

- [x] **Step 4: Implement snapshot/mod.rs**

```rust
//! The snapshot directory: locations.ndjson, state.json, the boundary cache,
//! and the in-progress incoming file. See README "The snapshot".

pub mod instant;
pub mod merge;

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{KilnError, Result};

pub const LOCATIONS_FILE: &str = "locations.ndjson";
pub const STATE_FILE: &str = "state.json";
pub const INCOMING_FILE: &str = ".incoming.ndjson";
pub const BOUNDARIES_DIR: &str = "boundaries";
pub const EXTRACT_REPORT_FILE: &str = "_extract_report.json";

#[derive(Debug, Clone)]
pub struct Snapshot {
    pub dir: PathBuf,
}

impl Snapshot {
    pub fn new(dir: &Path) -> Self {
        Self { dir: dir.to_path_buf() }
    }
    pub fn locations(&self) -> PathBuf { self.dir.join(LOCATIONS_FILE) }
    pub fn locations_tmp(&self) -> PathBuf { self.dir.join("locations.ndjson.tmp") }
    pub fn state_path(&self) -> PathBuf { self.dir.join(STATE_FILE) }
    pub fn incoming(&self) -> PathBuf { self.dir.join(INCOMING_FILE) }
    pub fn boundaries(&self) -> PathBuf { self.dir.join(BOUNDARIES_DIR) }
    pub fn report_path(&self) -> PathBuf { self.dir.join(EXTRACT_REPORT_FILE) }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct State {
    pub server: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub watermark: Option<String>,
    pub count: usize,
    pub kiln_version: String,
    pub completed_at: String,
}

impl State {
    pub fn read(path: &Path) -> Result<Option<State>> {
        match std::fs::read_to_string(path) {
            Ok(text) => serde_json::from_str(&text)
                .map(Some)
                .map_err(|e| KilnError::Usage(format!("{}: not a valid state file: {e}", path.display()))),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(KilnError::io(path, e)),
        }
    }

    /// Atomic: write `<path>.tmp` then rename over `path`.
    pub fn write(&self, path: &Path) -> Result<()> {
        let tmp = path.with_extension("json.tmp");
        let text = serde_json::to_string_pretty(self)? + "\n";
        std::fs::write(&tmp, text).map_err(|e| KilnError::io(&tmp, e))?;
        std::fs::rename(&tmp, path).map_err(|e| KilnError::io(path, e))
    }
}

pub fn same_server(a: &str, b: &str) -> bool {
    a.trim_end_matches('/') == b.trim_end_matches('/')
}
```

Create `src/snapshot/merge.rs` as a placeholder (`//! Filled in Task 6.`). Add `mod snapshot;` to main.rs.

- [x] **Step 5: Run** `cargo test snapshot` (7 passed), clippy clean (dead code expected until Task 7; if `-D warnings` fails only on dead code in new modules, run clippy without `-D` for now and note it).

- [x] **Step 6: Commit** `git commit -m "Add snapshot paths, state file, and FHIR instant handling"`

---

## Task 2: HTTP client with retry

**Files:** `src/extract/mod.rs` (declarations only for now), `src/extract/client.rs`, `src/main.rs` (`mod extract;`)

- [x] **Step 1: Tests** (in client.rs; these use httptest and real sleeps of at most 1.5 s)

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use httptest::{matchers::*, responders::*, Expectation, Server};

    fn client(retries: usize) -> FhirClient {
        FhirClient::new(None, retries, std::time::Duration::from_secs(5)).unwrap()
    }

    #[test]
    fn backoff_doubles_from_half_a_second_and_is_clamped() {
        assert_eq!(backoff_delay(1, None).as_millis(), 500);
        assert_eq!(backoff_delay(2, None).as_millis(), 1000);
        assert_eq!(backoff_delay(3, None).as_millis(), 2000);
        assert_eq!(backoff_delay(20, None).as_secs(), 30);
        assert_eq!(backoff_delay(1, Some(7.0)).as_secs(), 7);
        assert_eq!(backoff_delay(1, Some(99999.0)).as_secs(), 30);
        assert_eq!(backoff_delay(1, Some(-5.0)).as_secs(), 0);
    }

    #[test]
    fn retry_after_parses_numeric_seconds_only() {
        assert_eq!(parse_retry_after(Some("7")), Some(7.0));
        assert_eq!(parse_retry_after(Some("1.5")), Some(1.5));
        assert_eq!(parse_retry_after(Some("Wed, 21 Oct 2026 07:28:00 GMT")), None);
        assert_eq!(parse_retry_after(None), None);
    }

    #[test]
    fn a_500_then_200_succeeds_with_retries() {
        let server = Server::run();
        server.expect(Expectation::matching(request::method_path("GET", "/b")).times(2)
            .respond_with(cycle![status_code(500), status_code(200).body("ok")]));
        let got = client(3).get(&server.url("/b").to_string()).unwrap();
        assert_eq!(got.body, b"ok");
    }

    #[test]
    fn a_429_honours_retry_after() {
        let server = Server::run();
        server.expect(Expectation::matching(request::method_path("GET", "/r")).times(2)
            .respond_with(cycle![status_code(429).append_header("Retry-After", "1"), status_code(200).body("ok")]));
        let t = std::time::Instant::now();
        client(3).get(&server.url("/r").to_string()).unwrap();
        assert!(t.elapsed() >= std::time::Duration::from_secs(1));
    }

    #[test]
    fn a_404_is_not_retried() {
        let server = Server::run();
        server.expect(Expectation::matching(request::method_path("GET", "/m")).times(1).respond_with(status_code(404)));
        let err = client(3).get(&server.url("/m").to_string()).unwrap_err();
        assert!(matches!(err, FetchError::Status { status: 404, .. }), "{err:?}");
    }

    #[test]
    fn exhausted_retries_report_the_last_status() {
        let server = Server::run();
        server.expect(Expectation::matching(request::method_path("GET", "/x")).times(2).respond_with(status_code(503)));
        let err = client(2).get(&server.url("/x").to_string()).unwrap_err();
        assert!(matches!(err, FetchError::Status { status: 503, .. }));
    }

    #[test]
    fn connection_refused_is_a_transport_error() {
        let err = client(1).get("http://127.0.0.1:9/nothing").unwrap_err();
        assert!(matches!(err, FetchError::Transport(_)), "{err:?}");
    }

    #[test]
    fn bearer_header_is_sent_when_a_token_is_set() {
        let server = Server::run();
        server.expect(Expectation::matching(all_of![
            request::method_path("GET", "/t"),
            request::headers(contains(("authorization", "Bearer secret"))),
            request::headers(contains(("accept", "application/fhir+json"))),
        ]).respond_with(status_code(200).body("ok")));
        FhirClient::new(Some("secret".into()), 1, std::time::Duration::from_secs(5)).unwrap()
            .get(&server.url("/t").to_string()).unwrap();
    }
}
```

- [x] **Step 2: Implement client.rs**

```rust
//! The one HTTP client. Bearer auth, FHIR accept header, and the retry
//! policy shared by paging and boundary fetches: connection errors, 5xx and
//! 429 are retried with exponential backoff (0.5 s doubling, clamped to
//! [0, 30] s, a numeric Retry-After replacing the computed delay); any other
//! 4xx is final.

use std::time::Duration;

use reqwest::blocking::Client;
use reqwest::header::{HeaderMap, HeaderValue, ACCEPT, AUTHORIZATION};

use crate::error::{KilnError, Result};

const RETRY_BASE: f64 = 0.5;
const RETRY_MAX: f64 = 30.0;

#[derive(Debug)]
pub enum FetchError {
    /// Final status after retries; `body` is at most 500 bytes of the response.
    Status { status: u16, body: String },
    /// Connection, TLS, timeout or protocol error after retries.
    Transport(String),
}

impl std::fmt::Display for FetchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FetchError::Status { status, body } if body.is_empty() => write!(f, "HTTP {status}"),
            FetchError::Status { status, body } => write!(f, "HTTP {status}: {body}"),
            FetchError::Transport(msg) => write!(f, "{msg}"),
        }
    }
}

pub struct Fetched {
    pub body: Vec<u8>,
}

pub struct FhirClient {
    http: Client,
    retries: usize,
}

pub fn backoff_delay(attempt: usize, retry_after: Option<f64>) -> Duration {
    let secs = retry_after.unwrap_or_else(|| RETRY_BASE * 2f64.powi(attempt.saturating_sub(1) as i32));
    Duration::from_secs_f64(secs.clamp(0.0, RETRY_MAX))
}

pub fn parse_retry_after(value: Option<&str>) -> Option<f64> {
    value?.trim().parse::<f64>().ok()
}

impl FhirClient {
    pub fn new(token: Option<String>, retries: usize, timeout: Duration) -> Result<Self> {
        let mut headers = HeaderMap::new();
        headers.insert(ACCEPT, HeaderValue::from_static("application/fhir+json"));
        if let Some(t) = token {
            let v = HeaderValue::from_str(&format!("Bearer {t}"))
                .map_err(|_| KilnError::Usage("token contains characters that are not valid in a header".into()))?;
            headers.insert(AUTHORIZATION, v);
        }
        let http = Client::builder()
            .default_headers(headers)
            .timeout(timeout)
            .user_agent(concat!("kiln/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|e| KilnError::Usage(format!("cannot build HTTP client: {e}")))?;
        Ok(Self { http, retries: retries.max(1) })
    }

    /// GET with the retry policy. Sleeps between attempts.
    pub fn get(&self, url: &str) -> std::result::Result<Fetched, FetchError> {
        let mut last: Option<FetchError> = None;
        for attempt in 1..=self.retries {
            match self.http.get(url).send() {
                Err(e) => {
                    last = Some(FetchError::Transport(format!("{url}: {e}")));
                    if attempt < self.retries {
                        std::thread::sleep(backoff_delay(attempt, None));
                    }
                }
                Ok(resp) => {
                    let status = resp.status().as_u16();
                    if status == 429 || status >= 500 {
                        let ra = parse_retry_after(resp.headers().get("retry-after").and_then(|v| v.to_str().ok()));
                        let body = resp.text().unwrap_or_default();
                        last = Some(FetchError::Status { status, body: body.chars().take(500).collect() });
                        if attempt < self.retries {
                            std::thread::sleep(backoff_delay(attempt, ra));
                        }
                    } else if status != 200 {
                        let body = resp.text().unwrap_or_default();
                        return Err(FetchError::Status { status, body: body.chars().take(500).collect() });
                    } else {
                        return match resp.bytes() {
                            Ok(b) => Ok(Fetched { body: b.to_vec() }),
                            Err(e) => Err(FetchError::Transport(format!("{url}: reading body: {e}"))),
                        };
                    }
                }
            }
        }
        Err(last.unwrap_or(FetchError::Transport(format!("{url}: no attempts made"))))
    }
}
```

`src/extract/mod.rs` for now: `pub mod client;` plus placeholders `pub mod cache; pub mod page; pub mod boundary;` with one-line placeholder files. Add `mod extract;` to main.rs.

- [x] **Step 3: Run** `cargo test extract::client` (8 passed; the 429 test takes about a second).

- [x] **Step 4: Commit** `git commit -m "Add the FHIR HTTP client with retry, backoff and Retry-After"`

---

## Task 3: Boundary cache

**Files:** `src/extract/cache.rs`

- [x] **Step 1: Tests**

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_is_sha256_of_the_url() {
        assert_eq!(cache_key("https://a/b").len(), 64);
        assert_ne!(cache_key("https://a/b"), cache_key("https://a/c"));
    }

    #[test]
    fn write_then_read_round_trips_and_leaves_no_temp_files() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Cache::new(dir.path());
        cache.write("https://a/b", b"geo").unwrap();
        assert_eq!(cache.read("https://a/b").unwrap(), Some(b"geo".to_vec()));
        let names: Vec<String> = std::fs::read_dir(dir.path()).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().to_string()).collect();
        assert_eq!(names.len(), 2, "{names:?}");
        assert!(names.iter().any(|n| n.ends_with(".bin")) && names.iter().any(|n| n.ends_with(".meta.json")));
        let meta: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(dir.path().join(format!("{}.meta.json", cache_key("https://a/b")))).unwrap()).unwrap();
        assert_eq!(meta["url"], "https://a/b");
        assert!(meta["fetched_at"].as_str().unwrap().ends_with('Z'));
    }

    #[test]
    fn a_miss_is_none_and_a_corrupt_entry_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Cache::new(dir.path());
        assert_eq!(cache.read("https://a/none").unwrap(), None);
        cache.write("https://a/b", b"geo").unwrap();
        std::fs::write(dir.path().join(format!("{}.bin", cache_key("https://a/b"))), b"tampered").unwrap();
        let err = cache.read("https://a/b").unwrap_err();
        assert!(err.contains("sha256"), "{err}");
    }

    #[test]
    fn a_bin_without_meta_is_a_miss() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Cache::new(dir.path());
        std::fs::write(dir.path().join(format!("{}.bin", cache_key("u"))), b"x").unwrap();
        assert_eq!(cache.read("u").unwrap(), None);
    }

    #[test]
    fn unwritable_dir_is_an_error_string_not_a_panic() {
        let cache = Cache::new(std::path::Path::new("/nonexistent/parent/that/cannot/be/made\0"));
        assert!(cache.write("u", b"x").is_err());
    }
}
```

- [x] **Step 2: Implement**

```rust
//! Content addressed cache of fetched boundary bytes, keyed by sha256 of the
//! URL. Two files per entry: `<key>.bin` and `<key>.meta.json` (url,
//! fetched_at, sha256 of the bin, verified on read). Written temp-then-rename,
//! bin before meta, so a meta file is the commit marker. Nothing here aborts a
//! run: every failure is an `Err(String)` the caller reports as `cache_error`.
//! Same layout as the Python cache, so directories are interchangeable.

use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::snapshot::instant::format_utc;

pub fn cache_key(url: &str) -> String {
    format!("{:x}", Sha256::digest(url.as_bytes()))
}

#[derive(Debug, Clone)]
pub struct Cache {
    dir: PathBuf,
}

impl Cache {
    pub fn new(dir: &Path) -> Self {
        Self { dir: dir.to_path_buf() }
    }

    fn paths(&self, url: &str) -> (PathBuf, PathBuf) {
        let key = cache_key(url);
        (self.dir.join(format!("{key}.bin")), self.dir.join(format!("{key}.meta.json")))
    }

    /// Ok(None) on a miss (no meta file). Err on a corrupt or unreadable entry.
    pub fn read(&self, url: &str) -> std::result::Result<Option<Vec<u8>>, String> {
        let (bin, meta) = self.paths(url);
        let meta_text = match std::fs::read_to_string(&meta) {
            Ok(t) => t,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(format!("{}: {e}", meta.display())),
        };
        let meta_json: serde_json::Value = serde_json::from_str(&meta_text).map_err(|e| format!("{}: {e}", meta.display()))?;
        let expected = meta_json["sha256"].as_str().ok_or_else(|| format!("{}: no sha256", meta.display()))?;
        let bytes = std::fs::read(&bin).map_err(|e| format!("{}: {e}", bin.display()))?;
        let actual = format!("{:x}", Sha256::digest(&bytes));
        if actual != expected {
            return Err(format!("{}: sha256 mismatch (entry corrupt or truncated)", bin.display()));
        }
        Ok(Some(bytes))
    }

    pub fn write(&self, url: &str, bytes: &[u8]) -> std::result::Result<(), String> {
        std::fs::create_dir_all(&self.dir).map_err(|e| format!("{}: {e}", self.dir.display()))?;
        let (bin, meta) = self.paths(url);
        atomic_write(&bin, bytes)?;
        let meta_json = serde_json::json!({
            "url": url,
            "fetched_at": format_utc(std::time::SystemTime::now()),
            "sha256": format!("{:x}", Sha256::digest(bytes)),
        });
        atomic_write(&meta, meta_json.to_string().as_bytes())
    }
}

fn atomic_write(path: &Path, bytes: &[u8]) -> std::result::Result<(), String> {
    let tmp = path.with_extension(format!("{}.tmp{}", path.extension().and_then(|e| e.to_str()).unwrap_or(""), std::process::id()));
    std::fs::write(&tmp, bytes).map_err(|e| format!("{}: {e}", tmp.display()))?;
    std::fs::rename(&tmp, path).map_err(|e| format!("{}: {e}", path.display()))
}
```

Note: two workers racing on the same URL each write their own `tmp<pid>`-suffixed file; the pid is the same within one process, so use a thread-unique suffix instead: `format!("{}.tmp{}-{:?}", ..., std::process::id(), std::thread::current().id())` (ThreadId's Debug is `ThreadId(N)`; sanitise to digits). Whichever renames last wins with identical bytes.

- [x] **Step 3: Run** `cargo test extract::cache` (5 passed). Verify interoperability: `python3 -c "import hashlib;print(hashlib.sha256(b'https://a/b').hexdigest())"` equals `cache_key("https://a/b")` (add that value as a literal assertion in the first test).

- [x] **Step 4: Commit** `git commit -m "Add the content addressed boundary cache"`

---

## Task 4: Paging into the incoming file

**Files:** `src/extract/page.rs`

- [x] **Step 1: Tests**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::extract::client::FhirClient;
    use crate::report::Report;
    use httptest::{matchers::*, responders::*, Expectation, Server};

    fn bundle(entries: &[serde_json::Value], next: Option<&str>) -> String {
        let mut b = serde_json::json!({"resourceType":"Bundle","type":"searchset","entry": entries.iter().map(|r| serde_json::json!({"resource": r})).collect::<Vec<_>>()});
        if let Some(n) = next { b["link"] = serde_json::json!([{"relation":"self","url":"x"},{"relation":"next","url":n}]); }
        b.to_string()
    }
    fn loc(id: &str, updated: &str, url: Option<&str>) -> serde_json::Value {
        let mut r = serde_json::json!({"resourceType":"Location","id":id,"meta":{"lastUpdated":updated}});
        if let Some(u) = url { r["extension"] = serde_json::json!([{"url":"https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson","valueAttachment":{"contentType":"application/geo+json","url":u}}]); }
        r
    }

    #[test]
    fn follows_next_links_and_records_notes() {
        let server = Server::run();
        let p2 = server.url("/fhir/Location?page=2").to_string();
        server.expect(Expectation::matching(all_of![request::method_path("GET", "/fhir/Location"), request::query(url_decoded(contains(("_count", "1000"))))]).times(1)
            .respond_with(status_code(200).body(bundle(&[loc("a", "2026-01-01T00:00:00Z", Some("https://b/a.geojson")), serde_json::json!({"resourceType":"Location"})], Some(&p2)))));
        server.expect(Expectation::matching(all_of![request::method_path("GET", "/fhir/Location"), request::query(url_decoded(contains(("page", "2"))))]).times(1)
            .respond_with(status_code(200).body(bundle(&[loc("b", "2026-02-01T00:00:00Z", None)], None))));
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join(".incoming.ndjson");
        let client = FhirClient::new(None, 1, std::time::Duration::from_secs(5)).unwrap();
        let mut report = Report::default();
        let res = page_locations(&client, &server.url("/fhir").to_string(), None, &out, &mut report).unwrap();
        assert_eq!(res.pages, 2);
        assert_eq!(res.notes.len(), 2);
        assert_eq!(res.notes[0].id, "a");
        assert_eq!(res.notes[0].boundary_url.as_deref(), Some("https://b/a.geojson"));
        assert_eq!(res.notes[1].last_updated.as_deref(), Some("2026-02-01T00:00:00Z"));
        assert_eq!(report.count("page_resource_skipped"), 1);
        let text = std::fs::read_to_string(&out).unwrap();
        assert_eq!(text.lines().count(), 2);
        assert!(text.lines().next().unwrap().starts_with("{\"resourceType\":\"Location\",\"id\":\"a\""));
    }

    #[test]
    fn incremental_adds_last_updated_ge() {
        let server = Server::run();
        server.expect(Expectation::matching(all_of![request::method_path("GET", "/fhir/Location"), request::query(url_decoded(contains(("_lastUpdated", "ge2026-01-01T00:00:00Z"))))]).times(1)
            .respond_with(status_code(200).body(bundle(&[], None))));
        let dir = tempfile::tempdir().unwrap();
        let client = FhirClient::new(None, 1, std::time::Duration::from_secs(5)).unwrap();
        let res = page_locations(&client, &server.url("/fhir/").to_string(), Some("2026-01-01T00:00:00Z"), &dir.path().join("i"), &mut Report::default()).unwrap();
        assert_eq!(res.notes.len(), 0);
    }

    #[test]
    fn a_repeated_next_link_is_an_error() {
        let server = Server::run();
        let me = server.url("/fhir/Location").to_string();
        server.expect(Expectation::matching(request::method_path("GET", "/fhir/Location")).times(..)
            .respond_with(status_code(200).body(bundle(&[], Some(&me)))));
        let dir = tempfile::tempdir().unwrap();
        let client = FhirClient::new(None, 1, std::time::Duration::from_secs(5)).unwrap();
        let err = page_locations(&client, &server.url("/fhir").to_string(), None, &dir.path().join("i"), &mut Report::default()).unwrap_err();
        assert!(err.to_string().contains("cyclic"), "{err}");
    }

    #[test]
    fn a_non_200_page_is_an_environment_error() {
        let server = Server::run();
        server.expect(Expectation::matching(request::method_path("GET", "/fhir/Location")).respond_with(status_code(401).body("nope")));
        let dir = tempfile::tempdir().unwrap();
        let client = FhirClient::new(None, 1, std::time::Duration::from_secs(5)).unwrap();
        let err = page_locations(&client, &server.url("/fhir").to_string(), None, &dir.path().join("i"), &mut Report::default()).unwrap_err();
        assert_eq!(err.exit_code(), 1);
        assert!(err.to_string().contains("401"));
    }
}
```

- [x] **Step 2: Implement**

```rust
//! Phase one: walk the Location search and append every resource to the
//! incoming file, noting per line what the later phases need.

use std::collections::HashSet;
use std::io::Write;
use std::path::Path;

use serde_json::Value;

use crate::error::{KilnError, Result};
use crate::extract::client::FhirClient;
use crate::fhir::location::BOUNDARY_EXTENSION_URLS;
use crate::report::Report;

pub const PAGE_SIZE: usize = 1000;
pub const MAX_PAGES: usize = 10_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PageNote {
    pub id: String,
    pub last_updated: Option<String>,
    /// A boundary attachment `url` with no `data`, if the resource has one.
    pub boundary_url: Option<String>,
}

pub struct PageResult {
    pub notes: Vec<PageNote>,
    pub pages: usize,
}

pub fn next_link(bundle: &Value) -> Option<String> {
    bundle.get("link")?.as_array()?.iter().find_map(|l| {
        (l.get("relation")?.as_str()? == "next").then(|| l.get("url")?.as_str().map(str::to_string)).flatten()
    })
}

pub fn boundary_url(resource: &Value) -> Option<String> {
    for ext in resource.get("extension")?.as_array()? {
        let url = ext.get("url").and_then(Value::as_str).unwrap_or("");
        if !BOUNDARY_EXTENSION_URLS.contains(&url) { continue; }
        let att = ext.get("valueAttachment")?.as_object()?;
        if att.get("data").and_then(Value::as_str).is_some_and(|d| !d.is_empty()) { return None; }
        if let Some(u) = att.get("url").and_then(Value::as_str).filter(|u| !u.is_empty()) {
            return Some(u.to_string());
        }
    }
    None
}

pub fn page_locations(
    client: &FhirClient,
    server: &str,
    since: Option<&str>,
    incoming: &Path,
    report: &mut Report,
) -> Result<PageResult> {
    let mut url = format!("{}/Location?_count={PAGE_SIZE}", server.trim_end_matches('/'));
    if let Some(s) = since {
        url.push_str(&format!("&_lastUpdated=ge{s}"));
    }
    if let Some(parent) = incoming.parent() {
        std::fs::create_dir_all(parent).map_err(|e| KilnError::io(parent, e))?;
    }
    let file = std::fs::File::create(incoming).map_err(|e| KilnError::io(incoming, e))?;
    let mut out = std::io::BufWriter::new(file);
    let mut seen: HashSet<String> = HashSet::new();
    let mut notes = Vec::new();
    let mut pages = 0usize;

    loop {
        if !seen.insert(url.clone()) {
            return Err(KilnError::Usage(format!("cyclic pagination: the server repeated the next link {url}")).into_environment());
        }
        if pages >= MAX_PAGES {
            return Err(environment(format!("more than {MAX_PAGES} pages; the server's pagination looks broken")));
        }
        let fetched = client.get(&url).map_err(|e| environment(format!("fetching {url}: {e}")))?;
        pages += 1;
        let bundle: Value = serde_json::from_slice(&fetched.body).map_err(|e| environment(format!("{url}: response is not JSON: {e}")))?;
        for (i, entry) in bundle.get("entry").and_then(Value::as_array).into_iter().flatten().enumerate() {
            let Some(resource) = entry.get("resource").filter(|r| r.is_object()) else {
                report.add("page_resource_skipped", &format!("page {pages} entry {i}"), "entry has no resource object");
                continue;
            };
            let Some(id) = resource.get("id").and_then(Value::as_str).filter(|s| !s.is_empty()) else {
                report.add("page_resource_skipped", &format!("page {pages} entry {i}"), "resource has no id");
                continue;
            };
            notes.push(PageNote {
                id: id.to_string(),
                last_updated: resource.pointer("/meta/lastUpdated").and_then(Value::as_str).map(str::to_string),
                boundary_url: boundary_url(resource),
            });
            serde_json::to_writer(&mut out, resource)?;
            out.write_all(b"\n").map_err(|e| KilnError::io(incoming, e))?;
        }
        match next_link(&bundle) {
            Some(next) => url = next,
            None => break,
        }
    }
    out.flush().map_err(|e| KilnError::io(incoming, e))?;
    eprintln!("paged {} resources over {pages} page(s)", notes.len());
    Ok(PageResult { notes, pages })
}
```

`environment(msg)` needs an error variant that exits 1 with a plain message. Add to `src/error.rs`:

```rust
    #[error("{0}")]
    Environment(String),
```
(exit code 1, i.e. the default arm) and `pub fn environment(msg: String) -> KilnError { KilnError::Environment(msg) }` in extract/mod.rs (or as an associated fn). Replace the `into_environment()` placeholder in the cyclic-pagination line with `return Err(environment(format!(..)))`.

- [x] **Step 3: Run** `cargo test extract::page` (4 passed).

- [x] **Step 4: Commit** `git commit -m "Page the Location search into the incoming file"`

---

## Task 5: Boundary fetch pool

**Files:** `src/extract/boundary.rs`

- [x] **Step 1: Tests**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::extract::cache::Cache;
    use crate::extract::client::FhirClient;
    use crate::report::Report;
    use httptest::{matchers::*, responders::*, Expectation, Server};

    fn client() -> FhirClient { FhirClient::new(None, 2, std::time::Duration::from_secs(5)).unwrap() }

    #[test]
    fn binary_resources_are_decoded_and_raw_bodies_pass_through() {
        let b64 = base64::Engine::encode(&base64::engine::general_purpose::STANDARD, b"{\"type\":\"Point\"}");
        let bin = format!(r#"{{"resourceType":"Binary","contentType":"application/geo+json","data":"{b64}"}}"#);
        assert_eq!(extract_payload(bin.as_bytes()).unwrap(), b"{\"type\":\"Point\"}");
        assert_eq!(extract_payload(b"{\"type\":\"Polygon\"}").unwrap(), b"{\"type\":\"Polygon\"}");
        assert_eq!(extract_payload(b"not json at all").unwrap(), b"not json at all");
        assert!(extract_payload(br#"{"resourceType":"Binary"}"#).unwrap_err().contains("no data"));
        assert!(extract_payload(br#"{"resourceType":"Binary","data":"!!"}"#).unwrap_err().contains("base64"));
    }

    #[test]
    fn fetches_into_the_cache_and_reports_in_order() {
        let server = Server::run();
        server.expect(Expectation::matching(request::method_path("GET", "/ok")).times(1).respond_with(status_code(200).body("{\"type\":\"Point\"}")));
        server.expect(Expectation::matching(request::method_path("GET", "/missing")).times(1).respond_with(status_code(404)));
        let dir = tempfile::tempdir().unwrap();
        let cache = Cache::new(dir.path());
        cache.write(&server.url("/cached").to_string(), b"c").unwrap();
        let urls = vec![server.url("/ok").to_string(), server.url("/missing").to_string(), server.url("/cached").to_string()];
        let mut report = Report::default();
        let opts = FetchOptions { concurrency: 2, cache: Some(cache.clone()), refresh: false, max_consecutive_failures: 50 };
        let summary = fetch_boundaries(&client(), &urls, &opts, &mut report).unwrap();
        assert_eq!((summary.cached, summary.fetched, summary.failed), (1, 1, 1));
        assert_eq!(cache.read(&urls[0]).unwrap(), Some(b"{\"type\":\"Point\"}".to_vec()));
        assert!(summary.failures[&urls[1]].contains("404"));
        assert_eq!(report.count("boundary_fetch_failed"), 0, "failures are reported by the merge, not here");
    }

    #[test]
    fn no_cache_keeps_bytes_in_memory() {
        let server = Server::run();
        server.expect(Expectation::matching(request::method_path("GET", "/ok")).respond_with(status_code(200).body("g")));
        let urls = vec![server.url("/ok").to_string()];
        let opts = FetchOptions { concurrency: 1, cache: None, refresh: false, max_consecutive_failures: 0 };
        let summary = fetch_boundaries(&client(), &urls, &opts, &mut Report::default()).unwrap();
        assert_eq!(summary.in_memory.get(&urls[0]).map(|v| v.as_slice()), Some(&b"g"[..]));
    }

    #[test]
    fn breaker_trips_on_consecutive_failures_with_no_success() {
        let server = Server::run();
        server.expect(Expectation::matching(request::method_path("GET", "/dead")).times(..).respond_with(status_code(500)));
        let urls: Vec<String> = (0..10).map(|i| server.url(&format!("/dead?i={i}")).to_string()).collect();
        let opts = FetchOptions { concurrency: 2, cache: None, refresh: false, max_consecutive_failures: 3 };
        let err = fetch_boundaries(&FhirClient::new(None, 1, std::time::Duration::from_secs(5)).unwrap(), &urls, &opts, &mut Report::default()).unwrap_err();
        assert_eq!(err.exit_code(), 1);
        assert!(err.to_string().contains("--max-consecutive-failures"), "{err}");
    }

    #[test]
    fn breaker_does_not_trip_after_one_success() {
        let server = Server::run();
        server.expect(Expectation::matching(request::method_path("GET", "/ok")).respond_with(status_code(200).body("g")));
        server.expect(Expectation::matching(request::method_path("GET", "/dead")).times(..).respond_with(status_code(500)));
        let mut urls = vec![server.url("/ok").to_string()];
        urls.extend((0..5).map(|i| server.url(&format!("/dead?i={i}")).to_string()));
        let opts = FetchOptions { concurrency: 1, cache: None, refresh: false, max_consecutive_failures: 2 };
        let summary = fetch_boundaries(&FhirClient::new(None, 1, std::time::Duration::from_secs(5)).unwrap(), &urls, &opts, &mut Report::default()).unwrap();
        assert_eq!((summary.fetched, summary.failed), (1, 5));
    }
}
```

- [x] **Step 2: Implement**

```rust
//! Phase two: resolve the distinct boundary URLs on a worker pool into the
//! cache (or memory with --no-cache). Workers only fetch and write cache
//! entries; the main thread consumes outcomes in submission order so the
//! counters and any reports are deterministic.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc;

use base64::Engine;

use crate::error::{KilnError, Result};
use crate::extract::cache::Cache;
use crate::extract::client::FhirClient;
use crate::report::Report;

const PROGRESS_MIN_ITEMS: usize = 50;

pub struct FetchOptions {
    pub concurrency: usize,
    /// None = --no-cache: bytes are kept in `FetchSummary::in_memory`.
    pub cache: Option<Cache>,
    pub refresh: bool,
    /// 0 disables the breaker.
    pub max_consecutive_failures: usize,
}

#[derive(Debug)]
enum Outcome {
    Cached,
    Fetched(Option<Vec<u8>>),
    Failed(String),
}

#[derive(Debug, Default)]
pub struct FetchSummary {
    pub cached: usize,
    pub fetched: usize,
    pub failed: usize,
    /// url -> failure detail, for the merge to report per resource.
    pub failures: HashMap<String, String>,
    /// url -> bytes, only with --no-cache.
    pub in_memory: HashMap<String, Vec<u8>>,
    pub cache_errors: Vec<String>,
}

/// A 200 body: a FHIR Binary is base64-decoded; anything else is the bytes.
pub fn extract_payload(body: &[u8]) -> std::result::Result<Vec<u8>, String> {
    if let Ok(v) = serde_json::from_slice::<serde_json::Value>(body) {
        if v.get("resourceType").and_then(|t| t.as_str()) == Some("Binary") {
            let data = v.get("data").and_then(|d| d.as_str()).filter(|d| !d.is_empty()).ok_or("Binary has no data")?;
            return base64::engine::general_purpose::STANDARD.decode(data).map_err(|e| format!("Binary data is not valid base64: {e}"));
        }
    }
    Ok(body.to_vec())
}

fn resolve_one(client: &FhirClient, url: &str, opts: &FetchOptions, cache_errors: &mut Vec<String>) -> Outcome {
    if let (Some(cache), false) = (&opts.cache, opts.refresh) {
        match cache.read(url) {
            Ok(Some(_)) => return Outcome::Cached,
            Ok(None) => {}
            Err(e) => cache_errors.push(e),
        }
    }
    let body = match client.get(url) {
        Ok(f) => f.body,
        Err(e) => return Outcome::Failed(format!("{url}: {e}")),
    };
    let bytes = match extract_payload(&body) {
        Ok(b) => b,
        Err(e) => return Outcome::Failed(format!("{url}: {e}")),
    };
    match &opts.cache {
        Some(cache) => {
            if let Err(e) = cache.write(url, &bytes) {
                cache_errors.push(e);
                return Outcome::Fetched(Some(bytes)); // keep it in memory so the merge still inlines it
            }
            Outcome::Fetched(None)
        }
        None => Outcome::Fetched(Some(bytes)),
    }
}

pub fn fetch_boundaries(client: &FhirClient, urls: &[String], opts: &FetchOptions, report: &mut Report) -> Result<FetchSummary> {
    let total = urls.len();
    let workers = opts.concurrency.max(1).min(total.max(1));
    let next = AtomicUsize::new(0);
    let stop = AtomicBool::new(false);
    let (tx, rx) = mpsc::channel::<(usize, Outcome, Vec<String>)>();

    let mut summary = FetchSummary::default();
    let interval = (total / 20).max(1);
    let mut consecutive = 0usize;
    let mut any_success = false;
    let mut pending: HashMap<usize, (Outcome, Vec<String>)> = HashMap::new();
    let mut cursor = 0usize;
    let mut breaker: Option<String> = None;

    std::thread::scope(|s| {
        for _ in 0..workers {
            let tx = tx.clone();
            let next = &next;
            let stop = &stop;
            s.spawn(move || loop {
                if stop.load(Ordering::Relaxed) { break; }
                let i = next.fetch_add(1, Ordering::Relaxed);
                if i >= total { break; }
                let mut errs = Vec::new();
                let outcome = resolve_one(client, &urls[i], opts, &mut errs);
                if tx.send((i, outcome, errs)).is_err() { break; }
            });
        }
        drop(tx);
        for (i, outcome, errs) in rx {
            pending.insert(i, (outcome, errs));
            while let Some((outcome, errs)) = pending.remove(&cursor) {
                let url = &urls[cursor];
                for e in errs {
                    summary.cache_errors.push(e.clone());
                    report.add("cache_error", url, &e);
                }
                match outcome {
                    Outcome::Cached => { summary.cached += 1; any_success = true; consecutive = 0; }
                    Outcome::Fetched(bytes) => {
                        summary.fetched += 1; any_success = true; consecutive = 0;
                        if let Some(b) = bytes { summary.in_memory.insert(url.clone(), b); }
                    }
                    Outcome::Failed(detail) => {
                        summary.failed += 1; consecutive += 1;
                        summary.failures.insert(url.clone(), detail.clone());
                        if opts.max_consecutive_failures > 0 && !any_success && consecutive >= opts.max_consecutive_failures && breaker.is_none() {
                            stop.store(true, Ordering::Relaxed);
                            breaker = Some(format!(
                                "{consecutive} consecutive boundary fetches failed with zero successes; this looks like a systematic problem (server unreachable, wrong token, or a bad base URL), not a handful of bad boundary URLs. Last failure: {detail}. Pass --max-consecutive-failures 0 to disable this check."
                            ));
                        }
                    }
                }
                cursor += 1;
                if total >= PROGRESS_MIN_ITEMS && (cursor % interval == 0 || cursor == total) {
                    eprintln!("resolved {cursor}/{total} boundaries ({} failed)", summary.failed);
                }
            }
            if breaker.is_some() { break; }
        }
    });

    eprintln!("boundaries: {} cached, {} fetched, {} failed", summary.cached, summary.fetched, summary.failed);
    match breaker {
        Some(msg) => Err(KilnError::Environment(msg)),
        None => Ok(summary),
    }
}
```

Note the breaker: after `stop` is set, the scope still joins running workers (they finish their current request) and the loop drains what arrived; outcomes past the trip are not applied. Verify the test still terminates in bounded time (workers check `stop` before each new item).

- [x] **Step 3: Run** `cargo test extract::boundary` (5 passed).

- [x] **Step 4: Commit** `git commit -m "Fetch boundary attachments on a worker pool with a circuit breaker"`

---

> Note from the Task 5 review: with `--retries 3` and a 300 s timeout, a dead server costs about 15 minutes per boundary attempt cycle, so at concurrency 8 the default breaker of 50 consecutive failures can take over an hour to trip. The breaker guards wasted work, not latency; an operator who wants fast failure lowers `--timeout` or `--max-consecutive-failures`. Task 9 should say this in the README.

## Task 6: Merge

**Files:** `src/snapshot/merge.rs`

- [x] **Step 1: Tests**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::extract::page::PageNote;
    use crate::report::Report;
    use std::collections::HashMap;

    fn line(id: &str, updated: &str, boundary_url: Option<&str>) -> String {
        let mut r = serde_json::json!({"resourceType":"Location","id":id,"meta":{"lastUpdated":updated}});
        if let Some(u) = boundary_url { r["extension"] = serde_json::json!([{"url":"https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson","valueAttachment":{"url":u}}]); }
        r.to_string()
    }
    fn note(id: &str, updated: &str, url: Option<&str>) -> PageNote {
        PageNote { id: id.into(), last_updated: Some(updated.into()), boundary_url: url.map(str::to_string) }
    }

    #[test]
    fn upserts_inlines_and_advances_the_watermark() {
        let dir = tempfile::tempdir().unwrap();
        let snap = Snapshot::new(dir.path());
        std::fs::write(snap.locations(), format!("{}\n{}\n", line("a", "2026-01-01T00:00:00Z", None), line("b", "2026-01-02T00:00:00Z", None))).unwrap();
        std::fs::write(snap.incoming(), format!("{}\n{}\n", line("b", "2026-01-03T00:00:00Z", Some("https://x/b")), line("c", "2026-01-04T00:00:00Z", None))).unwrap();
        let notes = vec![note("b", "2026-01-03T00:00:00Z", Some("https://x/b")), note("c", "2026-01-04T00:00:00Z", None)];
        let mut bytes = HashMap::new();
        bytes.insert("https://x/b".to_string(), b"{\"type\":\"Point\",\"coordinates\":[1,2]}".to_vec());
        let lookup = |u: &str| bytes.get(u).cloned();
        let mut report = Report::default();
        let stats = merge(&snap, &notes, false, &lookup, &HashMap::new(), &mut report).unwrap();
        assert_eq!((stats.total, stats.added, stats.updated), (3, 1, 1));
        assert_eq!(stats.watermark.as_deref(), Some("2026-01-04T00:00:00Z"));
        let text = std::fs::read_to_string(snap.locations()).unwrap();
        let ids: Vec<&str> = text.lines().map(|l| serde_json::from_str::<serde_json::Value>(l).unwrap()["id"].as_str().unwrap().to_string().leak() as &str).collect();
        assert_eq!(ids, vec!["a", "b", "c"]);
        let b: serde_json::Value = serde_json::from_str(text.lines().nth(1).unwrap()).unwrap();
        let att = &b["extension"][0]["valueAttachment"];
        assert!(att.get("url").is_none());
        assert_eq!(att["contentType"], "application/geo+json");
        assert!(att["data"].as_str().unwrap().len() > 10);
        assert!(!snap.locations_tmp().exists());
        assert!(!snap.incoming().exists());
    }

    #[test]
    fn full_ignores_the_old_snapshot_and_a_failed_boundary_is_reported() {
        let dir = tempfile::tempdir().unwrap();
        let snap = Snapshot::new(dir.path());
        std::fs::write(snap.locations(), format!("{}\n", line("old", "2026-01-01T00:00:00Z", None))).unwrap();
        std::fs::write(snap.incoming(), format!("{}\n", line("n", "2026-01-02T00:00:00Z", Some("https://x/n")))).unwrap();
        let notes = vec![note("n", "2026-01-02T00:00:00Z", Some("https://x/n"))];
        let mut failures = HashMap::new();
        failures.insert("https://x/n".to_string(), "HTTP 404".to_string());
        let mut report = Report::default();
        let stats = merge(&snap, &notes, true, &|_| None, &failures, &mut report).unwrap();
        assert_eq!((stats.total, stats.added, stats.updated), (1, 1, 0));
        assert_eq!(report.count("boundary_fetch_failed"), 1);
        let text = std::fs::read_to_string(snap.locations()).unwrap();
        assert!(!text.contains("\"old\""));
        assert!(text.contains("https://x/n"), "attachment left as a url");
    }

    #[test]
    fn duplicate_incoming_ids_keep_the_last_and_unparsed_old_lines_are_copied() {
        let dir = tempfile::tempdir().unwrap();
        let snap = Snapshot::new(dir.path());
        std::fs::write(snap.locations(), "not json\n").unwrap();
        std::fs::write(snap.incoming(), format!("{}\n{}\n", line("d", "2026-01-01T00:00:00Z", None), line("d", "2026-01-05T00:00:00Z", None))).unwrap();
        let notes = vec![note("d", "2026-01-01T00:00:00Z", None), note("d", "2026-01-05T00:00:00Z", None)];
        let mut report = Report::default();
        let stats = merge(&snap, &notes, false, &|_| None, &HashMap::new(), &mut report).unwrap();
        assert_eq!(stats.total, 2, "the unparsed line plus d");
        assert_eq!(report.count("snapshot_line_unparsed"), 1);
        let text = std::fs::read_to_string(snap.locations()).unwrap();
        assert_eq!(text.matches("\"id\":\"d\"").count(), 1);
        assert!(text.contains("2026-01-05"));
    }
}
```

- [x] **Step 2: Implement**

```rust
//! Phase three: stream the old snapshot and the incoming file into a new
//! locations.ndjson, inlining boundary bytes and computing the watermark.

use std::collections::{HashMap, HashSet};
use std::io::Write;

use base64::Engine;
use serde_json::Value;

use crate::error::{KilnError, Result};
use crate::extract::page::PageNote;
use crate::fhir::location::{BOUNDARY_EXTENSION_URLS, GEOJSON_CONTENT_TYPE};
use crate::fhir::ndjson::NdjsonReader;
use crate::report::Report;
use crate::snapshot::instant::later;
use crate::snapshot::Snapshot;

pub struct MergeStats {
    pub total: usize,
    pub added: usize,
    pub updated: usize,
    pub watermark: Option<String>,
}

fn inline_boundary(resource: &mut Value, url: &str, bytes: &[u8]) -> bool {
    let Some(exts) = resource.get_mut("extension").and_then(Value::as_array_mut) else { return false };
    for ext in exts.iter_mut() {
        let is_boundary = ext.get("url").and_then(Value::as_str).is_some_and(|u| BOUNDARY_EXTENSION_URLS.contains(&u));
        if !is_boundary { continue; }
        let Some(att) = ext.get_mut("valueAttachment").and_then(Value::as_object_mut) else { continue };
        if att.get("url").and_then(Value::as_str) != Some(url) { continue; }
        att.remove("url");
        att.insert("data".into(), Value::String(base64::engine::general_purpose::STANDARD.encode(bytes)));
        att.entry("contentType").or_insert_with(|| Value::String(GEOJSON_CONTENT_TYPE.into()));
        return true;
    }
    false
}

pub fn merge(
    snap: &Snapshot,
    notes: &[PageNote],
    full: bool,
    lookup: &dyn Fn(&str) -> Option<Vec<u8>>,
    failures: &HashMap<String, String>,
    report: &mut Report,
) -> Result<MergeStats> {
    let incoming_ids: HashSet<&str> = notes.iter().map(|n| n.id.as_str()).collect();
    // Last occurrence of each id in the incoming file wins.
    let mut last_index: HashMap<&str, usize> = HashMap::new();
    for (i, n) in notes.iter().enumerate() { last_index.insert(&n.id, i); }

    let tmp = snap.locations_tmp();
    let mut out = std::io::BufWriter::new(std::fs::File::create(&tmp).map_err(|e| KilnError::io(&tmp, e))?);
    let mut stats = MergeStats { total: 0, added: 0, updated: 0, watermark: None };
    let mut old_ids: HashSet<String> = HashSet::new();

    if !full && snap.locations().exists() {
        for line in NdjsonReader::open(&snap.locations())? {
            let line = line?;
            let id = serde_json::from_str::<Value>(&line.text).ok().and_then(|v| v.get("id").and_then(Value::as_str).map(str::to_string));
            match id {
                Some(id) if incoming_ids.contains(id.as_str()) => { old_ids.insert(id); continue; }
                Some(id) => {
                    if let Some(u) = serde_json::from_str::<Value>(&line.text).ok().and_then(|v| v.pointer("/meta/lastUpdated").and_then(Value::as_str).map(str::to_string)) {
                        stats.watermark = later(stats.watermark.as_deref(), &u);
                    }
                    old_ids.insert(id);
                }
                None => report.add("snapshot_line_unparsed", &format!("line {}", line.number), "not a JSON object with an id; copied unchanged"),
            }
            writeln!(out, "{}", line.text).map_err(|e| KilnError::io(&tmp, e))?;
            stats.total += 1;
        }
    }

    for (i, line) in NdjsonReader::open(&snap.incoming())?.enumerate() {
        let line = line?;
        let note = &notes[i];
        if last_index.get(note.id.as_str()) != Some(&i) { continue; }
        let mut resource: Value = serde_json::from_str(&line.text)?;
        if let Some(url) = &note.boundary_url {
            match lookup(url) {
                Some(bytes) => { inline_boundary(&mut resource, url, &bytes); }
                None => report.add("boundary_fetch_failed", &note.id, &format!("{url}: {}", failures.get(url).map(String::as_str).unwrap_or("not fetched"))),
            }
        }
        if let Some(u) = &note.last_updated { stats.watermark = later(stats.watermark.as_deref(), u); }
        serde_json::to_writer(&mut out, &resource)?;
        out.write_all(b"\n").map_err(|e| KilnError::io(&tmp, e))?;
        stats.total += 1;
        if old_ids.contains(&note.id) { stats.updated += 1 } else { stats.added += 1 }
    }
    out.flush().map_err(|e| KilnError::io(&tmp, e))?;
    drop(out);
    std::fs::rename(&tmp, snap.locations()).map_err(|e| KilnError::io(&tmp, e))?;
    let _ = std::fs::remove_file(snap.incoming());
    Ok(stats)
}
```

The incoming file and `notes` are index-aligned because the pager wrote one line per note; the `enumerate` relies on `NdjsonReader` skipping nothing (the pager never writes blank lines). If the counts differ, return an environment error ("incoming file and page notes disagree; rerun extract").

- [x] **Step 3: Run** `cargo test snapshot::merge` (3 passed).

- [x] **Step 4: Commit** `git commit -m "Merge the incoming file into the snapshot with boundary inlining"`

---

## Task 7: run_extract and the extract command

**Files:** `src/extract/mod.rs`, `src/main.rs`, `tests/extract.rs`

- [x] **Step 1: Integration tests (tests/extract.rs)**

A helper builds a fake server with a `/fhir/Location` search that honours `_lastUpdated` (return a subset when the query contains `ge`), and boundary endpoints. Tests, each with its own `Server::run()` and `tempdir`:

```rust
use std::process::Command;
use assert_cmd::prelude::*;
use httptest::{matchers::*, responders::*, Expectation, Server};

fn loc(id: &str, updated: &str, boundary: Option<&str>) -> serde_json::Value { /* as in page.rs tests */ }
fn bundle(entries: Vec<serde_json::Value>) -> String { /* no next link */ }

fn kiln() -> Command { Command::cargo_bin("kiln").unwrap() }

#[test]
fn full_extract_writes_snapshot_state_and_inlines_boundaries() { /* 2 locations, one with a boundary URL served by the same server; assert locations.ndjson has 2 lines, the boundary is inlined, state.json watermark = max lastUpdated, server recorded, boundaries/ has 2 files, stdout/stderr contain "boundaries: 0 cached, 1 fetched, 0 failed" and "snapshot: 2 resources, 2 new, 0 updated" */ }

#[test]
fn second_run_is_incremental_and_uses_the_cache() { /* first run full; second run: server expects a request whose query contains _lastUpdated=ge<watermark> and returns an empty bundle; assert file byte-identical, "0 new, 0 updated", state count unchanged */ }

#[test]
fn incremental_run_upserts_and_advances_the_watermark() { /* server: first request (no ge) returns a,b; request with ge returns b' (newer, new boundary URL) and c; run twice; assert 3 lines, b updated (new boundary inlined), watermark advanced, "1 new, 1 updated" */ }

#[test]
fn full_flag_replaces_the_snapshot() { /* after a run with a,b, run --full against a server returning only a; assert 1 line */ }

#[test]
fn snapshot_without_state_is_treated_as_full_unless_since() { /* pre-create locations.ndjson only; run without --since: server must receive a request WITHOUT _lastUpdated; run with --since T: request WITH ge T */ }

#[test]
fn server_mismatch_is_a_usage_error_and_full_repoints() { /* run against server A; run against server B -> exit 2, stderr mentions state.json; run --full against B -> success */ }

#[test]
fn circuit_breaker_aborts_and_keeps_the_old_snapshot() { /* run once ok; second run --full with 60 locations whose boundaries all 500, --retries 1 --max-consecutive-failures 5 -> exit 1, stderr contains "consecutive"; old locations.ndjson unchanged; .incoming.ndjson may exist */ }

#[test]
fn no_cache_and_refresh_conflict_is_usage() { /* --no-cache --refresh -> exit 2 */ }

#[test]
fn extract_report_is_written() { /* a bundle entry without id -> _extract_report.json counts page_resource_skipped 1 */ }
```

Write these out in full; each is 15-30 lines. Use `.times(..)` on expectations that may be hit a variable number of times and `.times(1)` where the count is the assertion (for example, that the cached boundary is never requested on the second run: register the boundary expectation with `.times(1)` and run twice).

- [x] **Step 2: Implement src/extract/mod.rs**

```rust
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

// The request timeout is a total per-request deadline (reqwest's blocking builder has no per-read timeout); a large boundary over a slow link can legitimately take minutes, hence the 300 s default on --timeout.

pub fn run_extract(args: &ExtractArgs) -> Result<()> {
    if args.no_cache && (args.refresh || args.cache_dir.is_some()) {
        return Err(KilnError::Usage("--no-cache cannot be combined with --refresh or --cache-dir".into()));
    }
    if args.concurrency == 0 {
        return Err(KilnError::Usage("--concurrency must be at least 1".into()));
    }
    if let Some(s) = &args.since {
        if parse_instant(s).is_none() {
            return Err(KilnError::Usage(format!("--since {s}: not a FHIR instant (e.g. 2026-01-02T03:04:05Z)")));
        }
    }
    let snap = Snapshot::new(&args.snapshot);
    std::fs::create_dir_all(&snap.dir).map_err(|e| KilnError::io(&snap.dir, e))?;

    // Mode: full, or incremental from the watermark / --since.
    let state = State::read(&snap.state_path())?;
    let since: Option<String> = if args.full {
        None
    } else if let Some(s) = &args.since {
        Some(s.clone())
    } else if let Some(st) = &state {
        if !same_server(&st.server, &args.server) {
            return Err(KilnError::Usage(format!(
                "snapshot was extracted from {} but --server is {}; pass --full to repoint it",
                st.server, args.server
            )));
        }
        st.watermark.clone()
    } else {
        None
    };
    let full = since.is_none();
    eprintln!("{}", match &since { Some(s) => format!("incremental extract since {s}"), None => "full extract".to_string() });

    let client = FhirClient::new(args.token.clone(), args.retries, Duration::from_secs(args.timeout))?;
    let mut report = Report::default();

    let paged = page_locations(&client, &args.server, since.as_deref(), &snap.incoming(), &mut report)?;

    let mut urls: Vec<String> = paged.notes.iter().filter_map(|n| n.boundary_url.clone()).collect();
    urls.sort();
    urls.dedup();
    let cache = if args.no_cache { None } else { Some(Cache::new(&args.cache_dir.clone().unwrap_or_else(|| snap.boundaries()))) };
    let opts = FetchOptions { concurrency: args.concurrency, cache: cache.clone(), refresh: args.refresh, max_consecutive_failures: args.max_consecutive_failures };
    let summary = fetch_boundaries(&client, &urls, &opts, &mut report)?;

    let in_memory: HashMap<String, Vec<u8>> = summary.in_memory;
    let lookup = |url: &str| -> Option<Vec<u8>> {
        if let Some(b) = in_memory.get(url) { return Some(b.clone()); }
        cache.as_ref().and_then(|c| c.read(url).ok().flatten())
    };
    let stats = merge(&snap, &paged.notes, full, &lookup, &summary.failures, &mut report)?;

    let new_state = State {
        server: args.server.trim_end_matches('/').to_string(),
        watermark: stats.watermark.clone(),
        count: stats.total,
        kiln_version: env!("CARGO_PKG_VERSION").to_string(),
        completed_at: format_utc(std::time::SystemTime::now()),
    };
    new_state.write(&snap.state_path())?;
    write_report(&snap.report_path(), &report)?;

    println!("snapshot: {} resources, {} new, {} updated, watermark {}", stats.total, stats.added, stats.updated, stats.watermark.as_deref().unwrap_or("none"));
    println!("{}", report.summary());
    Ok(())
}
```

Wire `Command::Extract(args) => extract::run_extract(&args)` in main.rs.

- [x] **Step 3: Run** `cargo test --test extract` (9 passed), full suite, `cargo clippy --all-targets -- -D warnings`.

- [x] **Step 4: Commit** `git commit -m "Add kiln extract: incremental snapshot from any FHIR R4 server"`

---

## Task 8: kiln run

**Files:** `src/run.rs`, `src/main.rs`, `tests/extract.rs`

- [ ] **Step 1: Tests (append to tests/extract.rs)**

```rust
#[test]
fn run_extracts_then_transforms() { /* server with 2 locations (one with position, one with boundary); kiln run --server .. --snapshot S --out O; assert O/locations/**/*.parquet exists and stdout has both "snapshot:" and "Wrote 2 rows" */ }

#[test]
fn run_skips_transform_when_extract_fails() { /* server returning 401 on the search; exit 1; O has no locations dir */ }
```

- [ ] **Step 2: Implement src/run.rs**

```rust
//! `kiln run`: extract then transform, with the snapshot as the handoff.

use crate::cli::RunArgs;
use crate::error::Result;
use crate::extract::run_extract;
use crate::transform::run_transform;

pub fn run(args: &RunArgs) -> Result<()> {
    run_extract(&args.extract)?;
    run_transform(&args.transform_args())
}
```

Wire `Command::Run(args) => run::run(&args)`.

- [ ] **Step 3: Run** all tests, clippy, fmt. **Commit** `git commit -m "Add kiln run: extract then transform"`

---

## Task 9: README and plan bookkeeping

- [ ] **Step 1:** README Status block: `extract` and `run` implemented; `diff` and `load` in progress; Python still provides `bake`, `bake-points`, `load`. Repository layout: `snapshot/` and `extract/` no longer "(in progress)"; add `run.rs`. "The snapshot" section: mention `_extract_report.json` and the `ge` watermark semantics; add the four new report kinds to the report list. "Commands" block: extract flags as implemented.
- [ ] **Step 2:** `cargo test`, commit `git commit -m "README: extract and run are implemented"`.

---

## Self-review

**Spec coverage.** CLI flags: Task 0. Snapshot layout and state: Task 1. Page phase: Task 4. Fetch phase with cache, retry, breaker, progress, Binary: Tasks 2, 3, 5. Merge with inlining, duplicates, watermark, unparsed lines: Task 6. Mode decision, server check, `--since`, Python-made snapshot, report file, summaries: Task 7. `run`: Task 8. Errors: Usage in Task 7 checks and `State::read`; Environment variant added in Task 4. Tests: the spec's eleven cases map to Task 7 tests 1-9 and Task 8 tests 1-2; unit tests in Tasks 1-6.

**Placeholder scan.** Task 7 step 1 lists tests as commented intents; the implementer writes them out in full using the helpers shown in Task 4's tests. Everything else carries code.

**Type consistency.** `FhirClient::new(Option<String>, usize, Duration)`, `get(&str) -> Result<Fetched, FetchError>`; `Cache::new(&Path)`, `read(&str) -> Result<Option<Vec<u8>>, String>`, `write(&str, &[u8]) -> Result<(), String>`; `page_locations(&FhirClient, &str, Option<&str>, &Path, &mut Report) -> Result<PageResult { notes: Vec<PageNote>, pages }>`; `fetch_boundaries(&FhirClient, &[String], &FetchOptions, &mut Report) -> Result<FetchSummary { cached, fetched, failed, failures, in_memory, cache_errors }>`; `merge(&Snapshot, &[PageNote], bool, &dyn Fn(&str) -> Option<Vec<u8>>, &HashMap<String,String>, &mut Report) -> Result<MergeStats>`; `State { server, watermark, count, kiln_version, completed_at }` with `read`/`write`; `KilnError::Environment(String)` added in Task 4 and used by Tasks 4, 5.
