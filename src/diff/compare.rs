//! What "changed" means: coordinates compare after rounding to seven
//! decimals (about a centimetre), so a float round trip through a GIS
//! tool is not an edit; resources compare as canonical JSON.

use geo::{Geometry, MapCoords};
use serde_json::Value;

use crate::geometry::to_wkb;

pub const DECIMALS: i32 = 7;

pub fn round_coord(v: f64) -> f64 {
    let f = 10f64.powi(DECIMALS);
    (v * f).round() / f
}

pub fn round_geometry(g: &Geometry<f64>) -> Geometry<f64> {
    g.map_coords(|c| geo::coord! { x: round_coord(c.x), y: round_coord(c.y) })
}

/// The comparison key for a geometry: little-endian WKB of the rounded
/// geometry. Two geometries with the same key are the same edit.
pub fn wkb_key(g: &Geometry<f64>) -> Vec<u8> {
    to_wkb(&round_geometry(g))
}

/// Object keys sorted recursively; arrays keep their order. `serde_json`
/// is built with `preserve_order`, so inserting in sorted order yields
/// sorted output.
pub fn canonical(v: &Value) -> Value {
    match v {
        Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            let mut out = serde_json::Map::new();
            for k in keys {
                out.insert(k.clone(), canonical(&m[k]));
            }
            Value::Object(out)
        }
        Value::Array(a) => Value::Array(a.iter().map(canonical).collect()),
        other => other.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn rounds_to_seven_decimals() {
        assert_eq!(round_coord(3.123456789), 3.1234568);
        assert_eq!(round_coord(3.25), 3.25);
        assert_eq!(round_coord(-0.00000004), -0.0);
    }

    #[test]
    fn float_noise_in_the_eighth_decimal_gives_the_same_key() {
        let a = geo::Geometry::Point(geo::Point::new(3.25, 6.25));
        let b = geo::Geometry::Point(geo::Point::new(3.250000004, 6.249999996));
        assert_eq!(wkb_key(&a), wkb_key(&b));
        let c = geo::Geometry::Point(geo::Point::new(3.2500001, 6.25));
        assert_ne!(wkb_key(&a), wkb_key(&c));
    }

    #[test]
    fn canonical_sorts_keys_recursively_and_keeps_arrays_in_order() {
        let v = json!({"b": [{"z": 1, "a": 2}], "a": {"y": null, "x": "s"}});
        assert_eq!(
            canonical(&v).to_string(),
            r#"{"a":{"x":"s","y":null},"b":[{"a":2,"z":1}]}"#
        );
    }
}
