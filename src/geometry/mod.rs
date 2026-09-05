//! One Location's geometry: the small summary pass one keeps (kind + bbox),
//! and the full result pass two writes (WKB + representative point).
//!
//! Invalid geometry is reported and passed through unchanged. kiln does not
//! repair; that hides registry problems from the people who must fix them.

pub mod geojson;
pub mod wkb;

use geo::{BoundingRect, Geometry, InteriorPoint, Validation};

pub use geojson::{kind_name, parse_boundary};
pub use wkb::to_wkb;

use crate::fhir::{Boundary, Location};
use crate::report::Report;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum GeomKind {
    Point,
    Polygon,
}

impl GeomKind {
    pub fn as_str(self) -> &'static str {
        match self {
            GeomKind::Point => "point",
            GeomKind::Polygon => "polygon",
        }
    }
}

/// What pass one retains per Location: 40 bytes, no coordinates.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct GeometrySummary {
    pub kind: GeomKind,
    /// xmin, ymin, xmax, ymax
    pub bbox: [f64; 4],
}

/// What pass two produces per Location.
#[derive(Debug, Clone, PartialEq)]
pub struct GeometryResult {
    pub kind: GeomKind,
    pub geometry: Geometry<f64>,
    pub bbox: [f64; 4],
    /// Representative point: the FHIR position when present, else a point inside the polygon.
    pub lon: f64,
    pub lat: f64,
}

impl GeometryResult {
    pub fn wkb(&self) -> Vec<u8> {
        to_wkb(&self.geometry)
    }
}

fn bbox_of(geom: &Geometry<f64>) -> Option<[f64; 4]> {
    let r = geom.bounding_rect()?;
    Some([r.min().x, r.min().y, r.max().x, r.max().y])
}

/// Parse the boundary (if inline), classify, validate, and pick the
/// representative point. `None` means the Location produces no row.
pub fn build(loc: &Location, report: &mut Report) -> Option<GeometryResult> {
    let polygon = match &loc.boundary {
        Some(Boundary::Inline(bytes)) => parse_boundary(bytes, &loc.id, report),
        Some(Boundary::Url(_)) | None => None,
    };

    if let Some(geom) = polygon {
        let kind = match &geom {
            Geometry::Point(_) => GeomKind::Point,
            Geometry::Polygon(_) | Geometry::MultiPolygon(_) => GeomKind::Polygon,
            other => {
                report.add(
                    "geometry_unexpected_type",
                    &loc.id,
                    &format!("unsupported type {}", kind_name(other)),
                );
                return None;
            }
        };
        let Some(bbox) = bbox_of(&geom) else {
            report.add("geometry_unexpected_type", &loc.id, "empty geometry");
            return None;
        };
        if !geom.is_valid() {
            report.add(
                "geometry_invalid",
                &loc.id,
                "boundary fails validity checks; written as is",
            );
        }
        let (lon, lat) = match (loc.position, &geom) {
            (Some(p), _) => p,
            (None, Geometry::Point(p)) => (p.x(), p.y()),
            (None, g) => match g.interior_point() {
                Some(p) => (p.x(), p.y()),
                None => ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0),
            },
        };
        return Some(GeometryResult {
            kind,
            geometry: geom,
            bbox,
            lon,
            lat,
        });
    }

    if let Some((lon, lat)) = loc.position {
        return Some(GeometryResult {
            kind: GeomKind::Point,
            geometry: Geometry::Point(geo::Point::new(lon, lat)),
            bbox: [lon, lat, lon, lat],
            lon,
            lat,
        });
    }

    report.add("no_geometry", &loc.id, "no position and no usable boundary");
    None
}

/// Pass one: the same decisions as `build`, keeping only kind and bbox.
pub fn summarize(loc: &Location, report: &mut Report) -> Option<GeometrySummary> {
    build(loc, report).map(|g| GeometrySummary {
        kind: g.kind,
        bbox: g.bbox,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fhir::{Boundary, Location};
    use crate::report::Report;

    fn loc(position: Option<(f64, f64)>, boundary: Option<&str>) -> Location {
        Location {
            id: "x".into(),
            position,
            boundary: boundary.map(|b| Boundary::Inline(b.as_bytes().to_vec())),
            ..Default::default()
        }
    }
    const SQUARE: &str = r#"{"type":"Polygon","coordinates":[[[3,6],[4,6],[4,7],[3,7],[3,6]]]}"#;
    const BOWTIE: &str = r#"{"type":"Polygon","coordinates":[[[0,0],[2,2],[2,0],[0,2],[0,0]]]}"#;

    #[test]
    fn position_only_is_a_point() {
        let mut r = Report::default();
        let g = build(&loc(Some((3.25, 6.25)), None), &mut r).unwrap();
        assert_eq!(g.kind, GeomKind::Point);
        assert_eq!((g.lon, g.lat), (3.25, 6.25));
        assert_eq!(g.bbox, [3.25, 6.25, 3.25, 6.25]);
    }

    #[test]
    fn polygon_gets_an_interior_representative_point() {
        let mut r = Report::default();
        let g = build(&loc(None, Some(SQUARE)), &mut r).unwrap();
        assert_eq!(g.kind, GeomKind::Polygon);
        assert!(g.lon > 3.0 && g.lon < 4.0 && g.lat > 6.0 && g.lat < 7.0);
        assert_eq!(g.bbox, [3.0, 6.0, 4.0, 7.0]);
    }

    #[test]
    fn polygon_plus_position_uses_the_position_as_representative_point() {
        let mut r = Report::default();
        let g = build(&loc(Some((3.2, 6.2)), Some(SQUARE)), &mut r).unwrap();
        assert_eq!(g.kind, GeomKind::Polygon);
        assert_eq!((g.lon, g.lat), (3.2, 6.2));
    }

    #[test]
    fn invalid_polygon_is_kept_and_reported_not_repaired() {
        let mut r = Report::default();
        let g = build(&loc(None, Some(BOWTIE)), &mut r).unwrap();
        assert_eq!(g.kind, GeomKind::Polygon);
        assert_eq!(r.count("geometry_invalid"), 1);
        assert_eq!(r.count("geometry_repaired"), 0);
    }

    #[test]
    fn nothing_usable_is_reported_as_no_geometry() {
        let mut r = Report::default();
        assert!(build(&loc(None, None), &mut r).is_none());
        assert_eq!(r.count("no_geometry"), 1);
        let mut r = Report::default();
        assert!(build(
            &loc(
                None,
                Some(r#"{"type":"LineString","coordinates":[[0,0],[1,1]]}"#)
            ),
            &mut r
        )
        .is_none());
        assert_eq!(r.count("geometry_unexpected_type"), 1);
    }

    #[test]
    fn empty_polygon_is_reported_not_written() {
        let mut r = Report::default();
        assert!(build(
            &loc(None, Some(r#"{"type":"Polygon","coordinates":[]}"#)),
            &mut r
        )
        .is_none());
        assert_eq!(r.count("geometry_unexpected_type"), 1);
    }

    #[test]
    fn summarize_matches_build_but_keeps_no_geometry() {
        let mut r = Report::default();
        let s = summarize(&loc(Some((3.2, 6.2)), Some(SQUARE)), &mut r).unwrap();
        assert_eq!(s.kind, GeomKind::Polygon);
        assert_eq!(s.bbox, [3.0, 6.0, 4.0, 7.0]);
        assert_eq!(std::mem::size_of::<GeometrySummary>(), 40);
    }
}
