"""Inference-routing snapshots for cron drift protection."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from hermes_constants import get_hermes_home


def optional_text(value: Any, *, strip_trailing_slash: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if strip_trailing_slash:
        normalized = normalized.rstrip("/")
    return normalized or None


def resolve_default_model_snapshot() -> Optional[str]:
    """Resolve the same profile default model used by the cron executor."""
    try:
        import yaml

        from hermes_cli.config import _expand_env_vars

        config_path = get_hermes_home() / "config.yaml"
        if not config_path.exists():
            return None
        with config_path.open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream) or {}
        try:
            from hermes_cli import managed_scope

            config = managed_scope.apply_managed_overlay(config)
        except Exception:
            pass
        config = _expand_env_vars(config)
        model_config = config.get("model") or {}
        if isinstance(model_config, str):
            return model_config.strip() or None
        if isinstance(model_config, dict):
            value = model_config.get("default") or model_config.get("model")
            return optional_text(value)
    except Exception:
        return None
    return None


def compute_provider_model_snapshots(
    *,
    provider: Any,
    model: Any,
    base_url: Any,
    no_agent: Any,
) -> tuple[Optional[str], Optional[str]]:
    """Capture only inference axes that remain coupled to profile defaults."""
    normalized_provider = optional_text(provider)
    normalized_model = optional_text(model)
    normalized_base_url = optional_text(base_url, strip_trailing_slash=True)
    if bool(no_agent):
        return None, None

    provider_snapshot = None
    if normalized_provider is None:
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider

            kwargs = {"requested": None}
            if normalized_base_url:
                kwargs["explicit_base_url"] = normalized_base_url
            runtime = resolve_runtime_provider(**kwargs)
            provider_snapshot = optional_text(runtime.get("provider"))
            if provider_snapshot:
                provider_snapshot = provider_snapshot.lower()
        except Exception:
            provider_snapshot = None

    model_snapshot = (
        resolve_default_model_snapshot() if normalized_model is None else None
    )
    return provider_snapshot, model_snapshot


def normalized_inference_axes(
    job: dict[str, Any],
) -> tuple[Optional[str], Optional[str], Optional[str], bool]:
    return (
        optional_text(job.get("provider")),
        optional_text(job.get("model")),
        optional_text(job.get("base_url"), strip_trailing_slash=True),
        bool(job.get("no_agent")),
    )


def inference_drift_changes(
    job: dict[str, Any],
    *,
    current_provider: Any,
    current_model: Any,
) -> tuple[str, ...]:
    """Return changed unpinned axes; missing legacy snapshots stay compatible."""
    changes: list[str] = []
    provider_snapshot = optional_text(job.get("provider_snapshot"))
    if provider_snapshot and optional_text(job.get("provider")) is None:
        current = optional_text(current_provider)
        if current and current.lower() != provider_snapshot.lower():
            changes.append(
                f"provider '{provider_snapshot.lower()}' -> '{current.lower()}'"
            )
    model_snapshot = optional_text(job.get("model_snapshot"))
    if model_snapshot and optional_text(job.get("model")) is None:
        current = optional_text(current_model)
        if current and current.lower() != model_snapshot.lower():
            changes.append(f"model '{model_snapshot.lower()}' -> '{current.lower()}'")
    return tuple(changes)


def inference_drift_error(job_id: str, changes: Iterable[str]) -> RuntimeError:
    summary = "; ".join(changes)
    return RuntimeError(
        "Skipped to prevent unintended spend: global inference config "
        f"drifted since this job was created ({summary}), and this job is "
        "unpinned. No inference call was made. To run on the new config, pin "
        "it explicitly: `cronjob action=update "
        f"job_id={job_id} provider=<provider> model=<model>` (or pin the "
        "original values to keep them). See #44585."
    )


__all__ = [
    "compute_provider_model_snapshots",
    "inference_drift_changes",
    "inference_drift_error",
    "normalized_inference_axes",
    "optional_text",
    "resolve_default_model_snapshot",
]
