# ADR-0001 §Phase 2.E-0: record_event activity_id stamp audit

## Methodology

通过 `rg` + 静态分析所有 `db.append_run_event` / `record_event` /
`publish_recorded_event` / 直接 `INSERT INTO run_events` 调用,跟踪每条 push
链路的 frame 来源,判断 `frame["activity_id"]` 在 INSERT 前是否填了带前缀格式
(`mission:<X>` / `chat:<sid>` / `team-conversation:<X>` / `act-*`)。

扫描范围覆盖:

- `hermes_team_mission/runtime/*.py`
- `hermes_team_mission/gateway/*.py`
- `hermes_team_mission/state/*.py`
- `tui_gateway/services/run_control.py`
- `tui_gateway/services/worker_*.py`
- `tui_gateway/methods/*.py`
- `hermes_state_runs.py` 中所有 `run_events` DAO 写入

`tui_gateway/methods/*.py` 中 activity/prompt/dispatch 方法主要创建 activity
command 或 worker dispatch metadata,不直接写 `run_events`;实际写入落到
`run_control`、worker router/bridge、`SessionDB.append_run_event`。

## Inventory

| Call site (file:line) | 触发场景 | Frame 来源 | activity_id 来源 | Stamped? | 备注 |
|---|---|---|---|---|---|
| `tui_gateway/services/run_control.py:1444` | backend `record_event` 入口 | caller params copy | `RunContext.activity_id` via `_apply_run_context_to_frame` | ✓ | `_apply_run_context_to_frame` 本 PR 未改 |
| `tui_gateway/services/run_control.py:1598` | `record_event` 持久化 | normalized frame | top-level/payload activity_id,再由 DAO 抽列 | ✓ | `append_run_event` 写列 |
| `tui_gateway/services/run_control.py:1811` | `publish_recorded_event` | caller params copy | `RunContext.activity_id` pre-stamp 后交给 `record_event` | ✓ | dispatch 行为未改 |
| `tui_gateway/services/run_control.py:1876` | worker crash/cancel synthesized terminal | `publish_run_terminal_event` 自构 frame | `activity_id` 参数 | ✓ | 本 PR 补缺 |
| `tui_gateway/server.py:765` | in-process prompt/session `_emit` | `_sessions` active run + payload | `payload.activity_id` / session RunContext / `chat:<stable_session_id>` | ✓ | 本 PR 补缺 |
| `tui_gateway/services/worker_frame_router.py:237` | worker `EventFrame` → main publish | worker stdout params | `record_run_start.run_context_json` parsed to RunContext | ✓ | worker event replay/publish OK |
| `tui_gateway/services/worker_frame_router.py:356` | abnormal `RunTerminalFrame` | router synthesized terminal kwargs | `dispatch_activity_id` or RunContext activity_id | ✓ | 本 PR 补缺 |
| `tui_gateway/services/worker_publish_bridge.py:266` | worker-side monkey-patched publish | agent/tool `publish_recorded_event` params | active worker RunContext | ✓ | 本 PR 补缺; prevents worker-side original persist NULL before main replay |
| `hermes_agent/orchestration/worker_runtime.py:670` | primary worker dispatch bookkeeping | run.submit/prompt.submit params | `run_context_json` or `dispatch_activity_id` retained in `RunInfo` | ✓ | source for router stamp |
| `tui_gateway/services/activity_reconciler.py:333` | `activity.command.*` events | reconciler self-built frame | explicit `activity_id` arg | ✓ | no RunContext needed |
| `tui_gateway/methods/prompt.py:412` | ordinary prompt activity command | prompt submit params | `chat:<stable_session_id>` | ✓ | command event path OK; `_emit` runtime event path fixed above |
| `tui_gateway/methods/dispatch.py:290` | agent dispatch worker run | dispatch params | `dispatch_activity_id` recorded in router | ✓ | terminal activity projection uses dispatch id |
| `tui_gateway/methods/dispatch.py:449` | team dispatch → team mission create | create metadata | `dispatch_activity_id` passed in mission metadata | ✓ | downstream leader/node use mission RunContext |
| `hermes_team_mission/gateway/runtime_methods.py:599` | @member chat submit | RunContext constructed by gateway | `act-member_chat:<conversation_session_id>:<member>` | ✓ | existing OK |
| `hermes_team_mission/gateway/runtime_methods.py:939` | leader/team conversation submit | RunContext constructed by gateway | `mission:<mission_id>` or `chat:<conversation_session_id>` | ✓ | existing OK |
| `hermes_team_mission/gateway/runtime_methods.py:2257` | `team_mission.node.start` | node run submit params | new RunContext `mission:<mission_id>` | ✓ | 本 PR 补缺 |
| `hermes_team_mission/state/session_events.py:352` | `append_team_mission_run_event` explicit path | event dict + run binding | explicit `mission:<mission_id>` | ✓ | 本 PR 补缺 |
| `hermes_team_mission/runtime/conversation_mirror.py:777` | node/synthesis → shared team conversation mirror | mirror dict 自构 | explicit `team-conversation:<conversation_id>` | ✓ | 本 PR 补缺; no longer inherits source mission activity |
| `hermes_state_runs.py:1115` | public `SessionDB.append_run_event` | caller event | top-level/payload/metadata or explicit `activity_id` kw | ✓/boundary | DAO does not invent activity for arbitrary legacy callers |
| `hermes_state_runs.py:1333` | stream delta coalesce update | existing row + new delta | merged event activity_id | ✓ | 本 PR 补缺 for NULL-old-row + stamped-new-delta case |
| `hermes_state_runs.py:2261` | orphan active-run recovery terminal | direct SQL INSERT | mission binding, team-conversation session, else `chat:<session>` | ✓ | 本 PR 补缺 |

## Findings

### Stamp 缺失链路

- `tui_gateway/server.py:_emit`: ordinary prompt/session runtime events had no explicit activity stamp. Fixed with `payload.activity_id` / session RunContext / `chat:<stable_session_id>`.
- `tui_gateway/services/worker_publish_bridge.py`: worker-side original `publish_recorded_event` could persist before the main router re-stamped. Fixed by stamping active RunContext into the emitted/original frame.
- `tui_gateway/services/worker_frame_router.py:on_run_terminal` + `run_control.publish_run_terminal_event`: abnormal worker terminal events were synthesized without activity. Fixed by carrying `dispatch_activity_id` or RunContext activity id into the terminal frame.
- `hermes_team_mission/gateway/runtime_methods.py:team_mission.node.start`: node runs did not carry `run_context_json`. Fixed by constructing mission RunContext and storing it in both submit params and binding metadata.
- `hermes_team_mission/state/session_events.py:append_team_mission_run_event`: explicit mission event append path did not stamp source event. Fixed with `mission:<mission_id>`.
- `hermes_team_mission/runtime/conversation_mirror.py`: mirror inherited source activity, usually `mission:<id>`, while target activity subscription is team conversation. Fixed with `team-conversation:<conversation_id>`.
- `hermes_state_runs.py:fail_orphaned_active_runs`: direct SQL recovery terminal insert bypassed DAO extraction and wrote NULL. Fixed with mission binding / team conversation / chat fallback.
- `hermes_state_runs.py` stream coalesce update: coalesced rows did not update `activity_id` column if the old row was missing it. Fixed by merging/storing activity_id.

### Stamp 已 OK 链路

- `record_event` and `publish_recorded_event` with RunContext.
- worker router normal `EventFrame` path with `record_run_start.run_context_json`.
- activity reconciler `activity.command.*` events.
- prompt/dispatch activity command metadata.
- leader submit and @member submit RunContext construction.
- `SessionDB.append_run_event` when caller supplies activity in top-level/payload/metadata/kwarg.

### 边界 case / 已知 NULL 链路(故意留空)

- Public DAO compatibility: direct third-party/test calls to `SessionDB.append_run_event(session_id, event)` with no `activity_id` still write NULL. This is not a backend push path; it preserves legacy migration behavior already covered by `storage_backfill_activity_id`. Backend runtime push paths audited above now stamp before or at source.
