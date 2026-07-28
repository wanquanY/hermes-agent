"""Dashboard HTTP surface for Computer Use readiness and permission grants."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
import logging
import sys
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()
logger = logging.getLogger(__name__)

_profile_scope: Callable[[str | None], AbstractContextManager[Any]] | None = None
_spawn_action: Callable[[list[str], str], Any] | None = None
_profile_cli_args: Callable[[str | None], list[str]] | None = None


def configure(
    *,
    profile_scope: Callable[[str | None], AbstractContextManager[Any]],
    spawn_action: Callable[[list[str], str], Any],
    profile_cli_args: Callable[[str | None], list[str]],
) -> None:
    global _profile_scope, _spawn_action, _profile_cli_args
    _profile_scope = profile_scope
    _spawn_action = spawn_action
    _profile_cli_args = profile_cli_args


def _dependencies():
    if _profile_scope is None or _spawn_action is None or _profile_cli_args is None:
        raise HTTPException(status_code=503, detail="Computer Use routes are not configured")
    return _profile_scope, _spawn_action, _profile_cli_args


@router.get("/api/tools/computer-use/status")
async def get_computer_use_status(profile: str | None = None):
    from tools.computer_use.permissions import computer_use_status

    profile_scope, _, _ = _dependencies()
    with profile_scope(profile):
        return computer_use_status()


@router.post("/api/tools/computer-use/permissions/grant")
async def grant_computer_use_permissions(profile: str | None = None):
    if sys.platform != "darwin":
        raise HTTPException(
            status_code=400,
            detail="Computer Use permission grants are a macOS concept.",
        )
    _, spawn_action, profile_cli_args = _dependencies()
    try:
        process = spawn_action(
            profile_cli_args(profile)
            + ["computer-use", "permissions", "grant"],
            "computer-use-grant",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to spawn Computer Use permission grant")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to request permissions: {exc}",
        ) from exc
    return {"ok": True, "pid": process.pid, "name": "computer-use-grant"}


__all__ = ["configure", "router"]
