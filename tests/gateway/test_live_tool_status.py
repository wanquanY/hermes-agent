"""Live status text stays independent from permanent tool-progress bubbles."""

from types import SimpleNamespace

from channels.platforms.base_delivery import BaseDeliveryMixin
from hermes_gateway.config import Platform
from hermes_gateway.session import SessionSource
from hermes_gateway.tool_progress_runtime import ToolProgressRuntime


class StatusAdapter(BaseDeliveryMixin):
    supports_status_text = True


def make_runtime(*, current=lambda: True):
    adapter = StatusAdapter()
    source = SessionSource(platform=Platform.SLACK, chat_id="C123", thread_id="T123")
    runner = SimpleNamespace(
        adapters={Platform.SLACK: adapter},
        _thread_metadata_for_source=lambda _source, _message_id: {"thread_id": "T123"},
    )
    runtime = ToolProgressRuntime(
        runner=runner,
        user_config={
            "display": {
                "platforms": {
                    "slack": {"tool_progress": "off", "live_status": "full"}
                }
            }
        },
        platform_key="slack",
        source=source,
        event_message_id="message-1",
        run_still_current=current,
        agent_provider=lambda: None,
        load_gateway_config=lambda: {},
    )
    return runtime, adapter


def test_live_status_callback_remains_enabled_when_progress_is_off():
    runtime, adapter = make_runtime()

    assert runtime.enabled is False
    assert runtime.callback_enabled is True
    runtime.callback("tool.started", "terminal", args={"command": "pytest"})
    assert adapter._status_text["C123"]

    runtime.callback("tool.completed", "terminal")
    assert adapter._status_text == {}


def test_stale_turn_does_not_clear_newer_status():
    runtime, adapter = make_runtime(current=lambda: False)
    adapter.set_status_text("C123", "newer turn")

    runtime.clear_live_status()

    assert adapter._status_text["C123"] == "newer turn"
