"""Profile-aware application service for dashboard cron management.

HTTP adapters translate requests and errors; this module owns profile routing,
normalization, validation, and calls into the canonical ``cron.jobs`` domain.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from hermes_gateway.profile_runtime import profile_runtime_scope

logger = logging.getLogger(__name__)


class CronDashboardError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class CronCreateCommand:
    schedule: str
    prompt: str = ""
    name: str = ""
    deliver: str = "local"
    skills: tuple[str, ...] = ()
    model: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    script: Optional[str] = None
    context_from: tuple[str, ...] = ()
    enabled_toolsets: tuple[str, ...] = ()
    workdir: Optional[str] = None
    no_agent: bool = False


def optional_text(value: Any, *, strip_trailing_slash: bool = False) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    if strip_trailing_slash:
        normalized = normalized.rstrip("/")
    return normalized or None


def string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_items: Iterable[Any] = re.split(r"[\n,]", value)
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return ()
    return tuple(str(item).strip() for item in raw_items if str(item).strip())


class CronDashboardService:
    """One coherent cron-management boundary shared by dashboard transports."""

    @staticmethod
    def default_profile() -> str:
        try:
            from hermes_cli.profiles import get_active_profile_name

            active = get_active_profile_name()
        except Exception:
            return "default"
        return "default" if active in {"default", "custom"} else active

    def profile_home(self, profile: Optional[str]) -> tuple[str, Path]:
        from hermes_cli import profiles

        raw = optional_text(profile) or self.default_profile()
        try:
            canonical = profiles.normalize_profile_name(raw)
            profiles.validate_profile_name(canonical)
        except ValueError as exc:
            raise CronDashboardError(400, str(exc)) from exc
        if not profiles.profile_exists(canonical):
            raise CronDashboardError(404, f"Profile '{canonical}' does not exist.")
        return canonical, profiles.get_profile_dir(canonical)

    @staticmethod
    def _annotate(job: Mapping[str, Any], profile: str, home: Path) -> dict[str, Any]:
        result = dict(job)
        result.update(
            {
                "profile": profile,
                "profile_name": profile,
                "hermes_home": str(home),
                "is_default_profile": profile == "default",
            }
        )
        return result

    def call_for_profile(
        self,
        profile: Optional[str],
        operation: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        profile_name, home = self.profile_home(profile)
        from cron import jobs

        with jobs.use_cron_store(home), profile_runtime_scope(home):
            result = getattr(jobs, operation)(*args, **kwargs)
        if isinstance(result, list):
            return [self._annotate(job, profile_name, home) for job in result]
        if isinstance(result, dict):
            return self._annotate(result, profile_name, home)
        return result

    def profile_names(self) -> tuple[str, ...]:
        from hermes_cli import profiles

        try:
            names = tuple(str(item.name) for item in profiles.list_profiles())
        except Exception:
            logger.exception("Profile discovery failed for cron dashboard")
            names = tuple(name for name, _home in profiles.profiles_to_serve(True))
        return tuple(dict.fromkeys(name for name in names if name))

    def find_job_profile(self, job_id: str) -> Optional[str]:
        for profile in self.profile_names():
            jobs = self.call_for_profile(profile, "list_jobs", True)
            if any(
                job.get("id") == job_id or job.get("name") == job_id
                for job in jobs
            ):
                return profile
        return None

    def fire_job_for_profile(self, profile: str, job_id: str) -> bool:
        """Execute one provider-claimed fire under one coherent profile scope."""
        _profile_name, home = self.profile_home(profile)
        from cron import jobs
        from cron.scheduler_provider import resolve_cron_scheduler

        with jobs.use_cron_store(home), profile_runtime_scope(home):
            provider = resolve_cron_scheduler()
            return bool(provider.fire_due(job_id, adapters=None, loop=None))

    def list_jobs(self, profile: str = "all") -> list[dict[str, Any]]:
        requested = optional_text(profile) or "all"
        if requested.lower() != "all":
            return self.call_for_profile(requested, "list_jobs", True)
        result: list[dict[str, Any]] = []
        for name in self.profile_names():
            try:
                result.extend(self.call_for_profile(name, "list_jobs", True))
            except Exception:
                logger.exception("Failed to list cron jobs for profile %s", name)
        return result

    def get_job(self, job_id: str, profile: Optional[str] = None) -> dict[str, Any]:
        selected = profile or self.find_job_profile(job_id)
        if not selected:
            raise CronDashboardError(404, "Job not found")
        job = self.call_for_profile(selected, "resolve_job_ref", job_id)
        if not job:
            raise CronDashboardError(404, "Job not found")
        return job

    def list_job_runs(
        self,
        job_id: str,
        profile: Optional[str] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return profile-scoped run sessions for a job or retained job id."""
        selected = profile or self.find_job_profile(job_id) or self.default_profile()
        profile_name, home = self.profile_home(selected)
        job = self.call_for_profile(profile_name, "resolve_job_ref", job_id)
        canonical = str(job.get("id") if job else job_id).strip()
        if not canonical:
            raise CronDashboardError(400, "job_id is required")
        try:
            bounded_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            bounded_limit = 20

        from hermes_agent.composition.cli_session_store import open_cli_session_store

        with profile_runtime_scope(home):
            store = open_cli_session_store(home / "state.db")
            try:
                runs = store.cron_run_history.list(
                    canonical,
                    limit=bounded_limit,
                )
            finally:
                store.close()

        now = time.time()
        for run in runs:
            last_active = _to_float(
                run.get("last_active"),
                _to_float(run.get("started_at"), 0.0),
            )
            run["is_active"] = (
                run.get("ended_at") is None and now - last_active < 300
            )
            run["archived"] = bool(run.get("archived"))
            run["profile"] = profile_name
        return {"runs": runs, "limit": bounded_limit}

    def _normalize_script(self, value: Any, home: Path) -> Optional[str]:
        text = optional_text(value)
        if not text:
            return None
        scripts_root = (home / "scripts").resolve()
        raw = Path(text).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (scripts_root / raw).resolve()
        try:
            relative = candidate.relative_to(scripts_root)
        except ValueError as exc:
            raise CronDashboardError(
                400,
                f"script must be inside {scripts_root}",
            ) from exc
        if not candidate.is_file():
            raise CronDashboardError(400, f"script does not exist: {candidate}")
        return str(relative)

    @staticmethod
    def _validate_effective_job(job: Mapping[str, Any]) -> None:
        prompt = optional_text(job.get("prompt"))
        script = optional_text(job.get("script"))
        skills = string_list(job.get("skills")) or string_list(job.get("skill"))
        if bool(job.get("no_agent")):
            if not script:
                raise CronDashboardError(400, "no_agent=True requires a script")
            return
        if not (prompt or skills or script):
            raise CronDashboardError(
                400,
                "agent cron jobs require a prompt, skill, or script",
            )

    def _validate_context(self, refs: Iterable[str], profile: str) -> None:
        for reference in refs:
            if not self.call_for_profile(profile, "get_job", reference):
                raise CronDashboardError(
                    400,
                    f"context_from job '{reference}' not found in profile '{profile}'",
                )

    def create_job(
        self,
        command: CronCreateCommand,
        profile: Optional[str] = None,
    ) -> dict[str, Any]:
        profile_name, home = self.profile_home(profile)
        script = self._normalize_script(command.script, home)
        self._validate_context(command.context_from, profile_name)
        effective = {
            "prompt": command.prompt,
            "skills": command.skills,
            "script": script,
            "no_agent": command.no_agent,
        }
        self._validate_effective_job(effective)
        try:
            return self.call_for_profile(
                profile_name,
                "create_job",
                prompt=command.prompt,
                schedule=command.schedule,
                name=command.name,
                deliver=optional_text(command.deliver) or "local",
                skills=list(command.skills) or None,
                model=optional_text(command.model),
                provider=optional_text(command.provider),
                base_url=optional_text(command.base_url, strip_trailing_slash=True),
                script=script,
                context_from=list(command.context_from) or None,
                enabled_toolsets=list(command.enabled_toolsets) or None,
                workdir=optional_text(command.workdir),
                no_agent=command.no_agent,
            )
        except CronDashboardError:
            raise
        except Exception as exc:
            raise CronDashboardError(400, str(exc)) from exc

    def normalize_updates(self, updates: Mapping[str, Any], home: Path) -> dict[str, Any]:
        normalized = dict(updates)
        for key in ("model", "provider", "workdir"):
            if key in normalized:
                normalized[key] = optional_text(normalized[key])
        if "script" in normalized:
            normalized["script"] = self._normalize_script(normalized["script"], home)
        if "base_url" in normalized:
            normalized["base_url"] = optional_text(
                normalized["base_url"],
                strip_trailing_slash=True,
            )
        if "deliver" in normalized:
            normalized["deliver"] = optional_text(normalized["deliver"]) or "local"
        for key in ("context_from", "enabled_toolsets", "skills"):
            if key in normalized:
                values = string_list(normalized[key])
                normalized[key] = list(values) or None
        return normalized

    def update_job(
        self,
        job_id: str,
        updates: Mapping[str, Any],
        profile: Optional[str] = None,
    ) -> dict[str, Any]:
        selected = profile or self.find_job_profile(job_id)
        if not selected:
            raise CronDashboardError(404, "Job not found")
        profile_name, home = self.profile_home(selected)
        existing = self.call_for_profile(profile_name, "resolve_job_ref", job_id)
        if not existing:
            raise CronDashboardError(404, "Job not found")
        normalized = self.normalize_updates(updates, home)
        if "context_from" in normalized:
            self._validate_context(normalized.get("context_from") or (), profile_name)
        if {"prompt", "skill", "skills", "script", "no_agent"}.intersection(normalized):
            effective = {**existing, **normalized}
            if "skills" in normalized and "skill" not in normalized:
                effective["skill"] = None
            self._validate_effective_job(effective)
        try:
            job = self.call_for_profile(
                profile_name,
                "update_job",
                existing["id"],
                normalized,
            )
        except ValueError as exc:
            raise CronDashboardError(400, str(exc)) from exc
        if not job:
            raise CronDashboardError(404, "Job not found")
        return job

    def mutate_job(
        self,
        operation: str,
        job_id: str,
        profile: Optional[str] = None,
    ) -> dict[str, Any]:
        selected = profile or self.find_job_profile(job_id)
        if not selected:
            raise CronDashboardError(404, "Job not found")
        job = self.call_for_profile(selected, operation, job_id)
        if not job:
            raise CronDashboardError(404, "Job not found")
        return job

    def delete_job(self, job_id: str, profile: Optional[str] = None) -> dict[str, bool]:
        selected = profile or self.find_job_profile(job_id)
        if not selected:
            raise CronDashboardError(404, "Job not found")
        try:
            removed = self.call_for_profile(selected, "remove_job", job_id)
        except ValueError as exc:
            raise CronDashboardError(400, str(exc)) from exc
        if not removed:
            raise CronDashboardError(404, "Job not found")
        return {"ok": True}


cron_dashboard_service = CronDashboardService()


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "CronCreateCommand",
    "CronDashboardError",
    "CronDashboardService",
    "cron_dashboard_service",
    "optional_text",
    "string_list",
]
