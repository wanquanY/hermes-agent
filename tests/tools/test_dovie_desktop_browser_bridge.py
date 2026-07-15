import json


def test_dovie_browser_bridge_uses_desktop_browser_commands(monkeypatch):
    from dovie_extension import browser_bridge

    calls = []

    def fake_call(command, payload=None, timeout=30.0):
        calls.append((command, payload, timeout))
        if command == "browser_use_observe_embedded":
            return {
                "title": "Example",
                "url": "https://example.com/",
                "elements": [{"ref": "e1", "tagName": "a", "text": "More"}],
            }
        return {"browserSessionId": "browser:electron:default"}

    monkeypatch.setenv("DOVIE_BACKEND_BRIDGE_URL", "http://127.0.0.1:1/api/dovie/invoke")
    monkeypatch.setenv("DOVIE_BACKEND_BRIDGE_TOKEN", "token")
    monkeypatch.setattr(browser_bridge, "call", fake_call)

    browser_bridge.navigate("https://example.com/")
    observation = browser_bridge.observe(max_nodes=25)
    browser_bridge.action("click", ref="@e1")
    browser_bridge.go_back()

    assert calls[0][0] == "browser_use_navigate"
    assert calls[0][1]["request"]["browserSessionId"] == "browser:electron:default"
    assert calls[0][1]["request"]["url"] == "https://example.com/"
    assert calls[1][0] == "browser_use_observe_embedded"
    assert calls[1][1]["request"]["maxNodes"] == 25
    assert calls[2][0] == "browser_use_action_embedded"
    assert calls[2][1]["request"]["action"] == "click"
    assert calls[2][1]["request"]["ref"] == "e1"
    assert calls[3][0] == "browser_use_go_back"

    snapshot = browser_bridge.snapshot_payload_from_observation(observation)
    assert snapshot["provider"] == "dovie_desktop"
    assert "@e1 <a> - More" in snapshot["snapshot"]


def test_dovie_browser_bridge_prefers_session_context(monkeypatch):
    from dovie_extension import browser_bridge
    from channels import session_context

    monkeypatch.setenv("DOVIE_BROWSER_SESSION_ID", "browser:electron:global")
    tokens = session_context.set_session_vars(
        session_key="session-a",
        dovie_browser_session_id="browser:hermes:session-a",
    )
    try:
        assert browser_bridge.browser_session_id() == "browser:hermes:session-a"
    finally:
        session_context.clear_session_vars(tokens)
        for var in session_context._VAR_MAP.values():
            var.set(session_context._UNSET)


def test_dovie_browser_session_id_for_gateway_session_is_stable():
    from dovie_extension import browser_bridge

    assert browser_bridge.browser_session_id_for_gateway_session("session-1") == "browser:hermes:session-1"
    assert browser_bridge.browser_session_id_for_gateway_session(" session/中文 key ") == "browser:hermes:session-key"


def test_browser_navigate_prefers_dovie_desktop_bridge(monkeypatch):
    from dovie_extension import browser_bridge
    from tools import browser_tool

    monkeypatch.setattr(browser_bridge, "available", lambda: True)
    monkeypatch.setattr(browser_bridge, "browser_session_id", lambda: "browser:electron:default")
    monkeypatch.setattr(
        browser_bridge,
        "navigate",
        lambda url: {
            "activeTabId": "tab-1",
            "tabs": [{"tabId": "tab-1", "url": url, "title": "Example"}],
        },
    )
    monkeypatch.setattr(
        browser_bridge,
        "observe",
        lambda max_nodes=200: {
            "title": "Example",
            "url": "https://example.com/",
            "elements": [{"ref": "e1", "tagName": "button", "text": "Submit"}],
        },
    )
    monkeypatch.setattr(browser_tool, "_run_browser_command", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("agent-browser should not run")))

    payload = json.loads(browser_tool.browser_navigate("https://example.com/", task_id="task-1"))

    assert payload["success"] is True
    assert payload["provider"] == "dovie_desktop"
    assert payload["browser_session_id"] == "browser:electron:default"
    assert payload["url"] == "https://example.com/"
    assert "@e1 <button> - Submit" in payload["snapshot"]


def test_browser_actions_prefer_dovie_desktop_bridge(monkeypatch):
    from dovie_extension import browser_bridge
    from tools import browser_tool

    calls = []

    monkeypatch.setattr(browser_bridge, "available", lambda: True)
    monkeypatch.setattr(browser_bridge, "action", lambda action_name, **kwargs: calls.append((action_name, kwargs)) or {"ok": True})
    monkeypatch.setattr(browser_tool, "_run_browser_command", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("agent-browser should not run")))

    click_payload = json.loads(browser_tool.browser_click("e1"))
    type_payload = json.loads(browser_tool.browser_type("@e2", "hello"))
    scroll_payload = json.loads(browser_tool.browser_scroll("down"))
    press_payload = json.loads(browser_tool.browser_press("Enter"))

    assert click_payload["provider"] == "dovie_desktop"
    assert type_payload["provider"] == "dovie_desktop"
    assert scroll_payload["provider"] == "dovie_desktop"
    assert press_payload["provider"] == "dovie_desktop"
    assert calls == [
        ("click", {"ref": "@e1"}),
        ("fill", {"ref": "@e2", "text": "hello"}),
        ("scroll", {"delta_y": 500}),
        ("press", {"key": "Enter"}),
    ]
