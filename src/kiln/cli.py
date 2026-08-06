"""kiln command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from kiln import __version__
from kiln.bake import (
    BakeError,
    bake,
    parse_alias_args,
    parse_country_arg,
    parse_level_arg,
)
from kiln.cache import DEFAULT_CACHE_DIR
from kiln.extract import (
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    DEFAULT_RETRIES,
    BoundaryFetchAborted,
    MalformedNdjsonError,
    fetch_locations,
    read_ndjson,
    resolve_boundary_urls,
    write_ndjson,
)
from kiln.frame import build_frame
from kiln.inspect import format_summary, summarize
from kiln.load import DEFAULT_BATCH_SIZE, LoadError, load
from kiln.points import (
    bake_points,
    parse_identifier_arg,
    parse_parent_arg,
    parse_where_arg,
)
from kiln.profile import BOUNDARY_EXTENSION_URLS, NATIONAL_ADMIN_CODE_SYSTEM, shred
from kiln.report import Report, check_duplicate_pcodes, check_points_within_parents
from kiln.shape import as_list
from kiln.write import (
    DEFAULT_PARTITION_BY,
    DEFAULT_ROW_GROUP_SIZE,
    KilnWriteError,
    probe_gdal,
    write_dataset,
)

USAGE_ERROR = 2


def _note_unresolved_boundary_urls(resources: list[dict], report: Report) -> None:
    """transform is offline; a url-only boundary cannot be fetched here."""
    for resource in resources:
        for extension in as_list(resource.get("extension")):
            ext_url = extension.get("url") if isinstance(extension, dict) else None
            if not isinstance(extension, dict) or ext_url not in BOUNDARY_EXTENSION_URLS:
                continue
            attachment = extension.get("valueAttachment") or {}
            if not isinstance(attachment, dict):
                continue
            if attachment.get("url") and not attachment.get("data"):
                report.add(
                    "boundary_unresolved_url",
                    resource.get("id", "<unknown>"),
                    f"{attachment['url']} — run `kiln extract` to inline it",
                )


def cmd_extract(args: argparse.Namespace) -> int:
    report = Report()
    resources = list(fetch_locations(args.server, args.token, since=args.since))
    try:
        resolve_boundary_urls(
            resources,
            report,
            token=args.token,
            concurrency=args.concurrency,
            retries=args.retries,
            cache_dir=None if args.no_cache else args.cache_dir,
            refresh=args.refresh,
            max_consecutive_failures=args.max_consecutive_failures,
        )
    except BoundaryFetchAborted as exc:
        # Systematic failure (server unreachable, wrong token, bad base
        # URL), not a data problem -- nothing was written, same as the
        # existing RuntimeErrors raised by fetch_locations for a protocol
        # fault (cyclic pagination, a non-JSON page, ...).
        print(f"kiln extract: {exc}", file=sys.stderr)
        return USAGE_ERROR
    count = write_ndjson(resources, Path(args.out))
    print(f"Wrote {count} Locations to {args.out}")
    print(report.summary())
    return 0


def cmd_transform(args: argparse.Namespace) -> int:
    source = Path(args.input)
    if not source.exists():
        print(f"Input file not found: {source}", file=sys.stderr)
        return USAGE_ERROR

    try:
        probe_gdal()
    except KilnWriteError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE_ERROR

    report = Report()
    try:
        resources = list(read_ndjson(source, report))
    except MalformedNdjsonError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE_ERROR
    _note_unresolved_boundary_urls(resources, report)

    locations = [loc for loc in (shred(r, report) for r in resources) if loc is not None]
    frame = build_frame(locations, report, country_override=args.country)

    if not frame.empty:
        check_duplicate_pcodes(frame, report)
        check_points_within_parents(frame, report)

    partition_by = tuple(args.partition_by.split(","))
    unknown_keys = [key for key in partition_by if key not in frame.columns]
    if unknown_keys:
        print(
            f"--partition-by: unknown column(s) {', '.join(unknown_keys)}. "
            f"Available columns: {', '.join(sorted(frame.columns))}",
            file=sys.stderr,
        )
        return USAGE_ERROR

    out_dir = Path(args.out)
    report_path = out_dir / "_report.json"
    # write_dataset makes out/locations/ itself atomic -- a failed run
    # leaves it exactly as it was. But cmd_transform used to write
    # _report.json only *after* write_dataset returned, so a failure left
    # whatever report a previous successful run wrote sitting there,
    # unchanged, still claiming that old run's success -- indistinguishable
    # from a report that's actually current. Delete it before attempting
    # the write: on failure the old (untouched) dataset is left with no
    # report describing it at all, which is a much louder, more honest
    # signal than a stale one; on success it's rewritten below to describe
    # exactly what's now on disk.
    report_path.unlink(missing_ok=True)
    try:
        written = write_dataset(
            frame,
            out_dir,
            report,
            partition_by=partition_by,
            row_group_size=args.row_group_size,
            geo_types=args.geo_types,
        )
    except KilnWriteError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE_ERROR

    out_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report.to_dict(), indent=2))

    print(f"Wrote {len(frame)} rows across {len(written)} partitions to {out_dir}")
    print(report.summary())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ndjson = Path(args.out) / "locations.ndjson"
    extract_args = argparse.Namespace(
        server=args.server,
        token=args.token,
        since=args.since,
        out=str(ndjson),
        concurrency=args.concurrency,
        retries=args.retries,
        cache_dir=args.cache_dir,
        no_cache=args.no_cache,
        refresh=args.refresh,
        max_consecutive_failures=args.max_consecutive_failures,
    )
    code = cmd_extract(extract_args)
    if code != 0:
        return code
    args.input = str(ndjson)
    return cmd_transform(args)


def cmd_inspect(args: argparse.Namespace) -> int:
    target = Path(args.out)
    if not target.exists():
        print(f"Directory not found: {target}", file=sys.stderr)
        return USAGE_ERROR
    print(format_summary(summarize(target)))
    return 0


def cmd_bake(args: argparse.Namespace) -> int:
    source = Path(args.input)
    if not source.exists():
        print(f"Input file not found: {source}", file=sys.stderr)
        return USAGE_ERROR

    report = Report()
    try:
        collection = json.loads(source.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"kiln bake: {source} is not valid JSON: {exc}", file=sys.stderr)
        return USAGE_ERROR

    try:
        resources = bake(
            collection,
            parse_country_arg(args.country),
            [parse_level_arg(level) for level in args.level],
            parse_alias_args(args.alias),
            args.code_system,
            report,
            dissolve_parents=args.dissolve_parents,
        )
    except BakeError as exc:
        # Fatal mapping/input problem: nothing written (same contract as
        # BoundaryFetchAborted in cmd_extract).
        print(f"kiln bake: {exc}", file=sys.stderr)
        return USAGE_ERROR

    count = write_ndjson(resources, Path(args.out))
    print(f"Wrote {count} Locations to {args.out}")
    print(report.summary())
    return 0


def cmd_bake_points(args: argparse.Namespace) -> int:
    import csv

    source = Path(args.input)
    admin_path = Path(args.admin)
    for path in (source, admin_path):
        if not path.exists():
            print(f"Input file not found: {path}", file=sys.stderr)
            return USAGE_ERROR

    report = Report()
    try:
        admin_resources = list(read_ndjson(admin_path, report))
    except MalformedNdjsonError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE_ERROR

    with source.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    try:
        resources = bake_points(
            rows,
            admin_resources,
            type_code=args.type,
            name_col=args.name_col,
            lat_col=args.lat_col,
            lon_col=args.lon_col,
            id_col=args.id_col,
            parents=[parse_parent_arg(parent) for parent in args.parent],
            identifiers=[parse_identifier_arg(item) for item in args.identifier],
            where=[parse_where_arg(item) for item in args.where],
            report=report,
        )
    except BakeError as exc:
        print(f"kiln bake-points: {exc}", file=sys.stderr)
        return USAGE_ERROR

    count = write_ndjson(resources, Path(args.out))
    print(f"Wrote {count} Locations to {args.out}")
    print(report.summary())
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    source = Path(args.input)
    if not source.exists():
        print(f"Input file not found: {source}", file=sys.stderr)
        return USAGE_ERROR

    report = Report()
    try:
        resources = list(read_ndjson(source, report))
    except MalformedNdjsonError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE_ERROR

    try:
        count = load(
            resources,
            args.server,
            args.token,
            retries=args.retries,
            batch_size=args.batch_size,
        )
    except LoadError as exc:
        print(f"kiln load: {exc}", file=sys.stderr)
        return USAGE_ERROR

    print(f"loaded: {count} upserted")
    print(report.summary())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kiln", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_transform_options(sub):
        sub.add_argument("--out", required=True, help="Output directory")
        sub.add_argument("--country", default=None, help="Override the derived country code")
        sub.add_argument(
            "--geo-types",
            dest="geo_types",
            choices=["both", "only", "legacy"],
            default="both",
        )
        sub.add_argument("--partition-by", default=",".join(DEFAULT_PARTITION_BY))
        sub.add_argument("--row-group-size", type=int, default=DEFAULT_ROW_GROUP_SIZE)

    def add_extract_options(sub):
        sub.add_argument(
            "--concurrency",
            type=int,
            default=DEFAULT_CONCURRENCY,
            help="Worker threads fetching url-referenced boundary attachments",
        )
        sub.add_argument(
            "--retries",
            type=int,
            default=DEFAULT_RETRIES,
            help="Attempts per boundary fetch before giving up (with backoff)",
        )
        sub.add_argument(
            "--cache-dir",
            dest="cache_dir",
            default=str(DEFAULT_CACHE_DIR),
            help=f"Local disk cache for fetched boundaries (default: {DEFAULT_CACHE_DIR})",
        )
        sub.add_argument(
            "--no-cache",
            dest="no_cache",
            action="store_true",
            help="Disable the boundary cache entirely -- neither read nor write it",
        )
        sub.add_argument(
            "--refresh",
            action="store_true",
            help=(
                "Ignore existing cache entries and re-fetch every boundary, "
                "still writing results back to the cache"
            ),
        )
        sub.add_argument(
            "--max-consecutive-failures",
            dest="max_consecutive_failures",
            type=int,
            default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
            help=(
                "Abort if this many boundary fetches in a row fail with zero "
                "successes anywhere in the run -- a signal of a systematic "
                "problem (server unreachable, wrong token, bad base URL) "
                "rather than a handful of bad boundary URLs. 0 disables this "
                f"check (default: {DEFAULT_MAX_CONSECUTIVE_FAILURES})"
            ),
        )

    extract = subparsers.add_parser("extract", help="Fetch Locations from a FHIR server")
    extract.add_argument("--server", required=True)
    extract.add_argument("--token", default=None)
    extract.add_argument("--since", default=None)
    extract.add_argument("--out", required=True)
    add_extract_options(extract)
    extract.set_defaults(func=cmd_extract)

    transform = subparsers.add_parser("transform", help="Convert NDJSON to GeoParquet")
    transform.add_argument("--in", dest="input", required=True)
    add_transform_options(transform)
    transform.set_defaults(func=cmd_transform)

    run = subparsers.add_parser("run", help="Extract then transform")
    run.add_argument("--server", required=True)
    run.add_argument("--token", default=None)
    run.add_argument("--since", default=None)
    add_extract_options(run)
    add_transform_options(run)
    run.set_defaults(func=cmd_run)

    inspect_cmd = subparsers.add_parser("inspect", help="Summarize a written dataset")
    inspect_cmd.add_argument("--out", required=True, help="Dataset directory")
    inspect_cmd.set_defaults(func=cmd_inspect)

    bake_cmd = subparsers.add_parser(
        "bake", help="Convert one-level admin GeoJSON to Location NDJSON"
    )
    bake_cmd.add_argument("--in", dest="input", required=True, help="GeoJSON file")
    bake_cmd.add_argument(
        "--country", required=True, help="Admin0 root as NAME=CODE, e.g. 'Nigeria=NGA'"
    )
    bake_cmd.add_argument(
        "--level",
        action="append",
        required=True,
        help=(
            "LEVEL=NAME_PROP[:CODE_PROP], repeatable and ordered; the last "
            "--level is the feature level and carries the geometry"
        ),
    )
    bake_cmd.add_argument(
        "--alias",
        action="append",
        default=[],
        help="LEVEL=PROPERTY holding ';'-separated alternate names",
    )
    bake_cmd.add_argument(
        "--code-system",
        dest="code_system",
        default=NATIONAL_ADMIN_CODE_SYSTEM,
        help=f"Identifier system URI for admin codes (default: {NATIONAL_ADMIN_CODE_SYSTEM})",
    )
    bake_cmd.add_argument(
        "--dissolve-parents",
        dest="dissolve_parents",
        action="store_true",
        help=(
            "Give minted ancestor levels derived boundaries too: the union of "
            "their children's geometries (consistent with the child tiling, "
            "not authoritative cartography)"
        ),
    )
    bake_cmd.add_argument("--out", required=True, help="Output NDJSON file")
    bake_cmd.set_defaults(func=cmd_bake)

    points_cmd = subparsers.add_parser(
        "bake-points",
        help="Convert point rows (CSV) to site Location NDJSON linked into an admin registry",
    )
    points_cmd.add_argument("--in", dest="input", required=True, help="CSV file")
    points_cmd.add_argument(
        "--admin",
        required=True,
        help="Baked admin-hierarchy NDJSON to resolve parents against (from kiln bake)",
    )
    points_cmd.add_argument(
        "--type",
        required=True,
        help="ICR location-type code for every row: facility, school, ...",
    )
    points_cmd.add_argument("--name-col", dest="name_col", required=True)
    points_cmd.add_argument("--lat-col", dest="lat_col", required=True)
    points_cmd.add_argument("--lon-col", dest="lon_col", required=True)
    points_cmd.add_argument(
        "--id-col",
        dest="id_col",
        required=True,
        help="Column holding a stable unique id usable as the FHIR resource id",
    )
    points_cmd.add_argument(
        "--parent",
        action="append",
        required=True,
        help=(
            "LEVEL=COLUMN, repeatable and ordered top-down (e.g. state=state "
            "lga=lga ward=ward); rows link to the deepest level that resolves"
        ),
    )
    points_cmd.add_argument(
        "--identifier",
        action="append",
        default=[],
        help="SYSTEM_URI=COLUMN, repeatable; empty column values are dropped",
    )
    points_cmd.add_argument(
        "--where",
        action="append",
        default=[],
        help="COLUMN=VALUE row filter, repeatable (all must match)",
    )
    points_cmd.add_argument("--out", required=True, help="Output NDJSON file")
    points_cmd.set_defaults(func=cmd_bake_points)

    load_cmd = subparsers.add_parser(
        "load", help="Upsert Location NDJSON into a FHIR store"
    )
    load_cmd.add_argument("--server", required=True)
    load_cmd.add_argument("--token", default=None)
    load_cmd.add_argument("--in", dest="input", required=True, help="NDJSON file")
    load_cmd.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Attempts per bundle before giving up (with backoff)",
    )
    load_cmd.add_argument(
        "--batch-size",
        dest="batch_size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Resources per transaction bundle (default: {DEFAULT_BATCH_SIZE})",
    )
    load_cmd.set_defaults(func=cmd_load)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "token", None) is None:
        args.token = os.environ.get("KILN_TOKEN")

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
