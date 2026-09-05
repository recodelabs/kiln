# Run once from the repo root to produce tests/fixtures/locations.ndjson
import base64
import json
import pathlib

B = "https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson"
P = "https://icr.healthcampaigns.org/identifiers/pcode"

def b64(geojson): return base64.b64encode(json.dumps(geojson).encode()).decode()
def box(x, y, s=1):
    return {"type": "Polygon", "coordinates": [[[x, y], [x + s, y], [x + s, y + s],
                                                [x, y + s], [x, y]]]}
def loc(id_, name, type_, parent=None, pcode=None, position=None,
        boundary=None, boundary_url=None):
    r = {"resourceType": "Location", "id": id_, "name": name, "status": "active",
         "type": [{"coding": [{"code": type_}]}]}
    if pcode:
        r["identifier"] = [{"system": P, "value": pcode}]
    if parent:
        r["partOf"] = {"reference": f"Location/{parent}"}
    if position:
        r["position"] = {"longitude": position[0], "latitude": position[1]}
    if boundary:
        r["extension"] = [{"url": B, "valueAttachment": {
            "contentType": "application/geo+json", "data": b64(boundary)}}]
    if boundary_url:
        r["extension"] = [{"url": B, "valueAttachment": {
            "contentType": "application/geo+json", "url": boundary_url}}]
    return r

resources = [
    loc("ng", "Nigeria", "admin-unit", pcode="NG", boundary=box(3, 6, 6)),
    loc("kano", "Kano", "admin-unit", parent="ng", pcode="NG001", boundary=box(3, 6, 3)),
    loc("nassarawa", "Nassarawa", "admin-unit", parent="kano", pcode="NG001002",
        boundary=box(3, 6, 1)),
    # settlement with BOTH a position and a boundary -> one row, polygon geometry
    loc("gama", "Gama", "settlement", parent="nassarawa", position=(3.2, 6.2),
        boundary=box(3.1, 6.1, 0.4)),
    loc("clinic", "Gama Clinic", "facility", parent="gama", position=(3.25, 6.25)),
    # point outside its nearest admin ancestor's polygon
    loc("stray", "Stray Post", "facility", parent="nassarawa", position=(50.0, 50.0)),
    # boundary by url -> transform alone cannot resolve it
    loc("remote", "Remote Area", "operational-area", parent="kano",
        boundary_url="https://files.test/remote.geojson"),
    # no geometry at all
    loc("ghost", "Ghost Ward", "admin-unit", parent="kano", pcode="NG001003"),
    # dangling partOf
    loc("orphan", "Orphan Site", "facility", parent="does-not-exist", position=(4.0, 7.0)),
    # duplicate pcode, clashing with nassarawa
    loc("dup", "Duplicate", "admin-unit", parent="kano", pcode="NG001002",
        boundary=box(4, 7, 1)),
    # cycle
    loc("cyc-a", "Cycle A", "admin-unit", parent="cyc-b", position=(5.0, 8.0)),
    loc("cyc-b", "Cycle B", "admin-unit", parent="cyc-a", position=(5.1, 8.1)),
]

out = pathlib.Path("tests/fixtures/locations.ndjson")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in resources) + "\n")
print(f"wrote {len(resources)} resources")
