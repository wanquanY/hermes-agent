# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import os
import shlex

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


_DESKTOP_TERMINAL_TASK_PREFIX = "dovie.desktop.terminal:"


def _desktop_terminal_task_id(session: dict) -> str:
    return f"{_DESKTOP_TERMINAL_TASK_PREFIX}{str(session.get('session_key') or '')}"


def _desktop_terminal_cwd(session: dict) -> str:
    workspace = session.get("workspace")
    workspace_path = workspace.get("path") if isinstance(workspace, dict) else ""
    return str(
        session.get("cwd")
        or workspace_path
        or os.environ.get("DOVIE_WORKSPACE_ROOT")
        or os.environ.get("TERMINAL_CWD")
        or os.getcwd()
    )


def _desktop_terminal_owner(params: dict, rid):
    """Resolve terminal ownership without requiring a live agent session.

    A conversation may be visible and fully persisted while its in-memory
    agent runtime is idle. Manual PTYs are owned by that durable conversation
    id plus its workspace binding, not by an ephemeral runtime container.
    """
    requested_id = str(params.get("session_id") or "").strip()
    if not requested_id:
        return None, _err(rid, 4006, "session_id required")

    _runtime_sid, live_session = _resolve_runtime_session(requested_id)
    if live_session is not None:
        owner = live_session
        stable_id = str(live_session.get("session_key") or requested_id)
    else:
        from tui_gateway.services.workspace import session_workspace_run_context

        stable_id = requested_id
        context = session_workspace_run_context(stable_id, params)
        if not context.get("cwd"):
            return None, _err(rid, 4004, "workspace is not bound to this conversation")
        owner = {
            "session_key": stable_id,
            "cwd": context["cwd"],
            "workspace": context.get("workspace") or {},
        }

    try:
        from tui_gateway.services.agent_terminal_bridge import bind_desktop_terminal_owner

        bind_desktop_terminal_owner(
            stable_id,
            transport=current_transport(),
            runtime_scope_key=str(
                params.get("runtime_scope_key")
                or params.get("runtimeScopeKey")
                or stable_id
            ),
        )
    except Exception:
        pass
    return owner, None


def _owned_process(session: dict, process_id: str, *, desktop_terminal_only: bool = False):
    from tools.process_registry import process_registry

    process = process_registry.get(process_id)
    if process is None or str(getattr(process, "session_key", "") or "") != str(
        session.get("session_key") or ""
    ):
        return None
    if desktop_terminal_only and not str(getattr(process, "task_id", "") or "").startswith(
        _DESKTOP_TERMINAL_TASK_PREFIX
    ):
        return None
    return process


def _desktop_terminal_entry(process) -> dict:
    interactive = bool(getattr(process, "_pty", None)) and not process.exited
    return {
        "process_id": process.id,
        "command": process.command,
        "cwd": process.cwd,
        "pid": process.pid,
        "started_at": process.started_at,
        # A checkpoint-recovered process has no live PTY handle and therefore
        # cannot accept user input even if its old PID still exists. Expose it
        # as exited so terminal.session.open creates a fresh interactive shell.
        "status": "running" if interactive else "exited",
        "exit_code": process.exit_code,
        "output_tail": process.output_buffer or "",
    }


def _desktop_terminal_processes(session: dict) -> list:
    from tools.process_registry import process_registry

    owner_key = str(session.get("session_key") or "")
    result = []
    for entry in process_registry.list_sessions(task_id=_desktop_terminal_task_id(session)):
        process = process_registry.get(entry["session_id"])
        if process is None or str(getattr(process, "session_key", "") or "") != owner_key:
            continue
        result.append(_desktop_terminal_entry(process))
    return sorted(result, key=lambda item: float(item.get("started_at") or 0))


def _session_processes(session: dict) -> list:
    """Background processes owned by this session (registry session_key match)."""
    from tools.process_registry import process_registry

    key = str(session.get("session_key") or "")
    owned = []
    for entry in process_registry.list_sessions():
        proc = process_registry.get(entry["session_id"])
        if proc is None or str(getattr(proc, "session_key", "") or "") != key:
            continue
        # The 200-char list preview is too thin for the desktop's inline
        # terminal viewer — ship a real tail alongside it.
        entry["output_tail"] = (proc.output_buffer or "")[-4000:]
        owned.append(entry)
    return owned


@method("process.list")
def _(rid, params: dict) -> dict:
    """Session-scoped view of the background process registry (desktop status stack)."""
    session, err = _sess(params, rid)
    if err:
        return err
    try:
        return _ok(rid, {"processes": _session_processes(session)})
    except Exception as e:
        return _err(rid, 5010, str(e))


@method("process.kill")
def _(rid, params: dict) -> dict:
    """Kill ONE background process — scoped to the caller's session so one
    window can't reap another session's work (unlike process.stop's kill_all)."""
    session, err = _sess(params, rid)
    if err:
        return err
    proc_id = str(params.get("process_id") or "")
    if not proc_id:
        return _err(rid, 4012, "process_id required")
    try:
        from tools.process_registry import process_registry

        proc = process_registry.get(proc_id)
        if proc is None or str(getattr(proc, "session_key", "") or "") != str(
            session.get("session_key") or ""
        ):
            return _err(rid, 4044, f"no such process: {proc_id}")
        return _ok(rid, process_registry.kill_process(proc_id))
    except Exception as e:
        return _err(rid, 5010, str(e))


@method("terminal.session.open")
def _(rid, params: dict) -> dict:
    """Open or restore the real desktop PTY owned by this Hermes session."""
    session, err = _desktop_terminal_owner(params, rid)
    if err:
        return err
    try:
        from tools.environments.local import _find_shell
        from tools.process_registry import process_registry

        reuse = params.get("reuse", True) is not False
        if reuse:
            running = [
                process
                for process in _desktop_terminal_processes(session)
                if process["status"] == "running"
            ]
            if running:
                return _ok(rid, {"terminal": running[-1], "reused": True})

        shell = _find_shell()
        process = process_registry.spawn_local(
            f"exec {shlex.quote(shell)} -l",
            cwd=_desktop_terminal_cwd(session),
            task_id=_desktop_terminal_task_id(session),
            session_key=str(session.get("session_key") or ""),
            use_pty=True,
        )
        if getattr(process, "_pty", None) is None:
            process_registry.kill_process(process.id, source="terminal.session.open")
            return _err(rid, 5011, "interactive PTY is unavailable on this runtime")
        return _ok(rid, {"terminal": _desktop_terminal_entry(process), "reused": False})
    except Exception as e:
        return _err(rid, 5010, str(e))


@method("terminal.session.list")
def _(rid, params: dict) -> dict:
    """List only the manual terminal PTYs owned by this Hermes session."""
    session, err = _desktop_terminal_owner(params, rid)
    if err:
        return err
    try:
        return _ok(rid, {"terminals": _desktop_terminal_processes(session)})
    except Exception as e:
        return _err(rid, 5010, str(e))


@method("terminal.session.write")
def _(rid, params: dict) -> dict:
    """Write raw user input to one session-owned desktop PTY."""
    session, err = _desktop_terminal_owner(params, rid)
    if err:
        return err
    process_id = str(params.get("process_id") or "").strip()
    if not process_id:
        return _err(rid, 4012, "process_id required")
    data = params.get("data")
    if not isinstance(data, str):
        return _err(rid, 4012, "data must be a string")
    if _owned_process(session, process_id, desktop_terminal_only=True) is None:
        return _err(rid, 4044, f"no such terminal: {process_id}")
    try:
        from tools.process_registry import process_registry

        result = process_registry.write_stdin(process_id, data)
        if result.get("status") != "ok":
            return _err(rid, 5012, str(result.get("error") or result.get("status")))
        return _ok(rid, result)
    except Exception as e:
        return _err(rid, 5010, str(e))


@method("terminal.session.resize")
def _(rid, params: dict) -> dict:
    """Resize one session-owned desktop PTY without changing Hermes TUI layout."""
    session, err = _desktop_terminal_owner(params, rid)
    if err:
        return err
    process_id = str(params.get("process_id") or "").strip()
    if not process_id:
        return _err(rid, 4012, "process_id required")
    try:
        cols = max(20, min(500, int(params.get("cols") or 80)))
        rows = max(5, min(300, int(params.get("rows") or 24)))
    except (TypeError, ValueError):
        return _err(rid, 4012, "cols and rows must be integers")
    process = _owned_process(session, process_id, desktop_terminal_only=True)
    if process is None:
        return _err(rid, 4044, f"no such terminal: {process_id}")
    pty = getattr(process, "_pty", None)
    if pty is None:
        return _err(rid, 5011, "terminal PTY is unavailable")
    try:
        resize = getattr(pty, "setwinsize", None)
        if callable(resize):
            resize(rows, cols)
        else:
            resize = getattr(pty, "set_size", None)
            if not callable(resize):
                return _err(rid, 5011, "terminal PTY does not support resize")
            resize(cols, rows)
        return _ok(rid, {"process_id": process_id, "cols": cols, "rows": rows})
    except Exception as e:
        return _err(rid, 5010, str(e))


@method("terminal.session.close")
def _(rid, params: dict) -> dict:
    """Terminate one session-owned manual terminal without touching agent jobs."""
    session, err = _desktop_terminal_owner(params, rid)
    if err:
        return err
    process_id = str(params.get("process_id") or "").strip()
    if not process_id:
        return _err(rid, 4012, "process_id required")
    if _owned_process(session, process_id, desktop_terminal_only=True) is None:
        return _err(rid, 4044, f"no such terminal: {process_id}")
    try:
        from tools.process_registry import process_registry

        return _ok(
            rid,
            process_registry.kill_process(process_id, source="terminal.session.close"),
        )
    except Exception as e:
        return _err(rid, 5010, str(e))
