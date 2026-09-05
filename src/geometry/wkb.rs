//! geo geometry -> little-endian WKB, the encoding Parquet GEOMETRY expects.

use wkb::writer::{write_geometry, WriteOptions};
use wkb::Endianness;

pub fn to_wkb(geom: &geo::Geometry<f64>) -> Vec<u8> {
    let mut out = Vec::new();
    let options = WriteOptions {
        endianness: Endianness::LittleEndian,
    };
    write_geometry(&mut out, geom, &options).expect("writing WKB to a Vec cannot fail");
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use geo::polygon;

    #[test]
    fn point_encodes_as_21_little_endian_bytes() {
        let bytes = to_wkb(&geo::Geometry::Point(geo::Point::new(3.25, 6.25)));
        assert_eq!(bytes.len(), 21);
        assert_eq!(bytes[0], 1);
        assert_eq!(&bytes[1..5], &1u32.to_le_bytes());
        assert_eq!(f64::from_le_bytes(bytes[5..13].try_into().unwrap()), 3.25);
    }

    #[test]
    fn polygon_encodes_with_type_3() {
        let poly = polygon![(x: 0., y: 0.), (x: 1., y: 0.), (x: 1., y: 1.), (x: 0., y: 0.)];
        let bytes = to_wkb(&geo::Geometry::Polygon(poly));
        assert_eq!(&bytes[1..5], &3u32.to_le_bytes());
    }
}
