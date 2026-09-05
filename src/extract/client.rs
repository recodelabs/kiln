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
const ERROR_BODY_CAP: usize = 500;

#[derive(Debug)]
pub enum FetchError {
    /// Final status after retries; `body` is at most ERROR_BODY_CAP chars of the response.
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

#[derive(Debug)]
pub struct Fetched {
    pub body: Vec<u8>,
}

pub struct FhirClient {
    http: Client,
    retries: usize,
}

pub fn backoff_delay(attempt: usize, retry_after: Option<f64>) -> Duration {
    let secs =
        retry_after.unwrap_or_else(|| RETRY_BASE * 2f64.powi(attempt.saturating_sub(1) as i32));
    Duration::from_secs_f64(secs.clamp(0.0, RETRY_MAX))
}

pub fn parse_retry_after(value: Option<&str>) -> Option<f64> {
    value?.trim().parse::<f64>().ok().filter(|v| v.is_finite())
}

fn cap(body: String) -> String {
    body.chars().take(ERROR_BODY_CAP).collect()
}

impl FhirClient {
    pub fn new(token: Option<String>, retries: usize, timeout: Duration) -> Result<Self> {
        let mut headers = HeaderMap::new();
        headers.insert(ACCEPT, HeaderValue::from_static("application/fhir+json"));
        if let Some(t) = token {
            let mut v = HeaderValue::from_str(&format!("Bearer {t}")).map_err(|_| {
                KilnError::Usage("token contains characters that are not valid in a header".into())
            })?;
            v.set_sensitive(true);
            headers.insert(AUTHORIZATION, v);
        }
        let http = Client::builder()
            .default_headers(headers)
            .timeout(timeout)
            .user_agent(concat!("kiln/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|e| KilnError::Usage(format!("cannot build HTTP client: {e}")))?;
        Ok(Self {
            http,
            retries: retries.max(1),
        })
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
                        let ra = parse_retry_after(
                            resp.headers()
                                .get("retry-after")
                                .and_then(|v| v.to_str().ok()),
                        );
                        last = Some(FetchError::Status {
                            status,
                            body: cap(resp.text().unwrap_or_default()),
                        });
                        if attempt < self.retries {
                            std::thread::sleep(backoff_delay(attempt, ra));
                        }
                    } else if status != 200 {
                        return Err(FetchError::Status {
                            status,
                            body: cap(resp.text().unwrap_or_default()),
                        });
                    } else {
                        return match resp.bytes() {
                            Ok(b) => Ok(Fetched { body: b.to_vec() }),
                            Err(e) => {
                                Err(FetchError::Transport(format!("{url}: reading body: {e}")))
                            }
                        };
                    }
                }
            }
        }
        Err(last.unwrap_or_else(|| FetchError::Transport(format!("{url}: no attempts made"))))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use httptest::{matchers::*, responders::*, Expectation, Server};

    // reqwest is built with `rustls-no-provider`; main.rs installs this for
    // the real binary, but tests need it too, and only once per process.
    fn ensure_crypto_provider() {
        let _ = rustls::crypto::ring::default_provider().install_default();
    }

    fn client(retries: usize) -> FhirClient {
        ensure_crypto_provider();
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
        assert_eq!(
            parse_retry_after(Some("Wed, 21 Oct 2026 07:28:00 GMT")),
            None
        );
        assert_eq!(parse_retry_after(None), None);
    }

    #[test]
    fn a_500_then_200_succeeds_with_retries() {
        let server = Server::run();
        server.expect(
            Expectation::matching(request::method_path("GET", "/b"))
                .times(2)
                .respond_with(cycle![status_code(500), status_code(200).body("ok")]),
        );
        let got = client(3).get(&server.url("/b").to_string()).unwrap();
        assert_eq!(got.body, b"ok");
    }

    #[test]
    fn a_429_honours_retry_after() {
        let server = Server::run();
        server.expect(
            Expectation::matching(request::method_path("GET", "/r"))
                .times(2)
                .respond_with(cycle![
                    status_code(429).append_header("Retry-After", "1"),
                    status_code(200).body("ok")
                ]),
        );
        let t = std::time::Instant::now();
        client(3).get(&server.url("/r").to_string()).unwrap();
        assert!(t.elapsed() >= std::time::Duration::from_secs(1));
    }

    #[test]
    fn a_404_is_not_retried() {
        let server = Server::run();
        server.expect(
            Expectation::matching(request::method_path("GET", "/m"))
                .times(1)
                .respond_with(status_code(404)),
        );
        let err = client(3).get(&server.url("/m").to_string()).unwrap_err();
        assert!(
            matches!(err, FetchError::Status { status: 404, .. }),
            "{err:?}"
        );
    }

    #[test]
    fn exhausted_retries_report_the_last_status() {
        let server = Server::run();
        server.expect(
            Expectation::matching(request::method_path("GET", "/x"))
                .times(2)
                .respond_with(status_code(503)),
        );
        let err = client(2).get(&server.url("/x").to_string()).unwrap_err();
        assert!(matches!(err, FetchError::Status { status: 503, .. }));
    }

    #[test]
    fn connection_refused_is_a_transport_error() {
        let err = client(1).get("http://127.0.0.1:9/nothing").unwrap_err();
        assert!(matches!(err, FetchError::Transport(_)), "{err:?}");
    }

    #[test]
    fn bearer_and_accept_headers_are_sent() {
        ensure_crypto_provider();
        let server = Server::run();
        server.expect(
            Expectation::matching(all_of![
                request::method_path("GET", "/t"),
                request::headers(contains(("authorization", "Bearer secret"))),
                request::headers(contains(("accept", "application/fhir+json"))),
            ])
            .respond_with(status_code(200).body("ok")),
        );
        FhirClient::new(Some("secret".into()), 1, std::time::Duration::from_secs(5))
            .unwrap()
            .get(&server.url("/t").to_string())
            .unwrap();
    }

    #[test]
    fn a_body_larger_than_the_cap_is_truncated_in_the_error() {
        let server = Server::run();
        server.expect(
            Expectation::matching(request::method_path("GET", "/big"))
                .respond_with(status_code(400).body("x".repeat(2000))),
        );
        let err = client(1).get(&server.url("/big").to_string()).unwrap_err();
        match err {
            FetchError::Status { body, .. } => assert_eq!(body.len(), 500),
            other => panic!("{other:?}"),
        }
    }
}
