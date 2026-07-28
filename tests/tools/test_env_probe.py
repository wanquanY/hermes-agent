from __future__ import annotations

import sys
import threading
import time

import pytest

from tools import env_probe


@pytest.fixture(autouse=True)
def reset_probe_cache():
    env_probe._reset_cache_for_tests()
    yield
    env_probe._reset_cache_for_tests()


def test_healthy_environment_is_silent(monkeypatch):
    monkeypatch.setattr(
        env_probe,
        "_python_version_of",
        lambda binary: "3.13.3" if binary == "python3" else None,
    )
    monkeypatch.setattr(env_probe, "_has_pip_module", lambda binary: True)
    monkeypatch.setattr(env_probe, "_detect_pep668", lambda binary: False)
    monkeypatch.setattr(env_probe, "_pip_python_version", lambda: "3.13")
    monkeypatch.setattr(env_probe.shutil, "which", lambda name: None)

    assert env_probe.get_environment_probe_line() == ""


def test_mismatched_pep668_environment_is_described(monkeypatch):
    monkeypatch.setattr(
        env_probe,
        "_python_version_of",
        lambda binary: {"python3": "3.11.15", "python": None}.get(binary),
    )
    monkeypatch.setattr(env_probe, "_has_pip_module", lambda binary: False)
    monkeypatch.setattr(env_probe, "_detect_pep668", lambda binary: True)
    monkeypatch.setattr(env_probe, "_pip_python_version", lambda: "3.12")
    monkeypatch.setattr(
        env_probe.shutil,
        "which",
        lambda name: None if name == "uv" else f"/usr/bin/{name}",
    )

    line = env_probe.get_environment_probe_line()

    assert "3.11.15" in line
    assert "no pip module" in line
    assert "mismatch" in line
    assert "PEP 668" in line
    assert "\n" not in line


@pytest.mark.parametrize("backend", ["docker", "modal", "ssh", "managed_modal"])
def test_remote_backends_are_skipped(monkeypatch, backend):
    monkeypatch.setenv("TERMINAL_ENV", backend)
    monkeypatch.setattr(env_probe, "_python_version_of", lambda binary: None)

    assert env_probe.get_environment_probe_line() == ""


def test_hung_probe_fails_open_for_concurrent_callers(monkeypatch):
    release = threading.Event()

    def stuck_probe():
        release.wait(timeout=30)
        return "Python toolchain: late result."

    monkeypatch.setattr(env_probe, "_build_probe_line", stuck_probe)
    monkeypatch.setattr(env_probe, "_PROBE_WAIT_TIMEOUT", 0.2)
    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(env_probe.get_environment_probe_line()),
            daemon=True,
        )
        for _ in range(4)
    ]

    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    elapsed = time.monotonic() - started
    release.set()

    assert all(not thread.is_alive() for thread in threads)
    assert results == ["", "", "", ""]
    assert elapsed < 2


def test_late_probe_result_is_published_after_recovery(monkeypatch):
    release = threading.Event()

    def slow_probe():
        release.wait(timeout=30)
        return "Python toolchain: recovered."

    monkeypatch.setattr(env_probe, "_build_probe_line", slow_probe)
    monkeypatch.setattr(env_probe, "_PROBE_WAIT_TIMEOUT", 0.1)

    assert env_probe.get_environment_probe_line() == ""
    release.set()
    assert env_probe._PROBE_DONE.wait(timeout=3)
    assert env_probe.get_environment_probe_line() == "Python toolchain: recovered."


def test_run_timeout_is_bounded_with_pipe_holding_descendant():
    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])\n"
        "time.sleep(10)\n"
    )

    started = time.monotonic()
    return_code, _stdout, stderr = env_probe._run(
        [sys.executable, "-c", script],
        timeout=0.5,
    )

    assert return_code == -1
    assert stderr == "timeout"
    assert time.monotonic() - started < 3
