"""Regression tests for TUI approval-prompt credential redaction."""

import inspect


class TestTuiApprovalEmitRedaction:
    def test_emit_approval_request_redacts_command_in_payload(self, monkeypatch):
        from tui_gateway import server as tui_server

        emitted = {}
        monkeypatch.setattr(
            tui_server,
            "_emit",
            lambda event, sid, payload=None: emitted.update(
                {"event": event, "sid": sid, "payload": payload}
            ),
        )
        fake_pat = "ghp_" + "X" * 36
        raw = f"curl -H 'Authorization: token {fake_pat}' https://api.github.com"
        tui_server._emit_approval_request("sess-1", {"command": raw, "description": "x"})

        assert emitted["event"] == "approval.request"
        assert fake_pat not in emitted["payload"]["command"]
        assert emitted["payload"]["description"] == "x"
        assert "github.com" in emitted["payload"]["command"]

    def test_emit_approval_request_handles_missing_command(self, monkeypatch):
        from tui_gateway import server as tui_server

        emitted = {}
        monkeypatch.setattr(
            tui_server,
            "_emit",
            lambda event, sid, payload=None: emitted.update({"payload": payload}),
        )
        tui_server._emit_approval_request("s", {"description": "no command here"})
        assert emitted["payload"] == {"description": "no command here"}
        tui_server._emit_approval_request("s", None)
        assert emitted["payload"] == {}

    def test_no_raw_command_emit_in_approval_registrations(self):
        from tui_gateway import server as tui_server

        src = inspect.getsource(tui_server)
        raw_emits = src.count('_emit("approval.request"')
        assert raw_emits == 1, (
            f'expected exactly 1 raw _emit("approval.request") inside the '
            f"redacting helper, found {raw_emits}"
        )
        assert "_emit_approval_request(sid, data)" in src
