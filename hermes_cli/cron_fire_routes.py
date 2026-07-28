"""Hosted-dashboard HTTP ingress for Chronos managed fires."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from hermes_agent.application.active_work_registry import (
    WorkRejected,
    get_process_active_work_registry,
)
from hermes_agent.application.cron_fire_service import cron_fire_service

logger = logging.getLogger(__name__)
router = APIRouter()
_background_tasks: set[asyncio.Task] = set()


def _profile_homes() -> tuple[tuple[str, Path], ...]:
    from hermes_cli.profiles import profiles_to_serve

    return tuple(profiles_to_serve(multiplex=True))


def _find_job_profile(
    job_id: str,
    profiles: Iterable[tuple[str, Path]],
) -> tuple[str, Path] | None:
    for profile_name, profile_home in profiles:
        try:
            if cron_fire_service.job_exists_for_profile(profile_home, job_id):
                return profile_name, profile_home
        except Exception:
            logger.exception(
                "Chronos job lookup failed for profile %s",
                profile_name,
            )
    return None


async def _authorized_profile_home(
    *,
    token: str,
    job_profile: tuple[str, Path] | None,
    profiles: tuple[tuple[str, Path], ...],
) -> Path | None:
    """Authenticate against the owning profile, or any profile for a gone job."""
    candidates = (job_profile,) if job_profile is not None else profiles
    for candidate in candidates:
        if candidate is None:
            continue
        _profile_name, profile_home = candidate
        claims = await asyncio.to_thread(
            cron_fire_service.verify_for_profile,
            profile_home,
            token,
        )
        if claims is not None:
            return profile_home
    return None


@router.post("/api/cron/fire")
async def cron_fire_webhook(request: Request):
    """Verify, admit, and asynchronously execute one managed cron fire."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    job_id = str(body.get("job_id") or "").strip() if isinstance(body, dict) else ""

    profiles = _profile_homes()
    job_profile = _find_job_profile(job_id, profiles) if job_id else None
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    authorized_home = await _authorized_profile_home(
        token=token,
        job_profile=job_profile,
        profiles=profiles,
    )
    if authorized_home is None:
        return JSONResponse({"error": "invalid fire token"}, status_code=401)
    if not job_id:
        return JSONResponse({"error": "missing job_id"}, status_code=400)
    if job_profile is None:
        return JSONResponse(
            {"status": "gone", "job_id": job_id},
            status_code=200,
        )

    registry = get_process_active_work_registry()
    try:
        lease = registry.register(
            kind="cron_fire",
            surface="dashboard",
            metadata={"job_id": job_id, "profile": job_profile[0]},
        )
    except WorkRejected:
        return JSONResponse(
            {"error": "runtime is draining"},
            status_code=503,
            headers={"Retry-After": "1"},
        )

    async def _fire() -> None:
        try:
            await asyncio.to_thread(
                cron_fire_service.fire_for_profile,
                authorized_home,
                job_id,
            )
        except Exception:
            logger.exception("Chronos fire failed for job %s", job_id)
        finally:
            lease.release()

    try:
        task = asyncio.create_task(_fire())
    except BaseException:
        lease.release()
        raise
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return JSONResponse(
        {"status": "accepted", "job_id": job_id},
        status_code=202,
    )
