"""Typed session state for runtime anti-thrash and circuit-breaker policy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionRuntimeStability:
    session_id: str
    compression_ineffective_count: int = 0
    compression_fallback_streak: int = 0
    compression_verdict_pending: bool = False
    compression_failure_cooldown_until: float = 0.0
    compression_failure_error: str = ""
    stream_stale_failures: int = 0
    stream_stale_retry_after: float = 0.0
    stream_stale_route_hash: str = ""
    stream_stale_last_error: str = ""
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session runtime stability requires session_id")
        for field_name in (
            "compression_ineffective_count",
            "compression_fallback_streak",
            "stream_stale_failures",
        ):
            if int(getattr(self, field_name)) < 0:
                raise ValueError(f"{field_name} must be non-negative")

    @classmethod
    def empty(cls, session_id: str) -> "SessionRuntimeStability":
        return cls(session_id=str(session_id or "").strip())


__all__ = ["SessionRuntimeStability"]
