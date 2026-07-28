"""Keep synchronous state access out of async gateway and worker handlers."""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ASYNC_OWNERS = (
    REPO_ROOT / "hermes_gateway",
    REPO_ROOT / "tui_gateway",
    REPO_ROOT / "hermes_agent" / "orchestration",
)

_PURE_MEMORY_CALLS = {
    "config.get_reset_policy",
    "_entries.get",
    "_entries.items",
    "_is_session_expired",
}

_SYNC_STORAGE_HELPERS = {
    "_db_for_stable_session",
    "_db_for_worker_rpc",
    "_get_db",
    "_load_resume_pending_candidates",
    "_schedule_resume_pending_sessions",
    "_shutdown_notification_source",
    "_suspend_stuck_loop_sessions",
    "build_process_event_source",
    "disable_telegram_topic_mode_for_chat",
    "get_goal_manager_for_event",
    "goal_still_active_for_session",
    "is_telegram_topic_lane",
    "open_cli_session_store",
    "record_telegram_topic_binding",
    "recover_telegram_topic_thread_id",
    "telegram_topic_root_status_message",
}


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _call_chain(call: ast.Call) -> str:
    try:
        return ast.unparse(call.func)
    except Exception:
        return _call_name(call)


class _AsyncBodyCallVisitor(ast.NodeVisitor):
    """Visit one async body without inspecting its off-loop sync closures."""

    def __init__(self) -> None:
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast API
        self.calls.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return


def _async_calls(function: ast.AsyncFunctionDef) -> list[ast.Call]:
    visitor = _AsyncBodyCallVisitor()
    for statement in function.body:
        visitor.visit(statement)
    return visitor.calls


def _is_direct_storage_call(call: ast.Call) -> bool:
    chain = _call_chain(call)
    if any(chain.endswith(allowed) for allowed in _PURE_MEMORY_CALLS):
        return False
    if ".session_store." in chain or "._session_db." in chain:
        return True
    name = _call_name(call)
    return name in _SYNC_STORAGE_HELPERS or name == "GoalManager"


def test_async_gateway_storage_uses_canonical_boundary() -> None:
    offenders: list[str] = []
    for root in ASYNC_OWNERS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for function in ast.walk(tree):
                if not isinstance(function, ast.AsyncFunctionDef):
                    continue
                for call in _async_calls(function):
                    if _is_direct_storage_call(call):
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{call.lineno} "
                            f"{function.name} -> {_call_chain(call)}"
                        )

    assert not offenders, (
        "Async storage access must use run_sqlite_io(); direct synchronous "
        "calls found:\n  " + "\n  ".join(sorted(offenders))
    )
