"""Unified lifecycle for synchronous and asynchronous subagent execution.

Activity/Run rows are the business source of truth.  The process-local runtime
registry below contains only interruptible execution handles needed while a
runner is alive; it never owns completion history or user-message injection.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
import contextvars
import logging
import os
import threading
import time
from typing import Any, Callable, Iterable
import uuid

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)


class ExecutionMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"


class ExecutionModeError(ValueError):
    pass


class PersistenceRequiredError(RuntimeError):
    pass


def resolve_execution_mode(
    *,
    execution_mode: object = None,
    background: object = None,
) -> ExecutionMode:
    """Resolve the explicit mode and the legacy ``background`` adapter."""
    explicit: ExecutionMode | None = None
    if execution_mode is not None and str(execution_mode).strip():
        normalized = str(execution_mode).strip().lower()
        try:
            explicit = ExecutionMode(normalized)
        except ValueError as exc:
            raise ExecutionModeError(
                "execution_mode must be 'sync' or 'async'"
            ) from exc
    legacy: ExecutionMode | None = None
    if background is not None:
        if isinstance(background, bool):
            enabled = background
        elif isinstance(background, (int, float)) and background in {0, 1}:
            enabled = bool(background)
        elif isinstance(background, str):
            normalized = background.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                enabled = True
            elif normalized in {"false", "0", "no", "off", ""}:
                enabled = False
            else:
                raise ExecutionModeError("background must be a boolean")
        else:
            raise ExecutionModeError("background must be a boolean")
        legacy = ExecutionMode.ASYNC if enabled else ExecutionMode.SYNC
    if explicit is not None and legacy is not None and explicit is not legacy:
        raise ExecutionModeError(
            "execution_mode conflicts with legacy background value"
        )
    return explicit or legacy or ExecutionMode.SYNC


@dataclass(frozen=True)
class SubagentTaskSpec:
    task_index: int
    goal: str
    child_session_id: str
    subagent_id: str = ""
    role: str = "leaf"
    model: str = ""
    toolsets: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChildExecution:
    task_index: int
    activity_id: str
    run_id: str
    turn_id: str
    child_session_id: str
    goal: str


@dataclass(frozen=True)
class ExecutionPlan:
    activity_id: str
    conversation_session_id: str
    parent_activity_id: str
    execution_scope_key: str
    mode: ExecutionMode
    children: tuple[ChildExecution, ...]
    persistent: bool
    owner_run_id: str = ""
    owner_turn_id: str = ""

    def handle(self, *, status: str = "running") -> dict[str, Any]:
        return {
            "activity_id": self.activity_id,
            "conversation_session_id": self.conversation_session_id,
            "parent_activity_id": self.parent_activity_id or None,
            "execution_mode": self.mode.value,
            "status": status,
            "child_activity_ids": [child.activity_id for child in self.children],
            "child_run_ids": [child.run_id for child in self.children],
            "persistent": self.persistent,
            **({"owner_run_id": self.owner_run_id} if self.owner_run_id else {}),
            **({"owner_turn_id": self.owner_turn_id} if self.owner_turn_id else {}),
        }


def _text(value: object) -> str:
    return str(value or "").strip()


def _new_id(prefix: str, factory: Callable[[], str]) -> str:
    return f"{prefix}{_text(factory()) or uuid.uuid4().hex}"


class SubagentExecutionService:
    """Persist and project one delegation through a single lifecycle."""

    def __init__(
        self,
        *,
        state_store: Any,
        conversation_session_id: str,
        parent_activity_id: str = "",
        execution_scope_key: str = "",
        participant_id: str = "",
        profile_id: str = "",
        owner_pid: int | None = None,
        owner_instance_id: str = "",
        owner_run_id: str = "",
        owner_turn_id: str = "",
        uuid_factory: Callable[[], str] | None = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._db = state_store
        self.conversation_session_id = _text(conversation_session_id)
        self.parent_activity_id = _text(parent_activity_id)
        self.execution_scope_key = _text(execution_scope_key) or self.conversation_session_id
        self.participant_id = _text(participant_id)
        self.profile_id = _text(profile_id)
        self.owner_pid = int(owner_pid if owner_pid is not None else os.getpid())
        self.owner_instance_id = _text(owner_instance_id)
        self.owner_run_id = _text(owner_run_id)
        self.owner_turn_id = _text(owner_turn_id)
        self._uuid_factory = uuid_factory or (lambda: uuid.uuid4().hex)
        self._time = time_fn

    @property
    def persistent(self) -> bool:
        return bool(
            self._db is not None
            and getattr(self._db, "activities", None) is not None
            and getattr(self._db, "runs", None) is not None
        )

    def create_plan(
        self,
        tasks: Iterable[SubagentTaskSpec],
        *,
        mode: ExecutionMode,
    ) -> ExecutionPlan:
        task_list = tuple(tasks)
        if not task_list:
            raise ValueError("at least one subagent task is required")
        if not self.conversation_session_id:
            raise ValueError("conversation_session_id is required")
        if mode is ExecutionMode.ASYNC and not self.persistent:
            raise PersistenceRequiredError(
                "async subagent execution requires an Activity/Run state store"
            )

        root_id = _new_id("act-agent_dispatch:", self._uuid_factory)
        children: list[ChildExecution] = []
        for task in task_list:
            activity_id = (
                root_id
                if len(task_list) == 1
                else _new_id("act-agent_dispatch:", self._uuid_factory)
            )
            children.append(
                ChildExecution(
                    task_index=task.task_index,
                    activity_id=activity_id,
                    run_id=_new_id("run-subagent-", self._uuid_factory),
                    turn_id=_new_id("turn-subagent-", self._uuid_factory),
                    child_session_id=(
                        _text(task.child_session_id)
                        or _new_id("subagent-session-", self._uuid_factory)
                    ),
                    goal=task.goal,
                )
            )
        plan = ExecutionPlan(
            activity_id=root_id,
            conversation_session_id=self.conversation_session_id,
            parent_activity_id=self.parent_activity_id,
            execution_scope_key=self.execution_scope_key,
            mode=mode,
            children=tuple(children),
            persistent=self.persistent,
            owner_run_id=self.owner_run_id,
            owner_turn_id=self.owner_turn_id,
        )
        if self.persistent:
            self._create_activity_rows(plan, task_list)
        return plan

    def _create_activity_rows(
        self,
        plan: ExecutionPlan,
        tasks: tuple[SubagentTaskSpec, ...],
    ) -> None:
        if len(tasks) > 1:
            self._db.activities.create(
                activity_id=plan.activity_id,
                conversation_id=plan.conversation_session_id,
                kind="agent_dispatch",
                parent_activity_id=plan.parent_activity_id or None,
                prompt_summary=f"{len(tasks)} subagents",
            )
        for task, child in zip(tasks, plan.children):
            self._db.activities.create(
                activity_id=child.activity_id,
                conversation_id=plan.conversation_session_id,
                kind="agent_dispatch",
                parent_activity_id=(
                    plan.activity_id if len(tasks) > 1 else plan.parent_activity_id or None
                ),
                prompt_summary=_text(task.goal)[:200],
            )

    def start(self, plan: ExecutionPlan) -> None:
        if not plan.persistent:
            return
        now = self._time()
        if len(plan.children) > 1:
            root_status = self._activity_status(plan.activity_id)
            if root_status in _TERMINAL_STATUSES:
                if root_status == "cancelled":
                    self.cancel(plan, reason="activity cancelled before start")
                return
            self._db.activities.update_status(plan.activity_id, "running", started_at=now)
        for child in plan.children:
            if self._activity_status(child.activity_id) in _TERMINAL_STATUSES:
                continue
            self._db.activities.update_status(child.activity_id, "running", started_at=now)
            self._db.runs.upsert(
                run_id=child.run_id,
                session_id=child.child_session_id,
                runtime_scope_key=plan.execution_scope_key,
                turn_id=child.turn_id,
                execution_session_id=child.child_session_id,
                status="running",
                started_at=now,
                updated_at=now,
                metadata=self._run_metadata(plan, child),
            )
            self._append_state_event(plan, child, status="running")

    def complete(self, plan: ExecutionPlan, result: dict[str, Any]) -> str:
        entries = result.get("results") if isinstance(result, dict) else None
        if not isinstance(entries, list):
            entries = [result] if isinstance(result, dict) else []
        by_index = {
            int(entry.get("task_index", index)): entry
            for index, entry in enumerate(entries)
            if isinstance(entry, dict)
        }
        terminal_statuses: list[str] = []
        for child in plan.children:
            entry = by_index.get(child.task_index) or {
                "task_index": child.task_index,
                "status": "error",
                "error": "subagent result missing",
                "summary": None,
            }
            proposed = self.complete_child(
                plan,
                task_index=child.task_index,
                result=entry,
            )
            terminal_statuses.append(
                self._activity_status(child.activity_id) or proposed
            )
        root_status = self._aggregate_status(terminal_statuses)
        self.complete_root(plan, status=root_status, result={"results": entries})
        return root_status

    def complete_child(
        self,
        plan: ExecutionPlan,
        *,
        task_index: int,
        result: dict[str, Any],
    ) -> str:
        child = next(
            (item for item in plan.children if item.task_index == int(task_index)),
            None,
        )
        if child is None:
            raise KeyError(f"unknown subagent task_index: {task_index}")
        status = self._terminal_status(result.get("status"))
        return self._finish_child(plan, child, status=status, result=result)

    def complete_root(
        self,
        plan: ExecutionPlan,
        *,
        status: str,
        result: dict[str, Any],
    ) -> None:
        if plan.persistent and len(plan.children) > 1:
            self._finish_root(plan, status=status, result=result)

    def cancel(self, plan: ExecutionPlan, *, reason: str) -> dict[int, str]:
        if not plan.persistent:
            return {}
        resolved_statuses: dict[int, str] = {}
        for child in plan.children:
            resolved_statuses[child.task_index] = self._finish_child(
                plan,
                child,
                status="cancelled",
                result={
                    "task_index": child.task_index,
                    "status": "cancelled",
                    "error": reason,
                    "summary": None,
                },
            )
        if len(plan.children) > 1:
            self._finish_root(
                plan,
                status="cancelled",
                result={"status": "cancelled", "reason": reason},
            )
        return resolved_statuses

    def cancel_child(
        self,
        plan: ExecutionPlan,
        *,
        activity_id: str,
        reason: str,
    ) -> str | None:
        """Cancel one fan-out child without interrupting its siblings."""
        target = next(
            (
                child
                for child in plan.children
                if child.activity_id == _text(activity_id)
            ),
            None,
        )
        if target is None:
            return None
        return self._finish_child(
            plan,
            target,
            status="cancelled",
            result={
                "task_index": target.task_index,
                "status": "cancelled",
                "error": reason,
                "summary": None,
            },
        )

    def is_cancelled(self, plan: ExecutionPlan) -> bool:
        if not plan.persistent:
            return False
        try:
            row = self._db.activities.get(plan.activity_id) or {}
            return _text(row.get("status")) == "cancelled"
        except Exception:
            logger.debug("activity cancel poll failed", exc_info=True)
            return False

    def cancelled_activity_ids(self, plan: ExecutionPlan) -> set[str]:
        """Return persisted root/child cancellations for runtime polling."""
        if not plan.persistent:
            return set()
        activity_ids = {plan.activity_id, *(child.activity_id for child in plan.children)}
        cancelled: set[str] = set()
        for activity_id in activity_ids:
            try:
                if self._activity_status(activity_id) == "cancelled":
                    cancelled.add(activity_id)
            except Exception:
                logger.debug("activity cancel poll failed", exc_info=True)
        return cancelled

    def _activity_status(self, activity_id: str) -> str:
        try:
            row = self._db.activities.get(activity_id) or {}
            return _text(row.get("status")).lower()
        except Exception:
            return ""

    def _finish_child(
        self,
        plan: ExecutionPlan,
        child: ChildExecution,
        *,
        status: str,
        result: dict[str, Any],
    ) -> str:
        if not plan.persistent:
            return status
        current = self._db.activities.get(child.activity_id) or {}
        existing = _text(current.get("status"))
        if existing in _TERMINAL_STATUSES:
            # The Activity transition may have been won by an external cancel
            # request.  Preserve that winner, but still converge the related
            # Run and typed event instead of leaving a durable running orphan.
            terminal_result = dict(result)
            terminal_result["status"] = existing
            if existing == "cancelled" and not terminal_result.get("error"):
                terminal_result["error"] = "activity cancelled"
            self._finish_child_run(
                plan,
                child,
                status=existing,
                result=terminal_result,
            )
            return existing
        now = self._time()
        summary = _text(result.get("summary") or result.get("error"))[:4000]
        self._db.activities.update_status(
            child.activity_id,
            status,
            result_summary=summary or None,
            result_json=result,
            completed_at=now,
        )
        self._finish_child_run(
            plan,
            child,
            status=status,
            result=result,
            now=now,
        )
        return status

    def _finish_child_run(
        self,
        plan: ExecutionPlan,
        child: ChildExecution,
        *,
        status: str,
        result: dict[str, Any],
        now: float | None = None,
    ) -> None:
        existing_run = self._db.runs.get(child.run_id) or {}
        if _text(existing_run.get("status")) in _TERMINAL_STATUSES:
            return
        completed_at = self._time() if now is None else now
        run_status = "failed" if status == "failed" else status
        self._db.runs.upsert(
            run_id=child.run_id,
            session_id=child.child_session_id,
            runtime_scope_key=plan.execution_scope_key,
            turn_id=child.turn_id,
            execution_session_id=child.child_session_id,
            status=run_status,
            updated_at=completed_at,
            completed_at=completed_at,
            error=_text(result.get("error")) if run_status != "completed" else "",
            metadata=self._run_metadata(plan, child),
        )
        self._append_state_event(plan, child, status=status, result=result)

    def _finish_root(
        self,
        plan: ExecutionPlan,
        *,
        status: str,
        result: dict[str, Any],
    ) -> None:
        current = self._db.activities.get(plan.activity_id) or {}
        existing = _text(current.get("status"))
        if existing in _TERMINAL_STATUSES:
            terminal_result = dict(result)
            terminal_result["status"] = existing
            self._append_root_event(
                plan,
                status=existing,
                result=terminal_result,
            )
            return
        self._db.activities.update_status(
            plan.activity_id,
            status,
            result_summary=f"{len(plan.children)} subagents: {status}",
            result_json=result,
            completed_at=self._time(),
        )
        self._append_root_event(plan, status=status, result=result)

    def _append_state_event(
        self,
        plan: ExecutionPlan,
        child: ChildExecution,
        *,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "activity_id": child.activity_id,
            "parent_activity_id": plan.activity_id if len(plan.children) > 1 else plan.parent_activity_id,
            "status": status,
            "execution_mode": plan.mode.value,
            "task_index": child.task_index,
        }
        if result is not None:
            payload["result"] = result
        try:
            self._db.runs.append_event(
                child.child_session_id,
                {
                    "type": "activity.state",
                    "session_id": child.child_session_id,
                    "conversation_session_id": plan.conversation_session_id,
                    "execution_session_id": child.child_session_id,
                    "runtime_scope_key": plan.execution_scope_key,
                    "run_id": child.run_id,
                    "turn_id": child.turn_id,
                    "activity_id": child.activity_id,
                    "internal": True,
                    "timestamp": self._time(),
                    "payload": payload,
                },
                activity_id=child.activity_id,
            )
        except Exception:
            logger.exception("subagent activity event persistence failed")

    def _append_root_event(
        self,
        plan: ExecutionPlan,
        *,
        status: str,
        result: dict[str, Any],
    ) -> None:
        try:
            existing_events = self._db.runs.list_events_by_activity(
                plan.activity_id,
                include_internal=True,
            )
            if any(
                event.get("type") == "activity.aggregate.state"
                and _text((event.get("payload") or {}).get("status")) == status
                for event in existing_events
            ):
                return
            self._db.runs.append_event(
                plan.conversation_session_id,
                {
                    "type": "activity.aggregate.state",
                    "session_id": plan.conversation_session_id,
                    "conversation_session_id": plan.conversation_session_id,
                    "runtime_scope_key": plan.execution_scope_key,
                    "activity_id": plan.activity_id,
                    "internal": True,
                    "timestamp": self._time(),
                    "payload": {
                        "activity_id": plan.activity_id,
                        "status": status,
                        "execution_mode": plan.mode.value,
                        "result": result,
                    },
                },
                activity_id=plan.activity_id,
            )
        except Exception:
            logger.exception("subagent aggregate event persistence failed")

    def _run_metadata(
        self,
        plan: ExecutionPlan,
        child: ChildExecution,
    ) -> dict[str, Any]:
        return {
            "activity_id": child.activity_id,
            "delegation_activity_id": plan.activity_id,
            "parent_activity_id": plan.activity_id if len(plan.children) > 1 else plan.parent_activity_id,
            "parent_conversation_session_id": plan.conversation_session_id,
            "execution_mode": plan.mode.value,
            "execution_owner": "subagent_execution_runtime",
            "restart_policy": "mark_failed",
            "gateway_pid": self.owner_pid,
            "gateway_instance_id": self.owner_instance_id,
            "is_fanout": len(plan.children) > 1,
            "task_index": child.task_index,
            "participant_id": self.participant_id,
            "profile_id": self.profile_id,
            "owner_run_id": plan.owner_run_id,
            "owner_turn_id": plan.owner_turn_id,
        }

    @staticmethod
    def _terminal_status(value: object) -> str:
        status = _text(value).lower()
        if status in {"completed", "success"}:
            return "completed"
        if status in {"cancelled", "canceled"}:
            return "cancelled"
        if status == "interrupted":
            return "interrupted"
        return "failed"

    @staticmethod
    def _aggregate_status(statuses: list[str]) -> str:
        if statuses and all(status == "completed" for status in statuses):
            return "completed"
        if statuses and all(
            status in {"completed", "cancelled"} for status in statuses
        ):
            return "cancelled"
        if statuses and all(
            status in {"completed", "interrupted"} for status in statuses
        ):
            return "interrupted"
        return "failed"


class DaemonThreadPoolExecutor:
    """Small bounded executor whose threads never own process shutdown.

    ``ThreadPoolExecutor`` intentionally installs non-daemon workers and its
    interpreter-exit hook joins them.  Detached subagents need the opposite
    shutdown contract.  Implement the narrow ``submit``/``shutdown`` surface
    locally instead of depending on CPython's private ``_worker`` internals.
    There is no hidden queue: submissions beyond ``max_workers`` fail.
    """

    def __init__(self, *, max_workers: int, thread_name_prefix: str = "daemon") -> None:
        self._max_workers = max(1, int(max_workers))
        self._thread_name_prefix = thread_name_prefix
        self._lock = threading.RLock()
        self._shutdown = False
        self._sequence = 0
        self._threads: set[threading.Thread] = set()
        self._futures: set[Future] = set()

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        future: Future = Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if len(self._futures) >= self._max_workers:
                raise RuntimeError("daemon executor capacity exceeded")
            sequence = self._sequence
            self._sequence += 1
            self._futures.add(future)

        def invoke() -> None:
            try:
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    future.set_result(fn(*args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)
            finally:
                with self._lock:
                    self._futures.discard(future)
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(
            name=f"{self._thread_name_prefix}_{sequence}",
            target=invoke,
            daemon=True,
        )
        with self._lock:
            self._threads.add(thread)
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._threads.discard(thread)
                self._futures.discard(future)
            raise
        return future

    def shutdown(
        self,
        wait: bool = True,
        *,
        cancel_futures: bool = False,
    ) -> None:
        with self._lock:
            self._shutdown = True
            futures = tuple(self._futures)
            threads = tuple(self._threads)
        if cancel_futures:
            for future in futures:
                future.cancel()
        if wait:
            for thread in threads:
                if thread is not threading.current_thread():
                    thread.join()


@dataclass
class _RuntimeEntry:
    plan: ExecutionPlan
    service: SubagentExecutionService
    interrupt_fn: Callable[[], None] | None
    interrupt_child_fn: Callable[[int], None] | None
    terminal_fn: Callable[[int, str, str], None] | None
    future: Future | None = None
    cancel_watcher: threading.Thread | None = None
    observed_cancellations: set[str] = field(default_factory=set)
    published_terminal_tasks: set[int] = field(default_factory=set)


class SubagentExecutionRuntime:
    """Own only active runner handles; durable state stays in Activity/Run."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._executor: DaemonThreadPoolExecutor | None = None
        self._max_workers = 0
        self._active: dict[str, _RuntimeEntry] = {}
        self._activity_index: dict[str, tuple[_RuntimeEntry, int | None]] = {}

    def submit(
        self,
        *,
        plan: ExecutionPlan,
        service: SubagentExecutionService,
        runner: Callable[[], dict[str, Any]],
        interrupt_fn: Callable[[], None] | None,
        max_workers: int,
        interrupt_child_fn: Callable[[int], None] | None = None,
        terminal_fn: Callable[[int, str, str], None] | None = None,
    ) -> dict[str, Any]:
        cap = max(1, int(max_workers))
        with self._lock:
            if len(self._active) >= cap:
                return {
                    "status": "rejected",
                    "error_code": "capacity_exceeded",
                    "error": f"async subagent capacity reached ({cap} active)",
                }
            if self._executor is None or cap > self._max_workers:
                self._executor = DaemonThreadPoolExecutor(
                    max_workers=cap,
                    thread_name_prefix="subagent-execution",
                )
                self._max_workers = cap
            entry = _RuntimeEntry(
                plan=plan,
                service=service,
                interrupt_fn=interrupt_fn,
                interrupt_child_fn=interrupt_child_fn,
                terminal_fn=terminal_fn,
            )
            self._active[plan.activity_id] = entry
            self._activity_index[plan.activity_id] = (entry, None)
            if len(plan.children) > 1:
                for child in plan.children:
                    self._activity_index[child.activity_id] = (
                        entry,
                        child.task_index,
                    )

        def execute() -> None:
            try:
                service.start(plan)
                if service.is_cancelled(plan):
                    self._interrupt(
                        entry,
                        reason="activity cancelled before runner start",
                    )
                    return
                result = runner() or {}
                service.complete(plan, result)
            except Exception as exc:
                logger.exception("async subagent execution failed activity=%s", plan.activity_id)
                service.complete(
                    plan,
                    {
                        "results": [
                            {
                                "task_index": child.task_index,
                                "status": "error",
                                "summary": None,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                            for child in plan.children
                        ]
                    },
                )
            finally:
                with self._lock:
                    self._active.pop(plan.activity_id, None)
                    for activity_id in {
                        plan.activity_id,
                        *(child.activity_id for child in plan.children),
                    }:
                        self._activity_index.pop(activity_id, None)

        worker_context = contextvars.copy_context()
        try:
            future = self._executor.submit(worker_context.run, execute)
        except Exception as exc:
            with self._lock:
                self._active.pop(plan.activity_id, None)
                for activity_id in {
                    plan.activity_id,
                    *(child.activity_id for child in plan.children),
                }:
                    self._activity_index.pop(activity_id, None)
            service.cancel(plan, reason=f"schedule failed: {exc}")
            return {
                "status": "rejected",
                "error_code": "schedule_failed",
                "error": f"failed to schedule async subagent: {exc}",
            }
        entry.future = future
        watcher = threading.Thread(
            target=self._watch_cancel,
            args=(entry,),
            name=f"subagent-cancel-{plan.activity_id[-12:]}",
            daemon=True,
        )
        entry.cancel_watcher = watcher
        watcher.start()
        return {"status": "running", **plan.handle(status="running")}

    def _watch_cancel(self, entry: _RuntimeEntry) -> None:
        while entry.future is not None and not entry.future.done():
            cancelled = entry.service.cancelled_activity_ids(entry.plan)
            if (
                entry.plan.activity_id in cancelled
                and entry.plan.activity_id not in entry.observed_cancellations
            ):
                self._interrupt(entry, reason="activity cancelled")
                return
            for child in entry.plan.children:
                if (
                    child.activity_id in cancelled
                    and child.activity_id not in entry.observed_cancellations
                ):
                    self._interrupt_child(
                        entry,
                        child_activity_id=child.activity_id,
                        task_index=child.task_index,
                        reason="child activity cancelled",
                    )
            time.sleep(0.25)

    def cancel(self, activity_id: str, *, reason: str = "cancelled") -> bool:
        with self._lock:
            indexed = self._activity_index.get(_text(activity_id))
        if indexed is None:
            return False
        entry, task_index = indexed
        if task_index is None:
            self._interrupt(entry, reason=reason)
        else:
            self._interrupt_child(
                entry,
                child_activity_id=_text(activity_id),
                task_index=task_index,
                reason=reason,
            )
        return True

    def _interrupt(self, entry: _RuntimeEntry, *, reason: str) -> None:
        entry.observed_cancellations.add(entry.plan.activity_id)
        entry.observed_cancellations.update(
            child.activity_id for child in entry.plan.children
        )
        # Persist the terminal winner before exposing the interrupt signal;
        # otherwise a cooperative runner can return immediately and race a
        # late completed Run write ahead of cancellation convergence.
        resolved_statuses: dict[int, str] = {}
        try:
            resolved_statuses = entry.service.cancel(entry.plan, reason=reason)
        except Exception:
            logger.exception(
                "subagent cancellation persistence failed activity=%s",
                entry.plan.activity_id,
            )
        for child in entry.plan.children:
            if resolved_statuses.get(child.task_index) == "cancelled":
                self._publish_terminal(
                    entry,
                    task_index=child.task_index,
                    status="cancelled",
                    reason=reason,
                )
        if entry.interrupt_fn is not None:
            try:
                entry.interrupt_fn()
            except Exception:
                logger.exception("subagent interrupt callback failed")

    def _interrupt_child(
        self,
        entry: _RuntimeEntry,
        *,
        child_activity_id: str,
        task_index: int,
        reason: str,
    ) -> None:
        entry.observed_cancellations.add(child_activity_id)
        resolved_status: str | None = None
        try:
            resolved_status = entry.service.cancel_child(
                entry.plan,
                activity_id=child_activity_id,
                reason=reason,
            )
        except Exception:
            logger.exception(
                "subagent child cancellation persistence failed activity=%s",
                child_activity_id,
            )
        if resolved_status == "cancelled":
            self._publish_terminal(
                entry,
                task_index=task_index,
                status="cancelled",
                reason=reason,
            )
        callback = entry.interrupt_child_fn
        if callback is not None:
            try:
                callback(task_index)
            except Exception:
                logger.exception("subagent child interrupt callback failed")
        elif entry.interrupt_fn is not None:
            # Generic runtime clients without a child-specific callback still
            # honor cancellation fail-safely, at the cost of the whole batch.
            try:
                entry.interrupt_fn()
            except Exception:
                logger.exception("subagent interrupt callback failed")

    def _publish_terminal(
        self,
        entry: _RuntimeEntry,
        *,
        task_index: int,
        status: str,
        reason: str,
    ) -> None:
        callback = entry.terminal_fn
        if callback is None:
            return
        with self._lock:
            if task_index in entry.published_terminal_tasks:
                return
            entry.published_terminal_tasks.add(task_index)
        try:
            callback(task_index, status, reason)
        except Exception:
            logger.exception(
                "subagent terminal callback failed activity=%s task_index=%s",
                entry.plan.activity_id,
                task_index,
            )

    def interrupt_all(self, *, reason: str) -> int:
        with self._lock:
            entries = tuple(self._active.values())
        for entry in entries:
            self._interrupt(entry, reason=reason)
        return len(entries)

    def cancel_owner_run(
        self,
        *,
        conversation_session_id: str,
        owner_run_id: str = "",
        owner_turn_id: str = "",
        reason: str,
    ) -> int:
        """Cancel every detached execution owned by one parent conversation run.

        Async delegation intentionally detaches child agents from
        ``AIAgent._active_children`` so the parent can continue.  The immutable
        owner identity on ``ExecutionPlan`` is therefore the only safe boundary
        for a later ``run.cancel``: conversation-only matching is too broad,
        while walking the mutable parent child list cannot see detached work.
        """

        conversation_id = _text(conversation_session_id)
        run_id = _text(owner_run_id)
        turn_id = _text(owner_turn_id)
        if not conversation_id or not (run_id or turn_id):
            return 0
        with self._lock:
            entries = tuple(
                entry
                for entry in self._active.values()
                if entry.plan.conversation_session_id == conversation_id
                and (not run_id or entry.plan.owner_run_id == run_id)
                and (not turn_id or entry.plan.owner_turn_id == turn_id)
            )
        for entry in entries:
            self._interrupt(entry, reason=reason)
        return len(entries)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def active_handles(self) -> list[dict[str, Any]]:
        with self._lock:
            return [entry.plan.handle(status="running") for entry in self._active.values()]

    def reset_for_tests(self) -> None:
        self.interrupt_all(reason="test reset")
        with self._lock:
            self._active.clear()
            self._activity_index.clear()


subagent_execution_runtime = SubagentExecutionRuntime()


__all__ = [
    "ChildExecution",
    "DaemonThreadPoolExecutor",
    "ExecutionMode",
    "ExecutionModeError",
    "ExecutionPlan",
    "PersistenceRequiredError",
    "SubagentExecutionRuntime",
    "SubagentExecutionService",
    "SubagentTaskSpec",
    "resolve_execution_mode",
    "subagent_execution_runtime",
]
