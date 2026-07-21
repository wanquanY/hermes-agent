from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hermes_cli import computer_use_routes


@contextmanager
def _scope(profile):
    yield profile


@pytest.mark.asyncio
async def test_status_runs_inside_requested_profile(monkeypatch):
    seen = []

    @contextmanager
    def scope(profile):
        seen.append(profile)
        yield

    expected = {"ready": True, "platform": "darwin"}
    monkeypatch.setattr(
        "tools.computer_use.permissions.computer_use_status",
        lambda: expected,
    )
    computer_use_routes.configure(
        profile_scope=scope,
        spawn_action=lambda *_args: None,
        profile_cli_args=lambda profile: ["--profile", profile] if profile else [],
    )

    assert await computer_use_routes.get_computer_use_status("studio") == expected
    assert seen == ["studio"]


@pytest.mark.asyncio
async def test_grant_spawns_profile_scoped_action(monkeypatch):
    calls = []
    monkeypatch.setattr(computer_use_routes.sys, "platform", "darwin")
    computer_use_routes.configure(
        profile_scope=_scope,
        spawn_action=lambda command, name: (
            calls.append((command, name)) or SimpleNamespace(pid=4321)
        ),
        profile_cli_args=lambda profile: ["--profile", profile] if profile else [],
    )

    result = await computer_use_routes.grant_computer_use_permissions("studio")
    assert result == {"ok": True, "pid": 4321, "name": "computer-use-grant"}
    assert calls == [
        (
            ["--profile", "studio", "computer-use", "permissions", "grant"],
            "computer-use-grant",
        )
    ]


@pytest.mark.asyncio
async def test_grant_rejects_non_macos(monkeypatch):
    monkeypatch.setattr(computer_use_routes.sys, "platform", "linux")
    with pytest.raises(HTTPException) as exc_info:
        await computer_use_routes.grant_computer_use_permissions()
    assert exc_info.value.status_code == 400
