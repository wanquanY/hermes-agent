"""Canonical identity and conflict policy for a durable Hermes run."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable


@dataclass(frozen=True)
class RunIdentity:
    """Stable ownership dimensions that may never be rewired for a run id."""

    run_id: str
    session_id: str
    worker_id: str = ""
    runtime_scope_key: str = ""
    agent_profile_id: str = ""

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        session_id: str,
        worker_id: str = "",
        runtime_scope_key: str = "",
        agent_profile_id: str = "",
        require_worker: bool = False,
    ) -> "RunIdentity":
        stable_run = str(run_id or "").strip()
        stable_session = str(session_id or "").strip()
        stable_worker = str(worker_id or "").strip()
        if not stable_run or not stable_session:
            raise ValueError("run_id and session_id are required")
        if require_worker and not stable_worker:
            raise ValueError("worker_id is required")
        return cls(
            run_id=stable_run,
            session_id=stable_session,
            worker_id=stable_worker,
            runtime_scope_key=(
                str(runtime_scope_key or "").strip() or stable_session
            ),
            agent_profile_id=str(agent_profile_id or "").strip(),
        )

    def claimed_with(self, incoming: "RunIdentity") -> "RunIdentity":
        """Fill legacy unclaimed worker/profile fields after compatibility."""
        ensure_run_identity_compatible(self, incoming)
        return RunIdentity(
            run_id=self.run_id,
            session_id=self.session_id,
            worker_id=self.worker_id or incoming.worker_id,
            runtime_scope_key=self.runtime_scope_key,
            agent_profile_id=self.agent_profile_id or incoming.agent_profile_id,
        )

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class CrossWiredRunError(RuntimeError):
    """A run id was presented with ownership different from durable truth."""

    def __init__(
        self,
        existing: RunIdentity,
        incoming: RunIdentity,
        mismatch_fields: Iterable[str],
    ) -> None:
        mismatches = tuple(dict.fromkeys(str(field) for field in mismatch_fields))
        self.existing = existing
        self.incoming = incoming
        self.mismatch_fields = mismatches
        super().__init__(
            "cross-wired run identity for "
            f"{incoming.run_id!r}: {', '.join(mismatches)}"
        )

    @property
    def details(self) -> dict[str, object]:
        return {
            "run_id": self.incoming.run_id,
            "mismatch_fields": list(self.mismatch_fields),
            "existing": self.existing.as_dict(),
            "incoming": self.incoming.as_dict(),
        }


def run_identity_mismatches(
    existing: RunIdentity,
    incoming: RunIdentity,
) -> tuple[str, ...]:
    """Return conflicting fields; empty worker/profile are first-claim slots."""
    mismatches: list[str] = []
    if existing.run_id != incoming.run_id:
        mismatches.append("run_id")
    if existing.session_id != incoming.session_id:
        mismatches.append("session_id")
    if existing.runtime_scope_key != incoming.runtime_scope_key:
        mismatches.append("runtime_scope_key")
    if (
        existing.worker_id
        and incoming.worker_id
        and existing.worker_id != incoming.worker_id
    ):
        mismatches.append("worker_id")
    if (
        existing.agent_profile_id
        and incoming.agent_profile_id
        and existing.agent_profile_id != incoming.agent_profile_id
    ):
        mismatches.append("agent_profile_id")
    return tuple(mismatches)


def ensure_run_identity_compatible(
    existing: RunIdentity,
    incoming: RunIdentity,
) -> None:
    mismatches = run_identity_mismatches(existing, incoming)
    if mismatches:
        raise CrossWiredRunError(existing, incoming, mismatches)


__all__ = [
    "CrossWiredRunError",
    "RunIdentity",
    "ensure_run_identity_compatible",
    "run_identity_mismatches",
]
