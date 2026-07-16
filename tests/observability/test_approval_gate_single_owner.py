from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _production_python_files():
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if "tests" in relative.parts or ".venv" in relative.parts:
            continue
        yield path


def test_execution_paths_only_import_unified_pre_tool_seam():
    legacy_importers = []
    unified_importers = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "hermes_cli.plugins":
                continue
            names = {alias.name for alias in node.names}
            if "get_pre_tool_call_block_message" in names:
                legacy_importers.append(path.relative_to(ROOT).as_posix())
            if "resolve_pre_tool_block" in names:
                unified_importers.append(path.relative_to(ROOT).as_posix())
    assert legacy_importers == []
    assert set(unified_importers) == {
        "agent/agent_runtime_helpers.py",
        "agent/tool_executor.py",
        "model_tools.py",
    }


def test_blocking_wait_and_prompt_orchestration_have_one_owner():
    await_callers = []
    prompt_callers = []
    for path in _production_python_files():
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else (
                function.id if isinstance(function, ast.Name) else ""
            )
            if name == "await_gateway_decision":
                await_callers.append(relative)
            if name == "prompt_dangerous_approval":
                prompt_callers.append(relative)
    assert await_callers == ["tools/approval_gate.py"]
    assert prompt_callers == ["tools/approval_gate.py"]


def test_approval_state_module_stays_below_project_file_limit():
    line_count = len((ROOT / "tools/approval.py").read_text(encoding="utf-8").splitlines())
    assert line_count < 2000
