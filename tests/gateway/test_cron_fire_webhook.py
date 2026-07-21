"""Chronos fire ingress contract for the aiohttp API server."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from channels.platforms.api_server import APIServerAdapter
from hermes_agent.application.active_work_registry import ActiveWorkRegistry
from hermes_gateway.config import PlatformConfig


def _adapter() -> APIServerAdapter:
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._active_work_registry = ActiveWorkRegistry()
    adapter._background_tasks = set()
    return adapter


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/cron/fire", adapter._handle_cron_fire)
    return app


@pytest.mark.asyncio
async def test_invalid_token_is_rejected(monkeypatch):
    adapter = _adapter()
    fire = MagicMock()
    monkeypatch.setattr(
        "channels.platforms.api_server_jobs.cron_fire_service.verify_token",
        lambda _token: None,
    )
    monkeypatch.setattr(
        "channels.platforms.api_server_jobs.cron_fire_service.fire_due",
        fire,
    )

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer forged"},
            json={"job_id": "abcdef123456"},
        )

    assert response.status == 401
    fire.assert_not_called()


@pytest.mark.asyncio
async def test_valid_fire_is_admitted_and_released(monkeypatch):
    adapter = _adapter()
    fired = asyncio.Event()
    monkeypatch.setattr(
        "channels.platforms.api_server_jobs.cron_fire_service.verify_token",
        lambda _token: {"purpose": "cron_fire"},
    )

    def _fire(*_args, **_kwargs):
        fired.set()
        return True

    monkeypatch.setattr(
        "channels.platforms.api_server_jobs.cron_fire_service.fire_due",
        _fire,
    )

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer valid"},
            json={"job_id": "abcdef123456"},
        )
        assert response.status == 202
        await asyncio.wait_for(fired.wait(), timeout=2)
        await asyncio.gather(*tuple(adapter._background_tasks))

    assert adapter._active_work_registry.snapshot() == ()


@pytest.mark.asyncio
async def test_drain_rejects_fire_before_background_handoff(monkeypatch):
    adapter = _adapter()
    adapter._active_work_registry.begin_drain()
    fire = MagicMock()
    monkeypatch.setattr(
        "channels.platforms.api_server_jobs.cron_fire_service.verify_token",
        lambda _token: {"purpose": "cron_fire"},
    )
    monkeypatch.setattr(
        "channels.platforms.api_server_jobs.cron_fire_service.fire_due",
        fire,
    )

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer valid"},
            json={"job_id": "abcdef123456"},
        )

    assert response.status == 503
    fire.assert_not_called()


def test_route_table_includes_cron_fire_and_profile_mirror():
    adapter = _adapter()
    paths = {path for _method, path, _handler in adapter._http_route_table()}
    assert "/api/cron/fire" in paths
    assert "/p/{profile}/api/cron/fire" in {
        f"/p/{{profile}}{path}" for path in paths
    }
