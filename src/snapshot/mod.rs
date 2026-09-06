//! The snapshot directory: locations.ndjson, state.json, the boundary cache,
//! and the in-progress incoming file. See README "The snapshot".

pub mod instant;
pub mod merge;

use std::io::Write;
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
        Self {
            dir: dir.to_path_buf(),
        }
    }
    pub fn locations(&self) -> PathBuf {
        self.dir.join(LOCATIONS_FILE)
    }
    pub fn locations_tmp(&self) -> PathBuf {
        self.dir.join(format!("{LOCATIONS_FILE}.tmp"))
    }
    pub fn state_path(&self) -> PathBuf {
        self.dir.join(STATE_FILE)
    }
    pub fn incoming(&self) -> PathBuf {
        self.dir.join(INCOMING_FILE)
    }
    pub fn boundaries(&self) -> PathBuf {
        self.dir.join(BOUNDARIES_DIR)
    }
    pub fn report_path(&self) -> PathBuf {
        self.dir.join(EXTRACT_REPORT_FILE)
    }
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
            Ok(text) => serde_json::from_str(&text).map(Some).map_err(|e| {
                KilnError::Usage(format!("{}: not a valid state file: {e}", path.display()))
            }),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) if e.kind() == std::io::ErrorKind::InvalidData => Err(KilnError::Usage(
                format!("{}: not a valid state file: {e}", path.display()),
            )),
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => Err(KilnError::Usage(
                format!("{}: not readable: {e}", path.display()),
            )),
            Err(e) => Err(KilnError::io(path, e)),
        }
    }

    /// Atomic: write `state.json.tmp`, fsync it, then rename over `path`.
    pub fn write(&self, path: &Path) -> Result<()> {
        let tmp = path.with_extension("json.tmp");
        let text = serde_json::to_string_pretty(self)? + "\n";
        let result: Result<()> = (|| {
            let mut file = std::fs::File::create(&tmp).map_err(|e| KilnError::io(&tmp, e))?;
            file.write_all(text.as_bytes())
                .map_err(|e| KilnError::io(&tmp, e))?;
            file.sync_all().map_err(|e| KilnError::io(&tmp, e))
        })();
        if let Err(e) = result {
            let _ = std::fs::remove_file(&tmp);
            return Err(e);
        }
        if let Err(e) = std::fs::rename(&tmp, path) {
            let _ = std::fs::remove_file(&tmp);
            return Err(KilnError::io(path, e));
        }
        // Durability: fsync the directory whose entry (the state.json
        // rename) just changed, so that change survives a crash right
        // after this returns. Not every platform/filesystem supports
        // fsync on a directory; that is a best-effort improvement, not
        // something worth failing an otherwise-successful write over.
        if let Some(parent) = path.parent() {
            if let Ok(d) = std::fs::File::open(parent) {
                let _ = d.sync_all();
            }
        }
        Ok(())
    }
}

pub fn same_server(a: &str, b: &str) -> bool {
    a.trim_end_matches('/') == b.trim_end_matches('/')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_are_under_the_snapshot_dir() {
        let s = Snapshot::new(std::path::Path::new("/x"));
        assert_eq!(
            s.locations(),
            std::path::PathBuf::from("/x/locations.ndjson")
        );
        assert_eq!(s.state_path(), std::path::PathBuf::from("/x/state.json"));
        assert_eq!(
            s.incoming(),
            std::path::PathBuf::from("/x/.incoming.ndjson")
        );
        assert_eq!(s.boundaries(), std::path::PathBuf::from("/x/boundaries"));
        assert_eq!(
            s.report_path(),
            std::path::PathBuf::from("/x/_extract_report.json")
        );
    }

    #[test]
    fn state_round_trips_and_is_absent_when_missing() {
        let dir = tempfile::tempdir().unwrap();
        let s = Snapshot::new(dir.path());
        assert!(State::read(&s.state_path()).unwrap().is_none());
        let state = State {
            server: "https://f/fhir".into(),
            watermark: Some("2026-01-01T00:00:00Z".into()),
            count: 3,
            kiln_version: "0.2.0".into(),
            completed_at: "2026-01-01T00:00:01Z".into(),
        };
        state.write(&s.state_path()).unwrap();
        assert_eq!(State::read(&s.state_path()).unwrap().unwrap(), state);
        assert!(!dir.path().join("state.json.tmp").exists());
        assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 1);
        let text = std::fs::read_to_string(s.state_path()).unwrap();
        assert!(text.ends_with('\n'));
    }

    #[test]
    fn state_without_watermark_omits_the_key() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("state.json");
        State {
            server: "s".into(),
            watermark: None,
            count: 0,
            kiln_version: "v".into(),
            completed_at: "t".into(),
        }
        .write(&p)
        .unwrap();
        assert!(!std::fs::read_to_string(&p).unwrap().contains("watermark"));
        assert_eq!(State::read(&p).unwrap().unwrap().watermark, None);
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
