"""The two directions must agree: what bake writes, transform understands.

bake -> shred -> build_frame exercises the whole offline pipeline with no
server and no GDAL (build_frame is pure; only write_dataset needs ogr2ogr).
"""

from kiln.bake import LevelSpec, bake
from kiln.frame import build_frame
from kiln.profile import NATIONAL_ADMIN_CODE_SYSTEM, shred
from kiln.report import Report

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[3.0, 6.0], [4.0, 6.0], [4.0, 7.0], [3.0, 7.0], [3.0, 6.0]]],
}

COLLECTION = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "state": "Bauchi",
                "statecode": "BA",
                "lga": "Alkaleri",
                "ward": ward,
            },
            "geometry": SQUARE,
        }
        for ward in ("Alkaleri East", "Alkaleri West")
    ],
}


def test_baked_resources_round_trip_through_the_export_pipeline():
    report = Report()
    resources = bake(
        COLLECTION,
        ("Nigeria", "NGA"),
        [
            LevelSpec("state", "state", "statecode"),
            LevelSpec("lga", "lga", None),
            LevelSpec("ward", "ward", None),
        ],
        {},
        NATIONAL_ADMIN_CODE_SYSTEM,
        report,
    )
    # country + state + lga + 2 wards.
    assert len(resources) == 5

    locations = [shred(r, report) for r in resources]
    assert all(location is not None for location in locations)

    # bake only attaches boundaries to the leaf (feature) level -- minting
    # parent geometry is an explicit v1 non-goal (design spec, "Non-goals").
    # build_frame's contract (frame.py docstring) is to drop any location
    # with no geometry, reporting it -- so only the 2 wards survive into
    # the frame; country/state/lga are correctly reported as `no_geometry`
    # rather than silently fabricated or wrongly included.
    #
    # bake also doesn't stamp a PCODE_SYSTEM identifier (P-code conflation
    # is a separate non-goal, backfilled later), so the export side's usual
    # pcode-derived `country` column has nothing to derive from. This is
    # the same situation `kiln transform --country` exists for: pass the
    # code explicitly, exactly as the CLI would for freshly-baked data.
    frame = build_frame(
        [loc for loc in locations if loc], report, country_override="NGA"
    )

    assert len(frame) == 2
    ward = frame[frame["id"] == "nga-ba-alkaleri-alkaleri-east"].iloc[0]
    assert ward["admin0_name"] == "Nigeria"
    assert ward["admin1_name"] == "Bauchi"
    assert ward["admin2_name"] == "Alkaleri"
    assert ward["admin3_name"] == "Alkaleri East"
    assert ward["geom_type"] == "polygon"
    assert ward["country"] == "NGA"
    assert ward.geometry.is_valid and not ward.geometry.is_empty

    assert report.counts() == {"no_geometry": 3}
