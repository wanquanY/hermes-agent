"""Focused regressions for the absorbed upstream browser safety boundaries."""

import json
import stat
from unittest.mock import patch

from tools import browser_camofox, browser_cdp_tool, browser_tool
from tools.browser_security import (
    evaluate_policy_error,
    expression_targets_private_url,
    risky_evaluate_reason,
)


def test_eval_url_scanner_finds_private_literal():
    blocked = expression_targets_private_url(
        "fetch('http://127.0.0.1:8080/admin')",
        is_blocked_url=lambda url: "127.0.0.1" in url,
    )

    assert blocked == "http://127.0.0.1:8080/admin"


def test_eval_denylist_detects_bracketed_sensitive_primitive():
    assert risky_evaluate_reason('document["cookie"]') == "document.cookie"
    assert (
        risky_evaluate_reason('globalThis["local" + "Storage"].token')
        == "web storage"
    )


def test_eval_denylist_is_opt_in_and_has_explicit_override():
    expression = "document.cookie"
    with patch("hermes_cli.config.read_raw_config", return_value={}):
        assert evaluate_policy_error(expression) is None
    with patch(
        "hermes_cli.config.read_raw_config",
        return_value={"browser": {"restrict_evaluate": True}},
    ):
        assert "document.cookie" in evaluate_policy_error(expression)
    with patch(
        "hermes_cli.config.read_raw_config",
        return_value={
            "browser": {
                "restrict_evaluate": True,
                "allow_unsafe_evaluate": True,
            }
        },
    ):
        assert evaluate_policy_error(expression) is None


def test_native_scroll_stops_before_private_page_action():
    blocked = json.dumps({"success": False, "error": "private page"})
    with patch("tools.browser_tool._dovie_browser_bridge", return_value=None), patch(
        "tools.browser_tool._is_camofox_mode", return_value=False
    ), patch(
        "tools.browser_tool._last_session_key", return_value="task"
    ), patch(
        "tools.browser_tool._blocked_private_page_action", return_value=blocked
    ), patch("tools.browser_tool._run_browser_command") as run_command:
        result = json.loads(browser_tool.browser_scroll("down", task_id="task"))

    assert result == {"success": False, "error": "private page"}
    run_command.assert_not_called()


def test_get_images_stops_before_eval_on_private_page():
    blocked = json.dumps({"success": False, "error": "private page"})
    with patch("tools.browser_tool._is_camofox_mode", return_value=False), patch(
        "tools.browser_tool._last_session_key", return_value="task"
    ), patch(
        "tools.browser_tool._blocked_private_page_action", return_value=blocked
    ), patch("tools.browser_tool._run_browser_command") as run_command:
        result = json.loads(browser_tool.browser_get_images(task_id="task"))

    assert result == {"success": False, "error": "private page"}
    run_command.assert_not_called()


def test_raw_cdp_blocks_sensitive_method_on_private_page():
    with patch(
        "tools.browser_tool._eval_ssrf_guard_active", return_value=True
    ), patch(
        "tools.browser_tool._current_page_private_url",
        return_value="http://127.0.0.1/admin",
    ):
        result = json.loads(
            browser_cdp_tool._browser_cdp_private_guard(
                task_id="task",
                method="Network.getAllCookies",
                params={},
            )
        )

    assert "private/internal" in result["error"]
    assert "Network.getAllCookies" in result["error"]


def test_raw_cdp_guard_runs_before_frame_supervisor_route():
    blocked = json.dumps({"error": "blocked before frame route"})
    with patch(
        "tools.browser_cdp_tool._browser_cdp_private_guard",
        return_value=blocked,
    ), patch("tools.browser_cdp_tool._browser_cdp_via_supervisor") as supervisor:
        result = json.loads(
            browser_cdp_tool.browser_cdp(
                method="DOM.getDocument",
                frame_id="frame-1",
                task_id="task",
            )
        )

    assert result["error"] == "blocked before frame route"
    supervisor.assert_not_called()


def test_camofox_private_page_guard_blocks_before_rest_action():
    session = {"tab_id": "tab-1", "user_id": "user-1"}
    with patch(
        "tools.browser_tool._eval_ssrf_guard_active", return_value=True
    ), patch(
        "tools.browser_tool._camofox_current_page_private_url",
        return_value="http://169.254.169.254/latest/meta-data",
    ):
        result = json.loads(
            browser_camofox._camofox_private_page_block(
                session,
                "task",
                "read a page snapshot",
            )
        )

    assert "private or internal address" in result["error"]


def test_stored_snapshot_is_redacted_and_owner_only(tmp_path):
    fake_key = "sk-STOREDSNAPSHOTSECRET1234567890"
    with patch("hermes_constants.get_hermes_dir", return_value=tmp_path):
        stored = browser_tool._store_full_snapshot(f"token={fake_key}")

    assert stored is not None
    stored_path = tmp_path / stored.rsplit("/", 1)[-1]
    content = stored_path.read_text(encoding="utf-8")
    assert "STOREDSNAPSHOTSECRET" not in content
    assert stat.S_IMODE(stored_path.stat().st_mode) == 0o600
