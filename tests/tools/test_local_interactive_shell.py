import os

import tools.environments.local as local_environment


def _executable(tmp_path, name: str):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_interactive_shell_prefers_explicit_override(monkeypatch, tmp_path):
    configured = _executable(tmp_path, "configured-shell")
    environment_shell = _executable(tmp_path, "environment-shell")
    account_shell = _executable(tmp_path, "account-shell")
    monkeypatch.setattr(
        local_environment,
        "_account_login_shell",
        lambda: str(account_shell),
    )

    assert local_environment._find_interactive_shell(
        {
            "TERMINAL_INTERACTIVE_SHELL": str(configured),
            "SHELL": str(environment_shell),
        }
    ) == str(configured)


def test_interactive_shell_uses_account_then_environment(monkeypatch, tmp_path):
    environment_shell = _executable(tmp_path, "environment-shell")
    account_shell = _executable(tmp_path, "account-shell")
    monkeypatch.setattr(
        local_environment,
        "_account_login_shell",
        lambda: str(account_shell),
    )

    assert local_environment._find_interactive_shell(
        {"SHELL": str(environment_shell)}
    ) == str(account_shell)
    monkeypatch.setattr(local_environment, "_account_login_shell", lambda: "")
    assert local_environment._find_interactive_shell(
        {"SHELL": str(environment_shell)}
    ) == str(environment_shell)


def test_interactive_terminal_env_preserves_native_home_and_shell(monkeypatch, tmp_path):
    native_home = tmp_path / "native-home"
    isolated_home = tmp_path / "isolated-home"
    native_home.mkdir()
    isolated_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        "hermes_constants.get_subprocess_home",
        lambda: str(isolated_home),
    )

    env = local_environment._interactive_terminal_env(
        {
            "HOME": str(native_home),
            "PATH": "/usr/bin:/bin",
            "SHELL": "/bin/bash",
        },
        None,
        shell="/bin/zsh",
    )

    assert env["HOME"] == str(native_home)
    assert env["SHELL"] == "/bin/zsh"
    assert env["TERM"] == "xterm-256color"
    assert env["COLORTERM"] == "truecolor"
