"""Spatial index cells: the tile a point falls in under a tiling scheme.

The ICR IG's `spatial-index` extension carries, per Location, the scheme
(quadkey | h3 | geohash), the level (quadkey zoom / H3 resolution / geohash
precision) and the cell key. The cell is a pure function of the position,
so kiln computes it at import (`bake-points --spatial-index quadkey:18`)
and, for resources loaded before the extension existed, as a backfill
edit (`kiln index`, Rust). Quadkey and geohash are implemented here with no
dependencies; H3 needs the `h3` library and is left for a later round.
"""

from __future__ import annotations

import math

from kiln.bake import BakeError

SPATIAL_INDEX_EXTENSION_URL = "https://icr.healthcampaigns.org/StructureDefinition/spatial-index"

SCHEMES = ("quadkey", "h3", "geohash")
QUADKEY_LEVELS = range(1, 24)
GEOHASH_LEVELS = range(1, 13)
_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"
_MAX_MERCATOR_LAT = 85.05112878


def parse_spatial_index_arg(arg: str) -> tuple[str, int]:
    """`SCHEME:LEVEL`, e.g. `quadkey:18` -> ("quadkey", 18)."""
    scheme, sep, level_text = arg.partition(":")
    if not sep or not level_text.strip().isdigit():
        raise BakeError(f"--spatial-index must be SCHEME:LEVEL (e.g. 'quadkey:18'), got {arg!r}")
    scheme = scheme.strip()
    level = int(level_text)
    if scheme not in SCHEMES:
        raise BakeError(f"--spatial-index scheme must be one of {', '.join(SCHEMES)}, got {scheme!r}")
    if scheme == "quadkey" and level not in QUADKEY_LEVELS:
        raise BakeError(f"quadkey zoom must be 1-23, got {level}")
    if scheme == "geohash" and level not in GEOHASH_LEVELS:
        raise BakeError(f"geohash precision must be 1-12, got {level}")
    if scheme == "h3":
        raise BakeError("h3 cells are not computed yet (needs the h3 library); use quadkey or geohash")
    return scheme, level


def quadkey(longitude: float, latitude: float, level: int) -> str:
    """Bing/XYZ tile quadkey of the point at `level` (zoom 1-23).

    Base-4 digits, one per zoom from 1: each digit is the quadrant of the
    tile within its parent, so every prefix is the containing tile at that
    shorter zoom. Latitude is clamped to the Web Mercator range.
    """
    lat = max(-_MAX_MERCATOR_LAT, min(_MAX_MERCATOR_LAT, latitude))
    lon = ((longitude + 180.0) % 360.0) - 180.0
    n = 1 << level
    x = int((lon + 180.0) / 360.0 * n)
    sin_lat = math.sin(math.radians(lat))
    y = int((0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * n)
    x = min(max(x, 0), n - 1)
    y = min(max(y, 0), n - 1)
    digits = []
    for i in range(level, 0, -1):
        mask = 1 << (i - 1)
        digit = 0
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        digits.append(str(digit))
    return "".join(digits)


def geohash(longitude: float, latitude: float, precision: int) -> str:
    """Base-32 geohash of the point at `precision` characters (1-12)."""
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    bits = 0
    bit_count = 0
    even = True
    out = []
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if longitude >= mid:
                bits = (bits << 1) | 1
                lon_lo = mid
            else:
                bits <<= 1
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if latitude >= mid:
                bits = (bits << 1) | 1
                lat_lo = mid
            else:
                bits <<= 1
                lat_hi = mid
        even = not even
        bit_count += 1
        if bit_count == 5:
            out.append(_GEOHASH_ALPHABET[bits])
            bits = 0
            bit_count = 0
    return "".join(out)


def compute_cell(scheme: str, level: int, longitude: float, latitude: float) -> str:
    if scheme == "quadkey":
        return quadkey(longitude, latitude, level)
    if scheme == "geohash":
        return geohash(longitude, latitude, level)
    raise BakeError(f"spatial index scheme {scheme!r} cannot be computed here")


def spatial_index_extension(scheme: str, level: int, cell: str) -> dict:
    """One `spatial-index` extension entry as the IG defines it."""
    return {
        "url": SPATIAL_INDEX_EXTENSION_URL,
        "extension": [
            {"url": "system", "valueCode": scheme},
            {"url": "level", "valueUnsignedInt": level},
            {"url": "cell", "valueString": cell},
        ],
    }
