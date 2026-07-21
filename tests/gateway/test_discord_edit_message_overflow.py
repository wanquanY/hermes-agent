"""Discord streaming-edit overflow contract regressions."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_gateway.config import PlatformConfig


def _ensure_discord_mock() -> None:
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from channels.platforms.discord import DiscordAdapter  # noqa: E402


def _adapter() -> DiscordAdapter:
    return DiscordAdapter(PlatformConfig(enabled=True, token="***"))


def _wire_channel(adapter, original_message, send_side_effect=None):
    sends = []

    async def fake_send(*, content, reference=None):
        sends.append((content, reference))
        if send_side_effect is not None:
            value = send_side_effect(len(sends), content, reference)
            if value is not None:
                return value
        return SimpleNamespace(
            id=9000 + len(sends),
            to_reference=MagicMock(return_value=object()),
        )

    channel = SimpleNamespace(
        id=555,
        fetch_message=AsyncMock(return_value=original_message),
        send=AsyncMock(side_effect=fake_send),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    return channel, sends


@pytest.mark.asyncio
async def test_midstream_overflow_stays_on_original_and_deduplicates_preview():
    adapter = _adapter()
    edits = []
    message = SimpleNamespace(
        id=42,
        edit=AsyncMock(side_effect=lambda *, content: edits.append(content)),
    )
    _channel, sends = _wire_channel(adapter, message)

    first = await adapter.edit_message("555", "42", "x" * 2500)
    second = await adapter.edit_message("555", "42", "x" * 3000)

    assert first.success is True
    assert second.success is True
    assert first.message_id == second.message_id == "42"
    assert len(edits) == 1
    assert len(edits[0]) <= adapter.MAX_MESSAGE_LENGTH
    assert sends == []


@pytest.mark.asyncio
async def test_final_overflow_edits_first_chunk_and_sends_all_continuations():
    adapter = _adapter()
    edits = []
    message = SimpleNamespace(
        id=42,
        edit=AsyncMock(side_effect=lambda *, content: edits.append(content)),
        to_reference=MagicMock(return_value=object()),
    )
    _channel, sends = _wire_channel(adapter, message)

    result = await adapter.edit_message(
        "555",
        "42",
        "q" * 6000 + "END_MARKER",
        finalize=True,
    )

    assert result.success is True
    assert result.continuation_message_ids
    assert result.message_id == result.continuation_message_ids[-1]
    assert len(edits) == 1
    assert len(sends) == len(result.continuation_message_ids)
    delivered = "".join(edits + [content for content, _ref in sends])
    assert "END_MARKER" in delivered
    assert all(
        len(chunk) <= adapter.MAX_MESSAGE_LENGTH
        for chunk in edits + [content for content, _ref in sends]
    )
    assert all(reference is not None for _content, reference in sends)


@pytest.mark.asyncio
async def test_partial_final_overflow_uses_shared_retry_contract():
    adapter = _adapter()
    message = SimpleNamespace(
        id=42,
        edit=AsyncMock(),
        to_reference=MagicMock(return_value=object()),
    )

    def send_side_effect(call_number, _content, _reference):
        if call_number == 1:
            return SimpleNamespace(
                id=9001,
                to_reference=MagicMock(return_value=object()),
            )
        raise RuntimeError("continuation failed")

    _wire_channel(adapter, message, send_side_effect)
    result = await adapter.edit_message(
        "555",
        "42",
        "k" * 6000,
        finalize=True,
    )

    assert result.success is False
    assert result.retryable is True
    assert result.message_id == "9001"
    assert result.raw_response["partial_overflow"] is True
    assert result.raw_response["delivered_prefix"]
    assert result.continuation_message_ids == ("9001",)


@pytest.mark.asyncio
async def test_reactive_length_error_uses_overflow_path_only_for_length():
    adapter = _adapter()
    calls = []

    def edit_effect(*, content):
        calls.append(content)
        if len(calls) == 1:
            raise RuntimeError(
                "400 Bad Request (error code: 50035): "
                "Must be 2000 or fewer in length"
            )

    message = SimpleNamespace(
        id=42,
        edit=AsyncMock(side_effect=edit_effect),
        to_reference=MagicMock(return_value=object()),
    )
    _wire_channel(adapter, message)

    result = await adapter.edit_message(
        "555",
        "42",
        "u" * 1500,
        finalize=True,
    )

    assert result.success is True
    assert len(calls) == 2
    assert adapter._is_length_overflow_error(
        RuntimeError("error code: 50035: Cannot reply to a system message")
    ) is False
