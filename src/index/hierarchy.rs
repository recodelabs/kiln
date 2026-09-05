//! Resolve partOf chains into depth, path, ancestor list and admin columns.
//! A plain map walk with a chain cache, ported from the Python: at single
//! country scale it is fast, and it can say *which* id dangles and *what*
//! the cycle path was.

use std::collections::{HashMap, HashSet};

use crate::fhir::location::ADMIN_UNIT_TYPE;
use crate::index::IndexRecord;
use crate::report::Report;

pub const MAX_DEPTH: usize = 12;
pub const ADMIN_COLUMNS: usize = 5;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct HierarchyInfo {
    pub depth: i32,
    pub admin_level: Option<i32>,
    pub path: String,
    pub ancestor_ids: Vec<String>,
    pub admin_names: [Option<String>; ADMIN_COLUMNS],
    pub admin_codes: [Option<String>; ADMIN_COLUMNS],
    pub country: Option<String>,
}

enum WalkError {
    Cycle { path: Vec<String>, node: String },
    TooDeep { path: Vec<String> },
}

/// Ids from the root down to `id` inclusive.
fn chain<'a>(
    id: &str,
    by_id: &HashMap<&'a str, &'a IndexRecord>,
    cache: &mut HashMap<String, Vec<String>>,
) -> Result<Vec<String>, WalkError> {
    if let Some(c) = cache.get(id) {
        return Ok(c.clone());
    }
    let mut walked: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    let mut current = id.to_string();
    loop {
        if seen.contains(&current) {
            walked.push(current.clone());
            return Err(WalkError::Cycle {
                path: walked,
                node: current,
            });
        }
        seen.insert(current.clone());
        walked.push(current.clone());
        if walked.len() > MAX_DEPTH {
            return Err(WalkError::TooDeep { path: walked });
        }
        if let Some(cached) = cache.get(&current) {
            // Splice: cached is root..current; walked is id..current (child first).
            let mut complete = cached.clone();
            complete.extend(walked.iter().rev().skip(1).cloned());
            if complete.len() > MAX_DEPTH {
                return Err(WalkError::TooDeep { path: complete });
            }
            for i in 0..complete.len() {
                cache.insert(complete[i].clone(), complete[..=i].to_vec());
            }
            return Ok(complete);
        }
        let parent = by_id
            .get(current.as_str())
            .and_then(|r| r.part_of.as_deref());
        match parent {
            Some(p) if by_id.contains_key(p) => current = p.to_string(),
            _ => break, // root, or dangling parent already reported
        }
    }
    walked.reverse();
    for i in 0..walked.len() {
        cache.insert(walked[i].clone(), walked[..=i].to_vec());
    }
    Ok(walked)
}

pub fn resolve_hierarchy(
    records: &[IndexRecord],
    report: &mut Report,
) -> HashMap<String, HierarchyInfo> {
    let by_id: HashMap<&str, &IndexRecord> = records.iter().map(|r| (r.id.as_str(), r)).collect();
    let mut cache: HashMap<String, Vec<String>> = HashMap::new();
    let mut resolved = HashMap::with_capacity(records.len());

    for r in records {
        if let Some(p) = &r.part_of {
            if !by_id.contains_key(p.as_str()) {
                report.add(
                    "orphan",
                    &r.id,
                    &format!("partOf references missing id {p}"),
                );
            }
        }
    }

    for r in records {
        let chain = match chain(&r.id, &by_id, &mut cache) {
            Ok(c) => c,
            Err(WalkError::Cycle { path, node }) => {
                if node == r.id {
                    report.add("cycle", &r.id, &path.join(" -> "));
                } else {
                    report.add(
                        "unreachable_ancestor",
                        &r.id,
                        &format!("cycle involving {node}"),
                    );
                }
                continue;
            }
            Err(WalkError::TooDeep { path }) => {
                report.add(
                    "too_deep",
                    &r.id,
                    &format!("chain exceeds MAX_DEPTH={MAX_DEPTH}: {}", path.join(" -> ")),
                );
                continue;
            }
        };

        let admin_chain: Vec<&IndexRecord> = chain
            .iter()
            .map(|id| by_id[id.as_str()])
            .filter(|rec| rec.type_code.as_deref() == Some(ADMIN_UNIT_TYPE))
            .collect();
        let admin_level =
            (r.type_code.as_deref() == Some(ADMIN_UNIT_TYPE)).then(|| admin_chain.len() as i32 - 1);

        let mut admin_names: [Option<String>; ADMIN_COLUMNS] = Default::default();
        let mut admin_codes: [Option<String>; ADMIN_COLUMNS] = Default::default();
        for (i, anc) in admin_chain.iter().take(ADMIN_COLUMNS).enumerate() {
            admin_names[i] = anc.name.clone();
            admin_codes[i] = anc.pcode.clone();
        }
        let country = admin_codes[0].clone();

        resolved.insert(
            r.id.clone(),
            HierarchyInfo {
                depth: chain.len() as i32 - 1,
                admin_level,
                path: format!("/{}", chain.join("/")),
                ancestor_ids: chain[..chain.len() - 1].to_vec(),
                admin_names,
                admin_codes,
                country,
            },
        );
    }
    resolved
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::IndexRecord;
    use crate::report::Report;

    fn rec(id: &str, parent: Option<&str>, ty: &str, pcode: Option<&str>) -> IndexRecord {
        IndexRecord {
            id: id.into(),
            part_of: parent.map(str::to_string),
            name: Some(id.to_uppercase()),
            pcode: pcode.map(str::to_string),
            type_code: Some(ty.into()),
            ..Default::default()
        }
    }

    fn fixture() -> Vec<IndexRecord> {
        vec![
            rec("ng", None, "admin-unit", Some("NG")),
            rec("kano", Some("ng"), "admin-unit", Some("NG001")),
            rec("nassarawa", Some("kano"), "admin-unit", Some("NG001002")),
            rec("gama", Some("nassarawa"), "settlement", None),
            rec("clinic", Some("gama"), "facility", None),
            rec("orphan", Some("does-not-exist"), "facility", None),
            rec("cyc-a", Some("cyc-b"), "admin-unit", None),
            rec("cyc-b", Some("cyc-a"), "admin-unit", None),
            rec("victim", Some("cyc-a"), "facility", None),
        ]
    }

    #[test]
    fn walks_past_non_admin_ancestors() {
        let mut report = Report::default();
        let resolved = resolve_hierarchy(&fixture(), &mut report);
        let clinic = &resolved["clinic"];
        assert_eq!(clinic.depth, 4);
        assert_eq!(clinic.admin_level, None);
        assert_eq!(clinic.path, "/ng/kano/nassarawa/gama/clinic");
        assert_eq!(clinic.ancestor_ids, vec!["ng", "kano", "nassarawa", "gama"]);
        assert_eq!(clinic.admin_names[0].as_deref(), Some("NG"));
        assert_eq!(clinic.admin_names[2].as_deref(), Some("NASSARAWA"));
        assert_eq!(clinic.admin_codes[2].as_deref(), Some("NG001002"));
        assert_eq!(clinic.admin_names[3], None);
        assert_eq!(clinic.country.as_deref(), Some("NG"));
        assert_eq!(resolved["nassarawa"].admin_level, Some(2));
        assert_eq!(resolved["ng"].depth, 0);
    }

    #[test]
    fn dangling_parent_is_reported_once_and_treated_as_root() {
        let mut report = Report::default();
        let resolved = resolve_hierarchy(&fixture(), &mut report);
        assert_eq!(report.count("orphan"), 1);
        assert_eq!(resolved["orphan"].depth, 0);
        assert_eq!(resolved["orphan"].country, None);
    }

    #[test]
    fn cycles_are_reported_and_dropped_with_their_descendants() {
        let mut report = Report::default();
        let resolved = resolve_hierarchy(&fixture(), &mut report);
        assert!(!resolved.contains_key("cyc-a"));
        assert!(!resolved.contains_key("victim"));
        assert_eq!(report.count("cycle"), 2);
        assert_eq!(report.count("unreachable_ancestor"), 1);
    }

    #[test]
    fn too_deep_chains_are_reported() {
        let mut recs = vec![rec("n0", None, "admin-unit", Some("X"))];
        for i in 1..20 {
            recs.push(rec(
                &format!("n{i}"),
                Some(&format!("n{}", i - 1)),
                "admin-unit",
                None,
            ));
        }
        let mut report = Report::default();
        let resolved = resolve_hierarchy(&recs, &mut report);
        // MAX_DEPTH=12: Python's _chain raises _TooDeepError when
        // len(walked) > MAX_DEPTH, so a chain of 13 nodes (n0..n12) is
        // already too deep. The deepest resolvable node is n11 (chain of
        // 12 nodes, depth 11); n12..n19 (8 nodes) are too_deep. See
        // python/tests/test_hierarchy.py::test_max_depth_boundary, which
        // pins this exact boundary.
        assert!(resolved.contains_key("n11"));
        assert!(!resolved.contains_key("n12"));
        assert_eq!(report.count("too_deep"), 8);
    }
}
