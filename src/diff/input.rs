//! One edited row, whatever file it came from. Only the README's writable
//! columns are kept; each reader coerces its native types into
//! `ColumnValue` and the rebuild step never sees a file format.

use std::collections::BTreeMap;

use geo::Geometry;
use serde_json::Value;

pub use crate::fhir::location::Identifier;

pub const TEXT_COLUMNS: [&str; 13] = [
    "name",
    "status",
    "description",
    "type",
    "physical_type",
    "part_of",
    "managing_organization",
    "pcode",
    "gers_id",
    "settlement_type",
    "delivery_strategy",
    "facility_level",
    "ownership",
];
pub const NUMBER_COLUMNS: [&str; 2] = ["position_longitude", "position_latitude"];
pub const ALIAS_COLUMN: &str = "alias";
pub const IDENTIFIER_COLUMN: &str = "identifier";
pub const ID_COLUMN: &str = "id";

#[derive(Debug, Clone, PartialEq)]
pub enum ColumnValue {
    /// Present and empty: the FHIR field is removed.
    Null,
    Text(String),
    Number(f64),
    TextList(Vec<String>),
    Identifiers(Vec<Identifier>),
}

#[derive(Debug, Clone, Default)]
pub struct InputRow {
    pub id: Option<String>,
    /// Only writable columns, keyed by README column name. Absent means untouched.
    pub columns: BTreeMap<String, ColumnValue>,
    /// 2D, unrounded; `None` means no geometry edit.
    pub geometry: Option<Geometry<f64>>,
    /// 1-based feature or row index, for messages.
    pub line: usize,
}

pub fn is_writable(name: &str) -> bool {
    TEXT_COLUMNS.contains(&name)
        || NUMBER_COLUMNS.contains(&name)
        || name == ALIAS_COLUMN
        || name == IDENTIFIER_COLUMN
}

fn text_list(v: &Value) -> Result<ColumnValue, String> {
    let Value::Array(items) = v else {
        return Err("expected a list of strings".into());
    };
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        match item.as_str() {
            Some(s) => out.push(s.to_string()),
            None => return Err("expected a list of strings".into()),
        }
    }
    Ok(if out.is_empty() {
        ColumnValue::Null
    } else {
        ColumnValue::TextList(out)
    })
}

fn identifiers(v: &Value) -> Result<ColumnValue, String> {
    let Value::Array(items) = v else {
        return Err("expected a list of {system, value} objects".into());
    };
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        let Value::Object(o) = item else {
            return Err("expected a list of {system, value} objects".into());
        };
        out.push(Identifier {
            system: o.get("system").and_then(Value::as_str).map(str::to_string),
            value: o.get("value").and_then(Value::as_str).map(str::to_string),
        });
    }
    Ok(if out.is_empty() {
        ColumnValue::Null
    } else {
        ColumnValue::Identifiers(out)
    })
}

/// A string holding JSON of the expected shape is accepted for the list
/// columns: GDAL flattens nested fields to JSON text on export.
fn list_or_json_string(
    v: &Value,
    parse: fn(&Value) -> Result<ColumnValue, String>,
) -> Result<ColumnValue, String> {
    match v {
        Value::String(s) if s.trim().is_empty() => Ok(ColumnValue::Null),
        Value::String(s) => match serde_json::from_str::<Value>(s) {
            Ok(inner @ Value::Array(_)) => parse(&inner),
            _ => Err("expected a list, or a string holding a JSON list".into()),
        },
        other => parse(other),
    }
}

/// Coerce one cell to the column's type. `Err` carries a message for the
/// `input_column_type` report; the caller drops the column for that row.
pub fn column_from_json(name: &str, v: &Value) -> Result<ColumnValue, String> {
    if v.is_null() {
        return Ok(ColumnValue::Null);
    }
    if TEXT_COLUMNS.contains(&name) {
        return match v {
            Value::String(s) if s.is_empty() => Ok(ColumnValue::Null),
            Value::String(s) => Ok(ColumnValue::Text(s.clone())),
            _ => Err("expected text".into()),
        };
    }
    if NUMBER_COLUMNS.contains(&name) {
        return match v {
            Value::Number(n) => n
                .as_f64()
                .map(ColumnValue::Number)
                .ok_or_else(|| "expected a number".into()),
            Value::String(s) if s.trim().is_empty() => Ok(ColumnValue::Null),
            Value::String(s) => s
                .trim()
                .parse::<f64>()
                .map(ColumnValue::Number)
                .map_err(|_| "expected a number".into()),
            _ => Err("expected a number".into()),
        };
    }
    if name == ALIAS_COLUMN {
        return list_or_json_string(v, text_list);
    }
    if name == IDENTIFIER_COLUMN {
        return list_or_json_string(v, identifiers);
    }
    Err(format!("{name} is not a writable column"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn writable_columns_are_the_readme_second_group() {
        assert!(is_writable("name"));
        assert!(is_writable("position_longitude"));
        assert!(is_writable("identifier"));
        assert!(is_writable("pcode"));
        assert!(!is_writable("admin1_name"));
        assert!(!is_writable("version_id"));
        assert!(!is_writable("id"));
        assert!(!is_writable("geometry"));
    }

    #[test]
    fn text_columns_coerce_and_empty_string_clears() {
        assert_eq!(column_from_json("name", &json!("Gama")), Ok(ColumnValue::Text("Gama".into())));
        assert_eq!(column_from_json("name", &json!("")), Ok(ColumnValue::Null));
        assert_eq!(column_from_json("name", &json!(null)), Ok(ColumnValue::Null));
        assert!(column_from_json("name", &json!(5)).is_err());
    }

    #[test]
    fn number_columns_accept_numbers_and_numeric_strings() {
        assert_eq!(column_from_json("position_longitude", &json!(3.25)), Ok(ColumnValue::Number(3.25)));
        assert_eq!(column_from_json("position_latitude", &json!("6.25")), Ok(ColumnValue::Number(6.25)));
        assert!(column_from_json("position_latitude", &json!("north")).is_err());
        assert!(column_from_json("position_latitude", &json!(true)).is_err());
    }

    #[test]
    fn alias_accepts_a_list_or_a_json_string_of_one() {
        let want = Ok(ColumnValue::TextList(vec!["a".into(), "b".into()]));
        assert_eq!(column_from_json("alias", &json!(["a", "b"])), want);
        assert_eq!(column_from_json("alias", &json!("[\"a\",\"b\"]")), want);
        assert_eq!(column_from_json("alias", &json!([])), Ok(ColumnValue::Null));
        assert!(column_from_json("alias", &json!([1])).is_err());
        assert!(column_from_json("alias", &json!("not json")).is_err());
    }

    #[test]
    fn identifier_accepts_objects_or_a_json_string_of_them() {
        let want = Ok(ColumnValue::Identifiers(vec![
            Identifier { system: Some("s".into()), value: Some("v".into()) },
            Identifier { system: None, value: Some("w".into()) },
        ]));
        let list = json!([{"system": "s", "value": "v"}, {"value": "w"}]);
        assert_eq!(column_from_json("identifier", &list), want);
        assert_eq!(column_from_json("identifier", &json!(list.to_string())), want);
        assert!(column_from_json("identifier", &json!(["s|v"])).is_err());
    }
}
