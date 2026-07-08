# Hermes Zero Debt Manifest

## Status

- Manifest version: `2026-07-08-p2`
- Active phase: `P2`
- Branch policy: execute destructive work only on `feat/hermes-zero-debt`.
- Ownership policy: one writable owner for the active branch; all other agent sessions are read-only.
- Frontend contract freeze: DoXie timeline v3.1 is frozen for this effort. New feedback after P0 goes to v3.2 and does not mutate this execution contract.

## Core Rules

1. Hermes owns runtime, storage, identity and gateway contracts. DoXie must not compensate for Hermes defects.
2. Final state is destructive and compatibility-free. During a phase, temporary shims are allowed only if they die before that phase closes.
3. A phase closes only when new owner is production-wired, old owner is deleted or behavior-empty, machine sign-off passes, and user sign-off passes.
4. Production entry points migrate by vertical slice: dispatch -> domain/repository -> SQLite -> wire response.
5. Kill-list paths are not deleted by name first. Delete only after the responsibility has moved to the target owner.
6. Allowlists are explicit path lists with reasons. Wildcard exemptions are not allowed.

## Frontend V3.1 Freeze

- Frozen contract name: DoXie Hermes Timeline v3.1.
- Frozen source path: `/Users/yangwanquan/Personal_projects/AIGC_dev/doxie/docs/hermes_agent_architecture_v3.md`.
- Frozen implementation surface:
  - `apps/desktop/src/services/hermes/timeline/`
  - `apps/desktop/src/stores/modules/hermes/timeline-engine/`
  - `apps/desktop/src/stores/modules/hermes/session-runtime/`
- Freeze rule: P0-P6 changes must preserve this contract. Any new frontend requirement is tracked as v3.2 and cannot change P0 acceptance.
- SHA rule: record the exact DoXie commit SHA in `docs/audits/zero_debt_phase_p0_human_signoff.md` before P1 begins.

## Keep List

| Path | Target owner | Reason |
|---|---|---|
| `hermes_agent/domain/` | Domain layer | Aggregate contracts, event ledger, run termination, interaction contract. |
| `hermes_agent/repositories/` | Repository layer | Target data-plane boundary. |
| `hermes_agent/gateway/` | Gateway layer | Target registry, pipeline, auth and handshake. |
| `hermes_agent/storage/` | Storage layer | Migrations and low-level storage primitives. |
| `hermes_team_mission/` | Transitional domain package | Must migrate or fold into target owners by vertical slice; not deleted by name first. |
| `tui_gateway/server.py` | Transport entry | WebSocket/JSON-RPC process entry survives, internals are replaced. |
| `channels/config.py` | Channel configuration owner | Owns `Platform`, `HomeChannel`, and `PlatformConfig`; `gateway.config` only re-exports during gateway retirement. |
| `channels/platform_registry.py` | Channel registry owner | Owns platform connected-check registry after P1. |
| `channels/session_identity.py` | Channel session identity owner | Owns `SessionSource`, `SessionContext`, and session-key construction after P1. |
| `channels/session_context.py` | Channel session context owner | Owns active session context extraction after P1. |
| `channels/runtime_status.py` | Channel runtime status owner | Owns channel-facing status rendering after P1. |
| `channels/sticker_cache.py` | Channel sticker cache owner | Owns sticker cache behavior after P1. |
| `channels/rich_sent_store.py` | Channel rich-message sent-store owner | Owns bounded sent-message lookup used by rich channel adapters. |
| `channels/whatsapp_identity.py` | Channel WhatsApp identity owner | Owns WhatsApp sender/session identity normalization after P1. |
| `channels/platforms/*` | Channel platform owner | Owns platform adapters after P1 channel migration. |
| `hermes_agent/gateway/runtime_config.py` | Gateway runtime config owner | Owns model/provider/reasoning/fallback config helpers while `gateway.run` is retired by later vertical slices. |
| `tests/` | Verification | Tests are preserved or rewritten around target owners. Obsolete tests may be deleted with the deleted behavior. |

## Kill List

Delete only after the listed responsibility has a production-wired target owner in the same phase.

| Legacy path | Target owner before deletion | Required proof |
|---|---|---|
| `hermes_state.py` | `hermes_agent/repositories/*` + `hermes_agent/domain/event_ledger.py` | Production imports gone; repository E2E tests pass. |
| `hermes_state_*.py` | Repository/domain services | Production imports gone; no direct run_events shadow writer remains. |
| `gateway/run.py` | `channels/*` + `hermes_agent/gateway/*` | Platform/slash responsibilities moved; production import grep is zero. |
| `gateway/status.py` | New gateway/status method owner or deleted behavior | Dispatch E2E proves replacement or no consumer. |
| `gateway/config.py` | New config method owner or deleted behavior | Dispatch E2E proves replacement or no consumer. |
| `gateway/session_context.py` | Identity/runtime context owner | Session identity E2E passes. |
| `gateway/channel_directory.py` | `channels/*` registry | Channel import-linter contract passes. |
| `gateway/shutdown_forensics.py` | Observability owner or deleted behavior | Shutdown observability tests pass or behavior removed. |
| `tui_gateway/methods/*` | `hermes_agent/gateway/methods/*` | Each method has dispatch -> repo -> SQLite -> wire response E2E. |
| `tui_gateway/services/run_control.py` | Run domain service + gateway pipeline | Silent fallback scan passes for removed path. |
| `tui_gateway/services/interaction_registry.py` | Concrete `hermes_agent/domain/interaction` implementation | Interaction lifecycle E2E passes from prompt/respond/worker. |
| `tui_gateway/services/worker_*.py` | Target WorkerPool/runtime owner | Submit/cancel/recovery/approval E2E passes. |
| `tui_gateway/core/method_registration.py` | `hermes_agent/gateway/registry.py` | Method registry production grep is zero. |
| `dovie_extension/gateway_methods.py` | `hermes_agent/gateway/registry.py` | Override production grep is zero. |

## Rebuild List

| Target | Missing or incomplete responsibility |
|---|---|
| `hermes_agent/domain/interaction.py` | Concrete InteractionRegistry implementation with persistence + delivery contract. |
| `hermes_agent/domain/runtime_state.py` | Unified runtime state for running/approval/completed/unread recovery. |
| `hermes_agent/gateway/methods/` | Full method set replacing `tui_gateway/methods/*`. |
| `hermes_agent/gateway/pipeline.py` | Sole wire-boundary identity alias folding and dispatch error model. |
| `hermes_agent/repositories/message_repo.py` | Message/timeline query ownership after `SessionDB` retirement. |
| `channels/platforms/` | Independent platform adapters. P1 moved file ownership and cleared channel imports from legacy `gateway.*`; remaining structural debt is adapter-file size and deeper per-adapter responsibility splits, not gateway fallback. |
| `channels/slash_commands/` | Not created in P1. Audit showed `gateway/slash_commands.py` was an unreferenced shadow mixin; live slash command dispatch remains in `gateway/run.py` until the gateway-run vertical slice migrates. |

## Production Grep Gates

These are final-state gates. P0 records them; later phases tighten from warning to blocking.

| Gate id | Pattern | Production allowlist |
|---|---|---|
| `no_sessiondb_production` | `from hermes_state|import hermes_state|SessionDB` | None after P2. |
| `no_legacy_identity_alias_internal` | `stored_session_id|stable_session_id|runtime_session_id` | `hermes_agent/gateway/pipeline.py` only, for inbound wire folding. |
| `no_method_modules` | `METHOD_MODULES` | None after P3. |
| `no_dovie_overrides` | `DOVIE_GATEWAY_METHOD_OVERRIDES` | None after P3. |
| `single_dispatch_registry` | structure: `tui_gateway/server.py` dispatch must use `hermes_agent.gateway.pipeline` and no secondary method→handler map may remain | `hermes_agent/gateway/registry.py` only after P3. |
| `worker_services_decomposed` | `tui_gateway/services/worker_*.py` line/method budget | Legacy worker services collapse to <=100 total lines after P4. |
| `no_relocated_worker_monolith` | class method-count >80 under blessed trees | No worker god-object may move into `hermes_agent/`, `channels/`, or `hermes_gateway/` after P4. |
| `worker_single_owner` | structural WorkerPool/runtime owner scan | Runtime worker lease/state ownership must have one target owner after P4. |
| `no_legacy_gateway_imports` | `^from gateway|^import gateway` | None after P5. |
| `gateway_directory_removed` | `gateway/` exists | Directory must be gone after P5. |
| `no_relocated_gateway_monolith` | class method-count >80 under blessed trees | `GatewayRunner` or any renamed equivalent may not move into clean trees after P5. |
| `gateway_run_decomposed` | `gateway/run.py` and target gateway file sizes | `gateway/run.py` gone; no target gateway file >800 lines after P5. |
| `event_ledger_single_writer` | `INSERT INTO run_events` | `hermes_agent/domain/event_ledger.py` only after P2. |
| `run_state_single_writer` | `UPDATE runs` | `hermes_agent/domain/run_terminator.py`, `hermes_agent/repositories/run_repo.py` only after P2. |
| `no_hermes_state_store_production_instantiation` | `HermesStateStore(` | None after P2. Renaming `SessionDB` to `HermesStateStore` does not satisfy P2. |
| `state_store_decomposed` | `hermes_agent/storage/state_store.py` + `state_mixins/*` | Combined implementation must collapse to <=100 lines after P2. Data-plane ownership belongs in aggregate repositories. |
| `hermes_state_store_no_methods` | `class HermesStateStore` method count | 0 after P2. The class may not remain as a renamed god-object. |
| `aggregate_table_single_owner` | raw `INSERT/UPDATE/DELETE` to aggregate tables | Physical writes must be owned by the declared aggregate repository/domain owner only after P2. |
| `no_silent_swallow_in_v3` | `except ...: pass` / equivalent silent swallow | 0 findings under `hermes_agent/` after P2. |
| `no_silent_swallow_in_clean_trees` | silent swallow scan under `hermes_agent/` + `hermes_gateway/` | 0 findings after P6; `channels/` adapter容错不在本次强制清零范围. |
| `no_relocated_god_objects` | class method-count >80 under blessed trees | No renamed/moved god-object survives final closure. |

Production scan excludes: `tests/`, `docs/`, `.venv/`, `__pycache__/`, `.import_linter_cache/`, build outputs.

## Tests And Docs Allowlist

Tests and docs may mention legacy names only when the file is explicitly listed here.

| Path | Allowed terms | Reason |
|---|---|---|
| `docs/hermes_zero_debt_manifest.md` | all legacy terms | This manifest defines removal gates. |
| `docs/hermes_zero_debt_execution_plan.md` | all legacy terms | This plan defines removal phases. |
| `docs/v3_execution_goals.md` | all legacy terms | Current phased implementation history. |
| `docs/v3_audit_report.md` | all legacy terms | Audit evidence. |
| `tests/observability/test_zero_debt_gates.py` | all legacy terms | Machine gate tests. |
| `scripts/zero_debt/verdict.py` | all legacy terms | Machine gate implementation. |

Additional test/doc mentions require adding an exact row with a reason.

## Wire Boundary Allowlist

| Path | Allowed aliases | Reason |
|---|---|---|
| `hermes_agent/gateway/pipeline.py` | `stored_session_id`, `stable_session_id`, `runtime_session_id` | Sole inbound folding boundary for old wire clients during migration. |

No domain, repository, runtime service, or gateway method implementation may own these aliases after Goal/P2 identity closure.

## Phase Sign-Off Contract

Each phase must create two files before the next phase starts:

- Machine sign-off: `docs/audits/zero_debt_phase_pX_verdict.json`
- User sign-off: `docs/audits/zero_debt_phase_pX_human_signoff.md`

The machine verdict is produced by:

```bash
python scripts/zero_debt/verdict.py --phase PX --json
```

The user sign-off must record the real-device result for that phase. P3+ requires the three DoXie symptoms: normal session display, no duplicate approval popup, stable message/tool/reasoning order.

Phase closure must be checked with `scripts/zero_debt/phase_closure.py`; a
pending or template-only human sign-off file does not permit entering the next
phase.
