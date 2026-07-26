from agent.tool_generation_events import invoke_tool_generation_callback


def test_identity_aware_callback_receives_tool_call_id():
    observed = []

    invoke_tool_generation_callback(
        lambda name, tool_call_id: observed.append((name, tool_call_id)),
        "write_file",
        "call-write-1",
    )

    assert observed == [("write_file", "call-write-1")]


def test_legacy_single_argument_callback_remains_supported():
    observed = []

    invoke_tool_generation_callback(
        lambda name: observed.append(name),
        "write_file",
        "call-write-1",
    )

    assert observed == ["write_file"]
