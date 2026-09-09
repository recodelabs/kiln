//! Where the raster comes from: a local file, or an http(s) URL fetched once
//! into `SNAPSHOT/rasters/` through the same content-addressed `Cache` the
//! boundary fetch uses (sha256 of the URL; bytes verified on read), so
//! re-runs and other years never re-download a country raster.

use std::path::Path;
use std::time::Duration;

use crate::error::{KilnError, Result};
use crate::extract::cache::Cache;
use crate::extract::client::FhirClient;

#[derive(Debug)]
pub struct RasterBytes {
    pub bytes: Vec<u8>,
    /// File name or last URL segment: for error messages and provenance text.
    pub label: String,
    /// Served from the cache rather than fetched or read.
    pub cached: bool,
}

pub fn is_url(source: &str) -> bool {
    source.starts_with("http://") || source.starts_with("https://")
}

pub fn label_of(source: &str) -> String {
    source
        .rsplit(['/', '\\'])
        .next()
        .filter(|s| !s.is_empty())
        .unwrap_or(source)
        .to_string()
}

/// Read `source` into memory. A URL goes through `cache_dir`; cache faults
/// are printed and treated as a miss, never as data.
pub fn load(
    source: &str,
    cache_dir: &Path,
    retries: usize,
    timeout: Duration,
) -> Result<RasterBytes> {
    let label = label_of(source);
    if !is_url(source) {
        let path = Path::new(source);
        let bytes = std::fs::read(path).map_err(|e| KilnError::io(path, e))?;
        return Ok(RasterBytes {
            bytes,
            label,
            cached: false,
        });
    }
    let cache = Cache::new(cache_dir);
    match cache.read(source) {
        Ok(Some(bytes)) => {
            return Ok(RasterBytes {
                bytes,
                label,
                cached: true,
            })
        }
        Ok(None) => {}
        Err(e) => eprintln!("kiln: raster cache: {e}; fetching again"),
    }
    let client = FhirClient::new(None, retries, timeout)?;
    let fetched = client
        .get(source)
        .map_err(|e| KilnError::Environment(format!("{source}: {e}")))?;
    if let Err(e) = cache.write(source, &fetched.body) {
        eprintln!("kiln: raster cache: {e}; continuing without caching");
    }
    Ok(RasterBytes {
        bytes: fetched.body,
        label,
        cached: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::raster::geotiff::FIXTURE;
    use httptest::{matchers::*, responders::*, Expectation, Server};

    #[test]
    fn labels_are_the_last_path_segment() {
        assert_eq!(
            label_of("https://h/x/nga_pop_2026_CN_100m_cog.tif"),
            "nga_pop_2026_CN_100m_cog.tif"
        );
        assert_eq!(label_of("/data/a.tif"), "a.tif");
        assert_eq!(label_of("a.tif"), "a.tif");
        assert!(is_url("https://h/a.tif") && is_url("http://h/a.tif") && !is_url("/h/a.tif"));
    }

    #[test]
    fn a_local_file_is_read_directly() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("pop.tif");
        std::fs::write(&path, FIXTURE).unwrap();
        let got = load(
            path.to_str().unwrap(),
            &dir.path().join("cache"),
            1,
            Duration::from_secs(5),
        )
        .unwrap();
        assert_eq!(got.bytes, FIXTURE);
        assert_eq!(got.label, "pop.tif");
        assert!(!got.cached);
        assert!(
            !dir.path().join("cache").exists(),
            "no cache for local files"
        );
    }

    #[test]
    fn a_url_is_fetched_once_then_served_from_the_cache() {
        let server = Server::run();
        server.expect(
            Expectation::matching(request::method_path("GET", "/nga.tif"))
                .times(1)
                .respond_with(status_code(200).body(FIXTURE.to_vec())),
        );
        let url = server.url("/nga.tif").to_string();
        let dir = tempfile::tempdir().unwrap();
        let cache = dir.path().join("rasters");

        let first = load(&url, &cache, 1, Duration::from_secs(5)).unwrap();
        assert_eq!(first.bytes, FIXTURE);
        assert_eq!(first.label, "nga.tif");
        assert!(!first.cached);
        assert_eq!(
            std::fs::read_dir(&cache).unwrap().count(),
            2,
            ".bin and .meta.json"
        );

        let second = load(&url, &cache, 1, Duration::from_secs(5)).unwrap();
        assert_eq!(second.bytes, FIXTURE);
        assert!(second.cached);
    }

    #[test]
    fn a_missing_url_is_an_environment_error() {
        let server = Server::run();
        server.expect(
            Expectation::matching(request::method_path("GET", "/none.tif"))
                .respond_with(status_code(404)),
        );
        let err = load(
            &server.url("/none.tif").to_string(),
            &tempfile::tempdir().unwrap().path().join("c"),
            1,
            Duration::from_secs(5),
        )
        .unwrap_err();
        assert_eq!(err.exit_code(), 1);
        assert!(err.to_string().contains("404"), "{err}");
    }

    #[test]
    fn a_missing_local_file_is_an_io_error_naming_the_path() {
        let err = load(
            "/definitely/not/here.tif",
            Path::new("/unused"),
            1,
            Duration::from_secs(5),
        )
        .unwrap_err();
        assert!(
            err.to_string().contains("/definitely/not/here.tif"),
            "{err}"
        );
    }
}
