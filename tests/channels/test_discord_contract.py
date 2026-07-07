from __future__ import annotations

from channels.platforms.discord import DiscordAdapter
from channels.platforms.discord import VoiceReceiver
from channels.platforms.discord import _build_allowed_mentions
from channels.platforms import discord as discord_module
from channels.platforms.discord import check_discord_requirements


class _AllowedMentionsProbe:
    def __init__(self, *, everyone=True, roles=True, users=True, replied_user=True):
        self.everyone = everyone
        self.roles = roles
        self.users = users
        self.replied_user = replied_user


def test_channels_discord_exports_adapter_contract() -> None:
    assert DiscordAdapter.__name__ == "DiscordAdapter"
    assert DiscordAdapter.MAX_MESSAGE_LENGTH == 2000
    assert VoiceReceiver.__name__ == "VoiceReceiver"
    assert isinstance(check_discord_requirements(), bool)


def test_channels_discord_allowed_mentions_contract(monkeypatch) -> None:
    if getattr(discord_module, "discord", None) is not None:
        monkeypatch.setattr(
            discord_module.discord,
            "AllowedMentions",
            _AllowedMentionsProbe,
            raising=False,
        )

    allowed_mentions = _build_allowed_mentions()
    if allowed_mentions is not None:
        assert allowed_mentions.everyone is False
        assert allowed_mentions.roles is False
