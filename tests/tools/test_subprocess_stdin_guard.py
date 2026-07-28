from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_subprocess_stdin.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("_stdin_guard", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guard_detects_supported_process_apis_and_import_aliases():
    guard = _load_guard()
    source = """
import asyncio as aio
import os
import subprocess as sp
from subprocess import check_call as invoke

sp.run(["tool"])
invoke(["tool"])
os.system("tool")
aio.create_subprocess_exec("tool")
"""

    violations = guard.find_subprocess_calls(source, "plugin.py")
    assert [item["line"] for item in violations] == [7, 8, 9, 10]


def test_guard_accepts_explicit_stdin_input_and_exemption():
    guard = _load_guard()
    source = """
import subprocess
subprocess.Popen(["tool"], stdin=subprocess.DEVNULL)
subprocess.run(["tool"], input="payload")
# Interactive OAuth flow. noqa: subprocess-stdin
subprocess.run(["tool"])
"""

    assert guard.find_subprocess_calls(source, "plugin.py") == []


def test_external_plugin_roots_follow_profile_and_project_opt_in(
    monkeypatch, tmp_path
):
    guard = _load_guard()
    profile_home = tmp_path / "profile"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "true")
    monkeypatch.chdir(project)

    assert guard.enabled_external_plugin_roots(REPO_ROOT) == (
        (profile_home / "plugins").resolve(),
        (project / ".hermes" / "plugins").resolve(),
    )


def test_all_tui_subprocess_calls_have_explicit_stdin_policy(tmp_path):
    environment = os.environ.copy()
    environment["HERMES_HOME"] = str(tmp_path / "isolated-hermes-home")
    environment.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stdout + result.stderr
