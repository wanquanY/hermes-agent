from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.lark_cli import register
from plugins.lark_cli.runtime import LarkCliProbe, LarkCliResult, LarkCliRuntime
from plugins.lark_cli.service import LarkCliService


def result(
    *,
    exit_code: int = 0,
    payload=None,
    stdout: str = "",
    stderr: str = "",
) -> LarkCliResult:
    return LarkCliResult(
        argv=("lark-cli",),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        payload=payload,
    )


class FakeRuntime:
    def __init__(self, root: Path, responses: list[LarkCliResult] | None = None):
        self.state_root = root / ".lark-cli"
        self.responses = list(responses or [])
        self.calls: list[tuple[list[str], dict]] = []

    def probe(self) -> LarkCliProbe:
        return LarkCliProbe(
            available=True,
            binary="/fake/lark-cli",
            version="1.0.77",
            compatible=True,
        )

    def run(self, arguments, **kwargs):
        args = list(arguments)
        self.calls.append((args, kwargs))
        if args[:2] == ["auth", "qrcode"]:
            output = Path(kwargs["cwd"]) / "authorization.png"
            output.write_bytes(b"png")
        if not self.responses:
            raise AssertionError(f"unexpected lark-cli call: {args}")
        return self.responses.pop(0)


def _approve(monkeypatch):
    monkeypatch.setattr(
        "tools.approval_gate.run_approval_gate",
        lambda **_: {"approved": True, "user_approved": True},
    )


def _deny(monkeypatch):
    monkeypatch.setattr(
        "tools.approval_gate.run_approval_gate",
        lambda **_: {
            "approved": False,
            "outcome": "denied",
            "message": "denied by user",
            "user_consent": False,
        },
    )


def test_runtime_uses_argv_and_profile_isolated_environment(tmp_path: Path):
    executable = tmp_path / "fake-lark-cli"
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, sys",
                "if sys.argv[1:] == ['--version']:",
                "    print('lark-cli version 1.0.77')",
                "else:",
                "    print(json.dumps({",
                "        'ok': True,",
                "        'argv': sys.argv[1:],",
                "        'config': os.environ.get('LARKSUITE_CLI_CONFIG_DIR'),",
                "        'secret_visible': bool(os.environ.get('DO_NOT_LEAK')),",
                "    }))",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    runtime = LarkCliRuntime(
        binary=executable,
        hermes_home=tmp_path / "profile",
        base_env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "TERMINAL_CWD": str(tmp_path / "workspace"),
            "DO_NOT_LEAK": "secret",
        },
    )
    (tmp_path / "workspace").mkdir()

    executed = runtime.run(["calendar", "+agenda", "--title", "a; echo unsafe"])

    assert executed.ok is True
    assert executed.payload["argv"] == [
        "calendar",
        "+agenda",
        "--title",
        "a; echo unsafe",
    ]
    assert executed.payload["secret_visible"] is False
    assert executed.payload["config"] == str(
        (tmp_path / "profile" / ".lark-cli" / "config").resolve()
    )
    assert runtime.workspace_root == (tmp_path / "workspace").resolve()


def test_runtime_treats_payload_ok_false_as_failure():
    command = LarkCliResult(
        argv=("lark-cli", "auth", "check"),
        exit_code=0,
        stdout='{"ok": false}',
        stderr="",
        payload={"ok": False},
    )
    assert command.ok is False


def test_result_does_not_duplicate_structured_error_as_diagnostics():
    payload = {
        "ok": False,
        "error": {"type": "validation", "message": "invalid"},
    }
    command = result(
        exit_code=2,
        payload=payload,
        stderr=json.dumps(payload),
    )

    assert command.to_dict() == {
        "ok": False,
        "exit_code": 2,
        "result": payload,
    }


def test_status_distinguishes_binding_from_usable_identity(tmp_path: Path):
    runtime = FakeRuntime(
        tmp_path,
        [
            result(payload={"workspace": "hermes", "appId": "cli_app"}),
            result(payload={"identity": "none"}),
        ],
    )

    response = LarkCliService(runtime=runtime).status(verify=False)

    assert response["ok"] is False
    assert response["binding"]["ok"] is True
    assert response["next_action"] == "authorize_user_or_use_bot"


def test_bind_requires_human_approval(tmp_path: Path, monkeypatch):
    runtime = FakeRuntime(tmp_path, [result(payload={"ok": True})])
    service = LarkCliService(runtime=runtime)
    _deny(monkeypatch)

    denied = service.bind(identity="user-default")

    assert denied["ok"] is False
    assert denied["approval_required"] is True
    assert runtime.calls == []


def test_user_default_bind_reuses_hermes_and_forces_only_after_approval(
    tmp_path: Path, monkeypatch
):
    runtime = FakeRuntime(tmp_path, [result(payload={"ok": True})])
    service = LarkCliService(runtime=runtime)
    _approve(monkeypatch)

    bound = service.bind(identity="user-default")

    assert bound["ok"] is True
    assert runtime.calls[0][0] == [
        "config",
        "bind",
        "--source",
        "hermes",
        "--identity",
        "user-default",
        "--force",
    ]


def test_high_risk_write_retries_with_yes_only_after_approval(
    tmp_path: Path, monkeypatch
):
    runtime = FakeRuntime(
        tmp_path,
        [
            result(
                exit_code=10,
                payload={
                    "ok": False,
                    "error": {"type": "confirmation", "risk": "high_risk_write"},
                },
            ),
            result(payload={"ok": True, "data": {"created": True}}),
        ],
    )
    service = LarkCliService(runtime=runtime)
    _approve(monkeypatch)

    created = service.run_business_command(
        command="calendar",
        arguments=["+create", "--summary", "Review"],
        identity="user",
    )

    assert created["ok"] is True
    assert runtime.calls[0][0] == [
        "calendar",
        "+create",
        "--summary",
        "Review",
        "--as",
        "user",
    ]
    assert runtime.calls[1][0][-1] == "--yes"


def test_high_risk_write_is_not_retried_when_denied(tmp_path: Path, monkeypatch):
    runtime = FakeRuntime(
        tmp_path,
        [result(exit_code=10, payload={"error": {"type": "confirmation"}})],
    )
    service = LarkCliService(runtime=runtime)
    _deny(monkeypatch)

    denied = service.run_business_command(command="task", arguments=["+delete", "x"])

    assert denied["approval_required"] is True
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("forbidden", ["--yes", "-y", "--force", "--as=user", "--profile"])
def test_business_arguments_cannot_bypass_plugin_policy(tmp_path: Path, forbidden: str):
    runtime = FakeRuntime(tmp_path)
    service = LarkCliService(runtime=runtime)

    response = service.run_business_command(
        command="im",
        arguments=["+send", forbidden],
    )

    assert response["ok"] is False
    assert runtime.calls == []


def test_device_login_is_two_step_and_device_code_stays_in_private_state(
    tmp_path: Path,
):
    runtime = FakeRuntime(
        tmp_path,
        [
            result(
                payload={
                    "verification_url": "https://open.feishu.cn/device/opaque",
                    "device_code": "device-secret",
                    "expires_in": 600,
                }
            ),
            result(payload={"ok": True, "file_path": "authorization.png"}),
            result(payload={"ok": True, "userName": "Ada"}),
        ],
    )
    service = LarkCliService(runtime=runtime)

    started = service.start_login(domains=["calendar"])

    assert started["status"] == "authorization_required"
    assert "device_code" not in started
    assert Path(started["qr_code_path"]).is_file()
    state_path = (
        runtime.state_root
        / "auth-flows"
        / started["authorization_id"]
        / "flow.json"
    )
    assert json.loads(state_path.read_text())["device_code"] == "device-secret"
    if os.name != "nt":
        assert state_path.stat().st_mode & 0o777 == 0o600

    completed = service.complete_login(
        authorization_id=started["authorization_id"]
    )

    assert completed["ok"] is True
    assert runtime.calls[-1][0] == [
        "auth",
        "login",
        "--device-code",
        "device-secret",
        "--json",
    ]
    assert not state_path.exists()


def test_device_code_is_redacted_from_completion_diagnostics(tmp_path: Path):
    runtime = FakeRuntime(
        tmp_path,
        [
            result(
                payload={
                    "verification_url": "https://open.feishu.cn/device/opaque",
                    "device_code": "device-secret",
                    "expires_in": 30,
                }
            ),
            result(payload={"ok": True, "file_path": "authorization.png"}),
            result(exit_code=4, stderr="failed for device-secret"),
        ],
    )
    service = LarkCliService(runtime=runtime)
    started = service.start_login(domains=["calendar"])

    completed = service.complete_login(
        authorization_id=started["authorization_id"]
    )

    assert started["expires_in"] == 30
    assert "device-secret" not in json.dumps(completed)
    assert completed["diagnostics"] == "failed for [REDACTED]"


def test_skill_reader_uses_cli_embedded_version_matched_content(tmp_path: Path):
    runtime = FakeRuntime(
        tmp_path,
        [result(payload={"skill": "lark-calendar", "content": "# Calendar"})],
    )
    service = LarkCliService(runtime=runtime)

    response = service.read_skill("lark-calendar", "references/create.md")

    assert response["ok"] is True
    assert runtime.calls[0][0] == [
        "skills",
        "read",
        "lark-calendar",
        "references/create.md",
        "--json",
    ]


def test_plugin_registers_tools_cli_and_skill_manifest_contract():
    registrations = SimpleNamespace(tools=[], cli=[])

    class Context:
        def register_tool(self, **kwargs):
            registrations.tools.append(kwargs)

        def register_cli_command(self, **kwargs):
            registrations.cli.append(kwargs)

    register(Context())

    assert {item["name"] for item in registrations.tools} == {
        "lark_cli_status",
        "lark_cli_auth",
        "lark_cli_run",
        "lark_cli_skill",
    }
    assert registrations.cli[0]["name"] == "lark"


def test_plugin_cli_dispatch_preserves_failure_exit_code(monkeypatch):
    from plugins import lark_cli

    monkeypatch.setattr(lark_cli, "lark_command", lambda _args: 3)

    with pytest.raises(SystemExit) as exc_info:
        lark_cli._dispatch_lark_cli(SimpleNamespace())

    assert exc_info.value.code == 3
