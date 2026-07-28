"""kiln command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kiln import __version__
from kiln.extract import (
    fetch_locations,
    read_ndjson,
    resolve_boundary_urls,
    write_ndjson,
)
from kiln.frame import build_frame
from kiln.profile import BOUNDARY_EXTENSION_URL, shred
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
            if not isinstance(extension, dict) or extension.get("url") != BOUNDARY_EXTENSION_URL:
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
    resolve_boundary_urls(resources, report, token=args.token)
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
    resources = list(read_ndjson(source))
    _note_unresolved_boundary_urls(resources, report)

    locations = [loc for loc in (shred(r, report) for r in resources) if loc is not None]
    frame = build_frame(locations, report, country_override=args.country)

    if not frame.empty:
        check_duplicate_pcodes(frame, report)
        check_points_within_parents(frame, report)

    out_dir = Path(args.out)
    try:
        written = write_dataset(
            frame,
            out_dir,
            report,
            partition_by=tuple(args.partition_by.split(",")),
            row_group_size=args.row_group_size,
            geo_types=args.geo_types,
        )
    except KilnWriteError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE_ERROR

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_report.json").write_text(json.dumps(report.to_dict(), indent=2))

    print(f"Wrote {len(frame)} rows across {len(written)} partitions to {out_dir}")
    print(report.summary())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ndjson = Path(args.out) / "locations.ndjson"
    extract_args = argparse.Namespace(
        server=args.server, token=args.token, since=args.since, out=str(ndjson)
    )
    code = cmd_extract(extract_args)
    if code != 0:
        return code
    args.input = str(ndjson)
    return cmd_transform(args)


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

    extract = subparsers.add_parser("extract", help="Fetch Locations from a FHIR server")
    extract.add_argument("--server", required=True)
    extract.add_argument("--token", default=None)
    extract.add_argument("--since", default=None)
    extract.add_argument("--out", required=True)
    extract.set_defaults(func=cmd_extract)

    transform = subparsers.add_parser("transform", help="Convert NDJSON to GeoParquet")
    transform.add_argument("--in", dest="input", required=True)
    add_transform_options(transform)
    transform.set_defaults(func=cmd_transform)

    run = subparsers.add_parser("run", help="Extract then transform")
    run.add_argument("--server", required=True)
    run.add_argument("--token", default=None)
    run.add_argument("--since", default=None)
    add_transform_options(run)
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    import os

    if getattr(args, "token", None) is None:
        args.token = os.environ.get("KILN_TOKEN")

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
