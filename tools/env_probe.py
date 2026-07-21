"""Bounded local Python-toolchain probe for the system prompt.

The probe adds one compact line only when the local terminal environment is
unusual (missing/mismatched pip, PEP 668, or missing ``python3``). Remote
terminal backends are skipped because their toolchain is discovered inside
the sandbox instead.

Prompt construction must never depend on a subprocess completing. A single
daemon worker owns the probe, callers wait on an event for a bounded interval,
and subprocess output is captured through temporary files so inherited Windows
pipe handles cannot wedge timeout cleanup.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_LOCK = threading.Lock()
_CACHED_LINE: Optional[str] = None
_PROBE_DONE = threading.Event()
_PROBE_THREAD: Optional[threading.Thread] = None
_PROBE_GEN = 0
_PROBE_WAIT_TIMEOUT = 10.0
_WAIT_ALREADY_TIMED_OUT = False

_REMOTE_BACKENDS = frozenset(
    {"docker", "singularity", "modal", "daytona", "ssh", "managed_modal"}
)


def _run(cmd: list[str], timeout: float = 3.0) -> tuple[int, str, str]:
    """Run a short subprocess without unbounded captured-pipe cleanup."""
    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                result = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return -1, "", "timeout"
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read().decode("utf-8", "replace").strip()
            stderr = stderr_file.read().decode("utf-8", "replace").strip()
            return result.returncode, stdout, stderr
    except FileNotFoundError:
        return -1, "", "not found"
    except OSError as exc:
        return -1, "", f"oserror: {exc}"


def _python_version_of(binary: str) -> Optional[str]:
    if not shutil.which(binary):
        return None
    rc, stdout, _stderr = _run(
        [
            binary,
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')",
        ]
    )
    return stdout if rc == 0 and stdout else None


def _has_pip_module(binary: str) -> bool:
    if not shutil.which(binary):
        return False
    rc, _stdout, _stderr = _run([binary, "-m", "pip", "--version"])
    return rc == 0


def _detect_pep668(binary: str) -> bool:
    if not shutil.which(binary):
        return False
    code = (
        "import os;"
        "stdlib=os.path.dirname(os.__file__);"
        "marker=os.path.join(stdlib,'EXTERNALLY-MANAGED');"
        "print('yes' if os.path.exists(marker) else 'no')"
    )
    rc, stdout, _stderr = _run([binary, "-c", code])
    return rc == 0 and stdout.strip() == "yes"


def _pip_python_version() -> Optional[str]:
    if not shutil.which("pip"):
        return None
    rc, stdout, _stderr = _run(["pip", "--version"])
    if rc != 0 or "(python " not in stdout or not stdout.endswith(")"):
        return None
    try:
        return stdout.rsplit("(python ", 1)[1][:-1].strip()
    except (AttributeError, IndexError):
        return None


def _build_probe_line() -> str:
    backend = (os.getenv("TERMINAL_ENV") or "local").strip().lower()
    if backend in _REMOTE_BACKENDS:
        return ""

    python3_version = _python_version_of("python3")
    python_version = _python_version_of("python")
    python3_has_pip = _has_pip_module("python3") if python3_version else False
    pip_python_version = _pip_python_version()
    python3_pep668 = _detect_pep668("python3") if python3_version else False
    has_uv = shutil.which("uv") is not None

    mismatch = bool(
        pip_python_version
        and python3_version
        and not python3_version.startswith(pip_python_version)
    )
    if (
        python3_version is not None
        and python3_has_pip
        and not mismatch
        and (not python3_pep668 or has_uv)
    ):
        return ""

    details: list[str] = []
    if python3_version:
        python3_detail = f"python3={python3_version}"
        if not python3_has_pip:
            python3_detail += " (no pip module)"
        details.append(python3_detail)
    else:
        details.append("python3=missing")

    if python_version and python_version != python3_version:
        details.append(f"python={python_version}")
    elif not python_version and python3_version:
        details.append("python=missing (use python3)")

    if pip_python_version:
        if mismatch:
            details.append(f"pip→python{pip_python_version} (mismatch)")
        elif not python3_has_pip:
            details.append(f"pip→python{pip_python_version}")
    elif not python3_has_pip:
        details.append("pip=missing")

    if python3_pep668:
        details.append("PEP 668=yes (use venv or uv)")
    if has_uv:
        details.append("uv=installed")

    return "Python toolchain: " + ", ".join(details) + "." if details else ""


def get_environment_probe_line(*, force_refresh: bool = False) -> str:
    """Return the cached line, failing open after a bounded wait."""
    global _CACHED_LINE, _PROBE_THREAD, _PROBE_GEN, _WAIT_ALREADY_TIMED_OUT
    if force_refresh:
        with _CACHE_LOCK:
            _CACHED_LINE = None
            _PROBE_DONE.clear()
            _PROBE_THREAD = None
            _PROBE_GEN += 1
            _WAIT_ALREADY_TIMED_OUT = False

    if _PROBE_DONE.is_set():
        return _CACHED_LINE or ""

    _ensure_probe_started()
    wait_timeout = 0.05 if _WAIT_ALREADY_TIMED_OUT else _PROBE_WAIT_TIMEOUT
    if not _PROBE_DONE.wait(timeout=wait_timeout):
        if not _WAIT_ALREADY_TIMED_OUT:
            _WAIT_ALREADY_TIMED_OUT = True
            logger.warning(
                "Environment probe exceeded %.1fs; building the prompt without it",
                _PROBE_WAIT_TIMEOUT,
            )
        return ""
    return _CACHED_LINE or ""


def _probe_worker(generation: int) -> None:
    global _CACHED_LINE
    try:
        line = _build_probe_line()
    except Exception as exc:
        logger.debug("Environment probe failed: %s", exc)
        line = ""
    with _CACHE_LOCK:
        if generation != _PROBE_GEN:
            return
        _CACHED_LINE = line
        _PROBE_DONE.set()


def _ensure_probe_started() -> None:
    global _PROBE_THREAD
    with _CACHE_LOCK:
        if _PROBE_DONE.is_set():
            return
        if _PROBE_THREAD is not None and _PROBE_THREAD.is_alive():
            return
        _PROBE_THREAD = threading.Thread(
            target=_probe_worker,
            args=(_PROBE_GEN,),
            name="env-probe",
            daemon=True,
        )
        _PROBE_THREAD.start()


def warm_environment_probe_async() -> None:
    """Start the single probe worker before prompt construction needs it."""
    _ensure_probe_started()


def _reset_cache_for_tests() -> None:
    global _CACHED_LINE, _PROBE_THREAD, _PROBE_GEN, _WAIT_ALREADY_TIMED_OUT
    with _CACHE_LOCK:
        _CACHED_LINE = None
        _PROBE_DONE.clear()
        _PROBE_THREAD = None
        _PROBE_GEN += 1
        _WAIT_ALREADY_TIMED_OUT = False
