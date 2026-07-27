"""Process and output boundary for the official ``lark-cli`` binary."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from hermes_constants import get_hermes_home

MINIMUM_LARK_CLI_VERSION = (1, 0, 77)
DEFAULT_TIMEOUT_SECONDS = 90
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_ARGUMENTS = 128
MAX_ARGUMENT_BYTES = 128 * 1024

_SAFE_ENV_KEYS = {
    "HOME",
    "USERPROFILE",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
}


@dataclass(frozen=True)
class LarkCliResult:
    """Normalized result independent of lark-cli's stdout/stderr variant."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    payload: Any = None
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        if self.timed_out or self.exit_code != 0:
            return False
        if isinstance(self.payload, dict) and self.payload.get("ok") is False:
            return False
        return True

    @property
    def confirmation_required(self) -> bool:
        if self.exit_code == 10:
            return True
        if not isinstance(self.payload, dict):
            return False
        error = self.payload.get("error")
        return (
            isinstance(error, dict)
            and str(error.get("type", "")).lower() == "confirmation"
        )

    def to_dict(self, *, include_argv: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": self.ok,
            "exit_code": self.exit_code,
        }
        if self.payload is not None:
            result["result"] = self.payload
        elif self.stdout:
            result["output"] = self.stdout
        diagnostics = _diagnostics_without_payload(self.stderr, self.payload)
        if diagnostics:
            result["diagnostics"] = diagnostics
        if self.timed_out:
            result["timed_out"] = True
        if self.truncated:
            result["truncated"] = True
        if self.confirmation_required:
            result["confirmation_required"] = True
        if include_argv:
            result["argv"] = list(self.argv)
        return result


@dataclass(frozen=True)
class LarkCliProbe:
    available: bool
    binary: str = ""
    version: str = ""
    compatible: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "binary": self.binary,
            "version": self.version,
            "compatible": self.compatible,
            "minimum_version": ".".join(map(str, MINIMUM_LARK_CLI_VERSION)),
            **({"reason": self.reason} if self.reason else {}),
        }


def _parse_json_output(stdout: str, stderr: str) -> Any:
    for candidate in (stdout.strip(), stderr.strip()):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        # Diagnostics occasionally precede the final JSON envelope. Parse the
        # last complete line rather than guessing across arbitrary text.
        for line in reversed(candidate.splitlines()):
            line = line.strip()
            if not line or line[0] not in "[{":
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _diagnostics_without_payload(stderr: str, payload: Any) -> str:
    """Keep real warnings without duplicating a structured stderr envelope."""
    candidate = stderr.strip()
    if not candidate or payload is None:
        return candidate
    try:
        if json.loads(candidate) == payload:
            return ""
    except json.JSONDecodeError:
        pass

    lines = candidate.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if not line or line[0] not in "[{":
            continue
        try:
            if json.loads(line) == payload:
                return "\n".join([*lines[:index], *lines[index + 1 :]]).strip()
        except json.JSONDecodeError:
            continue
    return candidate


def _bounded_read(file_obj, limit: int) -> tuple[str, bool]:
    file_obj.seek(0, os.SEEK_END)
    size = file_obj.tell()
    file_obj.seek(0)
    data = file_obj.read(limit)
    return data.decode("utf-8", errors="replace").strip(), size > limit


def _validate_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    if len(arguments) > MAX_ARGUMENTS:
        raise ValueError(f"lark-cli accepts at most {MAX_ARGUMENTS} arguments")
    normalized: list[str] = []
    total = 0
    for raw in arguments:
        if not isinstance(raw, str):
            raise ValueError("every lark-cli argument must be a string")
        if "\x00" in raw:
            raise ValueError("lark-cli arguments must not contain NUL bytes")
        encoded = raw.encode("utf-8")
        total += len(encoded)
        if total > MAX_ARGUMENT_BYTES:
            raise ValueError(
                f"lark-cli arguments exceed the {MAX_ARGUMENT_BYTES}-byte limit"
            )
        normalized.append(raw)
    return tuple(normalized)


class LarkCliRuntime:
    """Runs a pinned-compatible CLI with profile-isolated state."""

    def __init__(
        self,
        *,
        binary: str | Path | None = None,
        hermes_home: str | Path | None = None,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        self._binary_override = str(binary) if binary else ""
        self.hermes_home = Path(hermes_home) if hermes_home else get_hermes_home()
        self._base_env = dict(base_env) if base_env is not None else dict(os.environ)
        self._compatibility_result: LarkCliResult | None = None

    @property
    def state_root(self) -> Path:
        return self.hermes_home / ".lark-cli"

    @property
    def workspace_root(self) -> Path:
        for key in ("TERMINAL_CWD", "DOVIE_WORKSPACE_ROOT"):
            raw = str(self._base_env.get(key, "")).strip()
            if raw:
                candidate = Path(raw).expanduser()
                if candidate.is_dir():
                    return candidate.resolve()
        return Path.cwd().resolve()

    def resolve_binary(self) -> str:
        executable = "lark-cli.exe" if os.name == "nt" else "lark-cli"
        candidates = [
            self._binary_override,
            self._base_env.get("HERMES_LARK_CLI_BIN", ""),
            str(
                Path(self._base_env["DOVIE_DESKTOP_TOOL_RUNTIME"]) / "bin" / executable
            )
            if self._base_env.get("DOVIE_DESKTOP_TOOL_RUNTIME")
            else "",
            shutil.which(executable, path=self._base_env.get("PATH")),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.resolve())
        return ""

    def environment(self) -> dict[str, str]:
        env = {
            key: value
            for key, value in self._base_env.items()
            if key in _SAFE_ENV_KEYS and value
        }
        state_root = self.state_root.resolve()
        config_root = state_root / "config"
        data_root = state_root / "data"
        log_root = state_root / "logs"
        for directory in (self.hermes_home, state_root, config_root, data_root, log_root):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        env.update(
            {
                "HERMES_HOME": str(self.hermes_home.resolve()),
                "LARKSUITE_CLI_CONFIG_DIR": str(config_root),
                "LARKSUITE_CLI_DATA_DIR": str(data_root),
                "LARKSUITE_CLI_LOG_DIR": str(log_root),
                "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
            }
        )
        return env

    def probe(self) -> LarkCliProbe:
        binary = self.resolve_binary()
        if not binary:
            return LarkCliProbe(
                available=False,
                reason=(
                    "lark-cli is not installed. Install the official "
                    "@larksuite/cli binary or package it in the Dovie tool runtime."
                ),
            )
        result = self._version_result()
        output = " ".join(part for part in (result.stdout, result.stderr) if part)
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
        if not match:
            return LarkCliProbe(
                available=True,
                binary=binary,
                reason="could not determine lark-cli version",
            )
        version_parts = tuple(int(part) for part in match.groups())
        version = ".".join(match.groups())
        compatible = version_parts >= MINIMUM_LARK_CLI_VERSION
        return LarkCliProbe(
            available=True,
            binary=binary,
            version=version,
            compatible=compatible,
            reason="" if compatible else "lark-cli is older than the supported contract",
        )

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        require_compatible: bool = True,
    ) -> LarkCliResult:
        args = _validate_arguments(arguments)
        binary = self.resolve_binary()
        if not binary:
            return LarkCliResult(
                argv=tuple(["lark-cli", *args]),
                exit_code=127,
                stdout="",
                stderr=(
                    "lark-cli is not installed. Install @larksuite/cli or set "
                    "HERMES_LARK_CLI_BIN to the official executable."
                ),
            )

        if require_compatible:
            version_result = self._version_result()
            version_text = " ".join(
                part for part in (version_result.stdout, version_result.stderr) if part
            )
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text)
            if not match or tuple(map(int, match.groups())) < MINIMUM_LARK_CLI_VERSION:
                return LarkCliResult(
                    argv=tuple([binary, *args]),
                    exit_code=2,
                    stdout="",
                    stderr=(
                        "Unsupported lark-cli version. "
                        f"Version {'.'.join(map(str, MINIMUM_LARK_CLI_VERSION))} "
                        "or newer is required."
                    ),
                )

        argv = tuple([binary, *args])
        run_cwd = Path(cwd) if cwd else self.workspace_root
        run_cwd.mkdir(parents=True, exist_ok=True)
        timed_out = False

        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            popen_kwargs: dict[str, Any] = {
                "cwd": str(run_cwd),
                "env": self.environment(),
                "stdin": subprocess.DEVNULL,
                "stdout": stdout_file,
                "stderr": stderr_file,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            process = subprocess.Popen(argv, **popen_kwargs)
            try:
                exit_code = process.wait(timeout=max(1, timeout_seconds))
            except subprocess.TimeoutExpired:
                timed_out = True
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=3)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                exit_code = process.wait()

            stdout, stdout_truncated = _bounded_read(stdout_file, MAX_CAPTURE_BYTES)
            stderr, stderr_truncated = _bounded_read(stderr_file, MAX_CAPTURE_BYTES)

        payload = None if stdout_truncated or stderr_truncated else _parse_json_output(
            stdout, stderr
        )
        return LarkCliResult(
            argv=argv,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            payload=payload,
            timed_out=timed_out,
            truncated=stdout_truncated or stderr_truncated,
        )

    def _version_result(self) -> LarkCliResult:
        cached = self._compatibility_result
        if cached is not None:
            return cached
        result = self.run(["--version"], timeout_seconds=10, require_compatible=False)
        self._compatibility_result = result
        return result
