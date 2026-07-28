from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.delegate_tool import (
    _build_child_output_delta_callback,
    _build_child_reasoning_delta_callback,
)


@pytest.mark.parametrize(
    ("builder", "event_type"),
    [
        (_build_child_output_delta_callback, "subagent.output_delta"),
        (_build_child_reasoning_delta_callback, "subagent.reasoning_delta"),
    ],
)
def test_child_stream_delta_uses_append_mode_and_utf16_offsets(
    builder,
    event_type: str,
) -> None:
    progress = Mock()
    parent = SimpleNamespace(tool_progress_callback=progress)
    callback = builder(
        0,
        "inspect repository",
        parent,
        subagent_id="subagent-1",
    )
    assert callback is not None

    callback("📋")
    callback(" result")

    first, second = progress.call_args_list
    assert first.args[:4] == (event_type, None, "📋", None)
    assert first.kwargs["mode"] == "append"
    assert first.kwargs["delta"] == "📋"
    assert first.kwargs["offset"] == 0
    assert second.args[:4] == (event_type, None, " result", None)
    assert second.kwargs["mode"] == "append"
    assert second.kwargs["delta"] == " result"
    assert second.kwargs["offset"] == 2
