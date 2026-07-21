import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli.session_export_commands import run_session_export


class _Sessions:
    def resolve_id(self, session_id):
        return "session-1" if session_id in {"s1", "session-1"} else None

    def get(self, session_id):
        return {"id": session_id, "model": "provider/model", "cwd": "/tmp"}

    def list_rich(self, **_kwargs):
        return [{"id": "session-1"}]

    def export(self, session_id):
        return {"id": session_id, "messages": []}

    def export_all(self, source=None):
        return [{"id": "session-1", "source": source or "cli"}]


class _Messages:
    def all_as_conversation(self, session_id, **_kwargs):
        assert session_id == "session-1"
        return [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]


def _db(tmp_path: Path):
    return SimpleNamespace(
        sessions=_Sessions(),
        messages=_Messages(),
        db_path=tmp_path / "state.db",
    )


def _args(**overrides):
    values = {
        "format": "trace",
        "output": None,
        "source": None,
        "session_id": "s1",
        "upload": False,
        "public": False,
        "no_redact": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_trace_export_writes_claude_code_jsonl(tmp_path):
    output = tmp_path / "trace.jsonl"

    run_session_export(_args(output=str(output)), _db(tmp_path))

    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert [record["type"] for record in records] == ["user", "assistant"]
    assert all(record["sessionId"] == "session-1" for record in records)


def test_trace_upload_is_explicit_and_private_by_default(tmp_path, capsys):
    with patch(
        "agent.trace_upload.upload_session_trace",
        return_value="Uploaded -> https://example.test/trace",
    ) as upload:
        run_session_export(_args(upload=True), _db(tmp_path))

    assert "Uploaded ->" in capsys.readouterr().out
    upload.assert_called_once_with(
        "session-1",
        redact=True,
        private=True,
        db_path=tmp_path / "state.db",
    )


def test_public_trace_requires_explicit_upload(tmp_path, capsys):
    run_session_export(_args(public=True), _db(tmp_path))

    assert "--public is only valid" in capsys.readouterr().out


def test_jsonl_export_remains_backward_compatible(tmp_path):
    output = tmp_path / "sessions.jsonl"

    run_session_export(
        _args(format="jsonl", output=str(output), session_id="s1"),
        _db(tmp_path),
    )

    assert json.loads(output.read_text())["id"] == "session-1"
