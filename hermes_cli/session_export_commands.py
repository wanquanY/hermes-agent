"""Session export command application service.

Keeps export policy out of the CLI composition root. Trace export is redacted
by default and uploads only in response to an explicit ``--upload`` request.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from hermes_constants import get_hermes_home


def run_session_export(args: Any, db: Any) -> None:
    export_format = str(getattr(args, "format", "jsonl") or "jsonl")
    if export_format == "trace":
        _run_trace_export(args, db)
        return
    _run_jsonl_export(args, db)


def _run_jsonl_export(args: Any, db: Any) -> None:
    output = str(getattr(args, "output", "") or "")
    if not output:
        print("JSONL export requires an output path (use - for stdout).")
        return

    requested = str(getattr(args, "session_id", "") or "")
    if requested:
        resolved = db.sessions.resolve_id(requested)
        if not resolved:
            print(f"Session '{requested}' not found.")
            return
        data = db.sessions.export(resolved)
        if not data:
            print(f"Session '{requested}' not found.")
            return
        rendered = json.dumps(data, ensure_ascii=False) + "\n"
        if output == "-":
            sys.stdout.write(rendered)
        else:
            Path(output).expanduser().write_text(rendered, encoding="utf-8")
            print(f"Exported 1 session to {output}")
        return

    sessions = db.sessions.export_all(source=getattr(args, "source", None))
    rendered = "".join(
        json.dumps(session, ensure_ascii=False) + "\n" for session in sessions
    )
    if output == "-":
        sys.stdout.write(rendered)
    else:
        Path(output).expanduser().write_text(rendered, encoding="utf-8")
        print(f"Exported {len(sessions)} sessions to {output}")


def _trace_session_ids(args: Any, db: Any) -> list[str]:
    requested = str(getattr(args, "session_id", "") or "")
    if requested:
        resolved = db.sessions.resolve_id(requested)
        if not resolved:
            print(f"Session '{requested}' not found.")
            return []
        return [resolved]

    source = getattr(args, "source", None)
    rows = db.sessions.list_rich(
        source=source,
        exclude_sources=None if source else ["tool"],
        limit=100_000 if source else 1,
    )
    if not rows:
        print("No session found to export. Pass --session-id.")
        return []
    return [str(row.get("id") or "") for row in rows if row.get("id")]


def _render_trace(db: Any, session_id: str, *, redact: bool) -> str:
    from agent.trace_upload import build_trace_jsonl

    meta = db.sessions.get(session_id) or {}
    messages = db.messages.all_as_conversation(
        session_id,
        include_ancestors=True,
        include_storage_metadata=True,
        include_inactive=True,
    )
    if not messages:
        return ""
    return build_trace_jsonl(
        messages,
        session_id=session_id,
        model=str(meta.get("model") or ""),
        cwd=str(meta.get("cwd") or ""),
        redact=redact,
    )


def _run_trace_export(args: Any, db: Any) -> None:
    from agent.trace_upload import TraceRedactionError, upload_session_trace

    session_ids = _trace_session_ids(args, db)
    if not session_ids:
        return
    redact = not bool(getattr(args, "no_redact", False))
    upload = bool(getattr(args, "upload", False))
    public = bool(getattr(args, "public", False))

    if public and not upload:
        print("--public is only valid with --format trace --upload.")
        return
    if upload:
        if len(session_ids) != 1:
            print("--upload exports exactly one session; pass --session-id.")
            return
        status = upload_session_trace(
            session_ids[0],
            redact=redact,
            private=not public,
            db_path=getattr(db, "db_path", None),
        )
        print(status)
        return

    try:
        rendered = [
            (session_id, _render_trace(db, session_id, redact=redact))
            for session_id in session_ids
        ]
    except TraceRedactionError:
        print("Redaction failed; refusing to export unredacted trace content.")
        return

    output = str(getattr(args, "output", "") or "")
    if len(rendered) == 1:
        session_id, jsonl = rendered[0]
        if not jsonl:
            print(f"No transcript to export for session '{session_id}'.")
            return
        if not output or output == "-":
            sys.stdout.write(jsonl)
        else:
            Path(output).expanduser().write_text(jsonl, encoding="utf-8")
            print(f"Exported 1 session trace to {output}")
        return

    output_dir = (
        Path(output).expanduser()
        if output and output != "-"
        else get_hermes_home() / "session-exports"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    exported = 0
    for session_id, jsonl in rendered:
        if not jsonl:
            continue
        (output_dir / f"{session_id}.trace.jsonl").write_text(
            jsonl,
            encoding="utf-8",
        )
        exported += 1
    print(f"Exported {exported} session trace(s) to {output_dir}")
