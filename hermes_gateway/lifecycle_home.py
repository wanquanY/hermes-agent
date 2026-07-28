"""Common gateway lifecycle helpers."""

from __future__ import annotations

from pathlib import Path

from hermes_constants import get_hermes_home

GATEWAY_HOME = get_hermes_home()


def gateway_home() -> Path:
    return GATEWAY_HOME
