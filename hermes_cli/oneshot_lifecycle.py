"""Process lifecycle owner for non-interactive one-shot invocations."""

from __future__ import annotations

import logging
import os
import sys
import traceback
from typing import Any, Callable

_cleanup_done = False


def hard_exit_after_oneshot(return_code: object) -> None:
    """Flush user-visible output and bypass unsafe native finalizers."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    try:
        logging.shutdown()
    except Exception:
        pass

    if return_code is None:
        exit_code = 0
    elif isinstance(return_code, int):
        exit_code = return_code
    else:
        exit_code = 1
    os._exit(exit_code)


def cleanup_oneshot_runtime() -> None:
    """Best-effort, idempotent cleanup of process-global runtime resources."""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    cleanup_steps: tuple[Callable[[], Any], ...] = (
        _cleanup_terminal_environments,
        _interrupt_delegations,
        _cleanup_browser_sessions,
        _shutdown_mcp,
        _shutdown_auxiliary_clients,
    )
    for cleanup in cleanup_steps:
        try:
            cleanup()
        except BaseException:
            # The outer owner still performs the hard exit if cleanup is
            # interrupted; no individual resource can block that boundary.
            continue


def _cleanup_terminal_environments() -> None:
    from tools.terminal_tool import cleanup_all_environments

    cleanup_all_environments()


def _interrupt_delegations() -> None:
    from tools.async_delegation import interrupt_all

    interrupt_all(reason="oneshot shutdown")


def _cleanup_browser_sessions() -> None:
    from tools.browser_tool import _emergency_cleanup_all_sessions

    _emergency_cleanup_all_sessions()


def _shutdown_mcp() -> None:
    from tools.mcp_tool import shutdown_mcp_servers

    shutdown_mcp_servers()


def _shutdown_auxiliary_clients() -> None:
    from agent.auxiliary_client import shutdown_cached_clients

    shutdown_cached_clients()


def run_and_exit_oneshot(
    prompt: str,
    *,
    model: object = None,
    provider: object = None,
    toolsets: object = None,
    usage_file: object = None,
) -> None:
    """Run one-shot mode, clean all owners, then cross the hard-exit boundary."""
    try:
        from hermes_cli.oneshot import run_oneshot

        return_code = run_oneshot(
            prompt,
            model=model,
            provider=provider,
            toolsets=toolsets,
            usage_file=usage_file,
        )
    except KeyboardInterrupt:
        return_code = 130
    except SystemExit as exc:
        if exc.code is not None and not isinstance(exc.code, int):
            print(exc.code, file=sys.stderr)
            return_code = 1
        else:
            return_code = exc.code
    except BaseException:
        try:
            traceback.print_exc()
        except Exception:
            pass
        return_code = 1

    try:
        cleanup_oneshot_runtime()
    finally:
        hard_exit_after_oneshot(return_code)
