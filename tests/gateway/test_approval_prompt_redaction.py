"""Regression tests for approval prompt credential redaction."""

from gateway.run import _redact_approval_command

_FAKE_GHP = "ghp_" + "X" * 36
_FAKE_OPENAI = "sk-proj-" + "X" * 40
_FAKE_JWT = "eyJ" + "X" * 20 + "." + "eyJ" + "X" * 24 + "." + "X" * 30


class TestRedactApprovalCommand:
    def test_redacts_github_pat(self):
        raw = "curl -H 'Authorization: token " + _FAKE_GHP + "' https://api.github.com/user"
        out = _redact_approval_command(raw)
        assert _FAKE_GHP not in out
        assert "curl" in out
        assert "github.com" in out

    def test_redacts_openai_key(self):
        raw = "export OPENAI_API_KEY=" + _FAKE_OPENAI + " && python s.py"
        out = _redact_approval_command(raw)
        assert _FAKE_OPENAI not in out
        assert "python s.py" in out

    def test_redacts_bearer_token(self):
        raw = "curl -H 'Authorization: Bearer " + _FAKE_JWT + "' https://api.example.com"
        out = _redact_approval_command(raw)
        assert _FAKE_JWT not in out

    def test_clean_command_passes_through_unchanged(self):
        raw = "ls -la /tmp && echo hello"
        assert _redact_approval_command(raw) == raw

    def test_forces_redaction_even_when_disabled(self, monkeypatch):
        raw = "curl -H 'Authorization: token " + _FAKE_GHP + "' https://api.github.com"
        monkeypatch.setattr("agent.redact._REDACT_ENABLED", False, raising=False)
        out = _redact_approval_command(raw)
        assert _FAKE_GHP not in out

    def test_handles_none_and_empty(self):
        assert _redact_approval_command("") == ""
        assert _redact_approval_command(None) == ""
