from __future__ import annotations

from hermes_text_delta import project_text_delta, utf16_code_unit_length


def _append_payload(current: str) -> dict:
    return {"mode": "append", "offset": utf16_code_unit_length(current)}


def test_repeated_markdown_tokens_are_not_dropped() -> None:
    text = ""
    for chunk in (
        "1. ",
        "**",
        "search_files",
        "**",
        " — 列出\n",
        "2. ",
        "**",
        "read_file",
        "**",
        " — 读取",
    ):
        text = project_text_delta(text, chunk, _append_payload(text)).text

    assert text == "1. **search_files** — 列出\n2. **read_file** — 读取"


def test_duplicate_delta_at_same_offset_is_idempotent() -> None:
    payload = {"mode": "append", "offset": 0}
    text = project_text_delta("", "好的", payload).text
    text = project_text_delta(text, "好的", payload).text

    assert text == "好的"


def test_offsets_use_utf16_code_units() -> None:
    text = project_text_delta("", "😀", {"mode": "append", "offset": 0}).text
    text = project_text_delta(text, " OK", {"mode": "append", "offset": 2}).text

    assert text == "😀 OK"
