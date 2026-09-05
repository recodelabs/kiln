pub mod hierarchy;
pub mod hilbert;
pub mod partition;

use crate::geometry::GeometrySummary;

/// What pass one keeps per Location. Strings are the only variable-size parts.
#[derive(Debug, Clone, Default)]
pub struct IndexRecord {
    pub id: String,
    pub part_of: Option<String>,
    pub name: Option<String>,
    pub pcode: Option<String>,
    pub type_code: Option<String>,
    /// Byte offset and length of the resource's line in locations.ndjson.
    pub offset: u64,
    pub len: usize,
    pub geometry: Option<GeometrySummary>,
    pub position: Option<(f64, f64)>,
    /// Set during pass one after hierarchy and country resolution.
    pub country: String,
    pub tier: String,
    pub hilbert: u64,
}

pub use hierarchy::{resolve_hierarchy, Hierarchy, HierarchyInfo, NodeIdx, ADMIN_COLUMNS};
