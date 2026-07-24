import threading

import pytest

from tui_gateway import server
from tui_gateway.services import run_control


class _ImmediateThread:
    """Run the prompt worker synchronously so the emitted lifecycle is assertable."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


def _session(agent):
    return {
        "agent": agent,
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        "transient": True,
    }


@pytest.fixture(autouse=True)
def _reset_gateway_state():
    server._sessions.pop("sid", None)
    run_control._reset_for_tests()
    yield
    server._sessions.pop("sid", None)
    run_control._reset_for_tests()


def _submit_prompt(monkeypatch, agent, *, run_id, turn_id):
    emitted = []
    server._sessions["sid"] = _session(agent)
    monkeypatch.setattr(server.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: emitted.append(args))
    monkeypatch.setattr(server, "make_stream_renderer", lambda cols: None)
    monkeypatch.setattr(server, "render_message", lambda raw, cols: None)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    server.handle_request(
        {
            "id": "1",
            "method": "prompt.submit",
            "params": {
                "_run_registry_reserved": True,
                "session_id": "sid",
                "text": "检查文件",
                "run_id": run_id,
                "turn_id": turn_id,
            },
        }
    )
    return emitted


def test_reasoning_segments_complete_in_order_across_tool_boundaries(monkeypatch):
    class _Agent:
        session_id = "session-key"
        reasoning_callback = None

        def run_conversation(
            self,
            prompt,
            conversation_history=None,
            stream_callback=None,
            **_kwargs,
        ):
            callbacks = server._agent_cbs("sid")
            self.reasoning_callback("先确认工作区。")
            callbacks["tool_gen_callback"]("terminal")
            callbacks["tool_start_callback"]("tool-1", "terminal", {"command": "pwd"})
            self.reasoning_callback("再检查文件内容。")
            callbacks["tool_start_callback"]("tool-2", "terminal", {"command": "ls"})
            self.reasoning_callback("最后整理结论。")
            stream_callback("检查完成。")
            return {
                "final_response": "检查完成。",
                "messages": [
                    {"role": "user", "content": "检查文件"},
                    {"role": "assistant", "content": "检查完成。"},
                ],
            }

    emitted = _submit_prompt(
        monkeypatch,
        _Agent(),
        run_id="run-reasoning-segments",
        turn_id="turn-reasoning-segments",
    )

    reasoning_deltas = [args[2] for args in emitted if args[0] == "reasoning.delta"]
    assert [payload["delta"] for payload in reasoning_deltas] == [
        "先确认工作区。",
        "再检查文件内容。",
        "最后整理结论。",
    ]
    assert [payload["offset"] for payload in reasoning_deltas] == [0, 0, 0]
    assert [payload["message_seq_in_run"] for payload in reasoning_deltas] == [1, 2, 3]
    assert [payload["client_message_id"] for payload in reasoning_deltas] == [
        "turn-reasoning-segments:assistant-segment:0",
        "turn-reasoning-segments:assistant-segment:1",
        "turn-reasoning-segments:assistant-segment:2",
    ]

    reasoning_completions = [
        args[2] for args in emitted if args[0] == "reasoning.available"
    ]
    assert [payload["text"] for payload in reasoning_completions] == [
        "先确认工作区。",
        "再检查文件内容。",
        "最后整理结论。",
    ]
    assert [payload["mode"] for payload in reasoning_completions] == [
        "replace",
        "replace",
        "replace",
    ]
    assert [payload["message_seq_in_run"] for payload in reasoning_completions] == [
        1,
        2,
        3,
    ]
    assert [payload["client_message_id"] for payload in reasoning_completions] == [
        "turn-reasoning-segments:assistant-segment:0",
        "turn-reasoning-segments:assistant-segment:1",
        "turn-reasoning-segments:assistant-segment:2",
    ]
    lifecycle_types = {
        "reasoning.delta",
        "reasoning.available",
        "message.delta",
    }
    assert [args[0] for args in emitted if args[0] in lifecycle_types] == [
        "reasoning.delta",
        "reasoning.available",
        "reasoning.delta",
        "reasoning.available",
        "reasoning.delta",
        "reasoning.available",
        "message.delta",
    ]

    message_deltas = [args[2] for args in emitted if args[0] == "message.delta"]
    assert message_deltas[-1]["client_message_id"] == (
        "turn-reasoning-segments:assistant-segment:2"
    )
    assert message_deltas[-1]["message_seq_in_run"] == 3

    complete_events = [args[2] for args in emitted if args[0] == "message.complete"]
    assert complete_events[-1]["client_message_id"] == (
        "turn-reasoning-segments:assistant-segment:2"
    )
    assert complete_events[-1]["message_seq_in_run"] == 3


def test_whitespace_only_reasoning_never_emits_or_owns_a_segment(monkeypatch):
    class _Agent:
        session_id = "session-key"
        reasoning_callback = None

        def run_conversation(
            self,
            prompt,
            conversation_history=None,
            stream_callback=None,
            **_kwargs,
        ):
            callbacks = server._agent_cbs("sid")
            self.reasoning_callback(" ")
            self.reasoning_callback("\n\t")
            callbacks["tool_gen_callback"]("terminal")
            callbacks["tool_start_callback"]("tool-blank", "terminal", {"command": "pwd"})
            stream_callback("当前工作目录已确认。")
            return {
                "final_response": "当前工作目录已确认。",
                "messages": [
                    {"role": "user", "content": "检查目录"},
                    {"role": "assistant", "content": "当前工作目录已确认。"},
                ],
            }

    emitted = _submit_prompt(
        monkeypatch,
        _Agent(),
        run_id="run-blank-reasoning",
        turn_id="turn-blank-reasoning",
    )

    assert [
        args for args in emitted if args[0] in {"reasoning.delta", "reasoning.available"}
    ] == []
    message_deltas = [args[2] for args in emitted if args[0] == "message.delta"]
    assert message_deltas[-1]["client_message_id"] == (
        "turn-blank-reasoning:assistant-segment:0"
    )
    assert message_deltas[-1]["message_seq_in_run"] == 1


def test_reasoning_buffers_leading_whitespace_and_drops_blank_followup_segment(monkeypatch):
    class _Agent:
        session_id = "session-key"
        reasoning_callback = None

        def run_conversation(
            self,
            prompt,
            conversation_history=None,
            stream_callback=None,
            **_kwargs,
        ):
            callbacks = server._agent_cbs("sid")
            self.reasoning_callback(" ")
            self.reasoning_callback("plan")
            self.reasoning_callback(" ")
            self.reasoning_callback("next")
            callbacks["tool_start_callback"]("tool-1", "terminal", {"command": "date"})
            self.reasoning_callback(" ")
            stream_callback("完成。")
            return {
                "final_response": "完成。",
                "messages": [
                    {"role": "user", "content": "检查时间"},
                    {"role": "assistant", "content": "完成。"},
                ],
            }

    emitted = _submit_prompt(
        monkeypatch,
        _Agent(),
        run_id="run-buffered-reasoning",
        turn_id="turn-buffered-reasoning",
    )

    reasoning_deltas = [args[2] for args in emitted if args[0] == "reasoning.delta"]
    assert [payload["delta"] for payload in reasoning_deltas] == [" plan", " ", "next"]
    assert [payload["offset"] for payload in reasoning_deltas] == [0, 5, 6]
    reasoning_completions = [
        args[2] for args in emitted if args[0] == "reasoning.available"
    ]
    assert [payload["text"] for payload in reasoning_completions] == [" plan next"]
    assert [payload["message_seq_in_run"] for payload in reasoning_completions] == [1]

    message_deltas = [args[2] for args in emitted if args[0] == "message.delta"]
    assert message_deltas[-1]["client_message_id"] == (
        "turn-buffered-reasoning:assistant-segment:1"
    )
    assert message_deltas[-1]["message_seq_in_run"] == 2


def test_interrupted_turn_completes_open_reasoning_before_message_terminal(monkeypatch):
    class _Agent:
        reasoning_callback = None

        def run_conversation(
            self,
            prompt,
            conversation_history=None,
            stream_callback=None,
            **_kwargs,
        ):
            self.reasoning_callback("正在等待模型响应。")
            return {
                "final_response": "Operation interrupted: waiting for model response.",
                "interrupted": True,
                "messages": [],
            }

    emitted = _submit_prompt(
        monkeypatch,
        _Agent(),
        run_id="run-interrupted-contract",
        turn_id="turn-interrupted-contract",
    )

    reasoning_completions = [
        event for event in emitted if event[0] == "reasoning.available"
    ]
    complete_events = [event for event in emitted if event[0] == "message.complete"]
    assert len(reasoning_completions) == 1
    assert reasoning_completions[0][2]["text"] == "正在等待模型响应。"
    assert reasoning_completions[0][2]["client_message_id"] == (
        "turn-interrupted-contract:assistant-segment:0"
    )
    assert complete_events[-1][2]["status"] == "interrupted"
    assert emitted.index(reasoning_completions[0]) < emitted.index(complete_events[-1])
