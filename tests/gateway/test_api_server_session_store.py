"""API session persistence concurrency and profile-isolation contracts."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from channels.config import PlatformConfig
from channels.platforms.api_server import APIServerAdapter
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _adapter():
    return APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-secret"})
    )


def _session_app(adapter):
    app = web.Application()
    app.router.add_get("/api/sessions", adapter._handle_list_sessions)
    app.router.add_post("/api/sessions", adapter._handle_create_session)
    return app


@pytest.mark.asyncio
async def test_session_reads_run_off_the_event_loop():
    adapter = _adapter()
    captured = {}

    class _Sessions:
        def get(self, session_id):
            captured["thread"] = threading.current_thread()
            return {"id": session_id, "source": "api_server"}

    adapter._session_db = SimpleNamespace(sessions=_Sessions())
    session, error = await adapter._get_existing_session_or_404("session-x")

    assert error is None
    assert session["id"] == "session-x"
    assert captured["thread"] is not threading.current_thread()


@pytest.mark.asyncio
async def test_first_store_construction_runs_off_the_event_loop(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter()
    captured = {}

    class _Sessions:
        def list_rich(self, **kwargs):
            return []

    def _open(db_path):
        captured["thread"] = threading.current_thread()
        captured["path"] = db_path
        return SimpleNamespace(sessions=_Sessions(), close=lambda: None)

    monkeypatch.setattr(
        "channels.platforms.api_server_session_store.open_cli_session_store",
        _open,
    )
    token = set_hermes_home_override(tmp_path / "profile")
    try:
        async with TestClient(TestServer(_session_app(adapter))) as client:
            response = await client.get(
                "/api/sessions",
                headers={"Authorization": "Bearer sk-secret"},
            )
        assert response.status == 200
        assert captured["thread"] is not threading.current_thread()
        assert captured["path"] == tmp_path / "profile" / "state.db"
    finally:
        reset_hermes_home_override(token)
        await adapter._close_session_stores()


@pytest.mark.asyncio
async def test_store_cache_is_namespaced_by_profile_home(tmp_path, monkeypatch):
    adapter = _adapter()
    opened = []

    def _open(db_path):
        store = SimpleNamespace(db_path=db_path, close=lambda: None)
        opened.append(store)
        return store

    monkeypatch.setattr(
        "channels.platforms.api_server_session_store.open_cli_session_store",
        _open,
    )

    first_token = set_hermes_home_override(tmp_path / "profiles" / "first")
    try:
        first = await adapter._ensure_session_db_async()
        first_again = await adapter._ensure_session_db_async()
    finally:
        reset_hermes_home_override(first_token)

    second_token = set_hermes_home_override(tmp_path / "profiles" / "second")
    try:
        second = await adapter._ensure_session_db_async()
    finally:
        reset_hermes_home_override(second_token)

    assert first is first_again
    assert first is not second
    assert [store.db_path for store in opened] == [
        tmp_path / "profiles" / "first" / "state.db",
        tmp_path / "profiles" / "second" / "state.db",
    ]
    await adapter._close_session_stores()


@pytest.mark.asyncio
async def test_concurrent_same_id_create_returns_one_created_one_conflict(
    tmp_path,
):
    adapter = _adapter()
    token = set_hermes_home_override(tmp_path / "profile")
    try:
        async with TestClient(TestServer(_session_app(adapter))) as client:
            first, second = await asyncio.gather(
                client.post(
                    "/api/sessions",
                    headers={"Authorization": "Bearer sk-secret"},
                    json={"id": "same-id"},
                ),
                client.post(
                    "/api/sessions",
                    headers={"Authorization": "Bearer sk-secret"},
                    json={"id": "same-id"},
                ),
            )
        assert sorted((first.status, second.status)) == [201, 409]
    finally:
        reset_hermes_home_override(token)
        await adapter._close_session_stores()
