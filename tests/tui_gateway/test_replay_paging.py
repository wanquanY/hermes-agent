from __future__ import annotations

import base64
import json

from tui_gateway.services.replay_paging import build_replay_page


def _event(seq: int, text: str) -> dict:
    return {
        "type": "subagent.tool",
        "seq": seq,
        "conversation_session_id": "conversation-replay",
        "run_id": "run-replay",
        "payload": {
            "subagent_id": "subagent-replay",
            "result_text": text,
        },
    }


def _wire_bytes(page) -> int:
    return len(
        json.dumps(page.as_result(), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def test_replay_pages_are_bounded_by_wire_bytes_not_only_event_count() -> None:
    events = [_event(seq, "界" * 9_000) for seq in range(1, 9)]

    first = build_replay_page(
        events,
        after_seq=0,
        replay_until_seq=8,
        source_has_more=False,
        max_bytes=64 * 1024,
    )

    assert first.has_more is True
    assert 0 < len(first.events) < len(events)
    assert first.next_after_seq == first.events[-1]["seq"]
    assert _wire_bytes(first) <= 64 * 1024


def test_single_oversized_event_is_reassembled_from_bounded_fragments() -> None:
    event = _event(7, "large-tool-output-" * 20_000)
    after_seq = 6
    fragment_offset = 0
    fragment_id = ""
    chunks: list[bytes] = []

    for _ in range(100):
        page = build_replay_page(
            [event],
            after_seq=after_seq,
            replay_until_seq=7,
            source_has_more=False,
            max_bytes=64 * 1024,
            fragment_seq=7 if fragment_offset else 0,
            fragment_offset=fragment_offset,
            fragment_id=fragment_id,
        )
        assert page.events == []
        assert page.fragment is not None
        assert _wire_bytes(page) <= 64 * 1024
        fragment = page.fragment
        fragment_id = str(fragment["id"])
        assert int(fragment["offset"]) == fragment_offset
        chunks.append(base64.b64decode(str(fragment["data"])))
        fragment_offset = int(fragment["next_offset"])
        if fragment["done"]:
            assert page.has_more is False
            assert page.next_after_seq == 7
            break
    else:
        raise AssertionError("fragment replay did not finish")

    assert json.loads(b"".join(chunks).decode("utf-8")) == event
