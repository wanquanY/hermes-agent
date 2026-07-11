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

### Current Progress

- Completed slice `platform base contract`:
  - moved implementation owner from `gateway/platforms/base.py` to `channels/platforms/base.py`;
  - added `channels` package to setuptools package discovery;
  - switched all Python imports from `gateway.platforms.base` to `channels.platforms.base`;
  - deleted `gateway/platforms/base.py`;
  - added guard `tests/observability/test_channels_p1_boundary.py`.
- Completed slice `platform shared helpers`:
  - moved shared helper owners from `gateway/platforms/helpers.py` and
    `gateway/platforms/_http_client_limits.py` to `channels.platforms`;
  - switched production and test imports to the new owners;
  - deleted the old helper files without leaving gateway shims;
  - extended the P1 boundary guard to fail on legacy base/helper/http-limit imports.
- Completed slice `telegram channel`:
  - moved `gateway/platforms/telegram.py` and `gateway/platforms/telegram_network.py`
    to `channels.platforms`;
  - split the large Telegram adapter into explicit channel owners:
    `telegram_connection.py` for polling/webhook startup, reconnect, and DM topic
    lifecycle; `telegram_delivery.py` for outbound send/edit/draft/media
    delivery; `telegram_callbacks.py` for model picker and callback query
    handling; and `telegram_inbound.py` for formatting, mention gates,
    observed group context, inbound media, batching, event construction, and
    reaction hooks;
  - reduced all Telegram runtime files below the 2000-line project file-size
    limit and removed duplicated inbound shadow methods from the public adapter;
  - switched gateway/tool/test imports and patch targets to the new owner;
  - removed the broken `plugins.platforms.telegram.telegram_network` import path;
  - added `tests/channels/test_telegram_contract.py`;
  - kept the old gateway telegram files deleted, with the P1 boundary guard covering
    both direct and package-level legacy imports.
- Completed slice `discord channel`:
  - moved `gateway/platforms/discord.py` to `channels.platforms.discord`;
  - split the large Discord adapter into explicit channel owners:
    `discord_command_sync.py` for slash-command reconciliation,
    `discord_delivery.py` for outbound delivery/media/reactions,
    `discord_voice.py` for voice-channel lifecycle, `discord_slash.py` for
    slash authorization/registration, `discord_context.py` for channel/thread
    context and interactive UI prompts, and `discord_inbound.py` for inbound
    attachment/message batching;
  - reduced all Discord runtime files below the 2000-line project file-size
    limit while keeping `channels.platforms.discord` as the public adapter and
    test patch surface;
  - switched gateway/tool/test imports and patch targets to the new owner;
  - deleted the old gateway Discord file without a shim;
  - added `tests/channels/test_discord_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    Discord imports.
- Completed slice `slack channel`:
  - moved `gateway/platforms/slack.py` to `channels.platforms.slack`;
  - switched gateway/tool/test imports and patch targets to the new owner;
  - deleted the old gateway Slack file without a shim;
  - added `tests/channels/test_slack_contract.py`;
  - fixed Slack optional dependency ownership so `aiohttp` remains available
    when Slack SDK dependencies are absent;
  - fixed stream fallback stale-preview cleanup while preserving tail-only
    fallback for partial-overflow delivery;
  - extended the P1 boundary guard to block direct and package-level legacy
    Slack imports.
- Completed slice `slack responsibility split`:
  - split Slack Block Kit text extraction, inbound routing/approvals/thread
    context/downloads/gating, and shared slash/thread support into explicit
    owners: `slack_blocks.py`, `slack_inbound.py`, and `slack_support.py`;
  - kept `channels.platforms.slack` as the public adapter entry and dependency
    patch surface while moving the large behavior clusters to owner mixins;
  - reduced all Slack files below the 2000-line project file-size limit;
  - validated with Slack contract, gateway, approval-button, channel-skill,
    mention, and slash-dispatch tests.
- Completed slice `base shared channel owners`:
  - split `channels.platforms.base` into explicit shared owners:
    `base_text.py` for text/thread/media-routing helpers,
    `base_network.py` for network/proxy helpers, `base_media_cache.py` for
    inbound media cache and delivery path policy, `base_models.py` for
    event/result models and prompt/skill projection helpers, and
    `base_delivery.py` for default send/media delivery behavior;
  - kept `channels.platforms.base` as the public aggregation surface while
    preserving existing monkeypatch/debug surfaces for cache dirs, media caps,
    and media delivery roots;
  - reduced `channels/platforms/base.py` below the 2000-line project
    file-size limit;
  - validated with platform-base, media-download, media-routing, proxy, image
    send, multi-image, local-file extraction, and HTTP-client-limit tests.
- Completed slice `feishu channel`:
  - moved `gateway/platforms/feishu.py`, `gateway/platforms/feishu_comment.py`,
    and `gateway/platforms/feishu_comment_rules.py` to `channels.platforms`;
  - split the large Feishu adapter into explicit channel owners:
    `feishu_message.py` for normalization/post parsing, `feishu_outbound.py`
    for send/edit/upload/card/request-builder behavior, `feishu_content.py`
    for inbound content/media/sender profile handling, `feishu_webhook.py`
    for webhook signature/rate/anomaly handling, `feishu_inbound_state.py`
    for batching/admission/bot identity/dedup state, and `feishu_onboard.py`
    for QR setup/probe support;
  - reduced all Feishu runtime files below the 2000-line project file-size limit;
  - switched gateway/tool/CLI/TUI/test imports and patch targets to the new owners;
  - deleted the old gateway Feishu files without shims;
  - added `tests/channels/test_feishu_contract.py`;
  - fixed bot sender profile resolution so Feishu bot-name lookup uses
    `open_id` while preserving tenant `user_id` in the sender profile;
  - aligned stream-consumer thread-routing test assertions with metadata
    enrichment by preserving the caller `thread_id`;
  - extended the P1 boundary guard to block direct and package-level legacy
    Feishu imports.
- Completed slice `qqbot channel`:
  - moved the `gateway/platforms/qqbot/` package to `channels.platforms.qqbot`;
  - switched gateway/CLI/test imports and patch targets to the new package owner;
  - deleted the old gateway QQBot package without a shim;
  - removed migrated QQBot/Base compatibility re-exports from
    `gateway.platforms.__init__`, leaving only not-yet-migrated Yuanbao there;
  - added `tests/channels/test_qqbot_contract.py`;
  - extended the P1 boundary guard to block legacy QQBot imports and old
    package directory reintroduction.
- Completed slice `qqbot media split`:
  - split QQBot voice transcription, audio conversion, REST API/media upload,
    approval/update prompt sending, and outbound media delivery into
    `channels/platforms/qqbot/media.py`;
  - kept `channels.platforms.qqbot.adapter` focused on transport lifecycle,
    inbound event parsing, interaction dispatch, and access policy;
  - reduced all QQBot package files below the 2000-line project file-size limit;
  - validated with QQBot contract, gateway, and TTS Opus routing tests.
- Completed slice `weixin channel`:
  - moved `gateway/platforms/weixin.py` to `channels.platforms.weixin`;
  - switched gateway/tool/CLI/TUI/test imports and patch targets to the new owner;
  - deleted the old gateway Weixin file without a shim;
  - added `tests/channels/test_weixin_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    Weixin imports.
- Completed slice `weixin formatting split`:
  - moved Weixin markdown normalization and chunk-packing pure functions to
    `channels/platforms/weixin_formatting.py`;
  - kept the public `WEIXIN_COPY_LINE_WIDTH` constant available from
    `channels.platforms.weixin`;
  - reduced `channels/platforms/weixin.py` below the 2000-line project limit.
- Completed slice `wecom channel`:
  - moved `gateway/platforms/wecom.py`, `gateway/platforms/wecom_callback.py`,
    and `gateway/platforms/wecom_crypto.py` to `channels.platforms`;
  - switched gateway/tool/test imports and patch targets to the new owners;
  - deleted the old gateway WeCom files without shims;
  - added `tests/channels/test_wecom_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    WeCom imports.
- Completed slice `whatsapp channel`:
  - moved `gateway/platforms/whatsapp.py` to `channels.platforms.whatsapp`;
  - switched gateway/test imports and patch targets to the new owner;
  - deleted the old gateway WhatsApp file without a shim;
  - added `tests/channels/test_whatsapp_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    WhatsApp imports.
- Completed slice `signal channel`:
  - moved `gateway/platforms/signal.py` and
    `gateway/platforms/signal_rate_limit.py` to `channels.platforms`;
  - switched gateway/tool/test imports and patch targets to the new owners;
  - deleted the old gateway Signal files without shims;
  - added `tests/channels/test_signal_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    Signal imports.
- Completed slice `sms channel`:
  - moved `gateway/platforms/sms.py` to `channels.platforms.sms`;
  - switched gateway/test imports to the new owner;
  - deleted the old gateway SMS file without a shim;
  - added `tests/channels/test_sms_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    SMS imports.
- Completed slice `email channel`:
  - moved `gateway/platforms/email.py` to `channels.platforms.email`;
  - switched gateway/test imports and patch targets to the new owner;
  - deleted the old gateway Email file without a shim;
  - added `tests/channels/test_email_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    Email imports.
- Completed slice `dingtalk channel`:
  - moved `gateway/platforms/dingtalk.py` to `channels.platforms.dingtalk`;
  - switched gateway/test imports and patch targets to the new owner;
  - deleted the old gateway DingTalk file without a shim;
  - added `tests/channels/test_dingtalk_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    DingTalk imports.
- Completed slice `homeassistant channel`:
  - moved `gateway/platforms/homeassistant.py` to `channels.platforms.homeassistant`;
  - switched gateway/test imports and patch targets to the new owner;
  - deleted the old gateway Home Assistant file without a shim;
  - added `tests/channels/test_homeassistant_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    Home Assistant imports.
- Completed slice `mattermost channel`:
  - moved `gateway/platforms/mattermost.py` to `channels.platforms.mattermost`;
  - switched gateway/test imports and patch targets to the new owner;
  - deleted the old gateway Mattermost file without a shim;
  - added `tests/channels/test_mattermost_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    Mattermost imports.
- Completed slice `matrix channel`:
  - moved `gateway/platforms/matrix.py` to `channels.platforms.matrix`;
  - switched gateway/tool/test imports, patch targets, and log assertions to
    the new owner;
  - deleted the old gateway Matrix file without a shim;
  - added `tests/channels/test_matrix_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    Matrix imports;
  - isolated Matrix proxy tests from host macOS system proxy auto-detection
    without changing production proxy behavior.
- Completed slice `matrix responsibility split`:
  - split Matrix support, E2EE crypto, reactions, room operations, and
    formatting into explicit owners:
    `matrix_support.py`, `matrix_crypto.py`, `matrix_reactions.py`,
    `matrix_room_ops.py`, and `matrix_formatting.py`;
  - kept `channels.platforms.matrix` as the public API and test patch surface
    for existing Matrix imports, including E2EE dependency checks and crypto
    state store access;
  - reduced all Matrix files below the 2000-line project file-size limit;
  - validated with Matrix channel/gateway/E2EE/mention/voice/tool-surface tests
    and P1 boundary gates.
- Completed slice `bluebubbles channel`:
  - moved `gateway/platforms/bluebubbles.py` to `channels.platforms.bluebubbles`;
  - switched gateway/tool/test imports and patch targets to the new owner;
  - deleted the old gateway BlueBubbles file without a shim;
  - added `tests/channels/test_bluebubbles_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    BlueBubbles imports.
- Completed slice `webhook ingress channels`:
  - moved `gateway/platforms/webhook.py` and
    `gateway/platforms/msgraph_webhook.py` to `channels.platforms`;
  - switched gateway/test imports and patch targets to the new owners;
  - deleted the old gateway webhook files without shims;
  - added `tests/channels/test_webhook_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    webhook imports.
- Completed slice `api_server channel`:
  - moved `gateway/platforms/api_server.py` to `channels.platforms.api_server`;
  - switched gateway/test imports and patch targets to the new owner;
  - deleted the old gateway API server file without a shim;
  - added `tests/channels/test_api_server_contract.py`;
  - extended the P1 boundary guard to block direct and package-level legacy
    API server imports;
  - restored the session chat message parser and session endpoint capability
    advertisement in the API server owner;
  - enforced API-server no-async-delivery semantics in `terminal_tool` so
    stateless HTTP turns do not promise background completion callbacks.
- Completed slice `api_server responsibility split`:
  - split API server support constants/helpers/store/middleware into
    `api_server_support.py`;
  - split session resource routes, cron jobs routes, structured run routes,
    and OpenAI Responses API routes into `api_server_sessions.py`,
    `api_server_jobs.py`, `api_server_runs.py`, and
    `api_server_responses.py`;
  - kept `channels.platforms.api_server` as the public adapter entry and
    compatibility import/patch surface for existing tests and integrations;
  - reduced all API server files below the 2000-line project file-size limit;
  - validated with API server contract, Responses API, multimodal, session,
    jobs, runs, toolset, and bind-guard tests.
- Completed slice `yuanbao channel family`:
  - moved `gateway/platforms/yuanbao.py`, `yuanbao_media.py`,
    `yuanbao_proto.py`, and `yuanbao_sticker.py` to `channels.platforms`;
  - switched gateway/tool/test imports and patch targets to the new owners;
  - deleted the old gateway Yuanbao files without shims;
  - added `tests/channels/test_yuanbao_contract.py`;
  - exposed `YuanbaoAdapter` lazily from `channels.platforms` for the previous
    package-level contract;
  - deleted the old `gateway/platforms/` package directory after the final
    owner migration.
- Completed slice `yuanbao responsibility split`:
  - split `channels/platforms/yuanbao.py` into explicit owners:
    `yuanbao_constants.py`, `yuanbao_markdown.py`, `yuanbao_auth.py`,
    `yuanbao_inbound.py`, and `yuanbao_outbound.py`;
  - kept `channels.platforms.yuanbao` as the public API/re-export surface for
    existing imports, while moving real responsibilities to their owner files;
  - reduced all Yuanbao files below the 2000-line project file-size limit;
  - validated with Yuanbao unit/integration tests and P1 boundary gates.
- Completed slice `channel support owners`:
  - moved platform config ownership to `channels.config`; `hermes_gateway.config`
    imports and re-exports while later gateway slices retire the old module;
  - moved platform registry, session identity/context, runtime status,
    sticker cache, and WhatsApp identity into `channels.*`;
  - added `channels.rich_sent_store` as the real bounded sent-message owner,
    replacing a hidden optional import failure in the Telegram adapter;
  - moved runtime model/provider/reasoning/fallback helpers to
    `hermes_agent.gateway.runtime_config`, with `gateway.run` wrappers
    delegating to the target owner;
  - deleted `gateway/platform_registry.py`, `gateway/session_context.py`,
    `gateway/status.py`, `gateway/sticker_cache.py`, and
    `gateway/whatsapp_identity.py` without compatibility shims.
- Completed slice `slash command shadow removal`:
  - deleted `gateway/slash_commands.py` after verifying it had no production
    entry-point import;
  - kept live slash dispatch in `gateway/run.py` for the later gateway-run
    vertical slice instead of creating a fake `channels/slash_commands` owner.
- Completed slice `model command regression`:
  - fixed `/model` flag parsing to preserve the session/global persistence
    axis;
  - fixed persistence when the existing config stores `model` as a flat string.
- P1 channel-owner migration is machine-verifiable, but the full zero-debt
  effort remains open. Remaining structural debt includes `gateway/run.py`
  vertical decomposition, formal human sign-off, and large adapter-file splits.

### Vertical Slices

0. Platform base contract: move shared adapter contracts to `channels.platforms.base`.
0. Platform shared helpers: move shared adapter helpers to `channels.platforms.helpers`
   and `channels.platforms._http_client_limits`.
1. `telegram` channel: move import ownership, add channel-level smoke test, prove one real-device path.
2. Platform batch 1: `discord`, `slack`, `feishu`, `wechat`, `qqbot`.
3. Platform batch 2: `whatsapp`, `signal`, `sms`, `email`, `dingtalk`, `homeassistant`, `mattermost`.
4. Slash commands: move command registry/runtime to standalone target owner.
5. Update `platform_connections` and gateway launch code to use new channel imports.
6. Delete old `gateway/platforms/*` and old slash-command owner once production imports are gone.

### Exit Gates

- `channels/platforms/*` imports only `hermes_agent.*`, `channels.*`, stdlib, and third-party SDKs.
- Production grep has no `gateway.platforms` import.
- `channels/*` production code has no legacy `gateway.*` import.
- Legacy channel support owners are deleted: `gateway/platform_registry.py`,
  `gateway/session_context.py`, `gateway/status.py`, `gateway/sticker_cache.py`,
  and `gateway/whatsapp_identity.py`.
- Machine verdict passes:

```bash
.venv/bin/python scripts/zero_debt/verdict.py --phase P1 --json
```

- Current machine test evidence:

```bash
.venv/bin/python scripts/zero_debt/verdict.py --phase P1 --json > docs/audits/zero_debt_phase_p1_verdict.json
# 86 checks, 0 failed

.venv/bin/pytest tests/channels tests/observability/test_channels_p1_boundary.py tests/observability/test_module_liveness_audit.py -q
# 72 passed, 1 warning

.venv/bin/pytest tests/channels tests/observability/test_channels_p1_boundary.py tests/observability/test_module_liveness_audit.py tests/gateway/test_config.py tests/gateway/test_platform_registry.py tests/gateway/test_platform_connected_checkers.py tests/gateway/test_session.py tests/gateway/test_session_env.py tests/gateway/test_session_race_guard.py tests/gateway/test_platform_base.py tests/gateway/test_api_server.py tests/gateway/test_api_server_runs.py tests/gateway/test_api_server_jobs.py tests/gateway/test_api_server_toolset.py tests/gateway/test_async_delivery_capability.py tests/gateway/test_status_command.py tests/gateway/test_gateway_shutdown.py tests/gateway/test_runner_startup_failures.py tests/gateway/test_runner_fatal_adapter.py tests/gateway/test_model_command_flat_string_config.py tests/gateway/test_telegram_format.py tests/gateway/test_telegram_network.py tests/gateway/test_sticker_cache.py -q
# 848 passed, 2 skipped, 161 warnings

git diff --check
# passed
```

- Repository-wide file-size debt discovered during P1 closure is recorded in
  `docs/audits/zero_debt_file_size_audit.md`. It is not a P1 platform-owner
  rollback condition, but any future slice touching an over-limit owner must
  extract responsibility in the same phase.

- User real-device smoke must still be recorded in
  `docs/audits/zero_debt_phase_p1_human_signoff.md` before P1 is formally
  closed. The execution checklist is documented in
  `docs/audits/zero_debt_phase_p1_real_device_runbook.md`.

## P2 - Data Plane Repository Ownership

Status: machine complete, awaiting user real-device sign-off. P2 began only
after `scripts/zero_debt/phase_closure.py --phase P1` passed. The original
read-only preflight remains recorded in
`docs/audits/zero_debt_phase_p2_preflight.md` as historical evidence.

Current machine evidence:

- `scripts/zero_debt/verdict.py --phase P2`: pass (15 checks).
- P2 aggregate regression: 355 passed.
- Required gateway/storage/TUI regression: 6371 passed, 53 skipped.
- Production has no `SessionDB` dependency or internal legacy identity alias owner.
- `scripts/zero_debt/phase_closure.py --phase P2` remains blocked until
  `docs/audits/zero_debt_phase_p2_human_signoff.md` records explicit approval.

Real-device procedure:

- `docs/audits/zero_debt_phase_p2_real_device_runbook.md`

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
- Machine verdict passes:

```bash
.venv/bin/python scripts/zero_debt/verdict.py --phase P2 --json
```

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
Phase closure is checked by `scripts/zero_debt/phase_closure.py`; it must fail
while the human sign-off file is missing, pending, or lacks explicit approval to
enter the next phase.
