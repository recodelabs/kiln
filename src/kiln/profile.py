"""ICR Location profile mapping.

Everything profile-specific lives here. Supporting a different Location
profile means editing this module and nothing else.

Profile: https://icr.healthcampaigns.org/StructureDefinition-ICRLocation.html
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field

from kiln.shape import as_list

BOUNDARY_EXTENSION_URL = (
    "https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson"
)
OVERLAYS_EXTENSION_URL = "https://icr.healthcampaigns.org/StructureDefinition/overlays-admin-unit"
SETTLEMENT_TYPE_EXTENSION_URL = (
    "https://icr.healthcampaigns.org/StructureDefinition/settlement-type"
)
DELIVERY_STRATEGY_EXTENSION_URL = (
    "https://icr.healthcampaigns.org/StructureDefinition/delivery-strategy"
)

PCODE_SYSTEM = "https://icr.healthcampaigns.org/identifiers/pcode"
GERS_SYSTEM = "https://icr.healthcampaigns.org/identifiers/overture-gers"

GEOJSON_CONTENT_TYPE = "application/geo+json"

ADMIN_UNIT_TYPE = "admin-unit"


@dataclass
class BoundaryRef:
    """A boundary attachment: either decoded inline bytes, or a URL to fetch."""

    data: bytes | None = None
    url: str | None = None


@dataclass
class RawLocation:
    """One FHIR Location flattened into the fields kiln cares about."""

    id: str
    name: str | None = None
    status: str | None = None
    loc_type: str | None = None
    physical_type: str | None = None
    pcode: str | None = None
    gers_id: str | None = None
    identifiers: list[dict] = field(default_factory=list)
    parent_id: str | None = None
    overlays_admin_unit_ids: list[str] = field(default_factory=list)
    settlement_type: str | None = None
    delivery_strategy: str | None = None
    position: tuple[float, float] | None = None
    boundary: BoundaryRef | None = None
    last_updated: str | None = None


def _first_coding_code(node: dict | list | None) -> str | None:
    """Pull the first coding.code out of a CodeableConcept or list of them."""
    if isinstance(node, list):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return None
    codings = as_list(node.get("coding"))
    if codings and isinstance(codings[0], dict):
        return codings[0].get("code")
    return None


def _strip_reference(reference: str | None) -> str | None:
    """`Location/loc-1` -> `loc-1`. Bare ids pass through unchanged."""
    if not reference or not isinstance(reference, str):
        return None
    return reference.rsplit("/", 1)[-1]


def _identifier_value(identifiers: list[dict], system: str) -> str | None:
    for identifier in identifiers:
        if isinstance(identifier, dict) and identifier.get("system") == system:
            return identifier.get("value")
    return None


def _read_boundary(extension: dict, location_id: str, report) -> BoundaryRef | None:
    value_attachment = extension.get("valueAttachment")
    if not isinstance(value_attachment, dict):
        report.add(
            "malformed_field",
            location_id,
            "boundary extension valueAttachment is not a dict",
        )
        return None
    attachment = value_attachment
    content_type = attachment.get("contentType")
    if content_type != GEOJSON_CONTENT_TYPE:
        report.add(
            "boundary_bad_content_type",
            location_id,
            f"expected {GEOJSON_CONTENT_TYPE}, got {content_type!r}",
        )
        return None

    if attachment.get("data"):
        try:
            return BoundaryRef(data=base64.b64decode(attachment["data"], validate=True))
        except (binascii.Error, ValueError, TypeError) as exc:
            report.add("boundary_bad_base64", location_id, str(exc))
            return None

    if attachment.get("url"):
        return BoundaryRef(url=attachment["url"])

    report.add("boundary_empty", location_id, "attachment has neither data nor url")
    return None


def shred(resource: dict, report) -> RawLocation | None:
    """Flatten one FHIR Location resource. Returns None if it has no id.

    `resource` is untrusted: it may have come straight off the wire (a
    Bundle entry) or out of a local NDJSON file nobody has validated, so
    even its top-level shape cannot be assumed.
    """
    if not isinstance(resource, dict):
        report.add("malformed_field", "<unknown>", "Location resource is not a dict")
        return None

    location_id = resource.get("id")
    if not location_id:
        report.add("missing_id", "<unknown>", "Location resource has no id")
        return None
    if not isinstance(location_id, str):
        report.add(
            "malformed_field", "<unknown>", f"Location id is not a string: {location_id!r}"
        )
        return None

    identifiers = resource.get("identifier")
    if identifiers is None:
        identifiers = []
    elif not isinstance(identifiers, list):
        report.add("malformed_field", location_id, "identifier is not a list")
        identifiers = []

    position = None
    if isinstance(resource.get("position"), dict):
        pos = resource["position"]
        longitude, latitude = pos.get("longitude"), pos.get("latitude")
        if longitude is not None and latitude is not None:
            # FHIR is longitude-first. x = longitude, y = latitude.
            try:
                position = (float(longitude), float(latitude))
            except (TypeError, ValueError):
                report.add(
                    "malformed_field",
                    location_id,
                    f"position coordinates not numeric: ({longitude!r}, {latitude!r})",
                )

    # Extract parent_id, guarding against non-dict partOf
    parent_id = None
    part_of = resource.get("partOf")
    if part_of is not None:
        if isinstance(part_of, dict):
            parent_id = _strip_reference(part_of.get("reference"))
        else:
            report.add("malformed_field", location_id, "partOf is not a dict")

    # Extract last_updated, guarding against non-dict meta
    last_updated = None
    meta = resource.get("meta")
    if meta is not None:
        if isinstance(meta, dict):
            last_updated = meta.get("lastUpdated")
        else:
            report.add("malformed_field", location_id, "meta is not a dict")

    # Build identifiers list, guarding against non-dict entries
    identifiers_list = []
    for i, identifier in enumerate(identifiers):
        if not isinstance(identifier, dict):
            report.add(
                "malformed_field",
                location_id,
                f"identifier[{i}] is not a dict",
            )
            continue
        identifiers_list.append(
            {"system": identifier.get("system"), "value": identifier.get("value")}
        )

    raw = RawLocation(
        id=location_id,
        name=resource.get("name"),
        status=resource.get("status"),
        loc_type=_first_coding_code(resource.get("type")),
        physical_type=_first_coding_code(resource.get("physicalType")),
        pcode=_identifier_value(identifiers, PCODE_SYSTEM),
        gers_id=_identifier_value(identifiers, GERS_SYSTEM),
        identifiers=identifiers_list,
        parent_id=parent_id,
        position=position,
        last_updated=last_updated,
    )

    extensions = resource.get("extension")
    if extensions is None:
        extensions = []
    elif not isinstance(extensions, list):
        report.add("malformed_field", location_id, "extension is not a list")
        extensions = []

    for extension in extensions:
        if not isinstance(extension, dict):
            report.add("malformed_field", location_id, "extension entry is not a dict")
            continue
        url = extension.get("url")
        if url == BOUNDARY_EXTENSION_URL and raw.boundary is None:
            raw.boundary = _read_boundary(extension, location_id, report)
        elif url == OVERLAYS_EXTENSION_URL:
            value_reference = extension.get("valueReference")
            if isinstance(value_reference, dict):
                target = _strip_reference(value_reference.get("reference"))
                if target:
                    raw.overlays_admin_unit_ids.append(target)
            elif value_reference is not None:
                report.add(
                    "malformed_field",
                    location_id,
                    "overlays extension valueReference is not a dict",
                )
        elif url == SETTLEMENT_TYPE_EXTENSION_URL:
            raw.settlement_type = extension.get("valueCode")
        elif url == DELIVERY_STRATEGY_EXTENSION_URL:
            raw.delivery_strategy = extension.get("valueCode")

    return raw
