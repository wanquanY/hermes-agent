from tui_gateway import ws


def test_workspace_current_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "workspace.current", "params": {"session_id": "s"}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_session_list_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "session.list", "params": {}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_run_status_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "run.status", "params": {"stored_session_id": "s"}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_events_unsubscribe_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "events.unsubscribe", "params": {"subscription_id": "sub"}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001
