from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

WORKER_SIDE_DIRS = (
    "agent",
    "tools",
    "hermes_team_mission/runtime",
    "hermes_team_mission/state",
    "tui_gateway/services",
)

EXCLUDED_FILENAMES = {
    "run_worker_host.py",
    "worker_db_proxy.py",
    "worker_supervisor.py",
}

IGNORED_DB_METHOD_NAMES = {
    "close",
    "db_path",
    "get",
}

# Existing worker IPC surface kept intentionally even though no current worker
# code path calls these methods through a statically visible DB handle.
EXPLICITLY_ALLOWED_WITHOUT_STATIC_WORKER_CALL = {
    "create_activity",
    "get_activity_for_mission",
    "get_session_index",
    "get_unread_completion_count",
    "list_active_mission_activities",
    "list_activities",
    "list_unread_completions",
    "update_session_cwd",
    "update_session_meta",
    "update_session_model",
    "upsert_session",
}


@dataclass(frozen=True)
class DBCallSite:
    method: str
    path: Path
    line: int

    @property
    def display(self) -> str:
        return f"{self.path.relative_to(REPO_ROOT)}:{self.line}"


def _is_db_handle_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "db" or node.id.endswith("_db")
    if isinstance(node, ast.Attribute):
        return node.attr in {"_db", "_session_db", "session_db"}
    return False


def _is_public_db_method_name(name: str) -> bool:
    return bool(name) and not name.startswith("_") and name not in IGNORED_DB_METHOD_NAMES


class _WorkerDBCallVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.call_sites: list[DBCallSite] = []

    def visit_Call(self, node: ast.Call) -> None:
        self._record_direct_db_method_call(node)
        self._record_getattr_db_method_call(node)
        self._record_run_control_db_method_helper_call(node)
        self.generic_visit(node)

    def _record_direct_db_method_call(self, node: ast.Call) -> None:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        if not _is_db_handle_expr(func.value):
            return
        if _is_public_db_method_name(func.attr):
            self.call_sites.append(DBCallSite(func.attr, self.path, node.lineno))

    def _record_getattr_db_method_call(self, node: ast.Call) -> None:
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "getattr":
            return
        if len(node.args) < 2 or not _is_db_handle_expr(node.args[0]):
            return
        name_arg = node.args[1]
        if not isinstance(name_arg, ast.Constant) or not isinstance(name_arg.value, str):
            return
        if _is_public_db_method_name(name_arg.value):
            self.call_sites.append(DBCallSite(name_arg.value, self.path, node.lineno))

    def _record_run_control_db_method_helper_call(self, node: ast.Call) -> None:
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "_db_method":
            return
        if len(node.args) < 2 or not _is_db_handle_expr(node.args[0]):
            return
        name_arg = node.args[1]
        if not isinstance(name_arg, ast.Constant) or not isinstance(name_arg.value, str):
            return
        if _is_public_db_method_name(name_arg.value):
            self.call_sites.append(DBCallSite(name_arg.value, self.path, node.lineno))


def _python_files_to_scan() -> list[Path]:
    files: list[Path] = []
    for dirname in WORKER_SIDE_DIRS:
        for path in (REPO_ROOT / dirname).rglob("*.py"):
            if path.name in EXCLUDED_FILENAMES:
                continue
            if path.name.startswith("test_") or "tests" in path.parts:
                continue
            files.append(path)
    return sorted(files)


def _scan_worker_side_db_calls() -> dict[str, list[DBCallSite]]:
    call_sites_by_method: dict[str, list[DBCallSite]] = {}
    for path in _python_files_to_scan():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _WorkerDBCallVisitor(path)
        visitor.visit(tree)
        for call_site in visitor.call_sites:
            call_sites_by_method.setdefault(call_site.method, []).append(call_site)
    return call_sites_by_method


def _worker_db_proxy_whitelist() -> set[str]:
    supervisor_path = REPO_ROOT / "tui_gateway/services/worker_supervisor.py"
    tree = ast.parse(supervisor_path.read_text(encoding="utf-8"), filename=str(supervisor_path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "DB_RPC_ALLOWED_METHODS" for target in node.targets):
            continue
        value = node.value
        if not isinstance(value, ast.Call) or not value.args:
            raise AssertionError("DB_RPC_ALLOWED_METHODS must be a frozenset literal")
        literal = value.args[0]
        if not isinstance(literal, (ast.Set, ast.List, ast.Tuple)):
            raise AssertionError("DB_RPC_ALLOWED_METHODS must contain a static string literal collection")
        methods: set[str] = set()
        for entry in literal.elts:
            if not isinstance(entry, ast.Constant) or not isinstance(entry.value, str):
                raise AssertionError("DB_RPC_ALLOWED_METHODS entries must be string literals")
            methods.add(entry.value)
        return methods
    raise AssertionError("DB_RPC_ALLOWED_METHODS not found")


def test_all_worker_side_db_method_calls_are_whitelisted() -> None:
    whitelist = _worker_db_proxy_whitelist()
    call_sites_by_method = _scan_worker_side_db_calls()

    missing = sorted(set(call_sites_by_method) - whitelist)
    assert not missing, (
        "Worker-side DB method calls missing from DB_RPC_ALLOWED_METHODS:\n"
        + "\n".join(
            f"- {method}: "
            + ", ".join(site.display for site in call_sites_by_method[method][:8])
            for method in missing
        )
    )


def test_whitelist_has_no_dead_entries() -> None:
    whitelist = _worker_db_proxy_whitelist()
    call_sites_by_method = _scan_worker_side_db_calls()

    dead_entries = sorted(
        whitelist
        - set(call_sites_by_method)
        - EXPLICITLY_ALLOWED_WITHOUT_STATIC_WORKER_CALL
    )
    assert not dead_entries, (
        "DB_RPC_ALLOWED_METHODS entries have no static worker-side call site:\n"
        + "\n".join(f"- {method}" for method in dead_entries)
    )
