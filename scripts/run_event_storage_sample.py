#!/usr/bin/env python3
"""Print run_events storage diagnostics grouped by event_type."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_constants import get_hermes_home
from tui_gateway.services.run_event_storage_sample import (
    collect_run_event_storage_sample,
    format_run_event_storage_sample,
)


def _default_state_db() -> Path:
    return Path(get_hermes_home()).expanduser() / "state.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=_default_state_db(),
        help="Path to Hermes state.db",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=50,
        help="Number of event_type rows to print in table mode",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )
    args = parser.parse_args()

    sample = collect_run_event_storage_sample(args.db)
    if args.json:
        print(json.dumps(sample, ensure_ascii=False, indent=2))
    else:
        print(format_run_event_storage_sample(sample, top=args.top))
    return 1 if sample.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
