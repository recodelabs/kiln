"""kiln command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from kiln import __version__
from kiln.extract import (
    DEFAULT_CONCURRENCY,
    DEFAULT_RETRIES,
    MalformedNdjsonError,
    fetch_locations,
    read_ndjson,
    resolve_boundary_urls,
    write_ndjson,
)
from kiln.frame import build_frame
from kiln.inspect import format_summary, summarize
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
    resolve_boundary_urls(
        resources,
        report,
        token=args.token,
        concurrency=args.concurrency,
        retries=args.retries,
    )
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "token", None) is None:
        args.token = os.environ.get("KILN_TOKEN")

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
