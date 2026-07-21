"""Chronos fire ingress contract for the hosted dashboard surface."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from hermes_agent.application.active_work_registry import (
    get_process_active_work_registry,
)
from hermes_cli import cron_fire_routes, web_server
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS


def _client() -> TestClient:
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = None
    return TestClient(web_server.app)


def _accept_work() -> None:
    registry = get_process_active_work_registry()
    if registry.snapshot():
        raise AssertionError("test requires an idle process work registry")
    registry.start_accepting()


def test_dashboard_exposes_public_fire_route():
    paths = {route.path for route in web_server.app.routes if hasattr(route, "path")}
    assert "/api/cron/fire" in paths
    assert "/api/cron/fire" in PUBLIC_API_PATHS


def test_invalid_token_is_the_public_route_security_boundary(tmp_path, monkeypatch):
    _accept_work()
    monkeypatch.setattr(cron_fire_routes, "_profile_homes", lambda: (("default", tmp_path),))
    monkeypatch.setattr(cron_fire_routes, "_find_job_profile", lambda *_args: ("default", tmp_path))
    monkeypatch.setattr(
        cron_fire_routes.cron_fire_service,
        "verify_for_profile",
        lambda *_args: None,
    )

    with _client() as client:
        response = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer forged"},
            json={"job_id": "abcdef123456"},
        )

    assert response.status_code == 401


def test_unknown_authenticated_job_returns_gone(tmp_path, monkeypatch):
    _accept_work()
    monkeypatch.setattr(cron_fire_routes, "_profile_homes", lambda: (("default", tmp_path),))
    monkeypatch.setattr(cron_fire_routes, "_find_job_profile", lambda *_args: None)
    monkeypatch.setattr(
        cron_fire_routes.cron_fire_service,
        "verify_for_profile",
        lambda *_args: {"purpose": "cron_fire"},
    )

    with _client() as client:
        response = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer valid"},
            json={"job_id": "gone-job"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "gone", "job_id": "gone-job"}


def test_valid_fire_runs_under_owning_profile(tmp_path, monkeypatch):
    _accept_work()
    fired = threading.Event()
    monkeypatch.setattr(cron_fire_routes, "_profile_homes", lambda: (("coder", tmp_path),))
    monkeypatch.setattr(cron_fire_routes, "_find_job_profile", lambda *_args: ("coder", tmp_path))
    monkeypatch.setattr(
        cron_fire_routes.cron_fire_service,
        "verify_for_profile",
        lambda *_args: {"purpose": "cron_fire"},
    )
    monkeypatch.setattr(
        cron_fire_routes.cron_fire_service,
        "fire_for_profile",
        lambda *_args: fired.set() or True,
    )

    with _client() as client:
        response = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer valid"},
            json={"job_id": "abcdef123456"},
        )
        assert response.status_code == 202
        assert fired.wait(timeout=2)

    assert get_process_active_work_registry().snapshot() == ()
