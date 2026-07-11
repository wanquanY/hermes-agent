"""Tests for tui_gateway JSON-RPC protocol plumbing."""

import io
import json
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_original_stdout = sys.stdout


@pytest.fixture(autouse=True)
def _restore_stdout():
    yield
    sys.stdout = _original_stdout


@pytest.fixture()
def server(tmp_path):
    hermes_home = tmp_path / "hermes-home"
    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(
            get_hermes_home=MagicMock(return_value=hermes_home),
            get_hermes_dir=MagicMock(
                side_effect=lambda current, _legacy=None: hermes_home / current
            ),
        ),
        "hermes_cli.env_loader": MagicMock(),
        "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        import importlib
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()
        mod._methods.clear()
        importlib.reload(mod)


@pytest.fixture()
def capture(server):
    """Redirect server's real stdout to a StringIO and return (server, buf)."""
    buf = io.StringIO()
    server._real_stdout = buf
    return server, buf


def _resume_gateway_db(tmp_path, rows=(), history_reader=None):
    from hermes_agent.storage.cli_session_store import open_cli_session_store

    db = open_cli_session_store(tmp_path / "resume-state.db")
    for session_id, title in rows:
        db.sessions.create(session_id, source="tui", title=title)
    _seed_resume_messages(db, rows, history_reader)
    return db


def _seed_resume_messages(db, rows=(), history_reader=None) -> None:
    if not callable(history_reader):
        return
    for session_id, _title in rows:
        messages = history_reader(session_id, include_ancestors=True)
        for index, message in enumerate(messages or [], start=1):
            db.messages.append(
                session_id=session_id,
                role=str((message or {}).get("role") or "user"),
                content=(
                    ""
                    if (message or {}).get("content") is None
                    else str((message or {}).get("content") or "")
                ),
                timestamp=float(index),
                metadata=(
                    (message or {}).get("metadata")
                    if isinstance((message or {}).get("metadata"), dict)
                    else None
                ),
            )


# ── JSON-RPC envelope ────────────────────────────────────────────────


def test_unknown_method(server):
    resp = server.handle_request({"id": "1", "method": "bogus"})
    assert resp["error"]["code"] == -32601


def test_ok_envelope(server):
    assert server._ok("r1", {"x": 1}) == {
        "jsonrpc": "2.0", "id": "r1", "result": {"x": 1},
    }


def test_err_envelope(server):
    assert server._err("r2", 4001, "nope") == {
        "jsonrpc": "2.0", "id": "r2", "error": {"code": 4001, "message": "nope"},
    }


# ── write_json ───────────────────────────────────────────────────────


def test_write_json(capture):
    server, buf = capture
    assert server.write_json({"test": True})
    assert json.loads(buf.getvalue()) == {"test": True}


def test_write_json_broken_pipe(server):
    class _Broken:
        def write(self, _): raise BrokenPipeError
        def flush(self): raise BrokenPipeError

    server._real_stdout = _Broken()
    assert server.write_json({"x": 1}) is False


def test_message_delta_normalizer_preserves_trailing_newlines_immediately():
    from tui_gateway.methods.prompt import _MessageDeltaNormalizer

    normalizer = _MessageDeltaNormalizer()

    assert normalizer.feed("让我继续读取") == {
        "mode": "append",
        "text": "让我继续读取",
        "delta": "让我继续读取",
        "offset": 0,
    }
    assert normalizer.feed("。\n\n") == {
        "mode": "append",
        "text": "。\n\n",
        "delta": "。\n\n",
        "offset": 6,
    }
    assert normalizer.feed("下一段") == {
        "mode": "append",
        "text": "下一段",
        "delta": "下一段",
        "offset": 9,
    }


def test_message_delta_normalizer_keeps_newlines_before_tool_boundary():
    from tui_gateway.methods.prompt import _MessageDeltaNormalizer

    normalizer = _MessageDeltaNormalizer()

    assert normalizer.feed("让我继续读取。\n\n") == {
        "mode": "append",
        "text": "让我继续读取。\n\n",
        "delta": "让我继续读取。\n\n",
        "offset": 0,
    }
    assert normalizer.feed(None) is None
    assert normalizer.feed("工具后正文") == {
        "mode": "append",
        "text": "工具后正文",
        "delta": "工具后正文",
        "offset": 9,
    }


def test_message_delta_normalizer_preserves_append_chunk_matching_prior_prefix():
    from tui_gateway.methods.prompt import _MessageDeltaNormalizer

    normalizer = _MessageDeltaNormalizer()

    assert normalizer.feed("文件已创建完成。") == {
        "mode": "append",
        "text": "文件已创建完成。",
        "delta": "文件已创建完成。",
        "offset": 0,
    }
    assert normalizer.feed("\n- **") == {
        "mode": "append",
        "text": "\n- **",
        "delta": "\n- **",
        "offset": 8,
    }
    assert normalizer.feed("文件名**") == {
        "mode": "append",
        "text": "文件名**",
        "delta": "文件名**",
        "offset": 13,
    }
    assert normalizer.text == "文件已创建完成。\n- **文件名**"


def test_message_delta_normalizer_accepts_explicit_snapshot_only():
    from tui_gateway.methods.prompt import _MessageDeltaNormalizer

    normalizer = _MessageDeltaNormalizer()

    assert normalizer.feed("你好") == {
        "mode": "append",
        "text": "你好",
        "delta": "你好",
        "offset": 0,
    }
    assert normalizer.feed({"mode": "snapshot", "text": "你好，世界"}) == {
        "mode": "append",
        "text": "，世界",
        "delta": "，世界",
        "offset": 2,
    }


def test_message_delta_normalizer_offsets_use_utf16_code_units():
    from tui_gateway.methods.prompt import _MessageDeltaNormalizer

    normalizer = _MessageDeltaNormalizer()

    assert normalizer.feed("📋") == {
        "mode": "append",
        "text": "📋",
        "delta": "📋",
        "offset": 0,
    }
    assert normalizer.feed(" 表格") == {
        "mode": "append",
        "text": " 表格",
        "delta": " 表格",
        "offset": 2,
    }
    assert normalizer.feed({"mode": "snapshot", "text": "📋 表格✅"}) == {
        "mode": "append",
        "text": "✅",
        "delta": "✅",
        "offset": 5,
    }


def test_write_json_closed_stream_returns_false(server):
    """ValueError ('I/O on closed file') used to bubble up; treat as gone."""

    class _Closed:
        def write(self, _): raise ValueError("I/O operation on closed file")
        def flush(self): raise ValueError("I/O operation on closed file")

    server._real_stdout = _Closed()
    assert server.write_json({"x": 1}) is False


def test_write_json_unicode_encode_error_re_raises(server):
    """A non-UTF-8 stdout encoding raises UnicodeEncodeError (a ValueError
    subclass).  It must NOT be swallowed as 'peer gone' — that would let
    `entry.py` exit cleanly via the False path and hide the real config
    bug.  We re-raise so the existing crash-log infrastructure records it."""

    class _AsciiOnly:
        def write(self, line):
            line.encode("ascii")  # raises UnicodeEncodeError on non-ascii
        def flush(self): pass

    server._real_stdout = _AsciiOnly()
    with pytest.raises(UnicodeEncodeError):
        server.write_json({"msg": "héllo"})


def test_write_json_unrelated_value_error_re_raises(server):
    """Only ValueError('...closed file...') means peer gone.  Other
    ValueErrors are programming errors and must surface."""

    class _BadValue:
        def write(self, _): raise ValueError("something else entirely")
        def flush(self): pass

    server._real_stdout = _BadValue()
    with pytest.raises(ValueError, match="something else entirely"):
        server.write_json({"x": 1})


def test_write_json_non_serializable_payload_re_raises(server):
    """Non-JSON-safe payloads are programming errors — they must NOT be
    silently dropped via the False path (which would trigger a clean exit
    in entry.py and mask the real bug)."""
    import io

    server._real_stdout = io.StringIO()
    with pytest.raises(TypeError):
        server.write_json({"obj": object()})


def test_write_json_peer_gone_oserror_on_flush_returns_false(server):
    """A flush that raises a peer-gone OSError (EPIPE) must not strand
    the lock or crash; it returns False so the dispatcher exits cleanly."""
    import errno

    written = []

    class _FlushPeerGone:
        def write(self, line): written.append(line)
        def flush(self): raise OSError(errno.EPIPE, "broken pipe")

    server._real_stdout = _FlushPeerGone()
    assert server.write_json({"x": 1}) is False
    assert written and json.loads(written[0]) == {"x": 1}


def test_write_json_non_peer_gone_oserror_re_raises(server):
    """Host I/O failures (ENOSPC, EACCES, EIO …) are NOT peer-gone — they
    must re-raise so the crash log records them instead of looking like
    a clean disconnect via the False path."""
    import errno

    class _DiskFull:
        def write(self, _): raise OSError(errno.ENOSPC, "no space left")
        def flush(self): pass

    server._real_stdout = _DiskFull()
    with pytest.raises(OSError, match="no space"):
        server.write_json({"x": 1})


def test_write_json_skips_flush_when_disable_flush_true(monkeypatch):
    """`StdioTransport` skips flush when `_DISABLE_FLUSH` is true.

    Tests the runtime *behaviour* via direct module-attr patch.  The env
    var → module constant wiring is covered by the dedicated env test
    below; reloading server.py here would re-register atexit hooks and
    recreate the worker pool.
    """
    import importlib

    transport_mod = importlib.import_module("tui_gateway.transport")
    monkeypatch.setattr(transport_mod, "_DISABLE_FLUSH", True)

    flushed = {"count": 0}
    written = []

    class _Stream:
        def write(self, line): written.append(line)
        def flush(self): flushed["count"] += 1

    stream = _Stream()
    transport = transport_mod.StdioTransport(lambda: stream, threading.Lock())

    assert transport.write({"x": 1}) is True
    assert flushed["count"] == 0


def test_disable_flush_env_var_actually_wires_to_module_constant(monkeypatch):
    """End-to-end: setting `HERMES_TUI_GATEWAY_NO_FLUSH=1` and importing
    `tui_gateway.transport` fresh actually flips `_DISABLE_FLUSH` true.

    Reloads only the transport module — server.py is untouched so its
    atexit hooks/worker pool stay intact."""
    import importlib

    monkeypatch.setenv("HERMES_TUI_GATEWAY_NO_FLUSH", "1")
    transport_mod = importlib.reload(importlib.import_module("tui_gateway.transport"))

    try:
        assert transport_mod._DISABLE_FLUSH is True
    finally:
        # Restore the env-disabled state so other tests see the default.
        monkeypatch.delenv("HERMES_TUI_GATEWAY_NO_FLUSH", raising=False)
        importlib.reload(transport_mod)


# ── _emit ────────────────────────────────────────────────────────────


def test_emit_with_payload(capture):
    server, buf = capture
    server._emit("test.event", "s1", {"key": "val"})
    msg = json.loads(buf.getvalue())

    assert msg["method"] == "event"
    assert msg["params"]["type"] == "test.event"
    assert msg["params"]["session_id"] == "s1"
    assert msg["params"]["payload"]["key"] == "val"


def test_emit_without_payload(capture):
    server, buf = capture
    server._emit("ping", "s2")

    assert "payload" not in json.loads(buf.getvalue())["params"]


def test_emit_realtime_frame_includes_stable_run_metadata(capture):
    server, buf = capture
    server._sessions["runtime-meta"] = {
        "session_key": "stored-meta",
        "active_run_id": "run-meta",
        "active_turn_id": "turn-meta",
        "active_runtime_scope_key": "scope-meta",
        "transport": None,
    }

    server._emit("message.delta", "runtime-meta", {"text": "hi"})
    params = json.loads(buf.getvalue())["params"]

    assert params["session_id"] == "runtime-meta"
    assert params["execution_session_id"] == "runtime-meta"
    assert params["conversation_session_id"] == "stored-meta"
    assert params["run_id"] == "run-meta"
    assert params["turn_id"] == "turn-meta"
    assert params["runtime_scope_key"] == "scope-meta"
    assert isinstance(params["seq"], int)
    assert params["seq"] > 0
    assert params["payload"]["text"] == "hi"


# ── Blocking prompt round-trip ───────────────────────────────────────


def test_block_and_respond(capture):
    server, _ = capture
    result = [None]

    threading.Thread(
        target=lambda: result.__setitem__(0, server._block("test.prompt", "s1", {"q": "?"}, timeout=5)),
    ).start()

    for _ in range(100):
        if server._pending:
            break
        threading.Event().wait(0.01)

    rid = next(iter(server._pending))
    server._answers[rid] = "my_answer"
    # _pending values are (sid, Event) tuples — unpack to set the Event
    _, ev = server._pending[rid]
    ev.set()

    threading.Event().wait(0.1)
    assert result[0] == "my_answer"


def test_clear_pending(server):
    ev = threading.Event()
    # _pending values are (sid, Event) tuples
    server._pending["r1"] = ("sid-x", ev)
    server._clear_pending()

    assert ev.is_set()
    assert server._answers["r1"] == ""


# ── Session lookup ───────────────────────────────────────────────────


def test_sess_missing(server):
    _, err = server._sess({"session_id": "nope"}, "r1")
    assert err["error"]["code"] == 4001


def test_sess_found(server):
    server._sessions["abc"] = {"agent": MagicMock()}
    s, err = server._sess({"session_id": "abc"}, "r1")

    assert s is not None
    assert err is None


def test_sess_resolves_conversation_session_id_to_running_runtime(server):
    idle = {"agent": MagicMock(), "session_key": "stored-1", "running": False}
    running = {
        "agent": MagicMock(),
        "session_key": "stored-1",
        "running": True,
        "run_updated_at": 20,
    }
    server._sessions["idle-runtime"] = idle
    server._sessions["running-runtime"] = running

    s, err = server._sess({"session_id": "stored-1"}, "r1")

    assert err is None
    assert s is running


# ── session.resume payload ────────────────────────────────────────────


def test_session_resume_returns_hydrated_messages(server, monkeypatch, tmp_path):
    def history_reader(_sid, include_ancestors=False):
        return [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "yo"},
            {"role": "tool", "content": "searched"},
            {"role": "assistant", "content": "   "},
            {"role": "assistant", "content": None},
            {"role": "narrator", "content": "skip"},
        ]

    db = _resume_gateway_db(
        tmp_path,
        rows=[("20260409_010101_abc123", "Hydrated")],
        history_reader=history_reader,
    )
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_make_agent", lambda sid, key, session_id=None: object())
    monkeypatch.setattr(server, "_init_session", lambda sid, key, agent, history, cols=80: None)
    monkeypatch.setattr(server, "_session_info", lambda _agent: {"model": "test/model"})

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            "params": {"session_id": "20260409_010101_abc123", "cols": 100},
        }
    )

    assert "error" not in resp
    assert resp["result"]["message_count"] == 3
    assert resp["result"]["messages"] == [
        {"role": "user", "text": "hello", "message_id": "1", "timestamp": 1.0},
        {"role": "assistant", "text": "yo", "message_id": "2", "timestamp": 2.0},
        {
            "role": "tool",
            "name": "tool",
            "context": "",
            "result_text": "searched",
            "message_id": "3",
            "timestamp": 3.0,
        },
    ]


def test_session_resume_reuses_live_running_runtime(server, monkeypatch, tmp_path):
    live_agent = MagicMock()
    server._sessions["runtime-live"] = {
        "agent": live_agent,
        "session_key": "stored-live",
        "history": [],
        "history_lock": threading.Lock(),
        "running": True,
        "active_run_id": "run-live",
        "run_started_at": 10,
        "run_updated_at": 20,
    }
    make_agent = MagicMock()
    db = _resume_gateway_db(tmp_path, rows=[("stored-live", "Stored Live")])
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_make_agent", make_agent)
    monkeypatch.setattr(server, "_session_info", lambda *_args: {"model": "test/model"})

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            "params": {"session_id": "stored-live"},
        }
    )

    assert "error" not in resp
    assert resp["result"]["session_id"] == "runtime-live"
    assert resp["result"]["resumed"] == "stored-live"
    assert resp["result"]["running"] is True
    assert resp["result"]["active_run_id"] == "run-live"
    make_agent.assert_not_called()


def test_session_recall_turn_rewrites_stored_session_without_live_runtime(server, monkeypatch, tmp_path):
    def history_reader(_sid, include_ancestors=False, include_storage_metadata=False):
        return [
            {
                "role": "user",
                "content": "hidden attachment context",
                "metadata": {
                    "turn_id": "turn-1",
                    "draft_text": "请读这个文件",
                    "attachments": [
                        {
                            "name": "spec.pdf",
                            "path": "/tmp/spec.pdf",
                            "mimeType": "application/pdf",
                            "size": 123,
                            "kind": "file",
                        },
                    ],
                },
            },
            {"role": "assistant", "content": "ok", "metadata": {"turn_id": "turn-1"}},
            {
                "role": "user",
                "content": "next",
                "metadata": {"turn_id": "turn-2", "draft_text": "下一条"},
            },
        ]

    db = _resume_gateway_db(
        tmp_path,
        rows=[("stored-1", "Stored")],
        history_reader=history_reader,
    )
    make_agent = MagicMock()
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_make_agent", make_agent)

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.recall_turn",
            "params": {"session_id": "stored-1", "turn_id": "turn-1"},
        }
    )

    assert "error" not in resp
    make_agent.assert_not_called()
    stored_rows = db._conn.execute(
        "SELECT role, content, timestamp, metadata_json FROM messages WHERE session_id = ? ORDER BY id",
        ("stored-1",),
    ).fetchall()
    assert [(row["role"], row["content"], row["timestamp"]) for row in stored_rows] == [
        ("user", "next", 3.0)
    ]
    assert [json.loads(row["metadata_json"])["turn_id"] for row in stored_rows] == ["turn-2"]
    assert resp["result"]["conversation_session_id"] == "stored-1"
    assert resp["result"]["removed_messages"] == 2
    assert resp["result"]["draft"]["text"] == "请读这个文件"
    assert resp["result"]["draft"]["attachments"][0]["name"] == "spec.pdf"
    assert resp["result"]["messages"] == [
        {
            "role": "user",
            "text": "next",
            "message_id": "3",
            "timestamp": 3.0,
            "metadata": {"turn_id": "turn-2", "draft_text": "下一条"},
        },
    ]


def test_session_recall_turn_matches_stored_client_message_id(server, monkeypatch, tmp_path):
    def history_reader(_sid, include_ancestors=False, include_storage_metadata=False):
        return [
            {
                "role": "user",
                "content": "hidden attachment context",
                "metadata": {
                    "turn_id": "turn-canonical",
                    "run_id": "run-canonical",
                    "client_message_id": "client-msg-1",
                    "draft_text": "恢复这个草稿",
                },
            },
            {"role": "assistant", "content": "ok", "metadata": {"turn_id": "turn-canonical"}},
        ]

    db = _resume_gateway_db(
        tmp_path,
        rows=[("stored-1", "Stored")],
        history_reader=history_reader,
    )
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_make_agent", MagicMock())

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.recall_turn",
            "params": {
                "session_id": "stored-1",
                "turn_id": "turn-local",
                "client_message_id": "client-msg-1",
            },
        }
    )

    assert "error" not in resp
    stored_count = db._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?",
        ("stored-1",),
    ).fetchone()[0]
    assert stored_count == 0
    assert resp["result"]["turn_id"] == "turn-local"
    assert resp["result"]["draft"]["text"] == "恢复这个草稿"


def test_session_recall_turn_matches_live_pending_run_id(server, monkeypatch):
    monkeypatch.setattr(server, "_get_db", lambda: None)
    live_agent = MagicMock()
    server._sessions["runtime-live"] = {
        "agent": live_agent,
        "session_key": "stored-live",
        "history": [],
        "history_lock": threading.Lock(),
        "running": True,
        "active_run_id": "run-canonical",
        "active_turn_id": "turn-canonical",
        "pending_turn": {
            "turn_id": "turn-canonical",
            "run_id": "run-canonical",
            "client_message_id": "client-msg-1",
            "draft_text": "恢复 pending 草稿",
        },
        "run_started_at": 10,
        "run_updated_at": 20,
    }

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.recall_turn",
            "params": {
                "session_id": "runtime-live",
                "turn_id": "turn-local",
                "run_id": "run-canonical",
                "client_message_id": "client-msg-1",
            },
        }
    )

    assert "error" not in resp
    assert resp["result"]["draft"]["text"] == "恢复 pending 草稿"
    assert server._sessions["runtime-live"]["running"] is False
    assert server._sessions["runtime-live"]["pending_turn"] is None
    live_agent.interrupt.assert_called_once()


def test_session_recall_turn_records_recall_boundary_with_run_id_and_seq(server, monkeypatch):
    class _DB:
        def __init__(self):
            self.events = [
                {
                    "type": "message.delta",
                    "session_id": "runtime-live",
                    "conversation_session_id": "stored-live",
                    "run_id": "run-canonical",
                    "turn_id": "turn-canonical",
                    "seq": 7,
                    "payload": {"text": "old"},
                }
            ]
            self.replaced = None
            self.messages = types.SimpleNamespace(replace=self.replace_messages)
            self.runs = types.SimpleNamespace(
                append_event=self.append_run_event,
                list_events=self.list_run_events,
                next_event_seq=self.next_run_event_seq,
            )

        def replace_messages(self, sid, messages):
            self.replaced = (sid, messages)

        def next_run_event_seq(self, session_id, fallback_seq=0):
            last = max(
                [
                    int(event.get("seq") or 0)
                    for event in self.events
                    if event.get("conversation_session_id") == session_id
                ],
                default=0,
            )
            return max(last + 1, int(fallback_seq or 0))

        def append_run_event(self, session_id, event, participant_id=""):
            frame = dict(event)
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            frame["payload"] = dict(payload)
            frame["conversation_session_id"] = session_id
            if not int(frame.get("seq") or 0):
                frame["seq"] = self.next_run_event_seq(session_id)
            self.events.append(frame)
            return frame

        def list_run_events(self, session_id, after_seq=0, **_kwargs):
            return [
                event
                for event in self.events
                if event.get("conversation_session_id") == session_id
                and int(event.get("seq") or 0) > int(after_seq or 0)
            ]

    db = _DB()
    monkeypatch.setattr(server, "_get_db", lambda: db)
    live_agent = MagicMock()
    server._sessions["runtime-live"] = {
        "agent": live_agent,
        "session_key": "stored-live",
        "history": [
            {
                "role": "user",
                "content": "old prompt",
                "metadata": {"turn_id": "turn-canonical"},
            },
            {"role": "assistant", "content": "old answer", "metadata": {"turn_id": "turn-canonical"}},
        ],
        "history_lock": threading.Lock(),
        "running": True,
        "active_run_id": "run-canonical",
        "active_turn_id": "turn-canonical",
        "run_started_at": 10,
        "run_updated_at": 20,
    }

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.recall_turn",
            "params": {"session_id": "runtime-live", "turn_id": "turn-canonical"},
        }
    )

    assert "error" not in resp
    recall_events = [event for event in db.events if event["type"] == "session.recalled"]
    assert len(recall_events) == 1
    recall_event = recall_events[0]
    assert recall_event["run_id"] == "run-canonical"
    assert recall_event["payload"]["run_id"] == "run-canonical"
    assert recall_event["seq"] == 8
    assert recall_event["seq"] > db.events[0]["seq"]
    assert server._sessions["runtime-live"]["active_run_id"] is None
    live_agent.interrupt.assert_called_once()


def test_session_status_returns_machine_readable_run_state(server):
    agent = MagicMock(model="gpt-test", provider="test-provider")
    agent.context_compressor = None
    server._sessions["runtime-status"] = {
        "agent": agent,
        "session_key": "stored-status",
        "running": True,
        "active_run_id": "run-status",
        "run_started_at": 11,
        "run_updated_at": 22,
        "history": [],
        "history_lock": threading.Lock(),
    }

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.status",
            "params": {"session_id": "stored-status"},
        }
    )

    assert "error" not in resp
    assert resp["result"]["session_id"] == "runtime-status"
    assert resp["result"]["conversation_session_id"] == "stored-status"
    assert resp["result"]["running"] is True
    assert resp["result"]["active_run_id"] == "run-status"
    assert resp["result"]["run_started_at"] == 11
    assert resp["result"]["run_updated_at"] == 22


def test_session_create_control_plane_only_persists_through_session_repo(
    server,
    monkeypatch,
    tmp_path,
):
    import importlib

    from hermes_agent.storage.cli_session_store import open_cli_session_store

    importlib.reload(importlib.import_module("tui_gateway.methods.session"))

    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "gpt-test")

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.create",
            "params": {
                "control_plane_only": True,
                "toolProgressMode": "verbose",
                "transient": True,
            },
        }
    )

    assert "error" not in resp
    assert resp["result"]["session_id"] == resp["result"]["conversation_session_id"]
    assert resp["result"]["info"]["control_plane_only"] is True
    assert resp["result"]["info"]["lazy"] is True
    assert resp["result"]["info"]["transient"] is True
    session_id = resp["result"]["conversation_session_id"]
    session_row = db._conn.execute(
        "SELECT id, source, model, transient FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    assert dict(session_row) == {
        "id": session_id,
        "source": "tui",
        "model": "gpt-test",
        "transient": 1,
    }
    index_row = db._conn.execute(
        "SELECT session_id, source, transient FROM session_index WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert dict(index_row) == {
        "session_id": session_id,
        "source": "tui",
        "transient": 1,
    }
    db.close()


def test_approval_control_plane_methods_accept_conversation_session_id(server, monkeypatch):
    import importlib

    importlib.reload(importlib.import_module("tui_gateway.methods.prompt"))

    class _DB:
        sessions = types.SimpleNamespace(
            get=lambda session_id: (
                {"id": session_id} if session_id == "stored-approval" else None
            )
        )

    yolo_sessions = set()
    approval_mod = types.SimpleNamespace(
        is_session_yolo_enabled=lambda session_id: session_id in yolo_sessions,
        enable_session_yolo=lambda session_id: yolo_sessions.add(session_id),
        disable_session_yolo=lambda session_id: yolo_sessions.discard(session_id),
        list_gateway_approvals=lambda session_id: [{"session_id": session_id}],
        resolve_gateway_approval=lambda session_id, choice, resolve_all=False: {
            "session_id": session_id,
            "choice": choice,
            "all": resolve_all,
        },
    )

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setitem(sys.modules, "tools.approval", approval_mod)

    pending = server.handle_request(
        {
            "id": "pending",
            "method": "approval.pending.list",
            "params": {"session_id": "stored-approval"},
        }
    )
    before = server.handle_request(
        {
            "id": "before",
            "method": "approval.policy.get",
            "params": {"conversation_session_id": "stored-approval"},
        }
    )
    updated = server.handle_request(
        {
            "id": "set",
            "method": "approval.policy.set",
            "params": {"session_id": "stored-approval", "mode": "full_access"},
        }
    )
    after = server.handle_request(
        {
            "id": "after",
            "method": "approval.policy.get",
            "params": {"session_id": "stored-approval"},
        }
    )

    assert "error" not in pending
    assert pending["result"]["approvals"] == [{"session_id": "stored-approval"}]
    assert "error" not in before
    assert before["result"] == {"mode": "default", "yolo": False}
    assert "error" not in updated
    assert updated["result"] == {"mode": "full_access", "yolo": True}
    assert "error" not in after
    assert after["result"] == {"mode": "full_access", "yolo": True}


def test_run_control_replays_events_and_tracks_status(capture, monkeypatch, tmp_path):
    server, _buf = capture
    db = _resume_gateway_db(tmp_path)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    db.sessions.create("stored-events", source="tui")
    db.runs.upsert(
        run_id="run-events",
        session_id="stored-events",
        turn_id="turn-events",
        status="running",
    )
    server._sessions["runtime-events"] = {
        "agent": MagicMock(model="gpt-test", provider="test-provider"),
        "session_key": "stored-events",
        "running": True,
        "active_run_id": "run-events",
        "active_turn_id": "turn-events",
        "run_started_at": 11,
        "run_updated_at": 22,
        "history": [],
        "history_lock": threading.Lock(),
    }

    server._emit("message.start", "runtime-events", {"run_id": "run-events", "turn_id": "turn-events"})

    replay = server.handle_request(
        {
            "id": "r1",
            "method": "events.subscribe",
            "params": {"conversation_session_id": "stored-events"},
        }
    )
    status = server.handle_request(
        {
            "id": "r2",
            "method": "run.status",
            "params": {"run_id": "run-events"},
        }
    )

    assert "error" not in replay
    assert replay["result"]["events"][0]["type"] == "message.start"
    assert replay["result"]["events"][0]["conversation_session_id"] == "stored-events"
    assert "error" not in status
    assert status["result"]["run"]["status"] == "running"

    server._emit("message.complete", "runtime-events", {"run_id": "run-events", "status": "complete"})
    done = server.handle_request(
        {
            "id": "r3",
            "method": "run.status",
            "params": {"run_id": "run-events"},
        }
    )

    assert done["result"]["run"]["status"] == "completed"


def test_terminal_event_releases_live_session_before_client_delivery(capture, monkeypatch):
    server, _buf = capture
    entered_write = threading.Event()
    release_write = threading.Event()
    observed_events = []
    write_released = []

    def _blocking_write_json(obj):
        observed_events.append(obj)
        params = obj.get("params") or {}
        if obj.get("method") == "event" and params.get("type") == "message.complete":
            entered_write.set()
            write_released.append(release_write.wait(timeout=2))
        return True

    agent = MagicMock(model="gpt-test", provider="test-provider")
    agent.context_compressor = None
    server._sessions["runtime-terminal"] = {
        "agent": agent,
        "session_key": "stored-terminal",
        "running": True,
        "active_run_id": "run-terminal",
        "active_turn_id": "turn-terminal",
        "run_started_at": 11,
        "run_updated_at": 22,
        "history": [],
        "history_lock": threading.Lock(),
    }
    monkeypatch.setattr(server, "write_json", _blocking_write_json)

    emitter = threading.Thread(
        target=server._emit,
        args=(
            "message.complete",
            "runtime-terminal",
            {
                "run_id": "run-terminal",
                "turn_id": "turn-terminal",
                "status": "complete",
            },
        ),
    )
    emitter.start()

    assert entered_write.wait(timeout=2)
    try:
        status = server.handle_request(
            {
                "id": "terminal-status",
                "method": "session.status",
                "params": {"conversation_session_id": "stored-terminal"},
            }
        )
    finally:
        release_write.set()
    emitter.join(timeout=2)

    assert not emitter.is_alive()
    assert observed_events
    assert write_released == [True]
    assert "error" not in status
    assert status["result"]["running"] is False
    assert status["result"]["active_run_id"] == ""
    assert status["result"]["active_turn_id"] == ""


def test_terminal_event_releases_live_session_before_subscription_delivery(capture):
    server, _buf = capture
    from tui_gateway.services import run_control

    observed_statuses = []

    class _StatusCheckingTransport:
        def write(self, obj):
            params = obj.get("params") or {}
            if obj.get("method") == "event" and params.get("type") == "message.complete":
                observed_statuses.append(
                    server.handle_request(
                        {
                            "id": "subscriber-status",
                            "method": "session.status",
                            "params": {"conversation_session_id": "stored-subscriber"},
                        }
                    )
                )
            return True

    agent = MagicMock(model="gpt-test", provider="test-provider")
    agent.context_compressor = None
    server._sessions["runtime-subscriber"] = {
        "agent": agent,
        "session_key": "stored-subscriber",
        "running": True,
        "active_run_id": "run-subscriber",
        "active_turn_id": "turn-subscriber",
        "run_started_at": 11,
        "run_updated_at": 22,
        "history": [],
        "history_lock": threading.Lock(),
    }
    subscription_id, _replay = run_control.subscribe_session_with_id(
        conversation_session_id="stored-subscriber",
        transport=_StatusCheckingTransport(),
    )

    try:
        server._emit(
            "message.complete",
            "runtime-subscriber",
            {
                "run_id": "run-subscriber",
                "turn_id": "turn-subscriber",
                "status": "complete",
            },
        )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)

    assert len(observed_statuses) == 1
    assert "error" not in observed_statuses[0]
    assert observed_statuses[0]["result"]["running"] is False
    assert observed_statuses[0]["result"]["active_run_id"] == ""
    assert observed_statuses[0]["result"]["active_turn_id"] == ""


def test_run_control_control_events_do_not_mark_session_busy(capture):
    server, _buf = capture
    agent = MagicMock(model="gpt-test", provider="test-provider")
    agent.context_compressor = None
    server._sessions["runtime-control"] = {
        "agent": agent,
        "session_key": "stored-control",
        "running": False,
        "history": [],
        "history_lock": threading.Lock(),
    }

    server._emit(
        "mission.approval.requested",
        "runtime-control",
        {
            "run_id": "team-mission:mission-1:conversation:plan",
            "mission_id": "mission-1",
        },
    )
    status = server.handle_request(
        {
            "id": "control-status",
            "method": "session.status",
            "params": {"conversation_session_id": "stored-control"},
        }
    )

    assert "error" not in status
    assert status["result"]["running"] is False
    assert status["result"]["active_run_id"] == ""


def test_run_submit_rejects_persisted_active_run(server, monkeypatch):
    class _Runs:
        def session_status(self, _session_id):
            return {
                "running": True,
                "active_run_id": "run-active",
                "active_turn_id": "turn-active",
                "last_event_seq": 3,
            }

        def list(self, _session_id, **_kwargs):
            return [
                {
                    "run_id": "run-active",
                    "turn_id": "turn-active",
                    "session_id": "stored-active",
                    "status": "running",
                    "started_at": 1,
                    "updated_at": 2,
                    "last_seq": 3,
                }
            ]

    db = types.SimpleNamespace(runs=_Runs())
    monkeypatch.setattr(server, "_get_db", lambda: db)

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "run.submit",
            "params": {"conversation_session_id": "stored-active", "text": "hello"},
        }
    )

    assert resp["error"]["code"] == 4009
    assert resp["error"]["data"]["active_run_id"] == "run-active"


def test_run_submit_preserves_prestart_cancelled_run(server, monkeypatch):
    session = {
        "agent": MagicMock(model="gpt-test", provider="test-provider"),
        "session_key": "stored-prestart-cancel",
        "running": False,
        "active_run_id": "",
        "active_turn_id": "",
        "history": [],
        "history_lock": threading.Lock(),
    }
    server._sessions["runtime-prestart-cancel"] = session
    monkeypatch.setattr(
        server,
        "_start_agent_build",
        MagicMock(side_effect=AssertionError("pre-cancelled submit must not start agent")),
    )
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        MagicMock(side_effect=AssertionError("pre-cancelled submit must not run prompt")),
    )

    cancelled = server.handle_request(
        {
            "id": "cancel",
            "method": "run.cancel",
            "params": {
                "conversation_session_id": "stored-prestart-cancel",
                "run_id": "run-prestart-cancel",
                "turn_id": "turn-prestart-cancel",
                "runtime_scope_key": "profile:agent-default",
            },
        }
    )
    submitted = server.handle_request(
        {
            "id": "submit",
            "method": "run.submit",
            "params": {
                "conversation_session_id": "stored-prestart-cancel",
                "run_id": "run-prestart-cancel",
                "turn_id": "turn-prestart-cancel",
                "text": "hello",
                "runtime_scope_key": "profile:agent-default",
                "_control_plane_reserved": True,
            },
        }
    )

    assert "error" not in cancelled
    assert "error" not in submitted
    assert submitted["result"]["status"] == "cancelled"
    assert submitted["result"]["run_id"] == "run-prestart-cancel"
    assert session["running"] is False
    assert session["active_run_id"] is None
    assert server._start_agent_build.call_count == 0
    assert server._run_prompt_submit.call_count == 0


def test_run_submit_extracts_image_paths_from_prompt_attachments(server, monkeypatch, tmp_path):
    from tui_gateway.methods import prompt as prompt_methods

    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    session = {
        "agent": MagicMock(model="gpt-test", provider="test-provider"),
        "session_key": "stored-image-submit",
        "running": False,
        "active_run_id": "",
        "active_turn_id": "",
        "history": [],
        "history_lock": threading.Lock(),
    }
    server._sessions["runtime-image-submit"] = session
    submitted = {}

    def fake_run_prompt_submit(rid, sid, target_session, text, submitted_images, turn_metadata):
        submitted.update({
            "rid": rid,
            "sid": sid,
            "session": target_session,
            "text": text,
            "submitted_images": submitted_images,
            "turn_metadata": turn_metadata,
        })

    monkeypatch.setattr(prompt_methods, "_start_agent_build", MagicMock())
    monkeypatch.setattr(prompt_methods, "_wait_agent", MagicMock(return_value=None))
    monkeypatch.setattr(prompt_methods, "_run_prompt_submit", fake_run_prompt_submit)
    monkeypatch.setattr(prompt_methods, "ensure_agent_runtime_current", MagicMock())
    monkeypatch.setattr(prompt_methods, "_apply_dovie_product_runtime_policy", MagicMock())

    resp = server.handle_request(
        {
            "id": "submit-image",
            "method": "run.submit",
            "params": {
                "conversation_session_id": "stored-image-submit",
                "run_id": "run-image",
                "turn_id": "turn-image",
                "text": "分析图片",
                "attachments": [
                    {
                        "name": "screen.png",
                        "path": str(image_path),
                        "mimeType": "image/png",
                        "kind": "image",
                    },
                    {
                        "name": "notes.txt",
                        "path": str(tmp_path / "notes.txt"),
                        "mimeType": "text/plain",
                        "kind": "file",
                    },
                ],
            },
        }
    )

    assert "error" not in resp
    deadline = time.time() + 2
    while not submitted and time.time() < deadline:
        time.sleep(0.01)
    assert submitted["submitted_images"] == [str(image_path)]
    assert submitted["turn_metadata"]["attachments"][0]["path"] == str(image_path)
    assert submitted["turn_metadata"]["attachments"][0]["kind"] == "image"


def test_prompt_image_refs_merge_submitted_images_and_text_refs(tmp_path):
    from tui_gateway.services.media import image_refs_for_prompt

    attached = tmp_path / "attached.png"
    extra = tmp_path / "extra.png"
    attached.write_bytes(b"\x89PNG\r\n\x1a\n")
    extra.write_bytes(b"\x89PNG\r\n\x1a\n")

    paths, urls = image_refs_for_prompt(
        [str(attached)],
        (
            f"看这个附件 {attached}，再对比 {extra} "
            "和 https://example.com/remote.png。"
        ),
    )

    assert paths == [str(attached), str(extra)]
    assert urls == ["https://example.com/remote.png"]


def test_events_subscribe_returns_subscription_id_and_unsubscribes(capture):
    server, _buf = capture
    token = server.bind_transport(server._stdio_transport)

    try:
        subscribed = server.handle_request(
            {
                "id": "r1",
                "method": "events.subscribe",
                "params": {"conversation_session_id": "stored-sub"},
            }
        )
        subscription_id = subscribed["result"]["subscription_id"]
        unsubscribed = server.handle_request(
            {
                "id": "r2",
                "method": "events.unsubscribe",
                "params": {"subscription_id": subscription_id},
            }
        )
    finally:
        server.reset_transport(token)

    assert subscription_id
    assert unsubscribed["result"]["removed"] == 1


def test_run_events_replays_without_creating_subscription(server, monkeypatch, tmp_path):
    from hermes_agent.domain.event_ledger import EventLedger
    from tui_gateway.services import run_control

    db = _resume_gateway_db(tmp_path)
    db.sessions.create("stored-run-events", source="tui")
    payload = {"text": "hello"}
    EventLedger(db._conn).append_runtime_frame(
        session_id="stored-run-events",
        run_id="run-a",
        turn_id="turn-a",
        execution_session_id="runtime-run-a",
        runtime_scope_key="profile:agent-a",
        participant_id="",
        activity_id="",
        event_type="message.delta",
        seq=2,
        timestamp=123.0,
        payload_json=json.dumps(payload, ensure_ascii=False),
        event_json=json.dumps(
            {
                "type": "message.delta",
                "conversation_session_id": "stored-run-events",
                "session_id": "runtime-run-a",
                "run_id": "run-a",
                "runtime_scope_key": "profile:agent-a",
                "seq": 2,
                "payload": payload,
            },
            ensure_ascii=False,
        ),
        status="",
        frame_blob=None,
        frame_format="",
        retention_class="",
    )
    monkeypatch.setattr(server, "_get_db", lambda: db)

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "run.events",
            "params": {
                "conversation_session_id": "stored-run-events",
                "after_seq": 1,
                "runtime_scope_key": "profile:agent-a",
                "run_id": "run-a",
                "limit": 321,
            },
        }
    )

    assert "error" not in resp
    assert resp["result"]["conversation_session_id"] == "stored-run-events"
    assert resp["result"]["last_event_seq"] == 2
    assert resp["result"]["events"][0]["run_id"] == "run-a"
    assert run_control._subscriptions_by_id == {}


def test_run_list_accepts_runtime_scope_and_status_filters(server, monkeypatch):
    captured = {}

    class _Runs:
        def list(self, session_id="", *, runtime_scope_key="", statuses=None, limit=200):
            captured.update(
                {
                    "session_id": session_id,
                    "runtime_scope_key": runtime_scope_key,
                    "statuses": statuses,
                    "limit": limit,
                }
            )
            return [{"run_id": "run-filtered", "status": "running", "runtime_scope_key": runtime_scope_key}]

    db = types.SimpleNamespace(runs=_Runs())
    monkeypatch.setattr(server, "_get_db", lambda: db)

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "run.list",
            "params": {"runtime_scope_key": "profile:alpha", "status": "running", "limit": 10},
        }
    )

    assert "error" not in resp
    assert resp["result"]["runs"][0]["run_id"] == "run-filtered"
    assert captured == {
        "session_id": "",
        "runtime_scope_key": "profile:alpha",
        "statuses": ["running"],
        "limit": 10,
    }


# ── Config I/O ───────────────────────────────────────────────────────


def test_config_load_missing(server, tmp_path):
    server._hermes_home = tmp_path
    assert server._load_cfg() == {}


def test_config_roundtrip(server, tmp_path):
    server._hermes_home = tmp_path
    server._save_cfg({"model": "test/model"})
    assert server._load_cfg()["model"] == "test/model"


# ── _cli_exec_blocked ────────────────────────────────────────────────


@pytest.mark.parametrize("argv", [
    [],
    ["setup"],
    ["gateway"],
    ["sessions", "browse"],
    ["config", "edit"],
])
def test_cli_exec_blocked(server, argv):
    assert server._cli_exec_blocked(argv) is not None


@pytest.mark.parametrize("argv", [
    ["version"],
    ["sessions", "list"],
])
def test_cli_exec_allowed(server, argv):
    assert server._cli_exec_blocked(argv) is None


# ── slash.exec skill command interception ────────────────────────────


def test_slash_exec_rejects_skill_commands(server):
    """slash.exec must reject skill commands so the TUI falls through to command.dispatch."""
    # Register a mock session
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid, "agent": None}

    # Mock scan_skill_commands to return a known skill
    fake_skills = {"/hermes-agent-dev": {"name": "hermes-agent-dev", "description": "Dev workflow"}}

    with patch("agent.skill_commands.get_skill_commands", return_value=fake_skills):
        resp = server.handle_request({
            "id": "r1",
            "method": "slash.exec",
            "params": {"command": "hermes-agent-dev", "session_id": sid},
        })

    # Should return an error so the TUI's .catch() fires command.dispatch
    assert "error" in resp
    assert resp["error"]["code"] == 4018
    assert "skill command" in resp["error"]["message"]


def test_slash_exec_handles_plugin_commands_in_live_gateway(server):
    """Plugin slash commands return normal slash.exec output without using the worker."""
    sid = "test-session"

    class Worker:
        def __init__(self):
            self.calls = []

        def run(self, cmd):
            self.calls.append(cmd)
            return f"worker:{cmd}"

    worker = Worker()
    server._sessions[sid] = {"session_key": sid, "agent": None, "slash_worker": worker}

    with patch(
        "hermes_cli.plugins.get_plugin_command_handler",
        lambda name: (lambda arg: f"plugin:{arg}") if name == "plugin-cmd" else None,
    ):
        resp = server.handle_request({
            "id": "r-plugin-slash",
            "method": "slash.exec",
            "params": {"command": "plugin-cmd hello", "session_id": sid},
        })

    assert "error" not in resp
    assert resp["result"] == {"output": "plugin:hello"}
    assert worker.calls == []


def test_slash_exec_plugin_lookup_failure_falls_back_to_worker(server):
    """Plugin discovery failures must not break ordinary slash-worker commands."""
    sid = "test-session"

    class Worker:
        def __init__(self):
            self.calls = []

        def run(self, cmd):
            self.calls.append(cmd)
            return f"worker:{cmd}"

    worker = Worker()
    server._sessions[sid] = {"session_key": sid, "agent": None, "slash_worker": worker}

    with patch(
        "hermes_cli.plugins.get_plugin_command_handler",
        side_effect=RuntimeError("discovery boom"),
    ):
        resp = server.handle_request({
            "id": "r-plugin-lookup-failure",
            "method": "slash.exec",
            "params": {"command": "help", "session_id": sid},
        })

    assert "error" not in resp
    assert resp["result"] == {"output": "worker:help"}
    assert worker.calls == ["help"]


def test_slash_exec_plugin_handler_error_returns_output(server):
    """Plugin handler failures return slash output so the TUI does not redispatch."""
    sid = "test-session"

    class Worker:
        def __init__(self):
            self.calls = []

        def run(self, cmd):
            self.calls.append(cmd)
            return f"worker:{cmd}"

    def handler(arg):
        raise RuntimeError(f"handler boom: {arg}")

    worker = Worker()
    server._sessions[sid] = {"session_key": sid, "agent": None, "slash_worker": worker}

    with patch(
        "hermes_cli.plugins.get_plugin_command_handler",
        lambda name: handler if name == "plugin-cmd" else None,
    ):
        resp = server.handle_request({
            "id": "r-plugin-handler-error",
            "method": "slash.exec",
            "params": {"command": "plugin-cmd hello", "session_id": sid},
        })

    assert "error" not in resp
    assert resp["result"] == {"output": "Plugin command error: handler boom: hello"}
    assert worker.calls == []


@pytest.mark.parametrize(
    "cmd",
    ["retry", "queue hello", "q hello", "steer fix the test", "plan", "undo", "rewind"],
)
def test_slash_exec_rejects_pending_input_commands(server, cmd):
    """slash.exec must reject commands that use _pending_input in the CLI."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid, "agent": None}

    resp = server.handle_request({
        "id": "r1",
        "method": "slash.exec",
        "params": {"command": cmd, "session_id": sid},
    })

    assert "error" in resp
    assert resp["error"]["code"] == 4018
    assert "pending-input command" in resp["error"]["message"]


def test_command_dispatch_queue_sends_message(server):
    """command.dispatch /queue returns {type: 'send', message: ...} for the TUI."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}

    resp = server.handle_request({
        "id": "r1",
        "method": "command.dispatch",
        "params": {"name": "queue", "arg": "tell me about quantum computing", "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == "tell me about quantum computing"


def test_command_dispatch_queue_requires_arg(server):
    """command.dispatch /queue without an argument returns an error."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}

    resp = server.handle_request({
        "id": "r2",
        "method": "command.dispatch",
        "params": {"name": "queue", "arg": "", "session_id": sid},
    })

    assert "error" in resp
    assert resp["error"]["code"] == 4004


def test_skills_manage_search_uses_tools_hub_sources(server):
    result = type("Result", (), {
        "description": "Build better terminal demos",
        "identifier": "openai/skills/showroom",
        "name": "showroom",
        "source": "official",
        "tags": ["demo"],
        "trust_level": "trusted",
    })()
    auth = MagicMock(return_value="auth")
    router = MagicMock(return_value=["source"])
    search = MagicMock(return_value=([result], {"official": 1}, []))
    fake_hub = types.SimpleNamespace(
        GitHubAuth=auth,
        create_source_router=router,
        parallel_search_sources=search,
    )

    with patch.dict(sys.modules, {"tools.skills_hub": fake_hub}):
        resp = server.handle_request({
            "id": "skills-search",
            "method": "skills.manage",
            "params": {"action": "search", "query": "showroom"},
        })

    assert "error" not in resp
    assert resp["result"] == {
        "results": [{
            "description": "Build better terminal demos",
            "identifier": "openai/skills/showroom",
            "name": "showroom",
            "source": "official",
            "tags": ["demo"],
            "trust": "trusted",
        }]
    }
    auth.assert_called_once_with()
    router.assert_called_once_with("auth")
    search.assert_called_once_with(["source"], query="showroom", source_filter="all", overall_timeout=6)


def test_skills_manage_list_returns_structured_items(server):
    fake_hub = types.SimpleNamespace(
        ensure_hub_dirs=MagicMock(),
        HubLockFile=MagicMock(return_value=types.SimpleNamespace(
            list_installed=MagicMock(return_value=[{
                "name": "showroom",
                "source": "official",
                "trust_level": "trusted",
                "identifier": "openai/skills/showroom",
                "install_path": "productivity/showroom",
            }])
        )),
        SKILLS_DIR=Path("/tmp/hermes_test/skills"),
    )
    fake_sync = types.SimpleNamespace(_read_manifest=MagicMock(return_value={"builtin-skill"}))
    fake_tools = types.SimpleNamespace(_find_all_skills=MagicMock(return_value=[
        {"name": "builtin-skill", "description": "Bundled", "category": "core"},
        {"name": "showroom", "description": "Demos", "category": "productivity"},
        {"name": "local-skill", "description": "Local", "category": ""},
    ]))
    fake_utils = types.SimpleNamespace(
        get_disabled_skill_names=MagicMock(return_value={"local-skill"})
    )

    with patch.dict(sys.modules, {
        "tools.skills_hub": fake_hub,
        "tools.skills_sync": fake_sync,
        "tools.skills_tool": fake_tools,
        "agent.skill_utils": fake_utils,
    }):
        resp = server.handle_request({
            "id": "skills-list",
            "method": "skills.manage",
            "params": {"action": "list"},
        })

    assert "error" not in resp
    result = resp["result"]
    assert result["skills"] == {
        "core": ["builtin-skill"],
        "productivity": ["showroom"],
        "uncategorized": ["local-skill"],
    }
    assert result["stats"]["total_skills"] == 3
    assert result["stats"]["enabled_skills"] == 2
    assert result["stats"]["disabled_skills"] == 1
    showroom = next(item for item in result["items"] if item["name"] == "showroom")
    assert showroom["source_type"] == "hub"
    assert showroom["can_uninstall"] is True
    assert showroom["enabled"] is True


def test_skills_list_returns_structured_items_without_market_router(server):
    fake_hub = types.SimpleNamespace(
        ensure_hub_dirs=MagicMock(),
        HubLockFile=MagicMock(return_value=types.SimpleNamespace(
            list_installed=MagicMock(return_value=[])
        )),
        SKILLS_DIR=Path("/tmp/hermes_test/skills"),
    )
    fake_sync = types.SimpleNamespace(_read_manifest=MagicMock(return_value={"builtin-skill"}))
    fake_tools = types.SimpleNamespace(_find_all_skills=MagicMock(return_value=[
        {"name": "builtin-skill", "description": "Bundled", "category": "core"},
    ]))
    fake_utils = types.SimpleNamespace(get_disabled_skill_names=MagicMock(return_value=set()))

    with patch.dict(sys.modules, {
        "tools.skills_hub": fake_hub,
        "tools.skills_sync": fake_sync,
        "tools.skills_tool": fake_tools,
        "agent.skill_utils": fake_utils,
    }):
        resp = server.handle_request({
            "id": "skills-list-local",
            "method": "skills.list",
            "params": {},
        })

    assert "error" not in resp
    assert resp["result"]["skills"] == {"core": ["builtin-skill"]}
    assert resp["result"]["items"][0]["source_type"] == "builtin"


def test_skills_list_realigns_cached_skill_modules_to_dovie_profile_home(server, tmp_path):
    profile_home = tmp_path / "draft-home"
    stale_home = tmp_path / "stale-home"
    skill_dir = profile_home / "skills" / "productivity" / "draft-skill"
    skill_dir.mkdir(parents=True)

    fake_hub = types.SimpleNamespace(
        HERMES_HOME=stale_home,
        SKILLS_DIR=stale_home / "skills",
        HUB_DIR=stale_home / "skills" / ".hub",
        LOCK_FILE=stale_home / "skills" / ".hub" / "lock.json",
        QUARANTINE_DIR=stale_home / "skills" / ".hub" / "quarantine",
        AUDIT_LOG=stale_home / "skills" / ".hub" / "audit.log",
        TAPS_FILE=stale_home / "skills" / ".hub" / "taps.json",
        INDEX_CACHE_DIR=stale_home / "skills" / ".hub" / "index-cache",
        ensure_hub_dirs=MagicMock(),
        HubLockFile=MagicMock(return_value=types.SimpleNamespace(
            list_installed=MagicMock(return_value=[])
        )),
    )
    fake_sync = types.SimpleNamespace(
        HERMES_HOME=stale_home,
        SKILLS_DIR=stale_home / "skills",
        MANIFEST_FILE=stale_home / "skills" / ".bundled_manifest",
        _read_manifest=MagicMock(return_value={}),
    )
    fake_tools = types.SimpleNamespace(
        HERMES_HOME=stale_home,
        SKILLS_DIR=stale_home / "skills",
        _find_all_skills=MagicMock(return_value=[
            {
                "name": "draft-skill",
                "description": "Draft scoped skill",
                "category": "productivity",
                "skill_dir": str(skill_dir),
            },
        ]),
    )
    fake_utils = types.SimpleNamespace(get_disabled_skill_names=MagicMock(return_value=set()))
    fake_constants = sys.modules["hermes_constants"]
    fake_constants.get_hermes_home.return_value = profile_home

    with patch.dict(sys.modules, {
        "tools.skills_hub": fake_hub,
        "tools.skills_sync": fake_sync,
        "tools.skills_tool": fake_tools,
        "agent.skill_utils": fake_utils,
    }):
        resp = server.handle_request({
            "id": "skills-list-scoped",
            "method": "skills.list",
            "params": {
                "dovie_profile": {
                    "id": "draft:one",
                    "runtimeScopeKey": "draft:one",
                    "hermesHomePath": str(profile_home),
                },
            },
        })

    scoped_skills_dir = profile_home.resolve() / "skills"
    scoped_hub_dir = scoped_skills_dir / ".hub"
    assert "error" not in resp
    assert fake_hub.SKILLS_DIR == scoped_skills_dir
    assert fake_hub.LOCK_FILE == scoped_hub_dir / "lock.json"
    assert fake_sync.MANIFEST_FILE == scoped_skills_dir / ".bundled_manifest"
    assert fake_tools.SKILLS_DIR == scoped_skills_dir
    assert resp["result"]["items"][0]["install_path"] == "productivity/draft-skill"


def test_skills_manage_uninstall_uses_hub_lifecycle(server):
    uninstall = MagicMock(return_value=(True, "Uninstalled 'showroom'"))
    fake_hub = types.SimpleNamespace(uninstall_skill=uninstall)
    fake_prompt_builder = types.SimpleNamespace(
        clear_skills_system_prompt_cache=MagicMock()
    )

    with patch.dict(sys.modules, {
        "tools.skills_hub": fake_hub,
        "agent.prompt_builder": fake_prompt_builder,
    }):
        resp = server.handle_request({
            "id": "skills-uninstall",
            "method": "skills.manage",
            "params": {"action": "uninstall", "query": "showroom"},
        })

    assert "error" not in resp
    assert resp["result"] == {
        "uninstalled": True,
        "name": "showroom",
        "message": "Uninstalled 'showroom'",
    }
    uninstall.assert_called_once_with("showroom")
    fake_prompt_builder.clear_skills_system_prompt_cache.assert_called_once_with(
        clear_snapshot=True
    )


def test_skills_manage_delete_uses_local_package_lifecycle(server):
    delete_skill_package = MagicMock(return_value=(True, "Deleted local skill package 'showroom'"))
    fake_hub = types.SimpleNamespace(delete_skill_package=delete_skill_package)
    fake_prompt_builder = types.SimpleNamespace(
        clear_skills_system_prompt_cache=MagicMock()
    )

    with patch.dict(sys.modules, {
        "tools.skills_hub": fake_hub,
        "agent.prompt_builder": fake_prompt_builder,
    }):
        resp = server.handle_request({
            "id": "skills-delete",
            "method": "skills.manage",
            "params": {"action": "delete", "query": "showroom"},
        })

    assert "error" not in resp
    assert resp["result"] == {
        "deleted": True,
        "name": "showroom",
        "message": "Deleted local skill package 'showroom'",
    }
    delete_skill_package.assert_called_once_with("showroom")
    fake_prompt_builder.clear_skills_system_prompt_cache.assert_called_once_with(
        clear_snapshot=True
    )


def test_skills_manage_copy_installed_uses_package_lifecycle(server, tmp_path):
    target_home = tmp_path / "draft-home"
    copy_installed_skill_to_home = MagicMock(return_value={
        "name": "showroom",
        "sourceSkillDir": "/source/showroom",
        "targetSkillDir": str(target_home / "skills" / "showroom"),
        "targetHermesHome": str(target_home),
    })
    fake_lifecycle = types.SimpleNamespace(
        copy_installed_skill_to_home=copy_installed_skill_to_home
    )

    with patch.dict(sys.modules, {"tools.skill_package_lifecycle": fake_lifecycle}):
        resp = server.handle_request({
            "id": "skills-copy-installed",
            "method": "skills.manage",
            "params": {
                "action": "copy_installed",
                "query": "showroom",
                "target_hermes_home": str(target_home),
            },
        })

    assert "error" not in resp
    assert resp["result"] == {
        "copied": True,
        "name": "showroom",
        "sourceSkillDir": "/source/showroom",
        "targetSkillDir": str(target_home / "skills" / "showroom"),
        "targetHermesHome": str(target_home),
    }
    copy_installed_skill_to_home.assert_called_once_with("showroom", str(target_home))


def test_skills_manage_import_archive_uses_package_lifecycle(server, tmp_path):
    archive = tmp_path / "showroom.zip"
    archive.write_bytes(b"zip")
    import_skill_archive_to_home = MagicMock(return_value={
        "name": "showroom",
        "sourceArchive": str(archive),
        "targetSkillDir": str(tmp_path / "skills" / "showroom"),
        "targetHermesHome": str(tmp_path),
        "installPath": "showroom",
    })
    fake_lifecycle = types.SimpleNamespace(
        import_skill_archive_to_home=import_skill_archive_to_home
    )
    fake_prompt_builder = types.SimpleNamespace(
        clear_skills_system_prompt_cache=MagicMock()
    )

    with patch.dict(sys.modules, {
        "tools.skill_package_lifecycle": fake_lifecycle,
        "agent.prompt_builder": fake_prompt_builder,
    }):
        resp = server.handle_request({
            "id": "skills-import-archive",
            "method": "skills.manage",
            "params": {
                "action": "import_archive",
                "archive_path": str(archive),
                "category": "productivity",
                "name": "showroom",
            },
        })

    assert "error" not in resp
    assert resp["result"] == {
        "imported": True,
        "name": "showroom",
        "sourceArchive": str(archive),
        "targetSkillDir": str(tmp_path / "skills" / "showroom"),
        "targetHermesHome": str(tmp_path),
        "installPath": "showroom",
    }
    import_skill_archive_to_home.assert_called_once_with(
        str(archive),
        category="productivity",
        name="showroom",
    )
    fake_prompt_builder.clear_skills_system_prompt_cache.assert_called_once_with(
        clear_snapshot=True
    )


def test_skills_manage_set_enabled_updates_config(server):
    saved = []
    fake_config = types.SimpleNamespace(
        load_config=MagicMock(return_value={"skills": {"disabled": ["showroom"]}}),
        save_config=MagicMock(side_effect=lambda cfg: saved.append(cfg)),
    )
    fake_prompt_builder = types.SimpleNamespace(
        clear_skills_system_prompt_cache=MagicMock()
    )

    with patch.dict(sys.modules, {
        "hermes_cli.config": fake_config,
        "agent.prompt_builder": fake_prompt_builder,
    }):
        resp = server.handle_request({
            "id": "skills-enable",
            "method": "skills.manage",
            "params": {"action": "enable", "query": "showroom"},
        })

    assert "error" not in resp
    assert resp["result"] == {"name": "showroom", "enabled": True, "platform": None}
    assert saved == [{"skills": {"disabled": []}}]


def test_platforms_manage_catalog_returns_structured_platforms(server):
    fake_gateway = types.SimpleNamespace(
        _all_platforms=MagicMock(return_value=[
            {
                "key": "feishu",
                "label": "Feishu / Lark",
                "token_var": "FEISHU_APP_ID",
                "setup_instructions": ["1. Create a Feishu app"],
                "vars": [
                    {"name": "FEISHU_APP_ID", "prompt": "App ID", "password": False},
                ],
            }
        ]),
        _platform_status=MagicMock(return_value="configured"),
    )
    fake_status = types.SimpleNamespace(
        read_runtime_status=MagicMock(return_value={
            "gateway_state": "running",
            "active_agents": 1,
            "platforms": {"feishu": {"state": "connected"}},
            "updated_at": "now",
        })
    )

    with patch.dict(sys.modules, {
        "hermes_cli.gateway": fake_gateway,
        "channels.runtime_status": fake_status,
    }):
        resp = server.handle_request({
            "id": "platforms-catalog",
            "method": "platforms.manage",
            "params": {"action": "catalog"},
        })

    assert "error" not in resp
    result = resp["result"]
    assert result["gateway"]["state"] == "running"
    assert result["platforms"][0]["id"] == "feishu"
    assert result["platforms"][0]["status"] == "connected"
    assert result["platforms"][0]["capabilities"]["pairing"] is True


def test_platforms_manage_schema_maps_setup_vars(server):
    fake_gateway = types.SimpleNamespace(
        _all_platforms=MagicMock(return_value=[
            {
                "key": "feishu",
                "label": "Feishu / Lark",
                "token_var": "FEISHU_APP_ID",
                "vars": [
                    {
                        "name": "FEISHU_APP_ID",
                        "prompt": "App ID",
                        "password": False,
                        "help": "App ID help",
                    },
                    {
                        "name": "FEISHU_APP_SECRET",
                        "prompt": "App Secret",
                        "password": True,
                    },
                ],
            }
        ]),
    )
    fake_config = types.SimpleNamespace(
        get_env_value=MagicMock(side_effect=lambda name: {
            "FEISHU_APP_ID": "cli_a",
            "FEISHU_APP_SECRET": "secret",
        }.get(name, "")),
    )

    with patch.dict(sys.modules, {
        "hermes_cli.gateway": fake_gateway,
        "hermes_cli.config": fake_config,
    }):
        resp = server.handle_request({
            "id": "platforms-schema",
            "method": "platforms.manage",
            "params": {"action": "schema", "platform": "feishu"},
        })

    assert "error" not in resp
    result = resp["result"]
    assert result["platform"] == "feishu"
    field_keys = [field["key"] for field in result["fields"]]
    assert field_keys[:3] == ["enabled", "FEISHU_APP_ID", "FEISHU_APP_SECRET"]
    assert result["value"]["FEISHU_APP_ID"] == "cli_a"
    assert result["value"]["FEISHU_APP_SECRET"] == "********"


def test_platforms_manage_patch_config_saves_env_and_config(server, monkeypatch):
    fake_gateway = types.SimpleNamespace(
        _all_platforms=MagicMock(return_value=[{"key": "feishu", "label": "Feishu"}]),
    )
    saved_env = []
    fake_config = types.SimpleNamespace(
        save_env_value=MagicMock(side_effect=lambda key, value: saved_env.append((key, value))),
    )
    import tui_gateway.methods.integrations as integrations

    saved_cfg = []
    monkeypatch.setattr(integrations, "_load_cfg", lambda: {})
    monkeypatch.setattr(integrations, "_save_cfg", lambda cfg: saved_cfg.append(cfg))

    with patch.dict(sys.modules, {
        "hermes_cli.gateway": fake_gateway,
        "hermes_cli.config": fake_config,
    }):
        resp = server.handle_request({
            "id": "platforms-patch",
            "method": "platforms.manage",
            "params": {
                "action": "patch_config",
                "platform": "feishu",
                "config": {
                    "enabled": True,
                    "FEISHU_APP_ID": "cli_a",
                    "FEISHU_APP_SECRET": "********",
                },
            },
        })

    assert "error" not in resp
    assert resp["result"]["configUpdated"] is True
    assert saved_cfg == [{"platforms": {"feishu": {"enabled": True}}}]
    assert saved_env == [("FEISHU_APP_ID", "cli_a")]


def test_platforms_manage_dingtalk_qr_flow_saves_credentials(server, monkeypatch):
    fake_gateway = types.SimpleNamespace(
        _all_platforms=MagicMock(return_value=[{"key": "dingtalk", "label": "DingTalk"}]),
    )
    saved_env = []
    fake_config = types.SimpleNamespace(
        save_env_value=MagicMock(side_effect=lambda key, value: saved_env.append((key, value))),
    )
    fake_dingtalk_auth = types.SimpleNamespace(
        begin_registration=MagicMock(return_value={
            "device_code": "device-1",
            "verification_uri_complete": "https://dingtalk.example/qr",
            "expires_in": 60,
            "interval": 2,
        }),
        poll_registration=MagicMock(return_value={
            "status": "SUCCESS",
            "client_id": "ding-id",
            "client_secret": "ding-secret",
        }),
    )
    import tui_gateway.methods.integrations as integrations

    saved_cfg = []
    monkeypatch.setattr(integrations, "_load_cfg", lambda: {})
    monkeypatch.setattr(integrations, "_save_cfg", lambda cfg: saved_cfg.append(cfg))

    with patch.dict(sys.modules, {
        "hermes_cli.gateway": fake_gateway,
        "hermes_cli.config": fake_config,
        "hermes_cli.dingtalk_auth": fake_dingtalk_auth,
    }):
        start = server.handle_request({
            "id": "platforms-qr-start",
            "method": "platforms.manage",
            "params": {"action": "qr.start", "platform": "dingtalk"},
        })
        assert "error" not in start

        status = server.handle_request({
            "id": "platforms-qr-status",
            "method": "platforms.manage",
            "params": {
                "action": "qr.status",
                "platform": "dingtalk",
                "flowId": start["result"]["flowId"],
            },
        })

    assert "error" not in status
    assert status["result"]["status"] == "confirmed"
    assert status["result"]["account"]["accountId"] == "ding-id"
    assert ("DINGTALK_CLIENT_ID", "ding-id") in saved_env
    assert ("DINGTALK_CLIENT_SECRET", "ding-secret") in saved_env
    assert saved_cfg == [{"platforms": {"dingtalk": {"enabled": True}}}]


def test_platforms_manage_feishu_qr_flow_does_not_persist_bot_display_name(server, monkeypatch):
    fake_gateway = types.SimpleNamespace(
        _all_platforms=MagicMock(return_value=[{"key": "feishu", "label": "Feishu"}]),
    )
    saved_env = []
    fake_config = types.SimpleNamespace(
        save_env_value=MagicMock(side_effect=lambda key, value: saved_env.append((key, value))),
    )
    fake_feishu = types.SimpleNamespace(
        _init_registration=MagicMock(),
        _begin_registration=MagicMock(return_value={
            "device_code": "device-1",
            "qr_url": "https://feishu.example/qr",
            "expire_in": 60,
            "interval": 2,
        }),
        _accounts_base_url=MagicMock(return_value="https://accounts.example"),
        _post_registration=MagicMock(return_value={
            "client_id": "cli-feishu",
            "client_secret": "secret-feishu",
            "user_info": {"tenant_brand": "feishu", "open_id": "ou_owner"},
        }),
        probe_bot=MagicMock(return_value={"bot_name": "赛克思 汪汪"}),
    )
    import tui_gateway.methods.integrations as integrations

    saved_cfg = []
    monkeypatch.setattr(integrations, "_load_cfg", lambda: {})
    monkeypatch.setattr(integrations, "_save_cfg", lambda cfg: saved_cfg.append(cfg))

    with patch.dict(sys.modules, {
        "hermes_cli.gateway": fake_gateway,
        "hermes_cli.config": fake_config,
        "channels.platforms.feishu": fake_feishu,
    }):
        start = server.handle_request({
            "id": "platforms-feishu-qr-start",
            "method": "platforms.manage",
            "params": {"action": "qr.start", "platform": "feishu"},
        })
        assert "error" not in start

        status = server.handle_request({
            "id": "platforms-feishu-qr-status",
            "method": "platforms.manage",
            "params": {
                "action": "qr.status",
                "platform": "feishu",
                "flowId": start["result"]["flowId"],
            },
        })

    assert "error" not in status
    assert status["result"]["status"] == "confirmed"
    assert status["result"]["account"]["label"] == "赛克思 汪汪"
    assert ("FEISHU_BOT_NAME", "赛克思 汪汪") not in saved_env
    assert ("FEISHU_APP_ID", "cli-feishu") in saved_env
    assert ("FEISHU_ALLOWED_USERS", "ou_owner") in saved_env


def test_platforms_manage_weixin_qr_flow_allows_scan_owner(server, monkeypatch):
    fake_gateway = types.SimpleNamespace(
        _all_platforms=MagicMock(return_value=[{"key": "weixin", "label": "Weixin"}]),
    )
    saved_env = []
    fake_config = types.SimpleNamespace(
        save_env_value=MagicMock(side_effect=lambda key, value: saved_env.append((key, value))),
    )

    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def _fake_api_get(_session, *, endpoint, **_kwargs):
        if "get_bot_qr" in endpoint:
            return {
                "qrcode": "qr-1",
                "qrcode_img_content": "https://weixin.example/qr.png",
            }
        if "get_qr_status" in endpoint:
            return {
                "status": "confirmed",
                "ilink_bot_id": "bot-1",
                "bot_token": "token-1",
                "baseurl": "https://weixin.example",
                "ilink_user_id": "owner@im.wechat",
            }
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    fake_weixin = types.SimpleNamespace(
        AIOHTTP_AVAILABLE=True,
        aiohttp=types.SimpleNamespace(ClientSession=_FakeClientSession),
        _make_ssl_connector=MagicMock(return_value=None),
        _api_get=_fake_api_get,
        ILINK_BASE_URL="https://weixin.example",
        EP_GET_BOT_QR="get_bot_qr",
        EP_GET_QR_STATUS="get_qr_status",
        QR_TIMEOUT_MS=1000,
        WEIXIN_CDN_BASE_URL="https://cdn.weixin.example",
        save_weixin_account=MagicMock(),
    )
    import tui_gateway.methods.integrations as integrations

    saved_cfg = []
    monkeypatch.setattr(integrations, "_load_cfg", lambda: {})
    monkeypatch.setattr(integrations, "_save_cfg", lambda cfg: saved_cfg.append(cfg))

    with patch.dict(sys.modules, {
        "hermes_cli.gateway": fake_gateway,
        "hermes_cli.config": fake_config,
        "channels.platforms.weixin": fake_weixin,
    }):
        start = server.handle_request({
            "id": "platforms-weixin-qr-start",
            "method": "platforms.manage",
            "params": {"action": "qr.start", "platform": "weixin"},
        })
        assert "error" not in start

        status = server.handle_request({
            "id": "platforms-weixin-qr-status",
            "method": "platforms.manage",
            "params": {
                "action": "qr.status",
                "platform": "weixin",
                "flowId": start["result"]["flowId"],
            },
        })

    assert "error" not in status
    assert status["result"]["status"] == "confirmed"
    assert ("WEIXIN_ALLOWED_USERS", "owner@im.wechat") in saved_env
    assert ("WEIXIN_HOME_CHANNEL", "owner@im.wechat") in saved_env
    assert saved_cfg == [{"platforms": {"weixin": {"enabled": True}}}]


def test_command_dispatch_steer_fallback_sends_message(server):
    """command.dispatch /steer with no active agent falls back to send."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid, "agent": None}

    resp = server.handle_request({
        "id": "r3",
        "method": "command.dispatch",
        "params": {"name": "steer", "arg": "focus on testing", "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == "focus on testing"


def test_command_dispatch_retry_finds_last_user_message(server):
    """command.dispatch /retry walks session['history'] to find the last user message."""
    sid = "test-session"
    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]
    server._sessions[sid] = {
        "session_key": sid,
        "agent": None,
        "history": history,
        "history_lock": threading.Lock(),
        "history_version": 0,
    }

    resp = server.handle_request({
        "id": "r4",
        "method": "command.dispatch",
        "params": {"name": "retry", "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == "second question"
    # Verify history was truncated: everything from last user message onward removed
    assert len(server._sessions[sid]["history"]) == 2
    assert server._sessions[sid]["history"][-1]["role"] == "assistant"
    assert server._sessions[sid]["history_version"] == 1


def test_command_dispatch_retry_empty_history(server):
    """command.dispatch /retry with empty history returns error."""
    sid = "test-session"
    server._sessions[sid] = {
        "session_key": sid,
        "agent": None,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
    }

    resp = server.handle_request({
        "id": "r5",
        "method": "command.dispatch",
        "params": {"name": "retry", "session_id": sid},
    })

    assert "error" in resp
    assert resp["error"]["code"] == 4018


def test_command_dispatch_retry_handles_multipart_content(server):
    """command.dispatch /retry extracts text from multipart content lists."""
    sid = "test-session"
    history = [
        {"role": "user", "content": [
            {"type": "text", "text": "analyze this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        ]},
        {"role": "assistant", "content": "I see the image."},
    ]
    server._sessions[sid] = {
        "session_key": sid,
        "agent": None,
        "history": history,
        "history_lock": threading.Lock(),
        "history_version": 0,
    }

    resp = server.handle_request({
        "id": "r6",
        "method": "command.dispatch",
        "params": {"name": "retry", "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == "analyze this"


def test_command_dispatch_returns_skill_payload(server):
    """command.dispatch returns structured skill payload for the TUI to send()."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}

    fake_skills = {"/hermes-agent-dev": {"name": "hermes-agent-dev", "description": "Dev workflow"}}
    fake_msg = "Loaded skill content here"

    with patch("agent.skill_commands.scan_skill_commands", return_value=fake_skills), \
         patch("agent.skill_commands.build_skill_invocation_message", return_value=fake_msg):
        resp = server.handle_request({
            "id": "r2",
            "method": "command.dispatch",
            "params": {"name": "hermes-agent-dev", "session_id": sid},
        })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "skill"
    assert result["message"] == fake_msg
    assert result["name"] == "hermes-agent-dev"


def test_command_dispatch_awaits_async_plugin_handler(server):
    async def _handler(arg):
        return f"async:{arg}"

    with patch(
        "hermes_cli.plugins.get_plugin_command_handler",
        lambda name: _handler if name == "async-cmd" else None,
    ):
        resp = server.handle_request({
            "id": "r-plugin",
            "method": "command.dispatch",
            "params": {"name": "async-cmd", "arg": "hello"},
        })

    assert "error" not in resp
    assert resp["result"] == {"type": "plugin", "output": "async:hello"}


# ── dispatch(): pool routing for long handlers (#12546) ──────────────


def test_dispatch_runs_short_handlers_inline(server):
    """Non-long handlers return their response synchronously from dispatch()."""
    server._methods["fast.ping"] = lambda rid, params: server._ok(rid, {"pong": True})

    resp = server.dispatch({"id": "r1", "method": "fast.ping", "params": {}})

    assert resp == {"jsonrpc": "2.0", "id": "r1", "result": {"pong": True}}


def test_dispatch_offloads_long_handlers_and_emits_via_stdout(capture):
    """Long handlers run on the pool and write their response via write_json."""
    server, buf = capture
    server._methods["slash.exec"] = lambda rid, params: server._ok(rid, {"output": "hi"})

    resp = server.dispatch({"id": "r2", "method": "slash.exec", "params": {}})
    assert resp is None

    for _ in range(50):
        if buf.getvalue():
            break
        time.sleep(0.01)

    written = json.loads(buf.getvalue())
    assert written == {"jsonrpc": "2.0", "id": "r2", "result": {"output": "hi"}}


def test_dispatch_long_handler_does_not_block_fast_handler(server):
    """A slow long handler must not prevent a concurrent fast handler from completing."""
    released = threading.Event()
    server._methods["slash.exec"] = lambda rid, params: (released.wait(timeout=5), server._ok(rid, {"done": True}))[1]
    server._methods["fast.ping"] = lambda rid, params: server._ok(rid, {"pong": True})

    t0 = time.monotonic()
    assert server.dispatch({"id": "slow", "method": "slash.exec", "params": {}}) is None

    fast_resp = server.dispatch({"id": "fast", "method": "fast.ping", "params": {}})
    fast_elapsed = time.monotonic() - t0

    assert fast_resp["result"] == {"pong": True}
    assert fast_elapsed < 0.5, f"fast handler blocked for {fast_elapsed:.2f}s behind slow handler"

    released.set()


def test_dispatch_session_compress_does_not_block_fast_handler(server):
    """Manual TUI compaction can take minutes, so it must not block the RPC loop."""
    released = threading.Event()

    def slow_compress(rid, params):
        released.wait(timeout=5)
        return server._ok(rid, {"done": True})

    server._methods["session.compress"] = slow_compress
    server._methods["fast.ping"] = lambda rid, params: server._ok(rid, {"pong": True})

    t0 = time.monotonic()
    assert server.dispatch({"id": "slow", "method": "session.compress", "params": {}}) is None

    fast_resp = server.dispatch({"id": "fast", "method": "fast.ping", "params": {}})
    fast_elapsed = time.monotonic() - t0

    assert fast_resp["result"] == {"pong": True}
    assert fast_elapsed < 0.5, f"fast handler blocked for {fast_elapsed:.2f}s behind session.compress"

    released.set()


def test_dispatch_long_handler_exception_produces_error_response(capture):
    """An exception inside a pool-dispatched handler still yields a JSON-RPC error."""
    server, buf = capture

    def boom(rid, params):
        raise RuntimeError("kaboom")

    server._methods["slash.exec"] = boom

    server.dispatch({"id": "r3", "method": "slash.exec", "params": {}})

    for _ in range(50):
        if buf.getvalue():
            break
        time.sleep(0.01)

    written = json.loads(buf.getvalue())
    assert written["id"] == "r3"
    assert written["error"]["code"] == -32000
    assert "kaboom" in written["error"]["message"]


def test_dispatch_unknown_long_method_still_goes_inline(server):
    """Method name not in _LONG_HANDLERS takes the sync path even if handler is slow."""
    server._methods["some.method"] = lambda rid, params: server._ok(rid, {"ok": True})

    resp = server.dispatch({"id": "r4", "method": "some.method", "params": {}})

    assert resp["result"] == {"ok": True}
