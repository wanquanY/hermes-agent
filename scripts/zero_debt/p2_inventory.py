#!/usr/bin/env python3
"""Generate P2 data-plane offender inventory.

This is a read-only audit helper for P2 planning. It uses the same production
scan scope and allowlists as ``verdict.py`` so slice-by-slice debt reduction can
be measured without hand-maintained grep summaries.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from aggregate_table_owners import find_shadow_table_writers
from verdict import (
    REPO_ROOT,
    _P2_IDENTITY_ALIAS_ALLOWLIST,
    _P2_IDENTITY_ALIAS_TOKENS,
    _P2_SESSIONDB_ALLOWLIST,
    _P2_SESSIONDB_TOKENS,
    _P2_RETIRED_STATE_PATHS,
    _class_method_count,
    _p2_hermes_state_store_instantiations,
    _p2_silent_swallow_offenders,
    _production_python_files,
)


def _scan(
    *,
    tokens: tuple[str, ...],
    allowlist: set[str],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in _production_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in allowlist:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(lines, start=1):
            matched = [token for token in tokens if token in line]
            if not matched:
                continue
            grouped[rel].append(
                {
                    "line": lineno,
                    "tokens": matched,
                    "text": line.strip(),
                }
            )
    return dict(sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])))


def build_inventory() -> dict[str, Any]:
    sessiondb = _scan(
        tokens=_P2_SESSIONDB_TOKENS,
        allowlist=_P2_SESSIONDB_ALLOWLIST,
    )
    identity_alias = _scan(
        tokens=_P2_IDENTITY_ALIAS_TOKENS,
        allowlist=_P2_IDENTITY_ALIAS_ALLOWLIST,
    )
    retired_state_paths = [
        path for path in _P2_RETIRED_STATE_PATHS if (REPO_ROOT / path).exists()
    ]
    state_store_methods = _class_method_count(
        "hermes_agent/storage/state_store.py",
        "HermesStateStore",
    )
    return {
        "phase": "P2",
        "status": "preflight_only",
        "gates": {
            "p2:no_sessiondb_production": _gate_summary(sessiondb),
            "p2:no_legacy_identity_alias_internal": _gate_summary(identity_alias),
            "p2:no_hermes_state_store_production_instantiation": _list_summary(
                _p2_hermes_state_store_instantiations()
            ),
            "p2:state_store_decomposed": {
                "total_offenders": len(retired_state_paths),
                "file_count": len(retired_state_paths),
                "files": [
                    {
                        "path": path,
                        "offenders": 1,
                        "first_lines": [{"line": "", "text": "legacy module remains"}],
                    }
                    for path in retired_state_paths
                ],
            },
            "p2:hermes_state_store_no_methods": {
                "total_offenders": state_store_methods,
                "file_count": 1 if state_store_methods else 0,
                "files": [
                    {
                        "path": "hermes_agent/storage/state_store.py",
                        "offenders": state_store_methods,
                        "first_lines": [
                            {
                                "line": "",
                                "text": f"HermesStateStore methods: {state_store_methods}",
                            }
                        ],
                    }
                ]
                if state_store_methods
                else [],
            },
            "p2:aggregate_table_single_owner": _shadow_writer_summary(
                find_shadow_table_writers()
            ),
            "p2:no_silent_swallow_in_v3": _list_summary(
                _p2_silent_swallow_offenders()
            ),
        },
    }


def _gate_summary(grouped: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "total_offenders": sum(len(items) for items in grouped.values()),
        "file_count": len(grouped),
        "files": [
            {
                "path": path,
                "offenders": len(items),
                "first_lines": items[:5],
            }
            for path, items in grouped.items()
        ],
    }


def _list_summary(items: list[str]) -> dict[str, Any]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for item in items:
        path = item.split(":", 1)[0]
        grouped[path].append(item)
    return {
        "total_offenders": len(items),
        "file_count": len(grouped),
        "files": [
            {
                "path": path,
                "offenders": len(path_items),
                "first_lines": [{"line": "", "text": text} for text in path_items[:5]],
            }
            for path, path_items in grouped.items()
        ],
    }


def _shadow_writer_summary(items: list[tuple[str, str, str]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path, table, snippet in items:
        grouped[path].append({"line": "", "tokens": [table], "text": snippet})
    return _gate_summary(dict(grouped))


def _render_markdown(inventory: dict[str, Any]) -> str:
    lines = [
        "# P2 Offender Inventory",
        "",
        "Generated by `scripts/zero_debt/p2_inventory.py`.",
        "",
        "Status: `preflight_only`",
        "",
        "## Summary",
        "",
        "| Gate | Total offenders | Files |",
        "|---|---:|---:|",
    ]
    for gate, data in inventory["gates"].items():
        lines.append(f"| `{gate}` | {data['total_offenders']} | {data['file_count']} |")

    for gate, data in inventory["gates"].items():
        lines.extend(
            [
                "",
                f"## {gate}",
                "",
                "| Offenders | File | First evidence |",
                "|---:|---|---|",
            ]
        )
        for file_info in data["files"]:
            first = file_info["first_lines"][0] if file_info["first_lines"] else {}
            text = str(first.get("text") or "").replace("|", "\\|")
            lines.append(
                f"| {file_info['offenders']} | `{file_info['path']}` | "
                f"`{first.get('line', '')}` {text} |"
            )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate P2 offender inventory")
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="Output format",
    )
    args = parser.parse_args(argv)

    inventory = build_inventory()
    if args.format == "markdown":
        print(_render_markdown(inventory), end="")
    else:
        print(json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
