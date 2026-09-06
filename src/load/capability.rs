//! `PUT` to a new id needs update-as-create. One clear preflight error
//! beats hundreds of identical 404s.

use std::collections::HashSet;

use serde_json::Value;

use crate::error::{KilnError, Result};

/// Resource types whose `rest.resource[].updateCreate` is true.
pub fn update_create_types(capability: &Value) -> HashSet<String> {
    let mut out = HashSet::new();
    for rest in capability
        .get("rest")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        for resource in rest
            .get("resource")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if resource.get("updateCreate").and_then(Value::as_bool) == Some(true) {
                if let Some(t) = resource.get("type").and_then(Value::as_str) {
                    out.insert(t.to_string());
                }
            }
        }
    }
    out
}

/// `needed` are the types of resources without a `versionId`: those are
/// creates, and `PUT` to a new id needs update-as-create.
pub fn check_update_create(capability: &Value, needed: &[String]) -> Result<()> {
    let supported = update_create_types(capability);
    let mut missing: Vec<&str> = needed
        .iter()
        .map(String::as_str)
        .filter(|t| !supported.contains(*t))
        .collect();
    missing.sort();
    missing.dedup();
    if missing.is_empty() {
        return Ok(());
    }
    Err(KilnError::Usage(format!(
        "the input creates new {} resources but this FHIR server does not advertise update-as-create for them \
         (CapabilityStatement rest.resource.updateCreate); kiln load PUTs resources by id, which needs it. \
         On Google Cloud Healthcare API set enableUpdateCreate=true on the store.",
        missing.join(", ")
    )))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn capability(types: &[(&str, bool)]) -> Value {
        json!({"resourceType": "CapabilityStatement", "rest": [{"resource":
            types.iter().map(|(t, u)| json!({"type": t, "updateCreate": u})).collect::<Vec<_>>()}]})
    }

    #[test]
    fn collects_the_types_that_advertise_update_create() {
        let got = update_create_types(&capability(&[("Location", true), ("Organization", false)]));
        assert!(got.contains("Location"));
        assert!(!got.contains("Organization"));
        assert!(update_create_types(&json!({"resourceType": "OperationOutcome"})).is_empty());
    }

    #[test]
    fn refuses_only_when_a_create_needs_a_missing_type() {
        let cap = capability(&[("Location", false)]);
        assert!(check_update_create(&cap, &[]).is_ok());
        let err = check_update_create(&cap, &["Location".to_string()]).unwrap_err();
        assert!(matches!(&err, KilnError::Usage(m) if m.contains("updateCreate") && m.contains("Location")), "{err}");
        assert!(check_update_create(&capability(&[("Location", true)]), &["Location".to_string()]).is_ok());
    }
}
