"""AgentRunBackend — the production ``WorkerRunBackend`` that actually
runs an agent inside the new run_worker subprocess.

Lifecycle per ``RunStartFrame``:

  1. Install ``WorkerPublishBridge``: monkey-patches
     ``run_control.publish_recorded_event`` and the clarify/approval
     registration points so every publish + interactive register also
     emits a frame over stdout.
  2. Start the agent on a background thread (the agent's tool loop
     uses ``threading.Event`` blocks for clarify/approval; the asyncio
     loop must stay free for stdin reads).
  3. Wait for the thread to finish.
  4. Emit ``RunTerminalFrame(status=completed|cancelled|failed)``.
  5. Uninstall the bridge.

The actual agent invocation is delegated to an injectable ``AgentRunner``
callable. Production binds it to the existing
``tui_gateway.methods.prompt._execute_prompt_submit`` (see ``__init__``).
Tests pass a stub so unit tests don't need to spin up the LLM stack.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from tui_gateway.run_worker import (
    Emit,
    LogFrame,
    RunStartFrame,
    RunTerminalFrame,
    WorkerRunBackend,
)
from tui_gateway.services.worker_publish_bridge import WorkerPublishBridge

_log = logging.getLogger(__name__)


# Sync callable run on a background thread. Returns whatever the agent
# returns; raise to signal failure. ``cancel_event`` is set when the
# main side asks for cancel — the runner should poll / propagate to
# the agent's cancel mechanism.
AgentRunner = Callable[[RunStartFrame, threading.Event], Any]


def _run_context_from_frame(frame: RunStartFrame) -> Any:
    params = frame.params if isinstance(frame.params, dict) else {}
    payload = params.get("run_context_json") or params.get("runContextJson")
    if payload is None:
        return None
    try:
        from hermes_team_mission.domain.run_context import RunContext

        return RunContext.from_payload(payload)
    except Exception as exc:
        _log.warning(
            "[agent-run-backend] run_context_json parse failed run_id=%s: %s",
            frame.run_id,
            exc,
        )
    return None


@dataclass
class _ActiveRun:
    run_id: str
    thread: threading.Thread
    cancel_event: threading.Event
    bridge: WorkerPublishBridge
    frame: RunStartFrame


class AgentRunBackend(WorkerRunBackend):
    """Production backend. Construct ONCE per worker process; ``start``
    can run sequentially (the legacy worker only ever ran one agent at
    a time per process)."""

    def __init__(
        self,
        runner: AgentRunner,
        *,
        loop_provider: Optional[Callable[[], asyncio.AbstractEventLoop]] = None,
    ) -> None:
        """
        Parameters
        ----------
        runner
            Sync callable invoked on a background thread. Production
            wires this to ``_execute_prompt_submit`` (see Phase 5c).
        loop_provider
            Optional override returning the asyncio loop the bridge
            should target. Defaults to ``asyncio.get_running_loop()``
            captured at ``start`` time.
        """
        self._runner = runner
        self._loop_provider = loop_provider
        self._active: Optional[_ActiveRun] = None
        self._lock = threading.RLock()

    async def start(self, frame: RunStartFrame, emit: Emit) -> None:
        loop = self._loop_provider() if self._loop_provider else asyncio.get_running_loop()
        cancel_event = threading.Event()

        with self._lock:
            if self._active is not None and self._active.thread.is_alive():
                # Refuse concurrent runs — the legacy worker is single-run
                # per process and the agent libraries assume the same.
                await emit(
                    RunTerminalFrame(
                        run_id=frame.run_id,
                        status="failed",
                        stored_session_id=frame.stored_session_id,
                        turn_id=frame.turn_id,
                        message="another run already active in this worker",
                    )
                )
                return

            run_context = _run_context_from_frame(frame)
            bridge = WorkerPublishBridge(emit=emit, loop=loop)
            bridge.install(
                stored_session_id=frame.stored_session_id,
                run_context=run_context,
            )

            # Container for the runner's return value / exception, captured
            # by the thread and read back here.
            result: dict[str, Any] = {"exc": None, "value": None}

            def _wrap() -> None:
                try:
                    result["value"] = self._runner(frame, cancel_event)
                except BaseException as exc:  # noqa: BLE001 — capture & surface
                    result["exc"] = exc

            thread = threading.Thread(
                target=_wrap,
                name=f"agent-run[{frame.run_id}]",
                daemon=True,
            )
            active = _ActiveRun(
                run_id=frame.run_id,
                thread=thread,
                cancel_event=cancel_event,
                bridge=bridge,
                frame=frame,
            )
            self._active = active
            thread.start()

        try:
            # Yield the event loop until the agent thread exits. ``join``
            # is blocking; bounce it through the executor so stdin/stdout
            # stays drained.
            await asyncio.get_running_loop().run_in_executor(None, thread.join)
        finally:
            with self._lock:
                if self._active is active:
                    self._active = None
            try:
                bridge.uninstall()
            except Exception:
                _log.exception("[agent-run-backend] bridge uninstall failed")

        status, message = _classify_outcome(result["exc"], cancel_event)
        await emit(
            RunTerminalFrame(
                run_id=frame.run_id,
                status=status,
                stored_session_id=frame.stored_session_id,
                turn_id=frame.turn_id,
                message=message,
            )
        )
        if result["exc"] is not None and status == "failed":
            await emit(LogFrame(level="error", text=f"agent crashed: {result['exc']!r}"))

    async def cancel(self, run_id: str) -> None:
        with self._lock:
            active = self._active
        if active is None or active.run_id != run_id:
            return
        active.cancel_event.set()

    async def shutdown(self) -> None:
        with self._lock:
            active = self._active
        if active is None:
            return
        active.cancel_event.set()
        # Best-effort join — the run loop's outer ``finally`` will
        # uninstall the bridge once the thread exits.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, active.thread.join, 5.0,
            )
        except Exception:
            pass


def _classify_outcome(
    exc: Optional[BaseException], cancel_event: threading.Event,
) -> tuple[str, str]:
    """Decide the worker→main ``RunTerminalFrame`` status from the
    agent thread's exit shape.

    Order matters: ``cancel_event`` is checked FIRST. The legacy
    ``InterruptedError`` path inside ``conversation_loop`` catches its
    own exception and returns a normal result dict with
    ``"interrupted": True`` — the thread exits cleanly (``exc is None``).
    If we ran the ``exc is None → completed`` check first, every
    user-cancelled run would be classified as ``completed`` and the
    main side's Phase 4c terminal gate (which intentionally skips
    publish for ``completed``, trusting the worker's own
    ``message.complete``) would mask the cancel — only the prompt
    layer's own ``message.complete(status="cancelled")`` emit (added
    in Phase 11) carries the truth. That ordering bug doesn't surface
    today because the gate's skip is correct for completed, but a
    future change to the gate would expose it. Cancel first keeps the
    classification honest regardless of how downstream layers handle
    each status."""
    if cancel_event.is_set():
        if exc is None or isinstance(exc, (KeyboardInterrupt, SystemExit)):
            return "cancelled", ""
        return "cancelled", str(exc)
    if exc is None:
        return "completed", ""
    return "failed", str(exc)
