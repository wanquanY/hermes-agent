from __future__ import annotations

import pytest

from tools import approval


@pytest.fixture(autouse=True)
def _deny_policy(monkeypatch):
    approval._pending.clear()
    approval._gateway_queues.clear()
    approval._session_yolo.clear()
    approval._permanent_approved.clear()
    monkeypatch.setattr(
        approval,
        "_get_approval_config",
        lambda: {"mode": "off", "deny": ["git push *", "terraform destroy*"]},
    )
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )
    yield
    approval._pending.clear()
    approval._gateway_queues.clear()
    approval._session_yolo.clear()
    approval._permanent_approved.clear()


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "GIT PUSH origin main",
        "terraform destroy -auto-approve",
    ],
)
def test_user_deny_precedes_mode_off_and_creates_no_pending(command):
    approval.enable_session_yolo("default")
    approval.approve_permanent(command)
    result = approval.check_all_command_guards(command, "local")
    assert result["approved"] is False
    assert result["user_deny"] is True
    assert result["outcome"] == "blocked"
    assert approval._pending == {}
    assert approval._gateway_queues == {}


def test_unmatched_command_still_obeys_normal_mode_policy():
    result = approval.check_all_command_guards("git status", "local")
    assert result["approved"] is True


def test_user_deny_is_not_bypassed_by_isolated_backend():
    result = approval.check_all_command_guards("git push origin main", "docker")
    assert result["approved"] is False
    assert result["user_deny"] is True


def test_execute_code_user_deny_precedes_mode_off_and_backend_skip(monkeypatch):
    monkeypatch.setattr(
        approval,
        "_get_approval_config",
        lambda: {"mode": "off", "deny": ["execute_code *terraform destroy*"]},
    )
    result = approval.check_execute_code_guard(
        'subprocess.run("terraform destroy -auto-approve", shell=True)',
        "docker",
    )
    assert result["approved"] is False
    assert result["user_deny"] is True
