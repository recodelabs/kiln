"""Offline: one-level GeoJSON FeatureCollection -> ICRLocation NDJSON resources.

The import mirror of frame.py's transform: pure, no network, no GDAL.
All FHIR-shape knowledge is delegated to `kiln.profile.build_location`.
Spec: docs/superpowers/specs/2026-08-05-admin-import-design.md.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


class BakeError(ValueError):
    """Fatal input/mapping problem: nothing is written, CLI exits 2.

    Distinct from per-feature data issues, which go to `Report` and skip
    only the affected feature. A BakeError means the *run* is wrong -- a
    mapping typo, the wrong CRS, a slug collision that would silently
    merge two admin units -- and proceeding would bake bad structure into
    every emitted resource.
    """


@dataclass(frozen=True)
class LevelSpec:
    """One `--level name=name_prop[:code_prop]` mapping flag, parsed."""

    name: str
    name_prop: str
    code_prop: str | None


_HYPHEN_RUNS = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase, ASCII-fold, non-alphanumerics -> '-', collapsed and stripped."""
    folded = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    return _HYPHEN_RUNS.sub("-", folded.lower()).strip("-")


def parse_country_arg(arg: str) -> tuple[str, str]:
    """`Nigeria=NGA` -> (name, code)."""
    name, sep, code = arg.partition("=")
    if not sep or not name or not code:
        raise BakeError(f"--country must be NAME=CODE (e.g. 'Nigeria=NGA'), got {arg!r}")
    return name, code


def parse_level_arg(arg: str) -> LevelSpec:
    """`state=state:statecode` or `ward=ward` -> LevelSpec."""
    name, sep, props = arg.partition("=")
    if not sep or not name or not props:
        raise BakeError(
            f"--level must be LEVEL=NAME_PROP[:CODE_PROP] (e.g. 'state=state:statecode'), "
            f"got {arg!r}"
        )
    parts = props.split(":")
    if len(parts) == 1:
        return LevelSpec(name=name, name_prop=parts[0], code_prop=None)
    if len(parts) == 2 and parts[0] and parts[1]:
        return LevelSpec(name=name, name_prop=parts[0], code_prop=parts[1])
    raise BakeError(f"--level {arg!r}: expected at most one ':' separating NAME_PROP:CODE_PROP")


def parse_alias_args(args: list[str]) -> dict[str, str]:
    """`['lga=lga_alt_names']` -> {'lga': 'lga_alt_names'}."""
    aliases: dict[str, str] = {}
    for arg in args:
        level, sep, prop = arg.partition("=")
        if not sep or not level or not prop:
            raise BakeError(f"--alias must be LEVEL=PROPERTY, got {arg!r}")
        aliases[level] = prop
    return aliases
