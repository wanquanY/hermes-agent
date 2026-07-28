from __future__ import annotations

import time

from tui_gateway.services.capability_operations import CapabilityOperationRegistry


def _wait_terminal(registry: CapabilityOperationRegistry, operation_id: str) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        snapshot = registry.get(operation_id)
        if snapshot and snapshot["terminal"]:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("capability operation did not become terminal")


def test_operation_publishes_stages_and_terminal_result():
    registry = CapabilityOperationRegistry()
    emitted: list[dict] = []

    def runner(update):
        update(phase="installing", progress=35, message="Installing")
        update(phase="discovering", progress=70, details={"tool_count": 2})
        return {"ok": True, "tool_count": 2}

    initial = registry.start(
        kind="mcp.install",
        target="demo",
        runner=runner,
        emit=emitted.append,
    )
    terminal = _wait_terminal(registry, initial["operation_id"])

    assert terminal["status"] == "succeeded"
    assert terminal["phase"] == "ready"
    assert terminal["result"] == {"ok": True, "tool_count": 2}
    assert terminal["details"] == {"tool_count": 2}
    assert any(item["phase"] == "installing" for item in emitted)
    assert any(item["phase"] == "discovering" for item in emitted)


def test_operation_preserves_degraded_as_a_successful_configuration_with_attention():
    registry = CapabilityOperationRegistry()

    initial = registry.start(
        kind="mcp.install",
        target="desktop-server",
        runner=lambda update: {
            "ok": False,
            "operation_status": "degraded",
            "operation_phase": "awaiting_external_runtime",
            "operation_message": "Start the desktop application",
        },
    )
    terminal = _wait_terminal(registry, initial["operation_id"])

    assert terminal["status"] == "degraded"
    assert terminal["phase"] == "awaiting_external_runtime"
    assert terminal["message"] == "Start the desktop application"
    assert terminal["result"] == {"ok": False}


def test_operation_converts_runner_errors_to_failed_state():
    registry = CapabilityOperationRegistry()

    def runner(update):
        raise RuntimeError("installation exploded")

    initial = registry.start(
        kind="plugin.install",
        target="broken/plugin",
        runner=runner,
    )
    terminal = _wait_terminal(registry, initial["operation_id"])

    assert terminal["status"] == "failed"
    assert terminal["error"] == "installation exploded"


def test_mcp_operation_never_exposes_task_group_wrapper_as_product_error():
    registry = CapabilityOperationRegistry()

    def runner(update):
        del update
        raise ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ConnectionRefusedError(61, "Connection refused")],
        )

    initial = registry.start(
        kind="mcp.install",
        target="desktop-server",
        runner=runner,
    )
    terminal = _wait_terminal(registry, initial["operation_id"])

    assert terminal["status"] == "failed"
    assert terminal["error"] == "The MCP server could not be reached"
    assert terminal["details"]["error_code"] == "connection_unreachable"
    assert "TaskGroup" not in terminal["error"]
    assert "Connection refused" in terminal["details"]["technical_error"]
