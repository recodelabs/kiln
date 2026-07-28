"""Summarize a written dataset: partitions, row groups and geo metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq


def summarize(out_dir: Path) -> dict:
    """Describe every parquet file under `out_dir`."""
    out_dir = Path(out_dir)
    partitions: list[dict] = []
    total_rows = 0

    for path in sorted(out_dir.rglob("*.parquet")):
        parquet = pq.ParquetFile(path)
        metadata = parquet.metadata
        geo = {}
        raw = (metadata.metadata or {}).get(b"geo")
        if raw:
            geo = json.loads(raw)
        primary = geo.get("primary_column", "geometry")
        column_meta = geo.get("columns", {}).get(primary, {})

        rows = metadata.num_rows
        groups = metadata.num_row_groups
        total_rows += rows

        partitions.append(
            {
                "path": path.relative_to(out_dir).as_posix(),
                "rows": rows,
                "row_groups": groups,
                "avg_row_group_rows": round(rows / groups) if groups else 0,
                "geometry_types": column_meta.get("geometry_types", []),
                "geo_version": geo.get("version"),
                "has_covering": "covering" in column_meta,
            }
        )

    return {
        "partitions": partitions,
        "totals": {
            "rows": total_rows,
            "partitions": len(partitions),
            "files": len(partitions),
        },
    }


def format_summary(summary: dict) -> str:
    if not summary["partitions"]:
        return "No parquet files found."
    lines = [
        (
            f"{summary['totals']['rows']} rows across "
            f"{summary['totals']['partitions']} partitions"
        ),
        "",
    ]
    for partition in summary["partitions"]:
        lines.append(
            f"  {partition['path']}\n"
            f"    rows={partition['rows']} "
            f"row_groups={partition['row_groups']} "
            f"avg={partition['avg_row_group_rows']}\n"
            f"    geo={partition['geo_version']} "
            f"covering={partition['has_covering']} "
            f"types={','.join(partition['geometry_types'])}"
        )
    return "\n".join(lines)
