from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

METHOD_SCAN_DIRS = (
    "tui_gateway/methods",
    "hermes_team_mission/gateway",
)

# Frozen legacy omissions: these methods are registered on the JSON-RPC gateway
# but were not part of the Dovie manifest surface at the M-3 follow-up audit.
# New @method registrations must be added to REQUIRED_GATEWAY_METHODS instead of this set.
EXPLICITLY_OMITTED_FROM_MANIFEST = {
    "agents.list",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "artifacts.prune",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "billing.auto_reload",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "billing.charge",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "billing.charge_status",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "billing.state",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "billing.step_up",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "browser.manage",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "cli.exec",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "clipboard.paste",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "command.dispatch",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "command.resolve",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "commands.catalog",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "complete.path",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "complete.slash",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "config.get",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "config.set",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "config.show",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "credits.view",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "delegation.pause",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "delegation.status",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "events.compact",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "events.prune",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "handoff.fail",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "handoff.request",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "handoff.state",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "image.attach",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "input.detect_drop",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "insights.get",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "model.disconnect",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "model.save_key",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "paste.collapse",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "platforms.manage",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "plugins.list",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "process.kill",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "process.list",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "process.stop",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "prompt.background",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "reload.env",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "reload.mcp",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "rollback.diff",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "rollback.list",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "rollback.restore",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "run.fail",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "run.list",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.activate",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.active_list",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.close",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.compress",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.cwd.set",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.history",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.index.list",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.interrupt",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.most_recent",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.recall_turn",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.resume",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.save",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.steer",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "session.undo",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "setup.runtime_check",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "setup.status",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "shell.exec",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "skills.list",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "skills.reload",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "slash.exec",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "spawn_tree.list",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "spawn_tree.load",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "spawn_tree.save",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "subagent.events.list",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "subagent.interrupt",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "subagent.runs.list",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "team_mission.conversation.recall_turn",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "team_mission.plan.reject",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "terminal.resize",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "tools.configure",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "tools.list",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "tools.prepare",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "tools.show",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "voice.record",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "voice.toggle",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "voice.tts",  # audited: legacy methods not yet declared in manifest, see issue/audit M-3 follow-up.
    "worker.dispatch_agent_async",  # audited: worker-to-main IPC, not a Dovie gateway manifest method.
    "worker.dispatch_team_async",  # audited: worker-to-main IPC, not a Dovie gateway manifest method.
}


def _manifest_ast() -> ast.Module:
    path = REPO_ROOT / "dovie_extension/manifest.py"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _static_assignment(name: str) -> ast.AST:
    for node in _manifest_ast().body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return node.value
    raise AssertionError(f"{name} not found in dovie_extension/manifest.py")


def _literal_string_collection(value: ast.AST, name: str) -> set[str]:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError) as exc:
        raise AssertionError(f"{name} must be a static literal") from exc

    if not isinstance(parsed, (list, tuple, set, dict)):
        raise AssertionError(f"{name} must be a static string collection")

    entries = parsed.keys() if isinstance(parsed, dict) else parsed
    methods: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str):
            raise AssertionError(f"{name} entries must be string literals")
        methods.add(entry)
    return methods


def parse_gateway_method_keys(path: str) -> set[str]:
    if path != "dovie_extension/manifest.py":
        raise AssertionError(f"unexpected manifest path: {path}")
    return _literal_string_collection(
        _static_assignment("REQUIRED_GATEWAY_METHODS"),
        "REQUIRED_GATEWAY_METHODS",
    )


def parse_required_methods(path: str) -> set[str]:
    if path != "dovie_extension/manifest.py":
        raise AssertionError(f"unexpected manifest path: {path}")
    return _literal_string_collection(_static_assignment("REQUIRED_METHODS"), "REQUIRED_METHODS")


def _method_decorator_name(decorator: ast.AST) -> str | None:
    if not isinstance(decorator, ast.Call):
        return None

    func = decorator.func
    if isinstance(func, ast.Name):
        is_method_decorator = func.id == "method"
    elif isinstance(func, ast.Attribute):
        is_method_decorator = func.attr == "method"
    else:
        is_method_decorator = False

    if not is_method_decorator:
        return None
    if not decorator.args:
        raise AssertionError("@method decorators must pass a static method name")

    method_name = decorator.args[0]
    if not isinstance(method_name, ast.Constant) or not isinstance(method_name.value, str):
        raise AssertionError("@method decorators must pass a static string literal")
    return method_name.value


def collect_method_decorators_recursively(paths: list[str]) -> set[str]:
    methods: set[str] = set()
    for dirname in paths:
        root = REPO_ROOT / dirname
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in node.decorator_list:
                    method_name = _method_decorator_name(decorator)
                    if method_name is not None:
                        methods.add(method_name)
    return methods


def test_all_registered_methods_declared_in_manifest() -> None:
    """Static AST scan: every @method belongs to the declared gateway ABI or omissions."""
    registered = collect_method_decorators_recursively(list(METHOD_SCAN_DIRS))
    manifest_keys = parse_gateway_method_keys("dovie_extension/manifest.py")

    gap = registered - manifest_keys - EXPLICITLY_OMITTED_FROM_MANIFEST
    assert not gap, (
        "Methods registered via @method but missing from REQUIRED_GATEWAY_METHODS:\n"
        + "\n".join(f"- {method}" for method in sorted(gap))
    )


def test_gateway_capabilities_exposes_declared_gateway_abi() -> None:
    """The runtime capability payload must expose the static gateway ABI verbatim."""
    from dovie_extension.manifest import gateway_capabilities

    manifest_keys = parse_gateway_method_keys("dovie_extension/manifest.py")
    assert set(gateway_capabilities()["methods"]) == manifest_keys


def test_required_methods_subset_of_gateway_methods() -> None:
    """REQUIRED_METHODS must be a subset of the declared gateway ABI."""
    required = parse_required_methods("dovie_extension/manifest.py")
    manifest_keys = parse_gateway_method_keys("dovie_extension/manifest.py")

    missing = required - manifest_keys
    assert not missing, (
        "REQUIRED_METHODS entries not in REQUIRED_GATEWAY_METHODS:\n"
        + "\n".join(f"- {method}" for method in sorted(missing))
    )
