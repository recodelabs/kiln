//! Boundary attachment bytes -> a `geo` geometry. Accepts a bare geometry, a
//! Feature, or a FeatureCollection, as the Python did.

use geo::Geometry;
use geojson::GeoJson;

use crate::report::Report;

fn to_geo(g: geojson::Geometry, id: &str, report: &mut Report) -> Option<Geometry<f64>> {
    match Geometry::<f64>::try_from(g) {
        Ok(geom) => Some(geom),
        Err(err) => {
            report.add("boundary_unparseable", id, &err.to_string());
            None
        }
    }
}

pub fn parse_boundary(bytes: &[u8], id: &str, report: &mut Report) -> Option<Geometry<f64>> {
    let text = match std::str::from_utf8(bytes) {
        Ok(t) => t,
        Err(err) => {
            report.add("boundary_unparseable", id, &err.to_string());
            return None;
        }
    };
    let parsed: GeoJson = match text.parse() {
        Ok(p) => p,
        Err(err) => {
            report.add("boundary_unparseable", id, &err.to_string());
            return None;
        }
    };
    match parsed {
        GeoJson::Geometry(g) => to_geo(g, id, report),
        GeoJson::Feature(f) => match f.geometry {
            Some(g) => to_geo(g, id, report),
            None => {
                report.add("boundary_unparseable", id, "Feature has no geometry");
                None
            }
        },
        GeoJson::FeatureCollection(fc) => {
            let geoms: Vec<geojson::Geometry> =
                fc.features.into_iter().filter_map(|f| f.geometry).collect();
            match geoms.len() {
                0 => {
                    report.add(
                        "boundary_unparseable",
                        id,
                        "FeatureCollection has no geometry",
                    );
                    None
                }
                1 => to_geo(geoms.into_iter().next().unwrap(), id, report),
                n => {
                    report.add(
                        "boundary_multi_feature",
                        id,
                        &format!("{n} features folded into one geometry"),
                    );
                    let mut polygons = Vec::new();
                    for g in geoms {
                        match to_geo(g, id, report) {
                            Some(Geometry::Polygon(p)) => polygons.push(p),
                            Some(Geometry::MultiPolygon(mp)) => polygons.extend(mp.0),
                            Some(other) => report.add(
                                "boundary_unparseable",
                                id,
                                &format!("non-polygon part {:?} ignored", kind_name(&other)),
                            ),
                            None => {}
                        }
                    }
                    if polygons.is_empty() {
                        None
                    } else {
                        Some(Geometry::MultiPolygon(geo::MultiPolygon(polygons)))
                    }
                }
            }
        }
    }
}

pub fn kind_name(g: &Geometry<f64>) -> &'static str {
    match g {
        Geometry::Point(_) => "Point",
        Geometry::Line(_) => "Line",
        Geometry::LineString(_) => "LineString",
        Geometry::Polygon(_) => "Polygon",
        Geometry::MultiPoint(_) => "MultiPoint",
        Geometry::MultiLineString(_) => "MultiLineString",
        Geometry::MultiPolygon(_) => "MultiPolygon",
        Geometry::GeometryCollection(_) => "GeometryCollection",
        Geometry::Rect(_) => "Rect",
        Geometry::Triangle(_) => "Triangle",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::report::Report;

    #[test]
    fn bare_geometry_feature_and_collection_all_parse() {
        let mut r = Report::default();
        let poly = r#"{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,1],[0,0]]]}"#;
        assert!(matches!(
            parse_boundary(poly.as_bytes(), "a", &mut r),
            Some(geo::Geometry::Polygon(_))
        ));
        let feature = format!(r#"{{"type":"Feature","properties":{{}},"geometry":{poly}}}"#);
        assert!(matches!(
            parse_boundary(feature.as_bytes(), "a", &mut r),
            Some(geo::Geometry::Polygon(_))
        ));
        let fc = format!(r#"{{"type":"FeatureCollection","features":[{feature}]}}"#);
        assert!(matches!(
            parse_boundary(fc.as_bytes(), "a", &mut r),
            Some(geo::Geometry::Polygon(_))
        ));
        assert_eq!(r.counts().len(), 0);
    }

    #[test]
    fn multi_feature_collection_folds_into_multipolygon_and_reports() {
        let mut r = Report::default();
        let poly = r#"{"type":"Feature","geometry":{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,1],[0,0]]]}}"#;
        let fc = format!(r#"{{"type":"FeatureCollection","features":[{poly},{poly}]}}"#);
        match parse_boundary(fc.as_bytes(), "a", &mut r) {
            Some(geo::Geometry::MultiPolygon(mp)) => assert_eq!(mp.0.len(), 2),
            other => panic!("{other:?}"),
        }
        assert_eq!(r.count("boundary_multi_feature"), 1);
    }

    #[test]
    fn garbage_is_reported_as_unparseable() {
        let mut r = Report::default();
        assert!(parse_boundary(b"not json", "a", &mut r).is_none());
        assert!(parse_boundary(
            br#"{"type":"FeatureCollection","features":[]}"#,
            "a",
            &mut r
        )
        .is_none());
        assert_eq!(r.count("boundary_unparseable"), 2);
    }
}
