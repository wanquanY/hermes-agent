"""Main-process recovery for runs orphaned by a gateway restart."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


def recover_orphaned_active_runs(
    db: Any,
    *,
    current_gateway_instance_id: str,
    live_execution_session_ids: set[str],
    resolve_fail_orphaned: Callable[[Any], Callable[..., Any] | None],
    report_diagnostic: Callable[..., None],
    db_label: Callable[[Any], str],
    stale_after_seconds: float = 300.0,
) -> int:
    """Fail durable active runs whose owning gateway process no longer exists.

    Recovery belongs to the main gateway process. Workers can query run state,
    but must never race the gateway owner or repeatedly scan the same durable
    rows while their parent process is restarting.
    """
    from tui_gateway.process_role import is_worker_process

    if is_worker_process():
        return 0
    method = resolve_fail_orphaned(db)
    if method is None:
        return 0
    gateway_instance_id = str(current_gateway_instance_id or "").strip()
    try:
        failed = int(
            method(
                live_execution_session_ids=set(live_execution_session_ids),
                current_pid=os.getpid(),
                current_gateway_instance_id=gateway_instance_id,
                stale_after_seconds=stale_after_seconds,
                owner_dead_grace_seconds=2.0,
                reason="gateway process restarted before run reached terminal state",
            )
            or 0
        )
        if failed:
            report_diagnostic(
                "orphaned-active-runs-recovered",
                db=db_label(db),
                failed=failed,
                current_pid=os.getpid(),
                current_gateway_instance_id=gateway_instance_id,
            )
        return failed
    except Exception as exc:
        report_diagnostic(
            "orphaned-active-run-recovery-error",
            db=db_label(db),
            error=str(exc),
            current_pid=os.getpid(),
            current_gateway_instance_id=gateway_instance_id,
        )
        return 0
