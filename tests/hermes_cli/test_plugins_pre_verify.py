from hermes_cli.plugins import get_pre_verify_continue_message


def test_pre_verify_accepts_hermes_continue_shape(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda hook_name, **kwargs: [
            {"action": "continue", "message": "run checks"}
        ],
    )

    assert get_pre_verify_continue_message(session_id="s") == "run checks"


def test_pre_verify_accepts_claude_stop_block_shape(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda hook_name, **kwargs: [
            {"decision": "block", "reason": "run formatter"}
        ],
    )

    assert get_pre_verify_continue_message() == "run formatter"


def test_pre_verify_uses_first_actionable_directive(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda hook_name, **kwargs: [
            "noise",
            {"action": "continue"},
            {"action": "continue", "message": " second "},
            {"action": "continue", "message": "third"},
        ],
    )

    assert get_pre_verify_continue_message() == "second"


def test_pre_verify_forwards_scope_signals(monkeypatch):
    seen = {}

    def capture(hook_name, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", capture)
    assert get_pre_verify_continue_message(
        coding=True,
        attempt=2,
        changed_paths=["a.py"],
    ) is None
    assert seen["coding"] is True
    assert seen["attempt"] == 2
    assert seen["changed_paths"] == ["a.py"]
