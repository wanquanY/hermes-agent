"""Paths for gateway-owned packaged assets."""

from __future__ import annotations

from pathlib import Path


def telegram_botfather_threads_settings_path() -> Path:
    """Return the Telegram BotFather topic-settings screenshot path."""
    return Path(__file__).resolve().parent / "assets" / "telegram-botfather-threads-settings.jpg"
