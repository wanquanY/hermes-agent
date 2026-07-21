"""FastAPI adapter for the profile-aware cron dashboard service."""

from __future__ import annotations

import inspect
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from hermes_cli.cron_dashboard_service import (
    CronCreateCommand,
    CronDashboardError,
    cron_dashboard_service,
    string_list,
)

router = APIRouter()


class CronJobCreate(BaseModel):
    prompt: str = ""
    schedule: str
    name: str = ""
    deliver: str = "local"
    skills: Optional[list[str]] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    script: Optional[str] = None
    context_from: Optional[Any] = None
    enabled_toolsets: Optional[list[str]] = None
    workdir: Optional[str] = None
    no_agent: bool = False


class CronJobUpdate(BaseModel):
    updates: dict[str, Any]


def _to_http_error(exc: CronDashboardError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


async def _run_cron_dashboard_io(function, *args, **kwargs):
    if inspect.iscoroutinefunction(function):
        raise TypeError("_run_cron_dashboard_io only accepts sync callables")
    result = await run_in_threadpool(function, *args, **kwargs)
    if inspect.isawaitable(result):
        raise TypeError("_run_cron_dashboard_io sync callable returned an awaitable")
    return result


def _call_service(method: str, *args, **kwargs):
    try:
        return getattr(cron_dashboard_service, method)(*args, **kwargs)
    except CronDashboardError as exc:
        raise _to_http_error(exc) from exc


def _cron_profile_dicts() -> list[dict[str, str]]:
    return [{"name": name} for name in cron_dashboard_service.profile_names()]


def _cron_default_profile() -> str:
    return cron_dashboard_service.default_profile()


def _cron_profile_home(profile: Optional[str]):
    try:
        return cron_dashboard_service.profile_home(profile)
    except CronDashboardError as exc:
        raise _to_http_error(exc) from exc


def _call_cron_for_profile(profile, operation, *args, **kwargs):
    try:
        return cron_dashboard_service.call_for_profile(
            profile,
            operation,
            *args,
            **kwargs,
        )
    except CronDashboardError as exc:
        raise _to_http_error(exc) from exc


def _find_cron_job_profile(job_id: str) -> Optional[str]:
    return cron_dashboard_service.find_job_profile(job_id)


def _fire_cron_job_for_profile(profile: str, job_id: str) -> bool:
    try:
        return cron_dashboard_service.fire_job_for_profile(profile, job_id)
    except CronDashboardError as exc:
        raise _to_http_error(exc) from exc


@router.get("/api/cron/jobs")
async def list_cron_jobs(profile: str = "all"):
    return await _run_cron_dashboard_io(_call_service, "list_jobs", profile)


@router.get("/api/cron/jobs/{job_id}")
async def get_cron_job(job_id: str, profile: Optional[str] = None):
    return await _run_cron_dashboard_io(
        _call_service,
        "get_job",
        job_id,
        profile,
    )


@router.get("/api/cron/jobs/{job_id}/runs")
async def list_cron_job_runs(
    job_id: str,
    profile: Optional[str] = None,
    limit: int = 20,
):
    return await _run_cron_dashboard_io(
        _call_service,
        "list_job_runs",
        job_id,
        profile,
        limit,
    )


@router.post("/api/cron/jobs")
async def create_cron_job(body: CronJobCreate, profile: Optional[str] = None):
    command = CronCreateCommand(
        prompt=body.prompt,
        schedule=body.schedule,
        name=body.name,
        deliver=body.deliver,
        skills=string_list(body.skills),
        model=body.model,
        provider=body.provider,
        base_url=body.base_url,
        script=body.script,
        context_from=string_list(body.context_from),
        enabled_toolsets=string_list(body.enabled_toolsets),
        workdir=body.workdir,
        no_agent=body.no_agent,
    )
    return await _run_cron_dashboard_io(
        _call_service,
        "create_job",
        command,
        profile,
    )


@router.put("/api/cron/jobs/{job_id}")
async def update_cron_job(
    job_id: str,
    body: CronJobUpdate,
    profile: Optional[str] = None,
):
    return await _run_cron_dashboard_io(
        _call_service,
        "update_job",
        job_id,
        body.updates,
        profile,
    )


async def _mutate(operation: str, job_id: str, profile: Optional[str]):
    return await _run_cron_dashboard_io(
        _call_service,
        "mutate_job",
        operation,
        job_id,
        profile,
    )


@router.post("/api/cron/jobs/{job_id}/pause")
async def pause_cron_job(job_id: str, profile: Optional[str] = None):
    return await _mutate("pause_job", job_id, profile)


@router.post("/api/cron/jobs/{job_id}/resume")
async def resume_cron_job(job_id: str, profile: Optional[str] = None):
    return await _mutate("resume_job", job_id, profile)


@router.post("/api/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(job_id: str, profile: Optional[str] = None):
    return await _mutate("trigger_job", job_id, profile)


@router.delete("/api/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str, profile: Optional[str] = None):
    return await _run_cron_dashboard_io(
        _call_service,
        "delete_job",
        job_id,
        profile,
    )


@router.get("/api/cron/delivery-targets")
async def get_cron_delivery_targets():
    targets = [
        {
            "id": "local",
            "name": "Local (save only)",
            "home_target_set": True,
            "home_env_var": None,
        }
    ]
    try:
        from cron.scheduler import cron_delivery_targets

        targets.extend(await run_in_threadpool(cron_delivery_targets))
    except Exception:
        pass
    return {"targets": targets}


__all__ = [
    "CronJobCreate",
    "CronJobUpdate",
    "_call_cron_for_profile",
    "_cron_default_profile",
    "_cron_profile_dicts",
    "_cron_profile_home",
    "_find_cron_job_profile",
    "_fire_cron_job_for_profile",
    "_run_cron_dashboard_io",
    "create_cron_job",
    "delete_cron_job",
    "get_cron_delivery_targets",
    "get_cron_job",
    "list_cron_jobs",
    "list_cron_job_runs",
    "pause_cron_job",
    "resume_cron_job",
    "router",
    "trigger_cron_job",
    "update_cron_job",
]
