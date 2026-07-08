#!/usr/bin/env python3
"""Hermes zero-debt phase verdict.

P0 intentionally validates the execution contract, not the final debt-free
state. Later phases can promote current warnings into blocking failures.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aggregate_table_owners import find_shadow_table_writers, format_shadow_table_writers


REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "docs" / "hermes_zero_debt_manifest.md"
EXECUTION_PLAN = REPO_ROOT / "docs" / "hermes_zero_debt_execution_plan.md"
VERDICT_SCRIPT = REPO_ROOT / "scripts" / "zero_debt" / "verdict.py"
PHASE_CLOSURE_SCRIPT = REPO_ROOT / "scripts" / "zero_debt" / "phase_closure.py"
GATE_TEST = REPO_ROOT / "tests" / "observability" / "test_zero_debt_gates.py"
P1_BOUNDARY_TEST = REPO_ROOT / "tests" / "observability" / "test_channels_p1_boundary.py"

_SCAN_EXCLUDED_DIRS = {
    ".venv",
    "__pycache__",
    ".import_linter_cache",
    "tests",
    "docs",
}

_P1_LEGACY_PATHS = [
    "gateway/platforms",
    "gateway/slash_commands.py",
    "gateway/slash_access.py",
    "gateway/platform_registry.py",
    "gateway/session_context.py",
    "gateway/status.py",
    "gateway/sticker_cache.py",
    "gateway/whatsapp_identity.py",
]

_P1_TARGET_PATHS = [
    "channels/__init__.py",
    "channels/config.py",
    "channels/platform_registry.py",
    "channels/rich_sent_store.py",
    "channels/runtime_status.py",
    "channels/session_context.py",
    "channels/session_identity.py",
    "channels/sticker_cache.py",
    "channels/whatsapp_identity.py",
    "channels/slash_commands/__init__.py",
    "channels/slash_commands/access.py",
    "channels/slash_commands/confirmation.py",
    "channels/slash_commands/handlers.py",
    "channels/platforms/__init__.py",
    "channels/platforms/base.py",
    "channels/platforms/base_delivery.py",
    "channels/platforms/base_media_cache.py",
    "channels/platforms/base_models.py",
    "channels/platforms/base_network.py",
    "channels/platforms/base_text.py",
    "channels/platforms/telegram.py",
    "channels/platforms/telegram_callbacks.py",
    "channels/platforms/telegram_connection.py",
    "channels/platforms/telegram_delivery.py",
    "channels/platforms/telegram_inbound.py",
    "channels/platforms/discord.py",
    "channels/platforms/discord_command_sync.py",
    "channels/platforms/discord_context.py",
    "channels/platforms/discord_delivery.py",
    "channels/platforms/discord_inbound.py",
    "channels/platforms/discord_slash.py",
    "channels/platforms/discord_voice.py",
    "channels/platforms/slack.py",
    "channels/platforms/slack_blocks.py",
    "channels/platforms/slack_inbound.py",
    "channels/platforms/slack_support.py",
    "channels/platforms/feishu.py",
    "channels/platforms/feishu_content.py",
    "channels/platforms/feishu_inbound_state.py",
    "channels/platforms/feishu_message.py",
    "channels/platforms/feishu_onboard.py",
    "channels/platforms/feishu_outbound.py",
    "channels/platforms/feishu_webhook.py",
    "channels/platforms/qqbot/adapter.py",
    "channels/platforms/qqbot/media.py",
    "channels/platforms/weixin.py",
    "channels/platforms/weixin_formatting.py",
    "channels/platforms/wecom.py",
    "channels/platforms/whatsapp.py",
    "channels/platforms/signal.py",
    "channels/platforms/sms.py",
    "channels/platforms/email.py",
    "channels/platforms/dingtalk.py",
    "channels/platforms/homeassistant.py",
    "channels/platforms/mattermost.py",
    "channels/platforms/matrix.py",
    "channels/platforms/matrix_crypto.py",
    "channels/platforms/matrix_formatting.py",
    "channels/platforms/matrix_reactions.py",
    "channels/platforms/matrix_room_ops.py",
    "channels/platforms/matrix_support.py",
    "channels/platforms/bluebubbles.py",
    "channels/platforms/webhook.py",
    "channels/platforms/api_server.py",
    "channels/platforms/api_server_jobs.py",
    "channels/platforms/api_server_responses.py",
    "channels/platforms/api_server_runs.py",
    "channels/platforms/api_server_sessions.py",
    "channels/platforms/api_server_support.py",
    "channels/platforms/yuanbao.py",
    "channels/platforms/yuanbao_auth.py",
    "channels/platforms/yuanbao_constants.py",
    "channels/platforms/yuanbao_inbound.py",
    "channels/platforms/yuanbao_markdown.py",
    "channels/platforms/yuanbao_outbound.py",
    "hermes_agent/gateway/runtime_config.py",
]

_P1_LEGACY_IMPORT_TOKENS = [
    "from gateway.platforms",
    "import gateway.platforms",
    "from gateway import rich_sent_store",
    "from gateway.platform_registry",
    "import gateway.platform_registry",
    "from gateway.session_context",
    "import gateway.session_context",
    "from gateway.status",
    "import gateway.status",
    "from gateway.sticker_cache",
    "import gateway.sticker_cache",
    "from gateway.whatsapp_identity",
    "import gateway.whatsapp_identity",
    "from gateway.slash_commands",
    "import gateway.slash_commands",
    "from gateway.slash_access",
    "import gateway.slash_access",
]

_P2_SESSIONDB_TOKENS = (
    "from hermes_state",
    "import hermes_state",
    "SessionDB",
)

_P2_IDENTITY_ALIAS_TOKENS = (
    "stored_session_id",
    "stable_session_id",
    "runtime_session_id",
)

_P2_IDENTITY_ALIAS_ALLOWLIST = {
    "hermes_agent/gateway/pipeline.py",
    "scripts/zero_debt/verdict.py",
}

_P2_RUN_EVENTS_INSERT_ALLOWLIST = {
    "hermes_agent/domain/event_ledger.py",
    "scripts/zero_debt/verdict.py",
}

_P2_RUNS_UPDATE_ALLOWLIST = {
    "hermes_agent/domain/run_terminator.py",
    "hermes_agent/repositories/run_repo.py",
    "scripts/zero_debt/verdict.py",
}

_P2_SESSIONDB_ALLOWLIST = {
    "scripts/zero_debt/verdict.py",
}

_P2_STATE_STORE_PATHS = [
    "hermes_agent/storage/state_store.py",
    "hermes_agent/storage/state_mixins/activities.py",
    "hermes_agent/storage/state_mixins/agent_profiles.py",
    "hermes_agent/storage/state_mixins/branch.py",
    "hermes_agent/storage/state_mixins/member_chat.py",
    "hermes_agent/storage/state_mixins/participants.py",
    "hermes_agent/storage/state_mixins/runs.py",
    "hermes_agent/storage/state_mixins/team_capabilities.py",
    "hermes_agent/storage/state_mixins/team_registry.py",
]

_P2_STATE_STORE_MAX_TOTAL_LINES = 100
_P2_HERMES_STATE_STORE_MAX_METHODS = 0

_GOD_OBJECT_MAX_CLASS_METHODS = 80
_GOD_OBJECT_BLESSED_TREES = ("hermes_agent", "channels", "hermes_gateway")
_GOD_OBJECT_EXCLUDE_PREFIXES = ("hermes_agent/storage/migrations/",)

_P3_LEGACY_METHOD_TOKENS = ("METHOD_MODULES",)
_P3_LEGACY_OVERRIDE_TOKENS = ("DOVIE_GATEWAY_METHOD_OVERRIDES",)
_P3_GATEWAY_ALLOWLIST = {
    "scripts/zero_debt/verdict.py",
}

_P4_WORKER_SERVICE_PATHS = [
    "tui_gateway/services/worker_db_proxy.py",
    "tui_gateway/services/worker_frame_router.py",
    "tui_gateway/services/worker_pool.py",
    "tui_gateway/services/worker_publish_bridge.py",
    "tui_gateway/services/worker_rpc_proxy.py",
    "tui_gateway/services/worker_runtime.py",
    "tui_gateway/services/worker_supervisor.py",
]
_P4_WORKER_MAX_TOTAL_LINES = 100
_P4_WORKER_ALLOWED_OWNER_FILES = {
    "hermes_agent/orchestration/worker_lease_manager.py",
    "hermes_agent/orchestration/worker_pool.py",
    "hermes_agent/orchestration/worker_runtime.py",
    "hermes_agent/runtime/worker_pool.py",
    "hermes_agent/runtime/worker_runtime.py",
}

_P5_GATEWAY_RUN_PATH = "gateway/run.py"
_P5_GATEWAY_RUN_MAX_LINES = 0
_P5_GATEWAY_TARGET_MAX_LINES = 800

_P6_SILENT_SWALLOW_ROOTS = ("hermes_agent", "hermes_gateway")


@dataclass(frozen=True)
class Check:
    id: str
    ok: bool
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "ok": self.ok, "message": self.message}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _git(args: list[str]) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _production_python_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if set(rel.parts) & _SCAN_EXCLUDED_DIRS:
            continue
        files.append(path)
    return files


def _contains_all(text: str, tokens: list[str]) -> bool:
    return all(token in text for token in tokens)


def _required_file_checks() -> list[Check]:
    required = [
        MANIFEST,
        EXECUTION_PLAN,
        VERDICT_SCRIPT,
        PHASE_CLOSURE_SCRIPT,
        GATE_TEST,
        P1_BOUNDARY_TEST,
    ]
    return [
        Check(
            id=f"file_exists:{path.relative_to(REPO_ROOT).as_posix()}",
            ok=path.exists(),
            message="exists" if path.exists() else "missing",
        )
        for path in required
    ]


def _manifest_checks() -> list[Check]:
    text = _read(MANIFEST)
    sections = [
        "## Frontend V3.1 Freeze",
        "## Keep List",
        "## Kill List",
        "## Rebuild List",
        "## Production Grep Gates",
        "## Tests And Docs Allowlist",
        "## Wire Boundary Allowlist",
        "## Phase Sign-Off Contract",
    ]
    checks = [
        Check(
            id="manifest:required_sections",
            ok=_contains_all(text, sections),
            message="all required sections present",
        ),
        Check(
            id="manifest:wire_boundary_explicit",
            ok=(
                "`hermes_agent/gateway/pipeline.py`" in text
                and "stored_session_id" in text
                and "stable_session_id" in text
                and "runtime_session_id" in text
            ),
            message="wire alias allowlist is explicit",
        ),
        Check(
            id="manifest:production_gates_defined",
            ok=_contains_all(
                text,
                [
                    "`no_sessiondb_production`",
                    "`no_legacy_identity_alias_internal`",
                    "`no_method_modules`",
                    "`no_dovie_overrides`",
                    "`event_ledger_single_writer`",
                    "`run_state_single_writer`",
                ],
            ),
            message="production grep gates are named",
        ),
    ]
    return checks


def _execution_plan_checks() -> list[Check]:
    text = _read(EXECUTION_PLAN)
    phase_tokens = [f"## P{i} " for i in range(7)]
    return [
        Check(
            id="plan:p0_to_p6_present",
            ok=_contains_all(text, phase_tokens),
            message="P0-P6 are defined",
        ),
        Check(
            id="plan:vertical_slice_rule",
            ok="dispatch -> domain/repository -> SQLite -> wire response" in text,
            message="vertical slice E2E rule is present",
        ),
        Check(
            id="plan:two_signoff_files",
            ok=(
                "zero_debt_phase_pX_verdict.json" in text
                and "zero_debt_phase_pX_human_signoff.md" in text
                and "scripts/zero_debt/phase_closure.py" in text
            ),
            message="machine verdict, human sign-off, and closure gate are required",
        ),
    ]


def _working_state_warnings() -> list[str]:
    warnings: list[str] = []
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch and branch != "feat/hermes-zero-debt":
        warnings.append(
            f"current branch is {branch!r}; destructive phases must run on 'feat/hermes-zero-debt'"
        )
    status = _git(["status", "--short"])
    if status:
        warnings.append("worktree is dirty; P1+ requires an explicit baseline or clean handoff")
    return warnings


def build_p0_verdict() -> dict[str, Any]:
    checks = [
        *_required_file_checks(),
        *_manifest_checks(),
        *_execution_plan_checks(),
    ]
    failed = [check for check in checks if not check.ok]
    return {
        "phase": "P0",
        "status": "fail" if failed else "pass",
        "checks": [check.as_dict() for check in checks],
        "warnings": _working_state_warnings(),
        "next_required_human_signoff": "docs/audits/zero_debt_phase_p0_human_signoff.md",
    }


def _p1_path_checks() -> list[Check]:
    checks: list[Check] = []
    for path in _P1_LEGACY_PATHS:
        full_path = REPO_ROOT / path
        checks.append(
            Check(
                id=f"p1_legacy_removed:{path}",
                ok=not full_path.exists(),
                message="removed" if not full_path.exists() else "still exists",
            )
        )
    for path in _P1_TARGET_PATHS:
        full_path = REPO_ROOT / path
        checks.append(
            Check(
                id=f"p1_target_exists:{path}",
                ok=full_path.exists(),
                message="exists" if full_path.exists() else "missing",
            )
        )
    return checks


def _p1_import_checks() -> list[Check]:
    legacy_import_offenders: list[str] = []
    channel_gateway_offenders: list[str] = []
    for path in _production_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if any(stripped.startswith(token) for token in _P1_LEGACY_IMPORT_TOKENS):
                legacy_import_offenders.append(f"{rel}:{lineno}: {stripped}")
            if rel.startswith("channels/") and (
                stripped.startswith("from gateway") or stripped.startswith("import gateway.")
            ):
                channel_gateway_offenders.append(f"{rel}:{lineno}: {stripped}")

    return [
        Check(
            id="p1:no_legacy_channel_imports",
            ok=not legacy_import_offenders,
            message=(
                "no production imports from moved gateway channel owners"
                if not legacy_import_offenders
                else "\n".join(legacy_import_offenders[:20])
            ),
        ),
        Check(
            id="p1:channels_do_not_import_gateway",
            ok=not channel_gateway_offenders,
            message=(
                "channels package has no gateway imports"
                if not channel_gateway_offenders
                else "\n".join(channel_gateway_offenders[:20])
            ),
        ),
    ]


def _scan_lines_for_tokens(
    *,
    tokens: tuple[str, ...],
    allowlist: set[str] | None = None,
) -> list[str]:
    offenders: list[str] = []
    allowed = allowlist or set()
    for path in _production_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in allowed:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(lines, start=1):
            if any(token in line for token in tokens):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    return offenders


def _python_line_count(rel_paths: list[str]) -> tuple[int, list[str]]:
    total = 0
    details: list[str] = []
    for rel in rel_paths:
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        total += line_count
        details.append(f"{rel}: {line_count}")
    return total, details


def _class_method_count(rel_path: str, class_name: str) -> int:
    path = REPO_ROOT / rel_path
    if not path.exists():
        return 0
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return sum(
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                for child in node.body
            )
    return 0


def _god_object_offenders() -> list[str]:
    offenders: list[str] = []
    for tree_name in _GOD_OBJECT_BLESSED_TREES:
        root = REPO_ROOT / tree_name
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if any(rel.startswith(prefix) for prefix in _GOD_OBJECT_EXCLUDE_PREFIXES):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                methods = sum(
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for child in node.body
                )
                if methods > _GOD_OBJECT_MAX_CLASS_METHODS:
                    offenders.append(f"{rel}::{node.name}: {methods} methods")
    return sorted(offenders)


def _legacy_gateway_import_offenders() -> list[str]:
    offenders: list[str] = []
    for path in _production_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith("gateway/"):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("from gateway") or stripped.startswith("import gateway"):
                offenders.append(f"{rel}:{lineno}: {stripped}")
    return offenders


def _method_mapping_offenders() -> list[str]:
    """Find non-target method->handler maps independent of constant names."""
    offenders: list[str] = []
    for path in _production_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith("hermes_agent/gateway/"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets: list[ast.expr]
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            else:
                targets = [node.target]
                value = node.value
            if not isinstance(value, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
                continue
            target_names = [
                target.id
                for target in targets
                if isinstance(target, ast.Name)
            ]
            name_text = " ".join(target_names).lower()
            if not any(token in name_text for token in ("method", "handler", "override", "registry")):
                continue
            try:
                literal_text = ast.get_source_segment(path.read_text(encoding="utf-8"), value) or ""
            except UnicodeDecodeError:
                literal_text = ""
            if "tui_gateway.methods" in literal_text or "gateway.methods" in literal_text:
                offenders.append(f"{rel}:{getattr(node, 'lineno', 0)}: {', '.join(target_names)}")
    return offenders


def _server_dispatches_through_pipeline() -> bool:
    path = REPO_ROOT / "tui_gateway" / "server.py"
    text = _read(path)
    return (
        "hermes_agent.gateway.pipeline" in text
        and ".dispatch(" in text
        and "MethodRegistry" in text
    )


def _silent_swallow_offenders_for_roots(root_names: tuple[str, ...]) -> list[str]:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from hermes_agent.observability.silent_swallow_lint import scan_paths

    roots = [REPO_ROOT / root for root in root_names if (REPO_ROOT / root).exists()]
    findings = scan_paths(roots)
    return [
        f"{finding.file}:{finding.line}: {finding.exception_type}: {finding.reason}"
        for finding in findings
    ]


def _p2_hermes_state_store_instantiations() -> list[str]:
    offenders: list[str] = []
    for path in _production_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _P2_SESSIONDB_ALLOWLIST:
            continue
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "HermesStateStore":
                source = lines[node.lineno - 1].strip() if node.lineno else ""
                offenders.append(f"{rel}:{node.lineno}: {source}")
    return offenders


def _p2_silent_swallow_offenders() -> list[str]:
    return _silent_swallow_offenders_for_roots(("hermes_agent",))


def _p2_data_plane_checks() -> list[Check]:
    sessiondb_offenders = _scan_lines_for_tokens(
        tokens=_P2_SESSIONDB_TOKENS,
        allowlist=_P2_SESSIONDB_ALLOWLIST,
    )
    identity_alias_offenders = _scan_lines_for_tokens(
        tokens=_P2_IDENTITY_ALIAS_TOKENS,
        allowlist=_P2_IDENTITY_ALIAS_ALLOWLIST,
    )
    run_events_insert_offenders = _scan_lines_for_tokens(
        tokens=("INSERT INTO run_events",),
        allowlist=_P2_RUN_EVENTS_INSERT_ALLOWLIST,
    )
    runs_update_offenders = _scan_lines_for_tokens(
        tokens=("UPDATE runs",),
        allowlist=_P2_RUNS_UPDATE_ALLOWLIST,
    )
    state_store_lines, state_store_line_details = _python_line_count(
        _P2_STATE_STORE_PATHS
    )
    state_store_method_count = _class_method_count(
        "hermes_agent/storage/state_store.py",
        "HermesStateStore",
    )
    hermes_state_store_instantiations = _p2_hermes_state_store_instantiations()
    shadow_writers = find_shadow_table_writers()
    silent_swallow_offenders = _p2_silent_swallow_offenders()
    return [
        Check(
            id="p2:no_sessiondb_production",
            ok=not sessiondb_offenders,
            message=(
                "production code has no hermes_state/SessionDB dependency"
                if not sessiondb_offenders
                else "\n".join(sessiondb_offenders[:30])
            ),
        ),
        Check(
            id="p2:no_legacy_identity_alias_internal",
            ok=not identity_alias_offenders,
            message=(
                "legacy identity aliases appear only at the wire folding boundary"
                if not identity_alias_offenders
                else "\n".join(identity_alias_offenders[:30])
            ),
        ),
        Check(
            id="p2:event_ledger_single_writer",
            ok=not run_events_insert_offenders,
            message=(
                "run_events INSERT is owned by EventLedger only"
                if not run_events_insert_offenders
                else "\n".join(run_events_insert_offenders[:30])
            ),
        ),
        Check(
            id="p2:run_state_single_writer",
            ok=not runs_update_offenders,
            message=(
                "runs UPDATE is owned by RunTerminator/RunRepo only"
                if not runs_update_offenders
                else "\n".join(runs_update_offenders[:30])
            ),
        ),
        Check(
            id="p2:no_hermes_state_store_production_instantiation",
            ok=not hermes_state_store_instantiations,
            message=(
                "production code does not instantiate HermesStateStore"
                if not hermes_state_store_instantiations
                else "\n".join(hermes_state_store_instantiations[:30])
            ),
        ),
        Check(
            id="p2:state_store_decomposed",
            ok=state_store_lines <= _P2_STATE_STORE_MAX_TOTAL_LINES,
            message=(
                f"state_store/state_mixins total lines {state_store_lines} <= "
                f"{_P2_STATE_STORE_MAX_TOTAL_LINES}"
                if state_store_lines <= _P2_STATE_STORE_MAX_TOTAL_LINES
                else "state_store/state_mixins still own data-plane bulk: "
                + f"{state_store_lines} lines; "
                + "; ".join(state_store_line_details)
            ),
        ),
        Check(
            id="p2:hermes_state_store_no_methods",
            ok=state_store_method_count <= _P2_HERMES_STATE_STORE_MAX_METHODS,
            message=(
                "HermesStateStore class has no remaining methods"
                if state_store_method_count <= _P2_HERMES_STATE_STORE_MAX_METHODS
                else f"HermesStateStore still has {state_store_method_count} methods"
            ),
        ),
        Check(
            id="p2:aggregate_table_single_owner",
            ok=not shadow_writers,
            message=(
                "all aggregate table writes are owned by their aggregate repositories"
                if not shadow_writers
                else (
                    f"{len(shadow_writers)} shadow writer sites:\n"
                    + format_shadow_table_writers(shadow_writers, limit=60)
                )
            ),
        ),
        Check(
            id="p2:no_silent_swallow_in_v3",
            ok=not silent_swallow_offenders,
            message=(
                "hermes_agent has no silent swallow handlers"
                if not silent_swallow_offenders
                else "\n".join(silent_swallow_offenders[:30])
            ),
        ),
    ]


def build_p1_verdict() -> dict[str, Any]:
    checks = [
        *_required_file_checks(),
        *_p1_path_checks(),
        *_p1_import_checks(),
    ]
    failed = [check for check in checks if not check.ok]
    return {
        "phase": "P1",
        "status": "fail" if failed else "pass",
        "checks": [check.as_dict() for check in checks],
        "warnings": [
            *_working_state_warnings(),
            (
                "P1 verdict checks static ownership gates only; attach the "
                "documented pytest and real-device results before human sign-off"
            ),
        ],
        "required_test_commands": [
            ".venv/bin/pytest tests/channels tests/observability/test_channels_p1_boundary.py tests/observability/test_module_liveness_audit.py -q",
            ".venv/bin/pytest tests/channels tests/observability/test_channels_p1_boundary.py tests/observability/test_module_liveness_audit.py tests/gateway/test_config.py tests/gateway/test_platform_registry.py tests/gateway/test_platform_connected_checkers.py tests/gateway/test_session.py tests/gateway/test_session_env.py tests/gateway/test_session_race_guard.py tests/gateway/test_platform_base.py tests/gateway/test_api_server.py tests/gateway/test_api_server_runs.py tests/gateway/test_api_server_jobs.py tests/gateway/test_api_server_toolset.py tests/gateway/test_async_delivery_capability.py tests/gateway/test_status_command.py tests/gateway/test_gateway_shutdown.py tests/gateway/test_runner_startup_failures.py tests/gateway/test_runner_fatal_adapter.py tests/gateway/test_model_command_flat_string_config.py tests/gateway/test_telegram_format.py tests/gateway/test_telegram_network.py tests/gateway/test_sticker_cache.py -q",
        ],
        "next_required_human_signoff": "docs/audits/zero_debt_phase_p1_human_signoff.md",
    }


def build_p2_verdict() -> dict[str, Any]:
    checks = [
        *_required_file_checks(),
        *_p2_data_plane_checks(),
    ]
    failed = [check for check in checks if not check.ok]
    return {
        "phase": "P2",
        "status": "fail" if failed else "pass",
        "checks": [check.as_dict() for check in checks],
        "warnings": [*_working_state_warnings()],
        "required_test_commands": [
            ".venv/bin/pytest tests/observability/test_zero_debt_gates.py -q",
            ".venv/bin/pytest tests/repositories/test_aggregate_table_single_owner.py tests/repositories/test_j4_6_cross_aggregate_isolation.py -q",
            ".venv/bin/pytest tests/gateway tests/storage tests/tui_gateway -q",
        ],
        "next_required_human_signoff": "docs/audits/zero_debt_phase_p2_human_signoff.md",
    }


def _p3_gateway_registry_checks() -> list[Check]:
    method_module_offenders = _scan_lines_for_tokens(
        tokens=_P3_LEGACY_METHOD_TOKENS,
        allowlist=_P3_GATEWAY_ALLOWLIST,
    )
    override_offenders = _scan_lines_for_tokens(
        tokens=_P3_LEGACY_OVERRIDE_TOKENS,
        allowlist=_P3_GATEWAY_ALLOWLIST,
    )
    mapping_offenders = _method_mapping_offenders()
    server_uses_pipeline = _server_dispatches_through_pipeline()
    return [
        Check(
            id="p3:no_method_modules",
            ok=not method_module_offenders,
            message=(
                "legacy METHOD_MODULES registration is gone"
                if not method_module_offenders
                else "\n".join(method_module_offenders[:30])
            ),
        ),
        Check(
            id="p3:no_dovie_overrides",
            ok=not override_offenders,
            message=(
                "DOVIE_GATEWAY_METHOD_OVERRIDES is gone"
                if not override_offenders
                else "\n".join(override_offenders[:30])
            ),
        ),
        Check(
            id="p3:single_dispatch_registry",
            ok=server_uses_pipeline and not mapping_offenders,
            message=(
                "tui_gateway/server.py dispatches through hermes_agent.gateway.pipeline and no secondary method maps remain"
                if server_uses_pipeline and not mapping_offenders
                else (
                    ("tui_gateway/server.py does not dispatch through hermes_agent.gateway.pipeline\n"
                     if not server_uses_pipeline else "")
                    + ("\n".join(mapping_offenders[:30]) if mapping_offenders else "")
                ).strip()
            ),
        ),
    ]


def build_p3_verdict() -> dict[str, Any]:
    checks = [
        *_required_file_checks(),
        *_p3_gateway_registry_checks(),
    ]
    failed = [check for check in checks if not check.ok]
    return {
        "phase": "P3",
        "status": "fail" if failed else "pass",
        "checks": [check.as_dict() for check in checks],
        "warnings": [*_working_state_warnings()],
        "required_test_commands": [
            ".venv/bin/pytest tests/observability/test_zero_debt_gates.py tests/gateway -q",
            "python scripts/zero_debt/phase_closure.py --phase P3 --json",
        ],
        "next_required_human_signoff": "docs/audits/zero_debt_phase_p3_human_signoff.md",
    }


def _worker_service_method_count(rel_path: str) -> int:
    path = REPO_ROOT / rel_path
    if not path.exists():
        return 0
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return 0
    return sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree))


def _p4_worker_checks() -> list[Check]:
    total_lines, line_details = _python_line_count(_P4_WORKER_SERVICE_PATHS)
    method_details = [
        f"{rel}: {_worker_service_method_count(rel)} methods"
        for rel in _P4_WORKER_SERVICE_PATHS
        if (REPO_ROOT / rel).exists()
    ]
    god_object_offenders = _god_object_offenders()
    worker_owner_offenders = [
        rel
        for rel in _P4_WORKER_SERVICE_PATHS
        if (REPO_ROOT / rel).exists()
    ]
    for path in _production_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _P4_WORKER_ALLOWED_OWNER_FILES:
            continue
        if not rel.startswith(("hermes_agent/", "tui_gateway/")):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "WorkerLeaseManager" in text and rel not in _P4_WORKER_SERVICE_PATHS:
            worker_owner_offenders.append(rel)
    worker_owner_offenders = sorted(set(worker_owner_offenders))
    return [
        Check(
            id="p4:worker_services_decomposed",
            ok=total_lines <= _P4_WORKER_MAX_TOTAL_LINES,
            message=(
                f"legacy worker services total lines {total_lines} <= {_P4_WORKER_MAX_TOTAL_LINES}"
                if total_lines <= _P4_WORKER_MAX_TOTAL_LINES
                else "legacy worker services still own runtime bulk: "
                + f"{total_lines} lines; "
                + "; ".join([*line_details, *method_details])
            ),
        ),
        Check(
            id="p4:no_relocated_worker_monolith",
            ok=not god_object_offenders,
            message=(
                "no god-object class in blessed runtime trees"
                if not god_object_offenders
                else "\n".join(god_object_offenders[:30])
            ),
        ),
        Check(
            id="p4:worker_single_owner",
            ok=not worker_owner_offenders,
            message=(
                "worker runtime has a single target owner"
                if not worker_owner_offenders
                else "worker runtime ownership still split:\n" + "\n".join(worker_owner_offenders[:30])
            ),
        ),
    ]


def build_p4_verdict() -> dict[str, Any]:
    checks = [
        *_required_file_checks(),
        *_p4_worker_checks(),
    ]
    failed = [check for check in checks if not check.ok]
    return {
        "phase": "P4",
        "status": "fail" if failed else "pass",
        "checks": [check.as_dict() for check in checks],
        "warnings": [*_working_state_warnings()],
        "required_test_commands": [
            ".venv/bin/pytest tests/observability/test_zero_debt_gates.py tests/tui_gateway tests/gateway -q",
            "python scripts/zero_debt/phase_closure.py --phase P4 --json",
        ],
        "next_required_human_signoff": "docs/audits/zero_debt_phase_p4_human_signoff.md",
    }


def _p5_gateway_checks() -> list[Check]:
    gateway_dir = REPO_ROOT / "gateway"
    legacy_gateway_imports = _legacy_gateway_import_offenders()
    god_object_offenders = _god_object_offenders()
    gateway_run_lines = len(_read(REPO_ROOT / _P5_GATEWAY_RUN_PATH).splitlines())
    target_monoliths: list[str] = []
    for root_name in ("hermes_gateway", "hermes_agent/gateway"):
        root = REPO_ROOT / root_name
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            lines = len(path.read_text(encoding="utf-8").splitlines())
            if lines > _P5_GATEWAY_TARGET_MAX_LINES:
                target_monoliths.append(f"{rel}: {lines} lines")
    return [
        Check(
            id="p5:gateway_directory_removed",
            ok=not gateway_dir.exists(),
            message="gateway/ is removed" if not gateway_dir.exists() else "gateway/ still exists",
        ),
        Check(
            id="p5:no_legacy_gateway_imports",
            ok=not legacy_gateway_imports,
            message=(
                "production code has no legacy gateway imports"
                if not legacy_gateway_imports
                else "\n".join(legacy_gateway_imports[:30])
            ),
        ),
        Check(
            id="p5:no_relocated_gateway_monolith",
            ok=not god_object_offenders,
            message=(
                "no gateway god-object class was relocated into blessed trees"
                if not god_object_offenders
                else "\n".join(god_object_offenders[:30])
            ),
        ),
        Check(
            id="p5:gateway_run_decomposed",
            ok=gateway_run_lines <= _P5_GATEWAY_RUN_MAX_LINES and not target_monoliths,
            message=(
                "gateway/run.py is gone and target gateway files are below monolith threshold"
                if gateway_run_lines <= _P5_GATEWAY_RUN_MAX_LINES and not target_monoliths
                else (
                    f"{_P5_GATEWAY_RUN_PATH}: {gateway_run_lines} lines; "
                    + ("target monoliths: " + "; ".join(target_monoliths) if target_monoliths else "")
                )
            ),
        ),
    ]


def build_p5_verdict() -> dict[str, Any]:
    checks = [
        *_required_file_checks(),
        *_p5_gateway_checks(),
    ]
    failed = [check for check in checks if not check.ok]
    return {
        "phase": "P5",
        "status": "fail" if failed else "pass",
        "checks": [check.as_dict() for check in checks],
        "warnings": [*_working_state_warnings()],
        "required_test_commands": [
            ".venv/bin/pytest tests/observability/test_zero_debt_gates.py tests/gateway tests/tui_gateway -q",
            "python scripts/zero_debt/phase_closure.py --phase P5 --json",
        ],
        "next_required_human_signoff": "docs/audits/zero_debt_phase_p5_human_signoff.md",
    }


def _p6_debt_closure_checks() -> list[Check]:
    silent_swallow_offenders = _silent_swallow_offenders_for_roots(_P6_SILENT_SWALLOW_ROOTS)
    god_object_offenders = _god_object_offenders()
    return [
        Check(
            id="p6:no_silent_swallow_in_clean_trees",
            ok=not silent_swallow_offenders,
            message=(
                "hermes_agent/hermes_gateway have no silent swallow handlers"
                if not silent_swallow_offenders
                else "\n".join(silent_swallow_offenders[:30])
            ),
        ),
        Check(
            id="p6:no_relocated_god_objects",
            ok=not god_object_offenders,
            message=(
                "no god-object classes remain in blessed trees"
                if not god_object_offenders
                else "\n".join(god_object_offenders[:30])
            ),
        ),
    ]


def build_p6_verdict() -> dict[str, Any]:
    checks = [
        *_required_file_checks(),
        *_p2_data_plane_checks(),
        *_p3_gateway_registry_checks(),
        *_p4_worker_checks(),
        *_p5_gateway_checks(),
        *_p6_debt_closure_checks(),
    ]
    failed = [check for check in checks if not check.ok]
    return {
        "phase": "P6",
        "status": "fail" if failed else "pass",
        "checks": [check.as_dict() for check in checks],
        "warnings": [*_working_state_warnings()],
        "required_test_commands": [
            ".venv/bin/pytest tests/observability tests/repositories tests/gateway tests/tui_gateway -q",
            "python scripts/zero_debt/phase_closure.py --phase P6 --json",
        ],
        "next_required_human_signoff": "docs/audits/zero_debt_phase_p6_human_signoff.md",
    }


def build_verdict(phase: str) -> dict[str, Any]:
    normalized = str(phase or "").upper()
    if normalized == "P0":
        return build_p0_verdict()
    if normalized == "P1":
        return build_p1_verdict()
    if normalized == "P2":
        return build_p2_verdict()
    if normalized == "P3":
        return build_p3_verdict()
    if normalized == "P4":
        return build_p4_verdict()
    if normalized == "P5":
        return build_p5_verdict()
    if normalized == "P6":
        return build_p6_verdict()
    return {
        "phase": normalized,
        "status": "fail",
        "checks": [
            {
                "id": "phase:supported",
                "ok": False,
                "message": "only P0, P1, and P2 verdicts are implemented; extend this script before later phase sign-off",
            }
        ],
        "warnings": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes zero-debt phase verdict")
    parser.add_argument("--phase", default="P0", help="Phase id, currently P0")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)

    verdict = build_verdict(args.phase)
    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{verdict['phase']} {verdict['status']}")
        for check in verdict["checks"]:
            marker = "ok" if check["ok"] else "fail"
            print(f"[{marker}] {check['id']}: {check['message']}")
        for warning in verdict.get("warnings", []):
            print(f"[warn] {warning}")
    return 0 if verdict["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
