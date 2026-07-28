import logging

from agent.dovie_diagnostics import emit_dovie_diagnostic, emit_dovie_runtime_diagnostic


def test_dovie_diagnostics_use_native_logging(caplog):
    caplog.set_level(logging.DEBUG, logger="hermes.dovie_diagnostics")

    emit_dovie_diagnostic("[team-test]", {"ok": True})
    emit_dovie_runtime_diagnostic("team-runtime", "stage-a", {"count": 1})

    messages = [record.getMessage() for record in caplog.records]
    assert "[team-test] {'ok': True}" in messages
    assert "team-runtime stage-a {'count': 1}" in messages
