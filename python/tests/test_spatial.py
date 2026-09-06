import math

import pytest

from kiln.bake import BakeError
from kiln.spatial import (
    SPATIAL_INDEX_EXTENSION_URL,
    compute_cell,
    geohash,
    parse_spatial_index_arg,
    quadkey,
    spatial_index_extension,
)


def _tile(longitude, latitude, level):
    # Independent XYZ tile arithmetic (the slippy-map formula) to cross-check quadkey().
    n = 2**level
    x = int((longitude + 180.0) / 360.0 * n)
    lat = math.radians(latitude)
    y = int((1.0 - math.log(math.tan(lat) + 1 / math.cos(lat)) / math.pi) / 2.0 * n)
    return x, y


def _quadkey_from_tile(x, y, level):
    out = ""
    for i in range(level, 0, -1):
        mask = 1 << (i - 1)
        out += str((1 if x & mask else 0) + (2 if y & mask else 0))
    return out


@pytest.mark.parametrize(
    "lon,lat,level",
    [(9.9, 10.5, 18), (-12.9469, 9.0144, 18), (3.39, 6.45, 12), (-122.33, 47.61, 3), (0.0, 0.0, 1)],
)
def test_quadkey_matches_the_slippy_tile_formula(lon, lat, level):
    x, y = _tile(lon, lat, level)
    assert quadkey(lon, lat, level) == _quadkey_from_tile(x, y, level)


def test_quadkey_is_level_digits_of_base4_and_prefix_hierarchical():
    q18 = quadkey(9.9, 10.5, 18)
    assert len(q18) == 18 and set(q18) <= set("0123")
    for level in (1, 5, 10, 17):
        assert q18.startswith(quadkey(9.9, 10.5, level))


def test_quadkey_quadrants_at_zoom_1():
    assert quadkey(-90, 45, 1) == "0"
    assert quadkey(90, 45, 1) == "1"
    assert quadkey(-90, -45, 1) == "2"
    assert quadkey(90, -45, 1) == "3"


def test_geohash_known_value():
    # Wikipedia's worked example: 57.64911, 10.40744 -> u4pruydqqvj
    assert geohash(10.40744, 57.64911, 11) == "u4pruydqqvj"
    assert geohash(10.40744, 57.64911, 4) == "u4pr"


def test_parse_spatial_index_arg():
    assert parse_spatial_index_arg("quadkey:18") == ("quadkey", 18)
    assert parse_spatial_index_arg("geohash:8") == ("geohash", 8)
    for bad in ("quadkey", "quadkey:x", "s2:5", "quadkey:24", "quadkey:0", "geohash:13", "h3:9"):
        with pytest.raises(BakeError):
            parse_spatial_index_arg(bad)


def test_extension_shape_matches_the_ig():
    ext = spatial_index_extension("quadkey", 18, compute_cell("quadkey", 18, 9.9, 10.5))
    assert ext["url"] == SPATIAL_INDEX_EXTENSION_URL
    assert {e["url"]: list(e)[1] for e in ext["extension"]} == {
        "system": "valueCode",
        "level": "valueUnsignedInt",
        "cell": "valueString",
    }
    assert ext["extension"][0]["valueCode"] == "quadkey"
    assert ext["extension"][1]["valueUnsignedInt"] == 18
    assert len(ext["extension"][2]["valueString"]) == 18
