import os
from unittest.mock import MagicMock, patch

from hermes_cli.session_cwd import restore_session_cwd


def test_restore_session_cwd_updates_process_and_tool_scope(tmp_path):
    with patch("hermes_cli.session_cwd.os.chdir") as chdir:
        environment = {}
        outcome = restore_session_cwd(
            {"cwd": str(tmp_path)},
            environ=environment,
        )

    assert outcome.status == "restored"
    assert outcome.changed is True
    assert environment["TERMINAL_CWD"] == str(tmp_path)
    chdir.assert_called_once_with(str(tmp_path))


def test_restore_session_cwd_missing_directory_is_non_destructive(tmp_path):
    missing = tmp_path / "moved-repository"
    environment = {"TERMINAL_CWD": "/keep/current"}

    with patch("hermes_cli.session_cwd.os.chdir") as chdir:
        outcome = restore_session_cwd(
            {"cwd": str(missing)},
            environ=environment,
        )

    assert outcome.status == "missing"
    assert environment["TERMINAL_CWD"] == "/keep/current"
    chdir.assert_not_called()


def test_restore_session_cwd_without_recorded_path_is_noop():
    with patch("hermes_cli.session_cwd.os.chdir") as chdir:
        outcome = restore_session_cwd({}, environ=os.environ.copy())

    assert outcome.status == "unrecorded"
    chdir.assert_not_called()


def test_mid_chat_resume_restores_recorded_cwd(tmp_path):
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "current"
    cli.agent = None
    cli._resumed = False
    cli._pending_title = None
    cli.conversation_history = []
    cli._console_print = MagicMock()
    cli._session_db = MagicMock()
    cli._session_db.sessions.get.return_value = {
        "id": "target",
        "title": "Target",
        "cwd": str(tmp_path),
    }
    cli._session_db.sessions.resolve_resume_id.return_value = "target"
    cli._session_db.messages.all_as_conversation.return_value = [
        {"role": "user", "content": "continue"}
    ]

    with (
        patch("hermes_cli.main._resolve_session_by_name_or_id", return_value="target"),
        patch("cli._cprint"),
        patch("cli._active_worktree", None),
        patch("hermes_cli.session_cwd.os.chdir") as chdir,
        patch.dict(os.environ, {}, clear=False),
    ):
        cli._handle_resume_command("/resume Target")
        assert os.environ["TERMINAL_CWD"] == str(tmp_path)

    chdir.assert_called_once_with(str(tmp_path))
