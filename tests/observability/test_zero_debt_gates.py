from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST = REPO_ROOT / "docs" / "hermes_zero_debt_manifest.md"
EXECUTION_PLAN = REPO_ROOT / "docs" / "hermes_zero_debt_execution_plan.md"
VERDICT = REPO_ROOT / "scripts" / "zero_debt" / "verdict.py"
PHASE_CLOSURE = REPO_ROOT / "scripts" / "zero_debt" / "phase_closure.py"
P2_INVENTORY = REPO_ROOT / "scripts" / "zero_debt" / "p2_inventory.py"
ZERO_DEBT_STATUS = REPO_ROOT / "scripts" / "zero_debt" / "status.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_phase_closure_module() -> ModuleType:
    scripts_dir = str(PHASE_CLOSURE.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("zero_debt_phase_closure", PHASE_CLOSURE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_zero_debt_p0_documents_exist() -> None:
    assert MANIFEST.exists()
    assert EXECUTION_PLAN.exists()
    assert VERDICT.exists()
    assert PHASE_CLOSURE.exists()
    assert P2_INVENTORY.exists()
    assert ZERO_DEBT_STATUS.exists()


def test_manifest_defines_required_allowlists_and_gates() -> None:
    text = _read(MANIFEST)
    for token in (
        "## Frontend V3.1 Freeze",
        "## Keep List",
        "## Kill List",
        "## Rebuild List",
        "## Production Grep Gates",
        "## Tests And Docs Allowlist",
        "## Wire Boundary Allowlist",
        "`hermes_agent/gateway/pipeline.py`",
        "`no_sessiondb_production`",
        "`no_legacy_identity_alias_internal`",
        "`single_dispatch_registry`",
        "`worker_services_decomposed`",
        "`no_relocated_worker_monolith`",
        "`worker_single_owner`",
        "`gateway_directory_removed`",
        "`no_relocated_gateway_monolith`",
        "`gateway_run_decomposed`",
        "`event_ledger_single_writer`",
        "`run_state_single_writer`",
        "`no_hermes_state_store_production_instantiation`",
        "`state_store_decomposed`",
        "`hermes_state_store_no_methods`",
        "`aggregate_table_single_owner`",
        "`no_silent_swallow_in_v3`",
        "`no_silent_swallow_in_clean_trees`",
        "`no_relocated_god_objects`",
    ):
        assert token in text


def test_execution_plan_requires_vertical_slices_and_two_signoffs() -> None:
    text = _read(EXECUTION_PLAN)
    for token in (
        "dispatch -> domain/repository -> SQLite -> wire response",
        "docs/audits/zero_debt_phase_pX_verdict.json",
        "docs/audits/zero_debt_phase_pX_human_signoff.md",
    ):
        assert token in text
    for phase in range(7):
        assert f"## P{phase} " in text


def test_p0_verdict_json_passes_contract_checks() -> None:
    output = subprocess.check_output(
        [sys.executable, str(VERDICT), "--phase", "P0", "--json"],
        cwd=REPO_ROOT,
        text=True,
    )
    verdict = json.loads(output)
    assert verdict["phase"] == "P0"
    assert verdict["status"] == "pass"
    assert verdict["checks"]
    assert all(check["ok"] for check in verdict["checks"])
    assert verdict["next_required_human_signoff"] == (
        "docs/audits/zero_debt_phase_p0_human_signoff.md"
    )


def test_p1_verdict_json_passes_channel_ownership_checks() -> None:
    output = subprocess.check_output(
        [sys.executable, str(VERDICT), "--phase", "P1", "--json"],
        cwd=REPO_ROOT,
        text=True,
    )
    verdict = json.loads(output)
    assert verdict["phase"] == "P1"
    assert verdict["status"] == "pass"
    assert verdict["checks"]
    assert all(check["ok"] for check in verdict["checks"])
    assert verdict["next_required_human_signoff"] == (
        "docs/audits/zero_debt_phase_p1_human_signoff.md"
    )
    assert verdict["required_test_commands"]


def test_p1_phase_closure_accepts_recorded_human_signoff() -> None:
    output = subprocess.check_output(
        [sys.executable, str(PHASE_CLOSURE), "--phase", "P1", "--json"],
        cwd=REPO_ROOT,
        text=True,
    )
    closure = json.loads(output)
    assert closure["phase"] == "P1"
    assert closure["status"] == "pass"
    assert any(
        check["id"] == "human_signoff:explicit_approval" and check["ok"]
        for check in closure["checks"]
    )


def test_phase_closure_accepts_explicit_human_approval(tmp_path: Path) -> None:
    signoff = tmp_path / "zero_debt_phase_p1_human_signoff.md"
    signoff.write_text(
        "# P1 Human Sign-Off\n\n"
        "签收状态：`passed`\n\n"
        "P1 真机验收通过，可以进入 P2。\n",
        encoding="utf-8",
    )
    phase_closure = _load_phase_closure_module()
    result = phase_closure._human_signoff_result("P1", signoff)
    assert result["ok"] is True
    assert result["id"] == "human_signoff:explicit_approval"


def test_p2_verdict_passes_after_data_plane_decomposition() -> None:
    result = subprocess.run(
        [sys.executable, str(VERDICT), "--phase", "P2", "--json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    verdict = json.loads(result.stdout)
    assert verdict["phase"] == "P2"
    assert verdict["status"] == "pass"
    checks = {check["id"]: check for check in verdict["checks"]}
    for gate_id in (
        "p2:no_sessiondb_production",
        "p2:no_legacy_identity_alias_internal",
        "p2:event_ledger_single_writer",
        "p2:run_state_single_writer",
        "p2:no_hermes_state_store_production_instantiation",
        "p2:state_store_decomposed",
        "p2:hermes_state_store_no_methods",
        "p2:aggregate_table_single_owner",
        "p2:no_silent_swallow_in_v3",
    ):
        assert gate_id in checks
    assert checks["p2:no_sessiondb_production"]["ok"]
    assert checks["p2:no_legacy_identity_alias_internal"]["ok"]
    assert checks["p2:no_hermes_state_store_production_instantiation"]["ok"]
    assert checks["p2:aggregate_table_single_owner"]["ok"]
    assert checks["p2:no_silent_swallow_in_v3"]["ok"]
    assert checks["p2:state_store_decomposed"]["ok"]
    assert checks["p2:hermes_state_store_no_methods"]["ok"]
    assert verdict["next_required_human_signoff"] == (
        "docs/audits/zero_debt_phase_p2_human_signoff.md"
    )


def test_p3_verdict_defines_gateway_registry_structure_gates() -> None:
    result = subprocess.run(
        [sys.executable, str(VERDICT), "--phase", "P3", "--json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    verdict = json.loads(result.stdout)
    assert verdict["phase"] == "P3"
    assert verdict["status"] == "pass"
    checks = {check["id"]: check for check in verdict["checks"]}
    for gate_id in (
        "p3:no_method_modules",
        "p3:no_dovie_overrides",
        "p3:single_dispatch_registry",
    ):
        assert gate_id in checks
        assert checks[gate_id]["ok"]
    assert verdict["next_required_human_signoff"] == (
        "docs/audits/zero_debt_phase_p3_human_signoff.md"
    )


def test_p4_verdict_defines_worker_decomposition_and_relocation_gates() -> None:
    result = subprocess.run(
        [sys.executable, str(VERDICT), "--phase", "P4", "--json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    verdict = json.loads(result.stdout)
    assert verdict["phase"] == "P4"
    assert verdict["status"] == "pass"
    checks = {check["id"]: check for check in verdict["checks"]}
    for gate_id in (
        "p4:worker_services_decomposed",
        "p4:no_relocated_worker_monolith",
        "p4:worker_single_owner",
    ):
        assert gate_id in checks
        assert checks[gate_id]["ok"]
    assert verdict["next_required_human_signoff"] == (
        "docs/audits/zero_debt_phase_p4_human_signoff.md"
    )


def test_p5_verdict_defines_gateway_retirement_relocation_gates() -> None:
    result = subprocess.run(
        [sys.executable, str(VERDICT), "--phase", "P5", "--json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    verdict = json.loads(result.stdout)
    assert verdict["phase"] == "P5"
    assert verdict["status"] == "pass"
    checks = {check["id"]: check for check in verdict["checks"]}
    for gate_id in (
        "p5:gateway_directory_removed",
        "p5:no_legacy_gateway_imports",
        "p5:no_relocated_gateway_monolith",
        "p5:no_mixin_recomposed_gateway_monolith",
        "p5:gateway_run_decomposed",
    ):
        assert gate_id in checks
        assert checks[gate_id]["ok"]
    assert verdict["next_required_human_signoff"] == (
        "docs/audits/zero_debt_phase_p5_human_signoff.md"
    )


def test_p6_verdict_defines_closure_safety_net_gates() -> None:
    result = subprocess.run(
        [sys.executable, str(VERDICT), "--phase", "P6", "--json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    verdict = json.loads(result.stdout)
    assert verdict["phase"] == "P6"
    assert verdict["status"] == "pass"
    checks = {check["id"]: check for check in verdict["checks"]}
    for gate_id in (
        "p6:no_silent_swallow_in_clean_trees",
        "p6:no_relocated_god_objects",
        "p2:aggregate_table_single_owner",
        "p3:single_dispatch_registry",
        "p5:no_relocated_gateway_monolith",
    ):
        assert gate_id in checks
        assert checks[gate_id]["ok"]
    assert verdict["next_required_human_signoff"] == (
        "docs/audits/zero_debt_phase_p6_human_signoff.md"
    )


def test_p2_inventory_reports_current_offender_baseline() -> None:
    output = subprocess.check_output(
        [sys.executable, str(P2_INVENTORY), "--format", "json"],
        cwd=REPO_ROOT,
        text=True,
    )
    inventory = json.loads(output)
    assert inventory["phase"] == "P2"
    gates = inventory["gates"]
    assert gates["p2:no_sessiondb_production"]["total_offenders"] == 0
    assert gates["p2:no_sessiondb_production"]["file_count"] == 0
    assert gates["p2:no_legacy_identity_alias_internal"]["total_offenders"] == 0
    assert gates["p2:no_legacy_identity_alias_internal"]["file_count"] == 0

    markdown = subprocess.check_output(
        [sys.executable, str(P2_INVENTORY), "--format", "markdown"],
        cwd=REPO_ROOT,
        text=True,
    )
    assert "`p2:no_sessiondb_production`" in markdown
    assert "`p2:no_legacy_identity_alias_internal`" in markdown


def test_zero_debt_status_separates_verdict_from_closure() -> None:
    output = subprocess.check_output(
        [sys.executable, str(ZERO_DEBT_STATUS), "--json"],
        cwd=REPO_ROOT,
        text=True,
    )
    status = json.loads(output)
    rows = {row["phase"]: row for row in status["phases"]}
    assert rows["P1"]["machine_verdict"] == "pass"
    assert rows["P1"]["closure"] == "pass"
    assert rows["P1"]["closure_failed_checks"] == []
    assert rows["P2"]["machine_verdict"] == "pass"
    assert rows["P2"]["closure"] == "fail"
    assert rows["P2"]["machine_failed_checks"] == []
    assert "human_signoff:exists" in rows["P2"]["closure_failed_checks"]
    assert "p2:aggregate_table_single_owner" not in rows["P2"]["machine_failed_checks"]
    assert "p2:no_sessiondb_production" not in rows["P2"]["machine_failed_checks"]
    assert "p2:no_legacy_identity_alias_internal" not in rows["P2"]["machine_failed_checks"]
