"""Atomic config persistence shared by gateway command services."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from hermes_cli.config import atomic_config_write

logger = logging.getLogger(__name__)


def save_gateway_config_key(config_path: Path, key_path: str, value: Any) -> bool:
    """Persist one dot-separated config key without losing sibling settings."""
    try:
        config: dict[str, Any] = {}
        if config_path.exists():
            with config_path.open(encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if isinstance(loaded, dict):
                config = loaded

        current = config
        keys = key_path.split(".")
        for key in keys[:-1]:
            child = current.get(key)
            if not isinstance(child, dict):
                child = {}
                current[key] = child
            current = child
        current[keys[-1]] = value
        atomic_config_write(config_path, config)
        return True
    except Exception as exc:
        logger.error("Failed to save config key %s: %s", key_path, exc)
        return False
