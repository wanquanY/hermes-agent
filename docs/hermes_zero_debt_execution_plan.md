# Hermes Zero Debt Execution Plan

## Objective

Eliminate Hermes shadow systems by destructive final-state migration:

- no frontend compensation for Hermes defects;
- no production `SessionDB` compatibility data plane;
- no legacy gateway method override system;
- no duplicated worker/runtime state owner;
- no silent fallback or silent swallow hiding failed migrations.

Execution uses vertical production slices, not file-first deletion. A phase can contain temporary shims, but phase exit cannot.

## Non-Negotiable Exit Rule

A phase is not closed until all are true:

1. New target owner is production-wired.
2. Old owner is deleted or behavior-empty.
3. End-to-end tests cover dispatch -> domain/repository -> SQLite -> wire response for each migrated entry point.
4. Machine sign-off JSON says `status: pass`.
5. User real-device sign-off exists.

## P0 - Contract Freeze And Gates

### Goal

Freeze contract and make gates executable before destructive migration starts.

### Deliverables

- `docs/hermes_zero_debt_manifest.md`
- `docs/hermes_zero_debt_execution_plan.md`
- `scripts/zero_debt/verdict.py`
- `tests/observability/test_zero_debt_gates.py`

### Machine Gates

- Required docs exist.
- Manifest has Keep/Kill/Rebuild lists.
- Manifest has production, tests/docs, and wire-boundary allowlists.
- Verdict script emits JSON for `--phase P0`.
- Zero-debt gate tests pass.

### Human Sign-Off

- User records DoXie frontend v3.1 commit SHA and confirms no P0/P1 frontend feedback will mutate this effort.
- User confirms branch ownership policy.

## P1 - Channels And Slash Commands Independence

### Goal

Move channel adapters and slash command responsibilities out of legacy `gateway/` before retiring it.

### Vertical Slices

1. `telegram` channel: move import ownership, add channel-level smoke test, prove one real-device path.
2. Platform batch 1: `discord`, `slack`, `feishu`, `wechat`, `qqbot`.
3. Platform batch 2: `whatsapp`, `signal`, `sms`, `email`, `dingtalk`, `homeassistant`, `mattermost`.
4. Slash commands: move command registry/runtime to standalone target owner.
5. Update `platform_connections` and gateway launch code to use new channel imports.
6. Delete old `gateway/platforms/*` and old slash-command owner once production imports are gone.

### Exit Gates

- `channels/platforms/*` imports only `hermes_agent.*`, `channels.*`, stdlib, and third-party SDKs.
- Production grep has no `gateway.platforms` import.
- At least one channel real-device smoke passes.
- `pytest tests/channels tests/gateway/channel_related -q` or the current equivalent passes.

## P2 - Data Plane Repository Ownership

### Goal

Move production storage ownership from `SessionDB`/`hermes_state*` to repositories and domain services.

### Vertical Slices

1. `session.create/get/list`: `SessionRepoImpl` owns behavior.
2. `agent_profile.*`: `AgentProfileRepoImpl` owns behavior.
3. `run.create/append/list`: `RunRepoImpl` + `EventLedger` own behavior.
4. `run.terminate/cancel`: `RunTerminator` owns terminal state.
5. `message/timeline` read APIs: `MessageRepoImpl` + `EventLedger` own behavior.
6. `tool.*` timeline projection: canonical output from `run_events`; `tool_events` remains projection only until deleted or isolated.
7. `conversation_participants`: conversation/session owner writes and reads participants.
8. `team_mission activity`: `TeamMissionRepoImpl` + `run_events` activity indexes own replay and watermarks.
9. `interaction lifecycle`: concrete domain `InteractionRegistry` owns requested/resolved/expired persistence and delivery.

### Slice DoD

Every slice must include an E2E test proving:

```text
gateway dispatch -> target service/repository -> SQLite state -> wire response
```

The same slice removes or deactivates the old production call path.

### Exit Gates

- Production code has no `from hermes_state`, `import hermes_state`, or `SessionDB`.
- Direct `INSERT INTO run_events` production ownership is `EventLedger` only.
- Direct `UPDATE runs` production ownership is `RunTerminator`/`RunRepo` only.
- Internal production code has no `stored_session_id`, `stable_session_id`, or `runtime_session_id` outside the wire-boundary allowlist.
- Full storage/repository/domain/gateway regression passes.

## P3 - Single Gateway Registry

### Goal

Replace legacy method modules and override dispatch with `hermes_agent.gateway.registry` and `pipeline`.

### Vertical Slices

1. `session.*`
2. `run.*`
3. `message.*` and timeline methods
4. `conversation.*`
5. `activity.*`
6. `interaction.*`
7. `profile.*`, `team.*`, `system.*`
8. handshake and error model

### Exit Gates

- `tui_gateway/server.py` dispatches through `hermes_agent.gateway.pipeline.dispatch`.
- No production `METHOD_MODULES`.
- No production `DOVIE_GATEWAY_METHOD_OVERRIDES`.
- `tui_gateway/methods/*` is deleted or behavior-empty with no production import.
- DoXie real-device three symptoms pass:
  - session content displays normally;
  - approval does not pop twice;
  - message/tool/reasoning order is stable.

## P4 - Runtime And Worker Single Owner

### Goal

Remove duplicated worker/runtime/run-control ownership.

### Vertical Slices

1. submit/run launch via target WorkerPool.
2. event streaming via target runtime event pipeline.
3. cancel/interrupt via target run control.
4. inactive conversation recovery via unified runtime state.
5. approval/clarify timeout and recovery via target interaction runtime.

### Exit Gates

- Old worker services have no production imports.
- Old run-control path has no production imports.
- Inactive sessions recover running/approval/completed/unread state correctly.
- Submit/cancel/recovery/approval E2E tests pass.

## P5 - Legacy Gateway Retirement

### Goal

Delete the old `gateway/` folder after channels and slash commands have moved.

### Exit Gates

- `gateway/` is gone or contains only an inert `__init__.py` if packaging still needs it temporarily.
- `pyproject.toml` no longer packages `gateway` or `gateway.*`.
- Production grep has no `^from gateway` or `^import gateway`.
- Legacy gateway sentinel test flips to assert absence.

## P6 - Observability And Debt Closure

### Goal

Make all zero-debt gates blocking and remove the last debt markers.

### Exit Gates

- `silent_swallow_lint.scan_paths([Path(".")])` returns zero production findings.
- No zero-debt related xfail markers remain.
- `lint-imports` passes.
- `mypy hermes_agent/` passes.
- `pytest tests/ -q` passes.
- `RUN_MUTATION_TESTS=1 pytest tests/observability/test_mutation_verification.py -q` passes.
- `.github/workflows/hermes-v3-gates.yml` runs zero-debt verdict as blocking.

## Commit And Review Policy

- Commit net line count is not a gate.
- Phase net debt reduction is a gate.
- Each commit should be reviewer-readable and own one slice or one mechanical cleanup.
- A slice cannot end with a hidden shadow system.
- A phase cannot close with a temporary shim.

## Sign-Off Files

Each phase must write:

```text
docs/audits/zero_debt_phase_pX_verdict.json
docs/audits/zero_debt_phase_pX_human_signoff.md
```

The verdict file is generated by `scripts/zero_debt/verdict.py`.
The human sign-off file records real-device results and the user's explicit approval to proceed.
