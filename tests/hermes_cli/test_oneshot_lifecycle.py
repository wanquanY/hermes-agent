"""One-shot process ownership, cleanup, and usage-report contracts."""

from __future__ import annotations

import json
import sys
import types

import pytest


def test_stateless_declaration_is_scoped():
    from channels.session_context import (
        async_delivery_supported,
        declare_stateless_channel,
        restore_async_delivery_capability,
    )

    assert async_delivery_supported() is True
    token = declare_stateless_channel()
    try:
        assert async_delivery_supported() is False
    finally:
        restore_async_delivery_capability(token)
    assert async_delivery_supported() is True


def test_cleanup_chain_is_ordered_and_idempotent(monkeypatch):
    from hermes_cli import oneshot_lifecycle as lifecycle

    events = []
    monkeypatch.setattr(lifecycle, "_cleanup_done", False)
    monkeypatch.setattr(
        lifecycle,
        "_cleanup_terminal_environments",
        lambda: events.append("terminal"),
    )
    monkeypatch.setattr(
        lifecycle, "_interrupt_delegations", lambda: events.append("delegation")
    )
    monkeypatch.setattr(
        lifecycle, "_cleanup_browser_sessions", lambda: events.append("browser")
    )
    monkeypatch.setattr(lifecycle, "_shutdown_mcp", lambda: events.append("mcp"))
    monkeypatch.setattr(
        lifecycle,
        "_shutdown_auxiliary_clients",
        lambda: events.append("auxiliary"),
    )

    lifecycle.cleanup_oneshot_runtime()
    lifecycle.cleanup_oneshot_runtime()

    assert events == ["terminal", "delegation", "browser", "mcp", "auxiliary"]


def test_run_and_exit_preserves_usage_path_and_hard_exits(monkeypatch):
    from hermes_cli import oneshot_lifecycle as lifecycle

    captured = {}
    events = []
    fake = types.ModuleType("hermes_cli.oneshot")

    def run(prompt, **kwargs):
        captured.update({"prompt": prompt, **kwargs})
        return 7

    fake.run_oneshot = run
    monkeypatch.setitem(sys.modules, "hermes_cli.oneshot", fake)
    monkeypatch.setattr(
        lifecycle, "cleanup_oneshot_runtime", lambda: events.append("cleanup")
    )
    monkeypatch.setattr(
        lifecycle,
        "hard_exit_after_oneshot",
        lambda code: events.append(f"exit:{code}"),
    )

    lifecycle.run_and_exit_oneshot("hello", usage_file="usage.json")

    assert captured["usage_file"] == "usage.json"
    assert events == ["cleanup", "exit:7"]


def test_hard_exit_still_runs_when_cleanup_is_interrupted(monkeypatch):
    from hermes_cli import oneshot_lifecycle as lifecycle

    fake = types.ModuleType("hermes_cli.oneshot")
    fake.run_oneshot = lambda *_args, **_kwargs: 0
    monkeypatch.setitem(sys.modules, "hermes_cli.oneshot", fake)
    monkeypatch.setattr(
        lifecycle,
        "cleanup_oneshot_runtime",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        lifecycle,
        "hard_exit_after_oneshot",
        lambda code: (_ for _ in ()).throw(SystemExit(code)),
    )

    with pytest.raises(SystemExit) as exc:
        lifecycle.run_and_exit_oneshot("hello")

    assert exc.value.code == 0


def test_usage_report_is_written_on_failure(tmp_path):
    from hermes_cli.oneshot import _write_usage_file

    path = tmp_path / "reports" / "usage.json"
    _write_usage_file(
        str(path),
        {"input_tokens": 12, "model": "m", "failed": False},
        failure="network down",
    )

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["input_tokens"] == 12
    assert report["model"] == "m"
    assert report["failed"] is True
    assert report["failure"] == "network down"
