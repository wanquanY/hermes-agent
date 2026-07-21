"""Detection and actionable guidance for reasoning-stream idle timeouts."""

from __future__ import annotations

from typing import Optional


_TRANSPORT_KILL_PATTERNS = (
    "broken pipe",
    "errno 32",
    "remote protocol",
    "connection reset",
    "connection lost",
    "peer closed",
    "server disconnected",
)


def is_thinking_timeout(classified: object, model: str, error_msg: str) -> bool:
    """Whether a known reasoning model timed out before yielding content."""
    from agent.reasoning_timeouts import get_reasoning_stale_timeout_floor

    reason = getattr(classified, "reason", None)
    if getattr(reason, "value", None) != "timeout":
        return False
    if get_reasoning_stale_timeout_floor(model) is None:
        return False
    lowered = str(error_msg or "").lower()
    return any(pattern in lowered for pattern in _TRANSPORT_KILL_PATTERNS)


def build_thinking_timeout_guidance(
    provider: str,
    model: str,
    model_label: Optional[str] = None,
) -> str:
    """Build profile-safe guidance for extending a reasoning stream deadline."""
    try:
        from hermes_cli.config import get_config_path

        config_path = str(get_config_path())
    except Exception:
        config_path = "the active Hermes config.yaml"
    label = model_label or model
    return (
        "\n\nThe model's thinking phase exceeded the upstream proxy's idle timeout "
        "before the first content token arrived. This is a known issue with "
        f"reasoning models such as {label} behind cloud gateways (NVIDIA NIM, "
        "OpenAI, Anthropic, DeepSeek). Try, in order:\n"
        f"1. Set `providers.{provider}.models.{model}.stale_timeout_seconds: 900` "
        f"in the active config `{config_path}` (the default profile uses "
        "`~/.hermes/config.yaml`). Hermes already applies a 600s built-in floor "
        "to the deepest known reasoning families; a later disconnect can still "
        "reflect a shorter upstream cap.\n"
        "2. Lower `reasoning_budget` or use `reasoning_effort: medium`.\n"
        "3. Use a smaller reasoning model when the task does not require the full budget."
    )


__all__ = ["build_thinking_timeout_guidance", "is_thinking_timeout"]
