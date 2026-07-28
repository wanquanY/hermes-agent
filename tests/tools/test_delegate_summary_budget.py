from __future__ import annotations

from pathlib import Path

import tools.delegate_tool as delegate_tool


class _Compressor:
    def __init__(self, context_length: int, max_tokens: int) -> None:
        self.context_length = context_length
        self.max_tokens = max_tokens


class _Parent:
    def __init__(self, *, context_length: int = 100_000, used_tokens: int = 1_000) -> None:
        self.context_compressor = _Compressor(context_length, 8_000)
        self.session_prompt_tokens = used_tokens
        self.session_id = "parent-session"


def test_dynamic_budget_is_split_across_fanout() -> None:
    parent = _Parent()
    one = delegate_tool._parent_summary_char_budget(parent, 1)
    five = delegate_tool._parent_summary_char_budget(parent, 5)
    twenty = delegate_tool._parent_summary_char_budget(parent, 20)
    assert one is not None and one > five > twenty >= delegate_tool._MIN_SUMMARY_CHARS


def test_small_summaries_preserve_exact_result(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"max_summary_chars": 100})
    monkeypatch.setattr("hermes_constants.get_hermes_dir", lambda *_args: tmp_path)
    results = [{"task_index": 0, "status": "completed", "summary": "short"}]
    delegate_tool._apply_summary_budget(results, _Parent())
    assert results == [{"task_index": 0, "status": "completed", "summary": "short"}]


def test_large_fanout_summaries_spill_losslessly_and_keep_order(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"max_summary_chars": 100})
    monkeypatch.setattr("hermes_constants.get_hermes_dir", lambda *_args: tmp_path)
    raw = "HEAD\n" + "x" * 500 + "\nTAIL"
    results = [
        {"task_index": index, "status": "completed", "summary": raw}
        for index in range(3)
    ]
    delegate_tool._apply_summary_budget(results, _Parent())
    assert [entry["task_index"] for entry in results] == [0, 1, 2]
    for entry in results:
        assert entry["summary_truncated"] is True
        assert "HEAD" in entry["summary"] and "TAIL" in entry["summary"]
        path = Path(entry["summary_full_path"])
        assert path.read_text(encoding="utf-8") == raw
        assert "read_file" in entry["summary"]


def test_summary_budget_is_idempotent_and_preserves_first_full_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"max_summary_chars": 100})
    monkeypatch.setattr("hermes_constants.get_hermes_dir", lambda *_args: tmp_path)
    original = "HEAD\n" + "x" * 500 + "\nTAIL"
    entry = {"task_index": 0, "status": "completed", "summary": original}

    delegate_tool._apply_summary_budget([entry], _Parent())
    first_projection = entry["summary"]
    first_path = entry["summary_full_path"]
    delegate_tool._apply_summary_budget([entry], _Parent())

    assert entry["summary"] == first_projection
    assert entry["summary_full_path"] == first_path
    assert Path(first_path).read_text(encoding="utf-8") == original
    assert len(list(tmp_path.rglob("*.txt"))) == 1


def test_unavailable_spill_directory_still_fails_bounded(monkeypatch) -> None:
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"max_summary_chars": 100})

    def fail_directory(*_args):
        raise OSError("storage unavailable")

    monkeypatch.setattr("hermes_constants.get_hermes_dir", fail_directory)
    original = "sensitive-" * 100
    entry = {"task_index": 0, "status": "completed", "summary": original}
    delegate_tool._apply_summary_budget([entry], _Parent())

    assert entry["summary_truncated"] is True
    assert entry["summary_original_chars"] == len(original)
    assert "summary_full_path" not in entry
    assert len(entry["summary"]) < len(original) + 400
    assert "oversized raw content was withheld" in entry["summary"]
