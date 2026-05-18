from unittest.mock import Mock, patch
import json


HOST = "example-host"
PORT = 9223
WS_URL = f"ws://{HOST}:{PORT}/devtools/browser/abc123"
HTTP_URL = f"http://{HOST}:{PORT}"
VERSION_URL = f"{HTTP_URL}/json/version"


class TestResolveCdpOverride:
    def test_keeps_full_devtools_websocket_url(self):
        from tools.browser_tool import _resolve_cdp_override

        assert _resolve_cdp_override(WS_URL) == WS_URL

    def test_resolves_http_discovery_endpoint_to_websocket(self):
        from tools.browser_tool import _resolve_cdp_override

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            resolved = _resolve_cdp_override(HTTP_URL)

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)

    def test_resolves_bare_ws_hostport_to_discovery_websocket(self):
        from tools.browser_tool import _resolve_cdp_override

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            resolved = _resolve_cdp_override(f"ws://{HOST}:{PORT}")

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)

    def test_falls_back_to_raw_url_when_discovery_fails(self):
        from tools.browser_tool import _resolve_cdp_override

        with patch("tools.browser_tool.requests.get", side_effect=RuntimeError("boom")):
            assert _resolve_cdp_override(HTTP_URL) == HTTP_URL

    def test_normalizes_provider_returned_http_cdp_url_when_creating_session(self, monkeypatch):
        import tools.browser_tool as browser_tool

        provider = Mock()
        provider.create_session.return_value = {
            "session_name": "cloud-session",
            "bb_session_id": "bu_123",
            "cdp_url": "https://cdp.browser-use.example/session",
            "features": {"browser_use": True},
        }

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        monkeypatch.setattr(browser_tool, "_active_sessions", {})
        monkeypatch.setattr(browser_tool, "_session_last_activity", {})
        monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
        monkeypatch.setattr(browser_tool, "_update_session_activity", lambda task_id: None)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "")
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)

        with patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            session_info = browser_tool._get_session_info("task-browser-use")

        assert session_info["cdp_url"] == WS_URL
        provider.create_session.assert_called_once_with("task-browser-use")
        mock_get.assert_called_once_with(
            "https://cdp.browser-use.example/session/json/version",
            timeout=10,
        )


class TestGetCdpOverride:
    def test_prefers_env_var_over_config(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.setenv("BROWSER_CDP_URL", HTTP_URL)
        monkeypatch.setattr(
            browser_tool,
            "read_raw_config",
            lambda: {"browser": {"cdp_url": "http://config-host:9222"}},
            raising=False,
        )

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            resolved = browser_tool._get_cdp_override()

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)


class TestNativeCdpTabs:
    def test_browser_tabs_lists_page_targets_and_marks_active(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: WS_URL)
        monkeypatch.setattr(browser_tool, "_active_cdp_targets", {"task": "target-2"})
        monkeypatch.setattr(browser_tool, "_last_active_session_key", {})
        monkeypatch.setattr(browser_tool, "_cdp_browser_call", lambda method, params=None, **kwargs: {
            "targetInfos": [
                {"targetId": "target-1", "type": "page", "title": "One", "url": "https://one.example"},
                {"targetId": "target-2", "type": "page", "title": "Two", "url": "https://two.example"},
                {"targetId": "devtools", "type": "page", "title": "DevTools", "url": "devtools://devtools"},
            ],
        })

        payload = json.loads(browser_tool.browser_tabs(task_id="task"))

        assert payload["success"] is True
        assert payload["active_tab_id"] == "target-2"
        assert [tab["tab_id"] for tab in payload["tabs"]] == ["target-1", "target-2"]
        assert [tab["active"] for tab in payload["tabs"]] == [False, True]

    def test_browser_new_tab_creates_blank_target_then_navigates(self, monkeypatch):
        import tools.browser_tool as browser_tool

        calls = []

        def fake_cdp(method, params=None, **kwargs):
            calls.append((method, params or {}))
            if method == "Target.createTarget":
                return {"targetId": "target-new"}
            if method == "Target.activateTarget":
                return {}
            raise AssertionError(method)

        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: WS_URL)
        monkeypatch.setattr(browser_tool, "_active_cdp_targets", {})
        monkeypatch.setattr(browser_tool, "_last_active_session_key", {})
        monkeypatch.setattr(browser_tool, "_cdp_browser_call", fake_cdp)
        monkeypatch.setattr(
            browser_tool,
            "browser_navigate",
            lambda url, task_id=None: json.dumps({"success": True, "url": url, "title": "Example"}),
        )

        payload = json.loads(browser_tool.browser_new_tab("https://example.com", task_id="task"))

        assert payload["success"] is True
        assert payload["tab_id"] == "target-new"
        assert payload["url"] == "https://example.com"
        assert calls == [
            ("Target.createTarget", {"url": "about:blank"}),
            ("Target.activateTarget", {"targetId": "target-new"}),
        ]
        assert browser_tool._active_cdp_targets["task"] == "target-new"

    def test_browser_select_tab_activates_target(self, monkeypatch):
        import tools.browser_tool as browser_tool

        calls = []

        def fake_cdp(method, params=None, **kwargs):
            calls.append((method, params or {}))
            if method == "Target.getTargets":
                return {"targetInfos": [
                    {"targetId": "target-1", "type": "page", "title": "One", "url": "https://one.example"},
                ]}
            if method == "Target.activateTarget":
                return {}
            raise AssertionError(method)

        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: WS_URL)
        monkeypatch.setattr(browser_tool, "_active_cdp_targets", {})
        monkeypatch.setattr(browser_tool, "_last_active_session_key", {})
        monkeypatch.setattr(browser_tool, "_cdp_browser_call", fake_cdp)
        monkeypatch.setattr(
            browser_tool,
            "browser_snapshot",
            lambda full=False, task_id=None: json.dumps({"success": True, "snapshot": "page", "element_count": 3}),
        )

        payload = json.loads(browser_tool.browser_select_tab("target-1", task_id="task"))

        assert payload["success"] is True
        assert payload["active_tab_id"] == "target-1"
        assert payload["snapshot"] == "page"
        assert ("Target.activateTarget", {"targetId": "target-1"}) in calls

    def test_browser_close_tab_closes_and_activates_next_target(self, monkeypatch):
        import tools.browser_tool as browser_tool

        calls = []
        listed = {"count": 0}

        def fake_cdp(method, params=None, **kwargs):
            calls.append((method, params or {}))
            if method == "Target.getTargets":
                listed["count"] += 1
                if listed["count"] == 1:
                    return {"targetInfos": [
                        {"targetId": "target-1", "type": "page", "title": "One", "url": "https://one.example"},
                        {"targetId": "target-2", "type": "page", "title": "Two", "url": "https://two.example"},
                    ]}
                return {"targetInfos": [
                    {"targetId": "target-2", "type": "page", "title": "Two", "url": "https://two.example"},
                ]}
            if method == "Target.closeTarget":
                return {"success": True}
            if method == "Target.activateTarget":
                return {}
            raise AssertionError(method)

        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: WS_URL)
        monkeypatch.setattr(browser_tool, "_active_cdp_targets", {"task": "target-1"})
        monkeypatch.setattr(browser_tool, "_last_active_session_key", {})
        monkeypatch.setattr(browser_tool, "_cdp_browser_call", fake_cdp)

        payload = json.loads(browser_tool.browser_close_tab("target-1", task_id="task"))

        assert payload["success"] is True
        assert payload["closed_tab_id"] == "target-1"
        assert payload["active_tab_id"] == "target-2"
        assert ("Target.closeTarget", {"targetId": "target-1"}) in calls
        assert ("Target.activateTarget", {"targetId": "target-2"}) in calls

    def test_uses_config_browser_cdp_url_when_env_missing(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("hermes_cli.config.read_raw_config", return_value={"browser": {"cdp_url": HTTP_URL}}), \
             patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            resolved = browser_tool._get_cdp_override()

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)
