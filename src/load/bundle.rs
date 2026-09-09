//! Transaction bundles of `PUT <Type>/<id>`, `ifMatch` on every entry that
//! has a version to check.

use serde_json::{json, Value};

pub struct Entry {
    pub resource: Value,
    pub resource_type: String,
    pub id: String,
    /// `meta.versionId` from the snapshot; `None` is a create.
    pub version: Option<String>,
}

pub struct Bundle {
    pub entries: Vec<Entry>,
}

pub fn if_match(version: &str) -> String {
    format!("W/\"{version}\"")
}

impl Bundle {
    pub fn to_json(&self) -> Value {
        let entries: Vec<Value> = self
            .entries
            .iter()
            .map(|e| {
                let mut request =
                    json!({"method": "PUT", "url": format!("{}/{}", e.resource_type, e.id)});
                if let Some(v) = &e.version {
                    request["ifMatch"] = Value::String(if_match(v));
                }
                json!({"resource": e.resource, "request": request})
            })
            .collect();
        json!({"resourceType": "Bundle", "type": "transaction", "entry": entries})
    }

    /// One line for `--dry-run`: `Location/a update@3, Location/b create`.
    pub fn describe(&self) -> String {
        self.entries
            .iter()
            .map(|e| match &e.version {
                Some(v) => format!("{}/{} update@{v}", e.resource_type, e.id),
                None => format!("{}/{} create", e.resource_type, e.id),
            })
            .collect::<Vec<_>>()
            .join(", ")
    }
}

/// Chunk already-ordered resources into bundles of `batch_size`. Every
/// resource has been validated to carry string `resourceType` and `id`.
pub fn plan(resources: Vec<Value>, batch_size: usize) -> Vec<Bundle> {
    let mut bundles = Vec::new();
    let mut current: Vec<Entry> = Vec::new();
    for resource in resources {
        let entry = Entry {
            resource_type: resource["resourceType"].as_str().unwrap_or("").to_string(),
            id: resource["id"].as_str().unwrap_or("").to_string(),
            version: resource
                .get("meta")
                .and_then(|m| m.get("versionId"))
                .and_then(Value::as_str)
                .map(str::to_string),
            resource,
        };
        current.push(entry);
        if current.len() == batch_size.max(1) {
            bundles.push(Bundle {
                entries: std::mem::take(&mut current),
            });
        }
    }
    if !current.is_empty() {
        bundles.push(Bundle { entries: current });
    }
    bundles
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn if_match_is_a_weak_etag() {
        assert_eq!(if_match("3"), "W/\"3\"");
    }

    #[test]
    fn entries_are_chunked_in_order_with_if_match_only_for_existing_resources() {
        let resources = vec![
            json!({"resourceType": "Location", "id": "a", "meta": {"versionId": "1"}}),
            json!({"resourceType": "Location", "id": "b"}),
            json!({"resourceType": "Organization", "id": "o", "meta": {"versionId": "7"}}),
        ];
        let bundles = plan(resources, 2);
        assert_eq!(bundles.len(), 2);
        assert_eq!(bundles[0].entries.len(), 2);
        assert_eq!(bundles[1].entries.len(), 1);
        let json = bundles[0].to_json();
        assert_eq!(json["resourceType"], "Bundle");
        assert_eq!(json["type"], "transaction");
        assert_eq!(
            json["entry"][0]["request"],
            json!({"method": "PUT", "url": "Location/a", "ifMatch": "W/\"1\""})
        );
        assert_eq!(
            json["entry"][1]["request"],
            json!({"method": "PUT", "url": "Location/b"})
        );
        assert_eq!(json["entry"][0]["resource"]["id"], "a");
        assert_eq!(
            bundles[1].to_json()["entry"][0]["request"]["url"],
            "Organization/o"
        );
        assert_eq!(
            bundles[0].describe(),
            "Location/a update@1, Location/b create"
        );
    }
}
