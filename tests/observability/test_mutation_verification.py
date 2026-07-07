"""Phase L — mutation verification (spec §12).

Each ``MutationSpec`` mutates one line targeting a specific invariant
(J2/J4/J7/J8) and asserts that the corresponding property/contract test
turns red. Green tests after mutation would mean the invariant is not
actually guarded by our suite — a silent regression risk that this Phase
exists to expose.

Tests are skipped if the harness is running under ``PYTEST_MUTATION_SUITE``
(prevents recursion when the outer mutation harness spawns pytest itself).
"""

from __future__ import annotations

import os
import sys

import pytest

from hermes_agent.observability import MutationSpec, apply_mutation


@pytest.fixture(autouse=True)
def _flush_hermes_module_cache_after_mutation():
    """Each mutation touches a hermes_agent source file. Even though the
    harness restores the file on disk, this parent pytest process may have
    already loaded a stale (mutated) version into ``sys.modules`` — any
    later test using that module would then run against the broken code.

    Drop every ``hermes_agent.*`` and ``tui_gateway.*`` entry from the
    parent's module cache after each mutation so the next test re-imports
    fresh source.
    """
    yield
    stale = [
        name
        for name in list(sys.modules)
        if name.startswith(("hermes_agent.", "tui_gateway."))
    ]
    for name in stale:
        del sys.modules[name]


_SKIP_REASON = (
    "mutation verification is gated behind RUN_MUTATION_TESTS=1 — it spawns "
    "subprocess pytest runs that would collide with the main pytest session's "
    "module cache when co-run with the full suite. Run explicitly: "
    "``RUN_MUTATION_TESTS=1 pytest tests/observability/test_mutation_verification.py``"
)


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J2_seq_allocator_no_increment():
    """Mutating SeqAllocator so it does NOT increment `next_seq` must turn the
    monotonicity property test red.
    """
    spec = MutationSpec(
        invariant="J2",
        source_path="hermes_agent/domain/seq_allocator.py",
        find=(
            "        UPDATE seq_counter\n"
            "           SET next_seq = next_seq + 1,\n"
            "               updated_at = ?\n"
            "         WHERE session_id = ?\n"
            "        RETURNING next_seq - 1\n"
            "        "
        ),
        replace=(
            "        UPDATE seq_counter\n"
            "           SET next_seq = next_seq,\n"
            "               updated_at = ?\n"
            "         WHERE session_id = ?\n"
            "        RETURNING next_seq - 1\n"
            "        "
        ),
        expected_failing_tests=(
            "tests/domain/test_seq_allocator_property.py::test_property_allocate_only_yields_strictly_monotonic_sequence",
        ),
        description="drop SeqAllocator increment; monotonicity invariant must catch it",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J2 mutation did NOT turn expected test red — invariant unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J4_run_terminator_lose_idempotency():
    """Removing the terminal-status short-circuit must turn the idempotency test red."""
    spec = MutationSpec(
        invariant="J4",
        source_path="hermes_agent/domain/run_terminator.py",
        find=(
            "        # Idempotency: terminal states are absorbing.\n"
            "        if existing_status in TERMINAL_RUN_STATUSES:"
        ),
        replace=(
            "        # Idempotency: terminal states are absorbing.\n"
            "        if False and existing_status in TERMINAL_RUN_STATUSES:"
        ),
        expected_failing_tests=(
            "tests/domain/test_run_terminator.py::test_terminate_run_idempotent_skip_after_first_terminal",
        ),
        description="disable RunStateMachine idempotency short-circuit",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J4 mutation did NOT turn expected test red — idempotency unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J7_pipeline_disable_snake_case():
    """Turning off snake_case normalization must turn the normalize test red."""
    spec = MutationSpec(
        invariant="J7",
        source_path="hermes_agent/gateway/pipeline.py",
        find="        snake_key = to_snake_case(str(key))",
        replace="        snake_key = str(key)",
        expected_failing_tests=(
            "tests/gateway_v3/test_pipeline.py::test_normalize_params_recursive",
        ),
        description="pipeline drops snake_case normalization",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J7 mutation did NOT turn expected test red — snake_case invariant unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J8_registry_accept_untagged():
    """Accepting untagged handlers must turn the ``rejects_untagged`` test red."""
    spec = MutationSpec(
        invariant="J8",
        source_path="hermes_agent/gateway/registry.py",
        find=(
            "        perm = get_permission(handler)\n"
            "        if perm is None:\n"
            "            raise RegistryError("
        ),
        replace=(
            "        perm = get_permission(handler)\n"
            "        if False and perm is None:\n"
            "            raise RegistryError("
        ),
        expected_failing_tests=(
            "tests/gateway_v3/test_registry.py::test_register_rejects_untagged_handler",
        ),
        description="registry accepts untagged handlers (invariant J8)",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J8 mutation did NOT turn expected test red — permission invariant unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J11_silent_swallow_lint_regression():
    """If the AST scanner stops flagging ``except: pass`` under hermes_agent/,
    the guard test must go green — which is a regression signal. We assert
    the scanner still flags mutated code by adding a silent-swallow line to
    a known-safe source file.
    """
    spec = MutationSpec(
        invariant="J11",
        source_path="hermes_agent/observability/silent_swallow_lint.py",
        # Weaken the detector: pass-only bodies no longer flagged as silent.
        find="    return all(isinstance(stmt, ast.Pass) for stmt in body)",
        replace="    return False  # mutation: pretend pass-only body is not silent",
        expected_failing_tests=(
            "tests/observability/test_silent_swallow_lint.py::test_scanner_flags_bare_except_pass",
        ),
        description="weaken silent-swallow detector; guard test must go red",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J11 mutation did NOT turn expected test red — silent-swallow guard unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J1_session_branch_loses_parent_identity():
    """Dropping ``parent_session_id`` on branch must turn the branch identity test red."""
    spec = MutationSpec(
        invariant="J1",
        source_path="hermes_agent/repositories/session_repo.py",
        find=(
            "        branch_spec = SessionSpec(\n"
            "            session_id=stable_new,\n"
            "            source=source.source,\n"
            "            title=spec.title or source.title,\n"
            "            display_title=spec.display_title or source.display_title,\n"
            "            session_kind=source.session_kind,\n"
            "            conversation_kind=source.conversation_kind,\n"
            "            parent_session_id=stable_src,\n"
            "        )"
        ),
        replace=(
            "        branch_spec = SessionSpec(\n"
            "            session_id=stable_new,\n"
            "            source=source.source,\n"
            "            title=spec.title or source.title,\n"
            "            display_title=spec.display_title or source.display_title,\n"
            "            session_kind=source.session_kind,\n"
            "            conversation_kind=source.conversation_kind,\n"
            "            parent_session_id=\"\",\n"
            "        )"
        ),
        expected_failing_tests=(
            "tests/repositories/test_session_repo_impl.py::test_branch_creates_child_session",
        ),
        description="branch loses parent_session_id identity",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J1 mutation did NOT turn expected test red — identity invariant unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J3_event_ledger_leaks_internal_events():
    """Removing the ``_internal.*`` filter must turn the default-filter test red."""
    spec = MutationSpec(
        invariant="J3",
        source_path="hermes_agent/domain/event_ledger.py",
        find=(
            "        if not include_internal:\n"
            "            clauses.append(\"event_type NOT LIKE '_internal.%'\")"
        ),
        replace=(
            "        if False and not include_internal:\n"
            "            clauses.append(\"event_type NOT LIKE '_internal.%'\")"
        ),
        expected_failing_tests=(
            "tests/domain/test_event_ledger.py::test_list_filters_internal_events_by_default",
        ),
        description="EventLedger stops hiding _internal.* by default",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J3 mutation did NOT turn expected test red — EventLedger invariant unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J5_worker_pool_leaks_inflight_state():
    """Skipping the run_terminal cleanup must turn the pool-cleanup test red."""
    spec = MutationSpec(
        invariant="J5",
        source_path="hermes_agent/orchestration/worker_pool.py",
        find=(
            "    def record_run_terminal(self, run_id: str) -> InflightRun | None:\n"
            "        r = str(run_id or \"\").strip()\n"
            "        record = self._inflight_by_run_id.pop(r, None)"
        ),
        replace=(
            "    def record_run_terminal(self, run_id: str) -> InflightRun | None:\n"
            "        r = str(run_id or \"\").strip()\n"
            "        record = self._inflight_by_run_id.get(r, None)"
        ),
        expected_failing_tests=(
            "tests/orchestration/test_worker_pool.py::test_record_run_terminal_removes_entry",
        ),
        description="pool retains terminal runs (single-replica invariant broken)",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J5 mutation did NOT turn expected test red — WorkerPool invariant unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J6_registry_accepts_duplicate_methods():
    """Bypassing the duplicate-name check must turn the duplicate-rejection test red."""
    spec = MutationSpec(
        invariant="J6",
        source_path="hermes_agent/gateway/registry.py",
        find=(
            "        if normalized in self._by_name:\n"
            "            existing = self._by_name[normalized]\n"
            "            raise RegistryError("
        ),
        replace=(
            "        if False and normalized in self._by_name:\n"
            "            existing = self._by_name[normalized]\n"
            "            raise RegistryError("
        ),
        expected_failing_tests=(
            "tests/gateway_v3/test_registry.py::test_register_rejects_duplicate",
        ),
        description="registry silently overwrites duplicate registrations (J6 broken)",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J6 mutation did NOT turn expected test red — single-registry invariant unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J9_err_helper_accepts_magic_string():
    """Weakening the ErrorCode type check must turn the enum-guard test red."""
    spec = MutationSpec(
        invariant="J9",
        source_path="hermes_agent/gateway/error_codes.py",
        find=(
            "    if not isinstance(code, ErrorCode):\n"
            "        raise TypeError(\n"
            "            f\"code must be an ErrorCode enum, got {type(code).__name__}={code!r}\"\n"
            "        )"
        ),
        replace=(
            "    if False and not isinstance(code, ErrorCode):\n"
            "        raise TypeError(\n"
            "            f\"code must be an ErrorCode enum, got {type(code).__name__}={code!r}\"\n"
            "        )"
        ),
        expected_failing_tests=(
            "tests/gateway_v3/test_error_codes.py::test_err_requires_error_code_enum",
        ),
        description="err() lets magic strings sneak in as codes (J9 broken)",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J9 mutation did NOT turn expected test red — ErrorCode registry invariant unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J1_identity_alias_fold_disabled():
    """Skipping the alias fold in the dispatch pipeline must turn the fold test red."""
    spec = MutationSpec(
        invariant="J1",
        source_path="hermes_agent/gateway/pipeline.py",
        find="    _fold_session_id_aliases(normalized)\n    return normalized",
        replace="    # mutation: skip alias fold\n    return normalized",
        expected_failing_tests=(
            "tests/gateway_v3/test_pipeline_identity_normalization.py::test_stored_session_id_folds_to_session_id",
        ),
        description="dispatch pipeline no longer folds legacy session_id aliases",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J1 alias-fold mutation did NOT turn expected test red — identity "
        f"normalization invariant unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J1_response_fold_disabled():
    """Removing the response-side alias fold from dispatch must turn the
    E2E scrub test red — legacy aliases would then leak to the wire.
    """
    spec = MutationSpec(
        invariant="J1",
        source_path="hermes_agent/gateway/pipeline.py",
        find='    return {"id": request_id, "result": fold_response_aliases(result)}',
        replace='    return {"id": request_id, "result": result}',
        expected_failing_tests=(
            "tests/gateway_v3/test_response_alias_fold.py::test_dispatch_scrubs_legacy_alias_from_wire_response",
        ),
        description="dispatch stops folding legacy aliases on the response side",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J1 response-fold mutation did NOT turn expected test red — "
        f"symmetric identity fold invariant unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_MUTATION_TESTS") != "1", reason=_SKIP_REASON
)
def test_mutation_J10_contract_version_drift():
    """Advertising a wrong contractVersion must turn the pin test red."""
    spec = MutationSpec(
        invariant="J10",
        source_path="tui_gateway/services/contract_capabilities.py",
        find='CONTRACT_VERSION = "3.1"',
        replace='CONTRACT_VERSION = "9.9"  # mutation: contract version drift',
        expected_failing_tests=(
            "tests/gateway/test_contract_capabilities.py::test_contract_version_is_3_1",
        ),
        description="contract version drifts away from frozen 3.1 (J10 broken)",
    )
    result = apply_mutation(spec)
    assert result.tests_red, (
        f"J10 mutation did NOT turn expected test red — contract version pin unguarded.\n"
        f"pytest exit={result.pytest_returncode}\nstdout tail:\n{result.stdout_tail}"
    )
