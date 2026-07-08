from __future__ import annotations

from typing import Any, Dict


def ensure_team_leader_message_run_state(
    db: Any,
    *,
    run_id: str,
    session_id: str,
    runtime_scope_key: str,
    result: Dict[str, Any] | None = None,
) -> None:
    run_id = str(run_id or "").strip()
    session_id = str(session_id or "").strip()
    if not run_id or not session_id or db.get_run(run_id):
        return
    result = result if isinstance(result, dict) else {}
    db.upsert_run(
        run_id=run_id,
        session_id=session_id,
        runtime_scope_key=str(runtime_scope_key or "").strip() or session_id,
        execution_session_id=str(result.get("execution_session_id") or result.get("session_id") or session_id),
        status=str(result.get("status") or "running"),
    )
