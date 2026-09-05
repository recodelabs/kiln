//! Which columns a row can be partitioned by, and how a value becomes a
//! filesystem-safe, collision-free `key=value` directory segment.

use std::collections::HashMap;

use sha2::{Digest, Sha256};

use crate::error::{KilnError, Result};
use crate::index::IndexRecord;
use crate::report::Report;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PartitionKey {
    Country,
    GeomType,
    Tier,
    Type,
}

impl PartitionKey {
    pub fn name(self) -> &'static str {
        match self {
            PartitionKey::Country => "country",
            PartitionKey::GeomType => "geom_type",
            PartitionKey::Tier => "tier",
            PartitionKey::Type => "type",
        }
    }

    /// The row's value for this key, rendered as text. `null` for a missing value.
    pub fn value(self, rec: &IndexRecord) -> String {
        match self {
            PartitionKey::Country => rec.country.clone(),
            PartitionKey::GeomType => rec
                .geometry
                .map(|g| g.kind.as_str().to_string())
                .unwrap_or_else(|| "null".into()),
            PartitionKey::Tier => rec.tier.clone(),
            PartitionKey::Type => rec.type_code.clone().unwrap_or_else(|| "null".into()),
        }
    }
}

pub fn parse_keys(spec: &str) -> Result<Vec<PartitionKey>> {
    spec.split(',')
        .map(|k| match k.trim() {
            "country" => Ok(PartitionKey::Country),
            "geom_type" => Ok(PartitionKey::GeomType),
            "tier" => Ok(PartitionKey::Tier),
            "type" => Ok(PartitionKey::Type),
            other => Err(KilnError::Usage(format!(
                "--partition-by: unknown key {other:?}; available: country, geom_type, tier, type"
            ))),
        })
        .collect()
}

/// Segment claims within one parent directory: segment -> original value.
#[derive(Debug, Default)]
pub struct Claims(HashMap<String, String>);

/// Render one value as a directory segment (without the `key=` prefix).
/// A `/`, `\` or NUL is replaced; two distinct values that render the same
/// get a short hash suffix on the second, so neither silently overwrites
/// the other.
pub fn segment(key: &str, value: &str, claims: &mut Claims, report: &mut Report) -> String {
    let candidate: String = value
        .chars()
        .map(|c| if c == '/' || c == '\\' { '_' } else { c })
        .filter(|c| *c != '\0')
        .collect();
    let candidate = if candidate.is_empty() {
        "empty".to_string()
    } else {
        candidate
    };
    let sanitized = candidate != value;

    match claims.0.get(&candidate) {
        None => {
            claims.0.insert(candidate.clone(), value.to_string());
            if sanitized {
                report.add(
                    "partition_value_sanitized",
                    &format!("{key}={value}"),
                    &format!(
                        "contained a path separator or control character; written to the directory {key}={candidate:?} instead"
                    ),
                );
            }
            candidate
        }
        Some(owner) if owner == value => candidate,
        Some(_) => {
            let mut salt = String::new();
            loop {
                let digest = Sha256::digest(format!("{candidate}\0{value}{salt}").as_bytes());
                let seg = format!(
                    "{candidate}~{:02x}{:02x}{:02x}{:02x}",
                    digest[0], digest[1], digest[2], digest[3]
                );
                match claims.0.get(&seg) {
                    Some(owner) if owner != value => salt.push('#'),
                    _ => {
                        claims.0.insert(seg.clone(), value.to_string());
                        report.add(
                            "partition_value_collision",
                            &format!("{key}={value}"),
                            &format!(
                                "renders to the same directory segment {key}={candidate:?} as a different value already written under this partition; disambiguated to {key}={seg:?} instead"
                            ),
                        );
                        return seg;
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::report::Report;

    #[test]
    fn parses_and_validates_keys() {
        assert_eq!(
            parse_keys("country,geom_type").unwrap(),
            vec![PartitionKey::Country, PartitionKey::GeomType]
        );
        assert!(parse_keys("country,nope")
            .unwrap_err()
            .to_string()
            .contains("nope"));
    }

    #[test]
    fn slashes_are_sanitised_and_reported() {
        let mut report = Report::default();
        let mut claims = Claims::default();
        assert_eq!(segment("country", "A/B", &mut claims, &mut report), "A_B");
        assert_eq!(report.count("partition_value_sanitized"), 1);
    }

    #[test]
    fn colliding_values_get_distinct_segments() {
        let mut report = Report::default();
        let mut claims = Claims::default();
        let first = segment("country", "A/B", &mut claims, &mut report);
        let second = segment("country", "A_B", &mut claims, &mut report);
        assert_eq!(first, "A_B");
        assert_ne!(second, first);
        assert!(second.starts_with("A_B~"));
        assert_eq!(report.count("partition_value_collision"), 1);
        // Same value again reuses its segment.
        assert_eq!(segment("country", "A/B", &mut claims, &mut report), "A_B");
    }

    #[test]
    fn values_of_records_render_null_for_missing() {
        use crate::geometry::{GeomKind, GeometrySummary};
        use crate::index::IndexRecord;
        let rec = IndexRecord {
            country: "NG".into(),
            tier: "site".into(),
            type_code: None,
            geometry: Some(GeometrySummary {
                kind: GeomKind::Point,
                bbox: [0.0; 4],
            }),
            ..Default::default()
        };
        assert_eq!(PartitionKey::Country.value(&rec), "NG");
        assert_eq!(PartitionKey::GeomType.value(&rec), "point");
        assert_eq!(PartitionKey::Tier.value(&rec), "site");
        assert_eq!(PartitionKey::Type.value(&rec), "null");
    }
}
