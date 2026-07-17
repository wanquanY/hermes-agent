from __future__ import annotations

import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "docs" / "upstream-sync" / "v2026.7" / "absorption-ledger.csv"
ALLOWED_DECISIONS = {"absorb", "equivalent", "skip-with-reason", "superseded"}
REQUIRED_COLUMNS = {
    "id",
    "kind",
    "phase",
    "decision",
    "title",
    "upstream_refs",
    "acceptance",
    "reason",
    "aliases",
}


def _rows() -> list[dict[str, str]]:
    with LEDGER.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_absorption_ledger_is_complete_and_machine_readable() -> None:
    rows = _rows()
    assert rows
    assert REQUIRED_COLUMNS <= set(rows[0])
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids))
    assert all(row["decision"] in ALLOWED_DECISIONS for row in rows)
    assert all(row["title"].strip() for row in rows)
    assert all(row["acceptance"].strip() for row in rows)
    assert all(row["reason"].strip() for row in rows)


def test_absorption_ledger_covers_all_audited_ids() -> None:
    ids = {row["id"] for row in _rows()}
    expected = {
        *(f"A{i}" for i in range(1, 6)),
        *(f"B{i}" for i in range(1, 7)),
        *(f"C{i}" for i in range(1, 4)),
        *(f"CB{i}" for i in range(1, 4)),
        *(f"CX{i}" for i in range(1, 5)),
        *(f"D{i}" for i in range(1, 5)),
        *(f"M{i}" for i in range(1, 5)),
        *(f"R{i}" for i in range(1, 5)),
        *(f"RT{i}" for i in range(1, 9)),
        *(f"U{i}" for i in range(1, 13)),
        *(f"SEC-CMD-{i:02d}" for i in range(1, 11)),
        *(f"SEC-PATH-{i:02d}" for i in range(1, 11)),
        *(f"SEC-CRED-{i:02d}" for i in range(1, 8)),
        *(f"SEC-NET-{i:02d}" for i in range(1, 5)),
        *(f"SEC-PROMPT-{i:02d}" for i in range(1, 4)),
        *(f"SEC-PLUGIN-{i:02d}" for i in range(1, 8)),
        *(f"SEC-SURFACE-{i:02d}" for i in range(1, 7)),
        *(f"LG{i}" for i in range(1, 5)),
        "BR1",
    }
    assert ids == expected


def test_high_risk_aliases_resolve_to_security_rows() -> None:
    aliases = {
        alias
        for row in _rows()
        for alias in row["aliases"].split(";")
        if alias
    }
    assert {f"S{i}" for i in range(1, 9)} <= aliases
