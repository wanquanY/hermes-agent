"""Read-only run_events storage sampling diagnostics."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from math import ceil
from pathlib import Path
from typing import Any


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        return {str(row["name"]) for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()}
    except sqlite3.OperationalError:
        return set()


def _percentile(sorted_values: list[int], percentile: float) -> int:
    if not sorted_values:
        return 0
    bounded = min(100.0, max(0.0, float(percentile)))
    index = max(0, ceil((bounded / 100.0) * len(sorted_values)) - 1)
    return sorted_values[index]


def collect_run_event_storage_sample(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path).expanduser()
    result: dict[str, Any] = {
        "db_path": str(path),
        "exists": path.exists(),
        "total_rows": 0,
        "total_payload_bytes": 0,
        "by_event_type": [],
        "error": "",
    }
    if not path.exists():
        result["error"] = "state.db not found"
        return result

    sizes_by_type: dict[str, list[int]] = defaultdict(list)
    try:
        with _connect_readonly(path) as conn:
            if not _table_exists(conn, "run_events"):
                result["error"] = "run_events table not found"
                return result
            columns = _table_columns(conn, "run_events")
            payload_terms = [
                f"COALESCE(length({column}), 0)"
                for column in ("payload_json", "event_json", "frame_blob")
                if column in columns
            ] or ["0"]
            rows = conn.execute(
                f"""
                SELECT COALESCE(event_type, '') AS event_type,
                       {' + '.join(payload_terms)} AS payload_bytes
                FROM run_events
                """
            )
            for row in rows:
                event_type = str(row["event_type"] or "unknown")
                payload_bytes = _safe_int(row["payload_bytes"])
                sizes_by_type[event_type].append(payload_bytes)
                result["total_rows"] += 1
                result["total_payload_bytes"] += payload_bytes
    except sqlite3.Error as exc:
        result["error"] = str(exc)
        return result

    by_event_type: list[dict[str, Any]] = []
    for event_type, sizes in sizes_by_type.items():
        sorted_sizes = sorted(sizes)
        by_event_type.append(
            {
                "event_type": event_type,
                "rows": len(sorted_sizes),
                "payload_bytes": sum(sorted_sizes),
                "p50_payload_bytes": _percentile(sorted_sizes, 50),
                "p95_payload_bytes": _percentile(sorted_sizes, 95),
                "max_payload_bytes": sorted_sizes[-1] if sorted_sizes else 0,
            }
        )
    result["by_event_type"] = sorted(
        by_event_type,
        key=lambda item: (
            -_safe_int(item.get("payload_bytes")),
            -_safe_int(item.get("rows")),
            str(item.get("event_type") or ""),
        ),
    )
    return result


def format_run_event_storage_sample(sample: dict[str, Any], *, top: int = 50) -> str:
    lines = [
        f"state_db: {sample.get('db_path')}",
        f"total_rows: {_safe_int(sample.get('total_rows'))}",
        f"total_payload_bytes: {_safe_int(sample.get('total_payload_bytes'))}",
    ]
    error = str(sample.get("error") or "")
    if error:
        lines.append(f"error: {error}")
        return "\n".join(lines)

    rows = list(sample.get("by_event_type") or [])[: max(0, int(top or 0))]
    if not rows:
        lines.append("event_type rows payload_bytes p50 p95 max")
        return "\n".join(lines)

    headers = ("event_type", "rows", "payload_bytes", "p50", "p95", "max")
    rendered_rows = [
        (
            str(row.get("event_type") or "unknown"),
            str(_safe_int(row.get("rows"))),
            str(_safe_int(row.get("payload_bytes"))),
            str(_safe_int(row.get("p50_payload_bytes"))),
            str(_safe_int(row.get("p95_payload_bytes"))),
            str(_safe_int(row.get("max_payload_bytes"))),
        )
        for row in rows
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rendered_rows))
        for index in range(len(headers))
    ]
    lines.append(" ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    for row in rendered_rows:
        lines.append(" ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(lines)
