"""Application service for authenticated external cron fires."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional


class CronFireService:
    """Keep Chronos verification and execution independent of HTTP stacks."""

    @staticmethod
    def verify_token(token: str) -> Optional[Mapping[str, Any]]:
        """Verify a purpose-scoped fire token in the active profile context."""
        from hermes_cli.config import cfg_get, load_config
        from plugins.cron_providers.chronos.verify import get_fire_verifier

        config = load_config()
        return get_fire_verifier()(
            token=token,
            expected_audience=cfg_get(
                config,
                "cron",
                "chronos",
                "expected_audience",
                default="",
            ),
            jwks_or_key=cfg_get(
                config,
                "cron",
                "chronos",
                "nas_jwks_url",
                default="",
            )
            or None,
            issuer=cfg_get(
                config,
                "cron",
                "chronos",
                "portal_url",
                default="",
            )
            or None,
        )

    @staticmethod
    def fire_due(job_id: str, *, adapters: Any = None, loop: Any = None) -> bool:
        """Atomically claim and execute one job in the active cron store."""
        from cron.scheduler_provider import resolve_cron_scheduler

        provider = resolve_cron_scheduler()
        return bool(provider.fire_due(job_id, adapters=adapters, loop=loop))

    def verify_for_profile(
        self,
        profile_home: Path,
        token: str,
    ) -> Optional[Mapping[str, Any]]:
        from hermes_gateway.profile_runtime import profile_runtime_scope

        with profile_runtime_scope(profile_home):
            return self.verify_token(token)

    @staticmethod
    def job_exists_for_profile(profile_home: Path, job_id: str) -> bool:
        from cron.jobs import get_job, use_cron_store
        from hermes_gateway.profile_runtime import profile_runtime_scope

        with profile_runtime_scope(profile_home), use_cron_store(profile_home):
            job = get_job(job_id)
            return bool(job and str(job.get("id") or "") == job_id)

    def fire_for_profile(self, profile_home: Path, job_id: str) -> bool:
        from cron.jobs import use_cron_store
        from hermes_gateway.profile_runtime import profile_runtime_scope

        with profile_runtime_scope(profile_home), use_cron_store(profile_home):
            return self.fire_due(job_id)


cron_fire_service = CronFireService()
