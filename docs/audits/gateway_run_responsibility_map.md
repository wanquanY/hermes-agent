# Audit — `gateway/run.py` Responsibility Map

**Purpose**: `docs/hermes_zero_debt_manifest.md` Kill List maps `gateway/run.py` to
"`channels/*` + `hermes_agent/gateway/*` — Platform/slash responsibilities moved".
**This mapping is under-specified.** The file is 18668 lines, hosts 1 top-level
class (`GatewayRunner`, ~10000 lines) + 45 module-level utilities + 3 entry
points, most of which are **gateway daemon runtime concerns** that fit
neither `channels/*` (adapter layer only) nor `hermes_agent/gateway/*`
(JSON-RPC dispatch layer only).

This audit enumerates the 13 responsibility blocks inside `gateway/run.py`
and proposes an owner for each so P1 slice 5–6 does not hit a wall.

**Author**: parallel-session audit; hand this to codex for absorption into
`docs/hermes_zero_debt_manifest.md` (Rebuild List) and
`docs/hermes_zero_debt_execution_plan.md` (P1 slice list).

---

## Summary counts

- Top-level utilities: **45 functions** (`_gateway_*`, `_load_*`, `_resolve_*`, …)
- `GatewayRunner` class methods: **211**
- Entry points: `start_gateway()` (async, line 18179), `main()` (line 18635), `_start_cron_ticker()` (line 18084)
- Slash-command handlers in `GatewayRunner`: **42** (already partially split to `gateway/slash_commands.py` mixin)

## The 13 responsibility blocks

Each block lists sample methods + line ranges (approximate) + proposed
target owner post-P1.

### Block 1 — Gateway daemon process lifecycle (~800 LOC)

Sample: `start_gateway()`, `main()`, `_start_cron_ticker()`,
`GatewayRunner.__init__`, `should_exit_cleanly`, `should_exit_with_failure`,
`exit_reason`, `exit_code`, `_request_clean_exit`, `stop()`,
`wait_for_shutdown()`, `shutdown_signal_handler`, `restart_signal_handler`.

**Proposed owner**: new package `hermes_gateway/` (or
`hermes_agent/hosts/gateway/`).
This is the `python -m gateway.run` daemon body. It orchestrates adapters,
signals, restart, and exit code — none of which fit `channels/*` or
`hermes_agent/gateway/*`.

### Block 2 — Adapter lifecycle & health (~1200 LOC)

Sample: `_safe_adapter_disconnect`, `_adapter_disconnect_timeout_secs`,
`_connect_adapter_with_timeout`, `_platform_connect_timeout_secs`,
`_handle_adapter_fatal_error`, `_pause_failed_platform`,
`_resume_paused_platform`, `_increment_restart_failure_counts`,
`_clear_restart_failure_count`, `_suspend_stuck_loop_sessions`.

**Proposed owner**: `hermes_gateway/adapter_supervisor.py` (or a submodule
of Block 1's owner). Not `channels/*` because supervision policy is
gateway-side, not adapter-side.

### Block 3 — Slash-command dispatcher core (~600 LOC)

Sample: `_check_unavailable_skill`, `_skill_slug_from_frontmatter`,
`_platform_config_key`, `_teams_pipeline_plugin_enabled`,
`_wire_teams_pipeline_runtime`, `_warn_if_docker_media_delivery_is_risky`,
`_has_setup_skill`, slash-command dispatch glue in `_handle_message`.

**Proposed owner**: `channels/slash_commands/dispatcher.py` (P1 rebuild
target the manifest already lists as `channels/slash_commands/`).
The 42 `_handle_*_command` methods themselves already live in
`gateway/slash_commands.py` (mixin) and move together.

### Block 4 — Voice / TTS mode state (~300 LOC)

Sample: `_voice_key`, `_load_voice_modes`, `_save_voice_modes`,
`_set_adapter_auto_tts_disabled`, `_set_adapter_auto_tts_enabled`,
`_sync_voice_mode_state_to_adapter`, `_should_send_voice_reply`.

**Proposed owner**: `channels/voice_mode.py` (per-channel adapter feature)
OR `hermes_gateway/voice_mode.py` (if it needs cross-adapter coordination).
Read the impl during migration to decide — most methods take an `adapter`
argument, suggesting per-channel is fine.

### Block 5 — Telegram topic-mode & thread-metadata (~700 LOC)

Sample: `_telegram_topic_mode_enabled`, `_is_telegram_topic_root_lobby`,
`_is_telegram_topic_lane`, `_should_send_telegram_lobby_reminder`,
`_telegram_topic_root_lobby_message`, `_telegram_topic_root_new_message`,
`_telegram_topic_new_header`, `_telegram_topic_help_text`,
`_record_telegram_topic_binding`, `_recover_telegram_topic_thread_id`,
`_thread_metadata_for_source` (called from ~6 sites).

**Proposed owner**: `channels/platforms/telegram/topic_mode.py`.
This is Telegram-adapter-specific UX. Do not scatter across generic
`channels/`.

### Block 6 — Session queue & FIFO (~500 LOC)

Sample: `_queue_during_drain_enabled`, `_enqueue_fifo`,
`_promote_queued_event`, `_queue_depth`, `_queue_or_replace_pending_event`,
`_dequeue_pending_event`, `_clear_goal_pending_continuations`,
`_is_goal_continuation_event`.

**Proposed owner**: `hermes_gateway/session_queue.py`. This is
gateway-runtime bookkeeping (queues events destined for a busy agent),
not adapter-side, not JSON-RPC.

### Block 7 — Session expiry & handoff watchers (~600 LOC)

Sample: `_session_expiry_watcher` (line 4711), `_handoff_watcher` (4493),
`_process_handoff` (4543), `_schedule_resume_pending_sessions`.

**Proposed owner**: `hermes_gateway/watchers.py` OR
`hermes_agent/orchestration/session_watchers.py`.
`_process_handoff` in particular touches `sessions` + `session_handoffs`
tables — this could delegate to `SessionRepo.branch()` /
`SessionRepo.handoff()` after P2. Recommend Block 7 owner call into
Repo layer, not raw SQL.

### Block 8 — Drain / restart / shutdown (~1400 LOC)

Sample: `_drain_active_agents`, `_interrupt_running_agents`,
`_notify_active_sessions_of_shutdown`, `_finalize_shutdown_agents`,
`_launch_detached_restart_command`, `_schedule_resume_pending_sessions`,
`_persist_active_agents`, `_snapshot_running_agents`, `request_restart`,
`_running_agent_count`, `_status_action_label`, `_status_action_gerund`,
`_should_emit_long_running_notification`, `_cleanup_agent_resources`.

**Proposed owner**: `hermes_gateway/shutdown.py` + `hermes_gateway/restart.py`.
This is process-lifecycle. Some parts (persist_active_agents +
snapshot_running_agents) may move into
`hermes_agent/orchestration/runtime_state.py` if unified with P4's
runtime state target.

### Block 9 — Runtime status projection (~400 LOC)

Sample: `_update_runtime_status`, `_update_platform_runtime_status`,
`_persist_active_agents`, `_snapshot_running_agents`, `status.py` interplay.

**Proposed owner**: `hermes_agent/orchestration/runtime_state.py` (this
is exactly the Rebuild List entry for P4 — "Unified runtime state for
running/approval/completed/unread recovery"). Redirect status writes
here.

### Block 10 — Config / env loading (~1000 LOC)

Sample: `_load_gateway_config`, `_resolve_gateway_model`,
`_resolve_hermes_bin`, `_resolve_runtime_agent_kwargs`,
`_resolve_turn_agent_config`, `_try_resolve_fallback_provider`,
`_load_prefill_messages`, `_load_ephemeral_system_prompt`,
`_load_reasoning_config`, `_load_service_tier`, `_load_show_reasoning`,
`_load_busy_input_mode`, `_load_restart_drain_timeout`,
`_load_background_notifications_mode`, `_load_provider_routing`,
`_load_fallback_model`, `_reload_runtime_env_preserving_config_authority`,
`_parse_session_key`, `_platform_config_key`,
`_resolve_session_agent_runtime`, `_resolve_session_reasoning_config`,
`_set_session_reasoning_override`, `_parse_reasoning_command_args`.

**Proposed owner**: `hermes_gateway/config/` (submodule). Config surface
is broad (adapter selection, model resolution, reasoning, provider
routing, service tier, background notifications). Do NOT flatten into
one file.

### Block 11 — Message-format / redaction utilities (~800 LOC)

Sample: `_gateway_platform_value`, `_redact_gateway_user_facing_secrets`,
`_redact_approval_command`, `_gateway_provider_error_reply`,
`_looks_like_gateway_provider_error`, `_sanitize_gateway_final_response`,
`_prepare_gateway_status_message`, `_telegramize_command_mentions`,
`_coerce_gateway_timestamp`, `_auto_continue_freshness_window`,
`_float_env`, `_is_fresh_gateway_interruption`, `_build_replay_entry`,
`_last_transcript_timestamp`, `_collect_auto_append_media_tags`,
`_collect_history_media_paths`, `_build_media_placeholder`,
`_build_document_context_note`, `_format_duration`,
`_probe_audio_duration`, `_is_control_interrupt_message`,
`_format_gateway_process_notification`, `_drain_gateway_watch_events`,
`_normalize_empty_agent_response`, `_is_dovie_runtime_auth_failure`,
`_should_clear_resume_pending_after_turn`,
`_preserve_queued_followup_history_offset`.

**Proposed owner**: split by purpose:
- Redaction / sanitization → `hermes_gateway/redaction.py`
- Timestamp / freshness → `hermes_gateway/freshness.py`
- Media / audio helpers → `channels/media.py`
- Provider error handling → `hermes_gateway/provider_errors.py`
- Approval / control interrupt helpers → `hermes_agent/domain/interaction.py` neighbors

Do NOT bulk-move to `channels/*` — most of these are gateway-side
policy, not adapter-side.

### Block 12 — SSL / Windows / bootstrap (~200 LOC)

Sample: `_ensure_ssl_certs`, `_ensure_windows_gateway_venv_imports`,
`_home_target_env_var`, `_home_thread_env_var`,
`_restart_notification_pending`.

**Proposed owner**: `hermes_gateway/bootstrap.py` (or absorb into
existing `hermes_bootstrap.py`). Small helper module.

### Block 13 — 42 slash-command handlers (~3200 LOC in `slash_commands.py` mixin)

Already extracted from `run.py` to `gateway/slash_commands.py` (per its own
docstring line 1-9). This block does NOT need re-splitting — it just
needs to move as-is to the new slash commands location.

**Proposed owner**: `channels/slash_commands/handlers.py` (or per-command
submodules if the file gets too large). Rebuild List already lists
`channels/slash_commands/`.

---

## Recommended Rebuild List additions (patch for the manifest)

The manifest currently lists 7 rebuild targets. This audit suggests
adding:

```markdown
| `hermes_gateway/` | Gateway daemon lifecycle: process start/stop/restart/drain, adapter supervision, session queue, watchers, config loading, redaction, freshness. Hosts GatewayRunner class extracted from gateway/run.py. |
| `hermes_gateway/adapter_supervisor.py` | Adapter connect/disconnect timeouts, pause/resume, restart failure counting. |
| `hermes_gateway/session_queue.py` | FIFO queue during drain, promote/dequeue pending events. |
| `hermes_gateway/watchers.py` | Session expiry + handoff watchers (delegates to SessionRepo after P2). |
| `hermes_gateway/config/` | Loader helpers for model / reasoning / provider routing / background notifications / voice / topic modes. |
| `hermes_agent/orchestration/runtime_state.py` | Unified running/approval/completed/unread state — extracted from GatewayRunner._persist_active_agents / _snapshot_running_agents (already in Rebuild List as `hermes_agent/domain/runtime_state.py`, but P4 orchestration owner is more appropriate). |
| `channels/slash_commands/handlers.py` | Move-in-place of the 42 `_handle_*_command` methods currently in gateway/slash_commands.py mixin. |
| `channels/platforms/telegram/topic_mode.py` | Telegram-specific topic thread binding + lobby/lane detection. |
| `channels/media.py` | Adapter-agnostic media helpers (audio duration probe, media path collectors, placeholders). |
```

## Recommended P1 execution-plan additions

P1 currently has slices 0-6. Insert **new slices 5.5 and 5.6**:

```
5.5  GatewayRunner lifecycle body:
     move Blocks 1, 2, 8 from gateway/run.py to hermes_gateway/
5.6  GatewayRunner peripherals:
     move Blocks 4, 5, 6, 7, 10, 11, 12
```

Then existing:

```
6.  Delete old gateway/platforms/* and gateway/slash_commands.py once
    consumers switched (unchanged).
7.  (NEW) gateway/run.py becomes empty shell or removed once Blocks 1-13
    have new homes. P1 is not closed until this file is deleted.
```

## Recommended Kill List refinement

Change:
```
| `gateway/run.py` | `channels/*` + `hermes_agent/gateway/*` | Platform/slash responsibilities moved |
```
to:
```
| `gateway/run.py` | `hermes_gateway/` + `channels/*` + `hermes_agent/orchestration/runtime_state.py` | All 13 responsibility blocks (see docs/audits/gateway_run_responsibility_map.md) migrated; production `from gateway.run` grep is zero |
```

## Open questions for codex / user

1. **Naming of `hermes_gateway/`** — is that acceptable, or should it be
   `hermes_agent/hosts/gateway/` (host = process embodiment)?
2. **Block 7 handoff logic** — some `_process_handoff` SQL touches
   `sessions` + `session_handoffs`. After P2, must this go through
   `SessionRepo` (yes per manifest), and if so, does `SessionRepo` need
   a new `handoff()` API added to its Protocol?
3. **Block 9 runtime status vs Block 8 shutdown snapshot** — both
   persist "active agents". If P4 unifies via
   `hermes_agent/orchestration/runtime_state.py`, does Block 8's
   `_finalize_shutdown_agents` also go through the same runtime state
   API?
4. **`start_gateway()` becomes what?** — `python -m hermes_gateway`?
   `hermes-gateway` console script in `pyproject.toml`? Needs an entry
   point decision.

---

**Method count total accounted for**: ~213 methods + 45 utils + 3
entry points → 261 items. GatewayRunner has 211 methods per grep. Some
methods appear in multiple blocks (e.g. `_thread_metadata_for_source`
is called from Blocks 5 + 8), so the block boundaries are
responsibility-based, not partition-strict.
