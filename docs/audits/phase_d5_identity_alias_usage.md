# Phase D5 — Identity alias usage audit

Spec §5.1 (v3.0.2) retires three legacy session identifiers in favour of the single `session_id`:

- `stored_session_id`
- `stable_session_id`
- `runtime_session_id`

Phase G dispatch pre-hook `snake_case`/alias normalization is the authoritative retirement channel — this report gives the concrete usage inventory so Phase D5 can plan the mechanical replacements without missing a call site.

## Summary — files touching each alias

| alias | file count | total occurrences |
|---|---:|---:|
| `stored_session_id` | 54 | 465 |
| `stable_session_id` | 37 | 351 |
| `runtime_session_id` | 46 | 256 |

## Top live-code hotspots per alias

These are the files under `hermes_state*.py`, `tui_gateway/`, `hermes_team_mission/`, `dovie_extension/`, `hermes_agent/`, `hermes_cli/`, `agent/`, `tools/`, `plugins/` — everywhere production dispatch flows through.

### `stored_session_id`

| file | occurrences |
|---|---:|
| `tui_gateway/methods/run.py` | 56 |
| `tui_gateway/services/run_control.py` | 46 |
| `hermes_team_mission/gateway/runtime_methods.py` | 45 |
| `tui_gateway/services/worker_frame_router.py` | 40 |
| `tui_gateway/methods/prompt.py` | 36 |
| `hermes_agent/orchestration/worker_runtime.py` | 32 |
| `tui_gateway/methods/workspace_artifacts.py` | 21 |
| `tui_gateway/services/worker_publish_bridge.py` | 14 |
| `tui_gateway/run_worker.py` | 13 |
| `tui_gateway/server.py` | 11 |
| `tui_gateway/methods/session_branch.py` | 11 |
| `tui_gateway/methods/session.py` | 11 |
| `tui_gateway/services/agent_runner.py` | 9 |
| `tui_gateway/services/agent_run_backend.py` | 9 |
| `hermes_state_runs.py` | 8 |

### `stable_session_id`

| file | occurrences |
|---|---:|
| `tui_gateway/methods/run.py` | 59 |
| `hermes_team_mission/state/session_conversations.py` | 51 |
| `tui_gateway/server.py` | 31 |
| `hermes_team_mission/state/conversation.py` | 31 |
| `tui_gateway/methods/prompt.py` | 27 |
| `hermes_state.py` | 21 |
| `tui_gateway/services/run_control.py` | 12 |
| `hermes_team_mission/state/conversation_status_event.py` | 10 |
| `hermes_team_mission/gateway/common.py` | 10 |
| `tui_gateway/services/profile_context.py` | 10 |
| `hermes_team_mission/read_model.py` | 9 |
| `hermes_team_mission/gateway/conversation_methods.py` | 9 |
| `tui_gateway/methods/session.py` | 8 |
| `tui_gateway/services/worker_db_proxy.py` | 8 |
| `tui_gateway/methods/conversation_render_snapshot.py` | 7 |

### `runtime_session_id`

| file | occurrences |
|---|---:|
| `hermes_state_runs.py` | 47 |
| `tui_gateway/services/run_control.py` | 31 |
| `hermes_team_mission/state/session_graph.py` | 19 |
| `hermes_team_mission/state/session_conversations.py` | 17 |
| `tui_gateway/methods/run.py` | 14 |
| `hermes_state.py` | 9 |
| `tui_gateway/server.py` | 9 |
| `tui_gateway/methods/session_branch.py` | 8 |
| `hermes_team_mission/runtime/history.py` | 7 |
| `hermes_team_mission/read_model.py` | 6 |
| `hermes_team_mission/state/session_rows.py` | 6 |
| `hermes_agent/repositories/run_repo.py` | 6 |
| `hermes_agent/storage/migrations/0040_identity_fk_and_orphan_cleanup.py` | 6 |
| `hermes_team_mission/state/event_log.py` | 5 |
| `hermes_team_mission/state/schema.py` | 5 |

## Sample references

First few lines per alias, for reviewers who want the flavour:

### `stored_session_id`

```
hermes_state_run_event_codec.py:75  event.setdefault("stored_session_id", _row_value(row, "session_id", ""))
hermes_state_branch.py:213  "stored_session_id": row["id"],
hermes_state_run_event_reference.py:94  event.get("stored_session_id")
hermes_state_run_event_reference.py:96  or payload.get("stored_session_id")
hermes_state_run_event_reference.py:138  event.get("stored_session_id")
hermes_state_run_event_reference.py:139  or payload.get("stored_session_id")
hermes_state_tool_events.py:73  event.get("stored_session_id"),
hermes_state_tool_events.py:75  payload.get("stored_session_id"),
```

### `stable_session_id`

```
hermes_state.py:2191  tmc.stable_session_id AS session_id,
hermes_state.py:2234  tmc.stable_session_id AS session_id,
hermes_state.py:2299  tmc.stable_session_id,
hermes_state.py:2311  tmc.stable_session_id,
hermes_state.py:2325  stable_session_id = str(row["stable_session_id"] or row["conversation_id"] or "").strip()
hermes_state.py:2326  if not mission_id or not stable_session_id:
hermes_state.py:2339  conversation_id=stable_session_id,
hermes_state.py:3600  "    ON member_participant.conversation_session_id = COALESCE(NULLIF(tmc.stable_session_id, ''), NULLIF(si.conversation_
```

### `runtime_session_id`

```
hermes_state_run_event_codec.py:76  event.setdefault("session_id", _row_value(row, "runtime_session_id", ""))
hermes_state_run_event_codec.py:77  event.setdefault("runtime_session_id", _row_value(row, "runtime_session_id", ""))
hermes_state_tool_events.py:260  "runtime_session_id": event.get("runtime_session_id") or event.get("session_id"),
hermes_state_tool_events.py:543  event.setdefault("runtime_session_id", _row_value(row, "runtime_session_id"))
hermes_state_tool_events.py:664  # session_id, runtime_session_id, runtime_scope_key, run_id, turn_id,
hermes_state.py:435  runtime_session_id TEXT,
hermes_state.py:460  runtime_session_id TEXT,
hermes_state.py:504  runtime_session_id TEXT,
```

## Phase D5 landing plan

1. **Phase G lands the dispatch pre-hook first** so the alias -> `session_id` translation lives at the boundary. Every internal handler then only sees `session_id`.
2. For each hotspot file above, sed the alias to `session_id`, run the affected pytest suite, and commit per file (or per cluster) to keep review scoped.
3. Migration `0040_identity_fk_and_orphan_cleanup.py` already converted the schema-level FK to `session_id`, so once code paths stop referencing the alias, the legacy column can be dropped in a follow-up migration.

Regenerate with:

```
python -c "from tests.observability.helpers... TODO"
```

(Regenerator script optional — this file is checked in only as a snapshot for the Phase D5 planning session.)