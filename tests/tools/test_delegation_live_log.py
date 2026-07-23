from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tools import delegation_live_log as live_log
from tools.delegation_live_log import (
    LiveTranscriptWriter,
    attach_live_transcript_callbacks,
    create_live_transcripts,
    prune_stale_live_dirs,
    update_manifest_statuses,
)


def _writer(tmp_path: Path, name: str = "deleg_test") -> LiveTranscriptWriter:
    return LiveTranscriptWriter(name, 0, "inspect repository", root=tmp_path)


def test_writer_records_ordered_events_and_streams(tmp_path):
    writer = _writer(tmp_path)
    writer.add_stream_delta("first ")
    writer.add_stream_delta("answer")
    writer.tool_start("terminal", {"command": "pwd"})
    writer.tool_result("terminal", "/workspace", duration=0.25)
    writer.thinking("verify result")
    writer.finalize({"status": "completed", "exit_reason": "completed"})

    content = writer.path.read_text(encoding="utf-8")
    assert "first answer" in content
    assert "-> terminal" in content
    assert "terminal ok 0.2s" in content
    assert "verify result" in content
    assert "end status=completed" in content
    assert content.index("first answer") < content.index("-> terminal")


def test_writer_redacts_every_secret_bearing_surface(tmp_path):
    api_key = "sk-proj-" + "L" * 32
    bearer = "sk-ant-api03-" + "R" * 32
    aws_secret = "wJalrXUtnFEMIK7MDENG" + "bPxRfiCY"
    writer = LiveTranscriptWriter(
        "deleg_secrets",
        0,
        f"deploy with {bearer}",
        context=f"OPENAI_API_KEY={api_key}",
        root=tmp_path,
    )
    writer.tool_start("terminal", f"Authorization: Bearer {bearer}")
    writer.tool_result(
        "terminal",
        f"OPENAI_API_KEY={api_key} AWS_SECRET_ACCESS_KEY={aws_secret}",
    )
    writer.add_stream_delta(f"provider returned {api_key}")
    writer.flush_stream()

    content = writer.path.read_text(encoding="utf-8")
    for secret in (api_key, bearer, aws_secret):
        assert secret not in content
    assert "OPENAI_API_KEY" in content


def test_redaction_failure_withholds_line(monkeypatch, tmp_path):
    import agent.redact as redact

    monkeypatch.setattr(
        redact,
        "redact_sensitive_text",
        MagicMock(side_effect=RuntimeError("redactor unavailable")),
    )
    writer = _writer(tmp_path, "deleg_fail_closed")
    writer.assistant_text("sensitive payload")

    content = writer.path.read_text(encoding="utf-8")
    assert "sensitive payload" not in content
    assert "line withheld" in content


def test_attach_callbacks_is_idempotent_and_preserves_inner_callbacks(tmp_path):
    writer = _writer(tmp_path, "deleg_callbacks")
    seen = []
    child = SimpleNamespace(
        tool_progress_callback=lambda *args, **kwargs: seen.append(("tool", args)),
        stream_delta_callback=lambda delta: seen.append(("stream", delta)),
        reasoning_callback=lambda delta: seen.append(("reasoning", delta)),
    )

    attach_live_transcript_callbacks(child, writer)
    first_progress = child.tool_progress_callback
    attach_live_transcript_callbacks(child, writer)
    assert child.tool_progress_callback is first_progress

    child.tool_progress_callback("tool.started", "terminal", "echo ok", None)
    child.stream_delta_callback("finished")
    child.stream_delta_callback(None)
    child.reasoning_callback("checking")

    content = writer.path.read_text(encoding="utf-8")
    assert "-> terminal(echo ok)" in content
    assert "finished" in content
    assert "checking" in content
    assert [kind for kind, _value in seen] == [
        "tool",
        "stream",
        "stream",
        "reasoning",
    ]


def test_attach_callbacks_preserves_execution_identity_binders(tmp_path):
    writer = _writer(tmp_path, "deleg_identity")
    bindings = []

    def callback(*_args, **_kwargs):
        return None

    callback._bind_execution_identity = lambda **identity: bindings.append(identity)
    child = SimpleNamespace(
        tool_progress_callback=callback,
        stream_delta_callback=callback,
        reasoning_callback=callback,
    )

    attach_live_transcript_callbacks(child, writer)
    expected = {
        "activity_id": "activity-child",
        "delegation_activity_id": "activity-dispatch",
        "owner_activity_id": "activity-parent",
    }
    child.tool_progress_callback._bind_execution_identity(**expected)
    child.stream_delta_callback._bind_execution_identity(**expected)
    child.reasoning_callback._bind_execution_identity(**expected)

    assert bindings == [expected, expected, expected]


def test_create_manifest_and_update_incrementally(monkeypatch, tmp_path):
    monkeypatch.setattr(live_log, "live_transcript_root", lambda: tmp_path)
    delegation_id, writers, paths = create_live_transcripts(
        [{"goal": "alpha"}, {"goal": "beta", "context": "second"}]
    )

    assert delegation_id is not None
    assert len(writers) == len(paths) == 2
    assert all(Path(path).exists() for path in paths)
    manifest_path = tmp_path / delegation_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [task["status"] for task in manifest["tasks"]] == ["running", "running"]

    update_manifest_statuses(
        delegation_id,
        [{"task_index": 0, "status": "completed"}],
        completed=False,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [task["status"] for task in manifest["tasks"]] == [
        "completed",
        "running",
    ]
    assert "completed" not in manifest

    update_manifest_statuses(
        delegation_id,
        [{"task_index": 1, "status": "error", "exit_reason": "failed"}],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tasks"][1]["exit_reason"] == "failed"
    assert "completed" in manifest


def test_manifest_keeps_task_indices_aligned_on_partial_writer_failure(
    monkeypatch,
    tmp_path,
):
    original_writer = live_log.LiveTranscriptWriter
    calls = 0

    def partial_writer(*args, **kwargs):
        nonlocal calls
        writer = original_writer(*args, root=tmp_path, **kwargs)
        if calls == 0:
            writer.path = None
        calls += 1
        return writer

    monkeypatch.setattr(live_log, "LiveTranscriptWriter", partial_writer)
    monkeypatch.setattr(live_log, "live_transcript_root", lambda: tmp_path)
    delegation_id, writers, paths = create_live_transcripts(
        [{"goal": "first"}, {"goal": "second"}]
    )

    assert writers[0] is None
    assert writers[1] is not None
    assert len(paths) == 1 and paths[0].endswith("task-1.log")
    manifest = json.loads(
        (tmp_path / delegation_id / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["tasks"][0]["log"] is None
    assert manifest["tasks"][1]["log"].endswith("task-1.log")


def test_prune_removes_only_expired_delegation_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(live_log, "live_transcript_root", lambda: tmp_path)
    old_dir = tmp_path / "deleg_old"
    recent_dir = tmp_path / "deleg_recent"
    old_dir.mkdir()
    recent_dir.mkdir()
    stale = time.time() - 8 * 86400
    os.utime(old_dir, (stale, stale))

    assert prune_stale_live_dirs(max_age_days=7) == 1
    assert not old_dir.exists()
    assert recent_dir.exists()
