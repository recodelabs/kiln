import shapely

from kiln.geometry import build_geometry, normalize_geojson
from kiln.profile import BoundaryRef, RawLocation
from kiln.report import Report

SQUARE = b'{"type":"Polygon","coordinates":[[[3,6],[4,6],[4,7],[3,7],[3,6]]]}'
# A C-shape whose centroid falls outside the polygon.
CRESCENT = (
    b'{"type":"Polygon","coordinates":[[[0,0],[3,0],[3,1],[1,1],[1,2],[3,2],'
    b"[3,3],[0,3],[0,0]]]}"
)


def test_normalize_accepts_a_bare_geometry():
    geom = normalize_geojson(SQUARE, "loc-1", Report())

    assert geom.geom_type == "Polygon"


def test_normalize_unwraps_a_feature():
    payload = b'{"type":"Feature","properties":{},"geometry":' + SQUARE + b"}"

    assert normalize_geojson(payload, "loc-1", Report()).geom_type == "Polygon"


def test_normalize_unwraps_a_single_feature_collection():
    payload = (
        b'{"type":"FeatureCollection","features":[{"type":"Feature",'
        b'"properties":{},"geometry":' + SQUARE + b"}]}"
    )

    assert normalize_geojson(payload, "loc-1", Report()).geom_type == "Polygon"


def test_normalize_folds_a_multi_feature_collection_and_warns():
    report = Report()
    payload = (
        b'{"type":"FeatureCollection","features":['
        b'{"type":"Feature","properties":{},"geometry":' + SQUARE + b"},"
        b'{"type":"Feature","properties":{},"geometry":' + SQUARE + b"}]}"
    )

    geom = normalize_geojson(payload, "loc-1", report)

    assert geom.geom_type in {"MultiPolygon", "Polygon"}
    assert report.counts() == {"boundary_multi_feature": 1}


def test_normalize_reports_unparseable_json():
    report = Report()

    assert normalize_geojson(b"{not json", "loc-1", report) is None
    assert report.counts() == {"boundary_unparseable": 1}


def test_position_only_location_becomes_a_point():
    raw = RawLocation(id="c", loc_type="facility", position=(8.5, 12.0))

    result = build_geometry(raw, Report())

    assert result.geom_type == "point"
    assert (result.lon, result.lat) == (8.5, 12.0)
    assert result.geometry.geom_type == "Point"


def test_point_uses_longitude_as_x_and_latitude_as_y():
    raw = RawLocation(id="c", position=(8.5, 12.0))

    geometry = build_geometry(raw, Report()).geometry

    assert (geometry.x, geometry.y) == (8.5, 12.0)


def test_boundary_only_location_becomes_a_polygon():
    raw = RawLocation(id="d", boundary=BoundaryRef(data=SQUARE))

    result = build_geometry(raw, Report())

    assert result.geom_type == "polygon"
    assert result.geometry.geom_type == "Polygon"


def test_polygon_lon_lat_come_from_point_on_surface_not_centroid():
    raw = RawLocation(id="d", boundary=BoundaryRef(data=CRESCENT))

    result = build_geometry(raw, Report())
    representative = shapely.Point(result.lon, result.lat)

    assert result.geometry.contains(representative)


def test_a_location_with_both_yields_one_row_polygon_geometry_point_coords():
    raw = RawLocation(id="e", position=(3.2, 6.2), boundary=BoundaryRef(data=SQUARE))

    result = build_geometry(raw, Report())

    assert result.geom_type == "polygon"
    assert result.geometry.geom_type == "Polygon"
    assert (result.lon, result.lat) == (3.2, 6.2)


def test_a_location_with_no_geometry_is_reported():
    report = Report()

    result = build_geometry(RawLocation(id="f"), report)

    assert result.geometry is None
    assert result.geom_type is None
    assert report.counts() == {"no_geometry": 1}


def test_an_invalid_ring_is_repaired_and_counted():
    report = Report()
    bowtie = b'{"type":"Polygon","coordinates":[[[0,0],[2,2],[2,0],[0,2],[0,0]]]}'

    result = build_geometry(RawLocation(id="g", boundary=BoundaryRef(data=bowtie)), report)

    assert result.geometry.is_valid
    assert report.counts() == {"geometry_repaired": 1}


def test_a_multipolygon_boundary_is_still_geom_type_polygon():
    payload = (
        b'{"type":"MultiPolygon","coordinates":[[[[0,0],[1,0],[1,1],[0,1],[0,0]]],'
        b"[[[2,2],[3,2],[3,3],[2,3],[2,2]]]]}"
    )

    result = build_geometry(RawLocation(id="h", boundary=BoundaryRef(data=payload)), Report())

    assert result.geom_type == "polygon"
    assert result.geometry.geom_type == "MultiPolygon"


def test_multi_feature_with_invalid_geometry_does_not_raise():
    report = Report()
    bowtie = b'{"type":"Polygon","coordinates":[[[0,0],[2,2],[2,0],[0,2],[0,0]]]}'
    payload = (
        b'{"type":"FeatureCollection","features":['
        b'{"type":"Feature","properties":{},"geometry":' + bowtie + b"},"
        b'{"type":"Feature","properties":{},"geometry":' + SQUARE + b"}]}"
    )

    geom = normalize_geojson(payload, "loc-1", report)

    assert geom is not None
    assert report.counts() == {"boundary_multi_feature": 1}


def test_geometry_collection_input_is_reported_and_dropped():
    report = Report()
    payload = (
        b'{"type":"GeometryCollection","geometries":['
        b'{"type":"Point","coordinates":[0,0]},'
        b'{"type":"LineString","coordinates":[[1,1],[2,2]]}'
        b"]}"
    )

    result = build_geometry(
        RawLocation(id="i", boundary=BoundaryRef(data=payload)), report
    )

    assert result.geometry is None
    assert result.geom_type is None
    assert report.counts() == {"geometry_unexpected_type": 1}


def test_degenerate_polygon_with_duplicate_points_becomes_linestring():
    report = Report()
    # A polygon with duplicate points that becomes a LineString after make_valid
    degenerate_polygon = (
        b'{"type":"Polygon","coordinates":[[[0,0],[1,1],[1,1],[1,1],[0,0]]]}'
    )

    result = build_geometry(
        RawLocation(id="j", boundary=BoundaryRef(data=degenerate_polygon)), report
    )

    assert result.geometry is None
    assert result.geom_type is None
    # Should have both geometry_repaired and geometry_unexpected_type reports
    counts = report.counts()
    assert counts["geometry_repaired"] == 1
    assert counts["geometry_unexpected_type"] == 1
