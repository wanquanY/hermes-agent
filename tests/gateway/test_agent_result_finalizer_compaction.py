from types import SimpleNamespace
from unittest.mock import MagicMock

from hermes_gateway.agent_result_finalizer import AgentResultFinalizer


def _finalizer() -> AgentResultFinalizer:
    return AgentResultFinalizer(
        session_store=MagicMock(),
        session_db=None,
        source=SimpleNamespace(platform=None),
        session_id="session-1",
        session_key="platform:key",
        tools=[],
        history_offset=12,
        history_media_paths=set(),
    )


def _agent(*, compacted_in_place: bool):
    return SimpleNamespace(
        session_id="session-1",
        _last_compaction_in_place=compacted_in_place,
        context_compressor=SimpleNamespace(
            last_prompt_tokens=10,
            context_length=100,
        ),
        session_prompt_tokens=11,
        session_completion_tokens=12,
        model="model",
    )


def test_in_place_compaction_rebaselines_gateway_history_offset():
    result = _finalizer().finalize(
        {
            "final_response": "done",
            "messages": [{"role": "assistant", "content": "done"}],
        },
        _agent(compacted_in_place=True),
    )

    assert result["history_offset"] == 0
    assert result["compacted_in_place"] is True
    assert result["session_id"] == "session-1"


def test_normal_turn_keeps_gateway_history_offset():
    result = _finalizer().finalize(
        {"final_response": "done", "messages": []},
        _agent(compacted_in_place=False),
    )

    assert result["history_offset"] == 12
    assert result["compacted_in_place"] is False


def test_empty_result_keeps_compaction_rebaseline():
    result = _finalizer().finalize(
        {"final_response": "", "messages": []},
        _agent(compacted_in_place=True),
    )

    assert result["history_offset"] == 0
    assert result["compacted_in_place"] is True
    assert result["session_id"] == "session-1"
