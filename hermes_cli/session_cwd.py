"""Process/tool working-directory restoration for resumed CLI sessions."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping, MutableMapping


@dataclass(frozen=True)
class SessionCwdRestore:
    status: str
    recorded_path: str = ""
    current_path: str = ""
    error: str = ""

    @property
    def changed(self) -> bool:
        return self.status == "restored"


def restore_session_cwd(
    session: Mapping[str, object] | None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> SessionCwdRestore:
    """Restore process and terminal-tool cwd from a durable session row.

    Missing or moved directories are explicit outcomes, not exceptions. The
    caller owns presentation so interactive and quiet CLI surfaces can render
    the same state appropriately.
    """
    recorded_value = (session or {}).get("cwd")
    if not recorded_value:
        return SessionCwdRestore(status="unrecorded")

    recorded = str(Path(str(recorded_value)).expanduser())
    try:
        current = os.getcwd()
    except OSError:
        current = ""

    if current:
        try:
            if os.path.realpath(recorded) == os.path.realpath(current):
                return SessionCwdRestore(
                    status="unchanged",
                    recorded_path=recorded,
                    current_path=current,
                )
        except OSError:
            pass

    if not os.path.isdir(recorded):
        return SessionCwdRestore(
            status="missing",
            recorded_path=recorded,
            current_path=current,
        )

    try:
        os.chdir(recorded)
    except OSError as exc:
        return SessionCwdRestore(
            status="failed",
            recorded_path=recorded,
            current_path=current,
            error=str(exc),
        )

    target_environ = os.environ if environ is None else environ
    target_environ["TERMINAL_CWD"] = recorded
    return SessionCwdRestore(
        status="restored",
        recorded_path=recorded,
        current_path=current,
    )


__all__ = ["SessionCwdRestore", "restore_session_cwd"]
