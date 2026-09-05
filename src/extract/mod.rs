//! FHIR extraction: the shared HTTP client, the page/boundary fetchers built
//! on it, and the cache that keeps repeat fetches off the network.

pub mod boundary;
pub mod cache;
pub mod client;
pub mod page;
