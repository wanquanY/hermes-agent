from tools.registry import ToolRegistry


def _handler(module_name: str, result: str = "plugin"):
    namespace = {"__name__": module_name, "result": result}
    exec("def handler(args, **kwargs):\n    return result\n", namespace)
    return namespace["handler"]


def _register_core(registry: ToolRegistry, name: str = "write_file"):
    registry.register(
        name=name,
        toolset="core",
        schema={"name": name, "parameters": {"type": "object", "properties": {}}},
        handler=_handler("tools.core", "core"),
    )


def _policy(registry: ToolRegistry, *, capability: bool, opt_in: bool):
    registry.register_plugin_override_policy(
        "hermes_plugins.third_party",
        plugin_id="third-party",
        capability_declared=capability,
        operator_opt_in=opt_in,
    )


def test_override_requires_capability_and_operator_opt_in():
    for capability, opt_in in ((False, False), (True, False), (False, True)):
        registry = ToolRegistry()
        _register_core(registry)
        _policy(registry, capability=capability, opt_in=opt_in)
        try:
            registry.register(
                name="write_file",
                toolset="plugin",
                schema={"name": "write_file", "parameters": {}},
                handler=_handler("hermes_plugins.third_party.handlers"),
                override=True,
            )
        except PermissionError:
            pass
        else:
            raise AssertionError((capability, opt_in))
        assert registry._tools["write_file"].toolset == "core"


def test_durable_policy_allows_delayed_handler_when_both_grants_exist():
    registry = ToolRegistry()
    _register_core(registry)
    _policy(registry, capability=True, opt_in=True)
    assert registry.register(
        name="write_file",
        toolset="plugin",
        schema={"name": "write_file", "parameters": {}},
        handler=_handler("hermes_plugins.third_party.delayed"),
        override=True,
    ) is True
    assert registry._tools["write_file"].toolset == "plugin"


def test_plugin_cannot_deregister_core_then_register_plain_name():
    registry = ToolRegistry()
    _register_core(registry)
    _policy(registry, capability=False, opt_in=True)
    namespace = {"__name__": "hermes_plugins.third_party.cleanup"}
    exec("def remove(registry):\n    registry.deregister('write_file')\n", namespace)
    try:
        namespace["remove"](registry)
    except PermissionError:
        pass
    else:
        raise AssertionError("cross-owner deregistration must fail closed")
    assert "write_file" in registry._tools


def test_mcp_refresh_remains_exempt_from_plugin_override_policy():
    registry = ToolRegistry()
    registry.register(
        name="remote_tool",
        toolset="mcp-server",
        schema={"name": "remote_tool", "parameters": {}},
        handler=_handler("tools.mcp_tool"),
    )
    namespace = {"__name__": "hermes_plugins.third_party.cleanup"}
    exec("def remove(registry):\n    registry.deregister('remote_tool')\n", namespace)
    namespace["remove"](registry)
    assert "remote_tool" not in registry._tools
