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
    total_size_bytes = 0

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

        # Calculate min and max row group sizes
        row_group_sizes = []
        for i in range(groups):
            row_group_sizes.append(metadata.row_group(i).num_rows)

        min_rg_rows = min(row_group_sizes) if row_group_sizes else 0
        max_rg_rows = max(row_group_sizes) if row_group_sizes else 0
        avg_rg_rows = round(rows / groups) if groups else 0

        size_bytes = path.stat().st_size
        total_size_bytes += size_bytes

        partitions.append(
            {
                "path": path.relative_to(out_dir).as_posix(),
                "rows": rows,
                "row_groups": groups,
                "min_row_group_rows": min_rg_rows,
                "avg_row_group_rows": avg_rg_rows,
                "max_row_group_rows": max_rg_rows,
                "size_bytes": size_bytes,
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
            "size_bytes": total_size_bytes,
        },
    }


def _human_size(size_bytes: int) -> str:
    """Convert bytes to human-readable size (KB, MB, GB)."""
    for unit, divisor in [("GB", 1e9), ("MB", 1e6), ("KB", 1e3)]:
        if size_bytes >= divisor:
            return f"{size_bytes / divisor:.1f}{unit}"
    return f"{size_bytes}B"


def format_summary(summary: dict) -> str:
    if not summary["partitions"]:
        return "No parquet files found."
    total_size = _human_size(summary["totals"]["size_bytes"])
    lines = [
        (
            f"{summary['totals']['rows']} rows across "
            f"{summary['totals']['partitions']} partitions ({total_size})"
        ),
        "",
    ]
    for partition in summary["partitions"]:
        size_str = _human_size(partition["size_bytes"])
        lines.append(
            f"  {partition['path']}\n"
            f"    rows={partition['rows']} "
            f"row_groups={partition['row_groups']} "
            f"(min={partition['min_row_group_rows']} "
            f"avg={partition['avg_row_group_rows']} "
            f"max={partition['max_row_group_rows']}) "
            f"size={size_str}\n"
            f"    geo={partition['geo_version']} "
            f"covering={partition['has_covering']} "
            f"types={','.join(partition['geometry_types'])}"
        )
    return "\n".join(lines)
