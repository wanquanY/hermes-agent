# Hermes Zero Debt P2 预检审计

审计日期：2026-07-08

状态：`preflight_only`

## 边界

P1 尚未获得用户真机签收，本审计只做 P2 只读预检，不代表 P2 已经开始。
在 `docs/audits/zero_debt_phase_p1_human_signoff.md` 明确通过、且
`scripts/zero_debt/phase_closure.py --phase P1` 通过之前，不得修改 P2
生产数据面路径。

当前状态可用以下只读命令确认：

```bash
.venv/bin/python scripts/zero_debt/status.py --json
```

## P2 目标

P2 的目标是把生产存储所有权从 `SessionDB` / `hermes_state*` 迁移到
repositories 与 domain services：

- `SessionRepoImpl` owning `sessions` / `session_index`
- `AgentProfileRepoImpl` owning agent profile tables
- `RunRepoImpl` + `EventLedger` owning `runs` / `run_events`
- `RunTerminator` owning terminal run state
- `MessageRepoImpl` owning `messages`
- later domain services owning participants, team mission activity, interaction lifecycle

## 已存在目标 owner

| Owner | 当前状态 | 备注 |
|---|---|---|
| `hermes_agent/repositories/session_repo.py` | 已存在 | 已实现 `create/get/list/update_index/branch/close`。 |
| `hermes_agent/repositories/run_repo.py` | 已存在 | 已接 `EventLedger` 与 `RunTerminator`，但 `run.list` 仍有 gateway 直查。 |
| `hermes_agent/repositories/message_repo.py` | 已存在 | 已实现 append/page/search/replace/metadata。 |
| `hermes_agent/repositories/agent_profile_repo.py` | 已存在 | 已实现 profile/profile_version/growth summary 基础能力。 |
| `hermes_agent/repositories/team_mission_repo.py` | 已存在 | P2 后段 slice 可承接 team mission activity。 |
| `hermes_agent/domain/event_ledger.py` | 已存在 | `run_events` 目标单一写入 owner；仍有 transitional `append_runtime_frame`。 |
| `hermes_agent/domain/run_terminator.py` | 已存在 | terminal state 目标单一 owner。 |
| `hermes_agent/domain/interaction.py` | 已存在 | interaction lifecycle 目标 owner。 |

## 当前生产绕过点

这些路径在 P2 必须被垂直切片逐个迁移，不能在 DoXie 或 gateway 下游兜底。

| 路径 | 现象 | P2 归属 |
|---|---|---|
| `gateway/session.py` | 直接构造 `SessionDB` 管理 session metadata/transcript。 | Slice 1: `session.create/get/list`。 |
| `gateway/run.py` | 大量直接使用 `SessionDB`、`format_session_db_unavailable`、usage/session 查询。 | P2/P3 交界；P2 先迁数据面，P3 再拆 gateway registry。 |
| `run_agent.py` | agent runtime 懒加载 `SessionDB` 做 recall 与消息持久化相关操作。 | Slice 3/5: run/message 数据面。 |
| `cron/scheduler.py` | cron path 直接构造 `SessionDB`，写 runtime session id。 | Slice 1/3: session/run ownership。 |
| `mcp_serve.py` | MCP event bridge poll `SessionDB` transcript。 | Slice 5: message/timeline read API。 |
| `gateway/mirror.py` | mirror path 直接构造 `SessionDB`。 | Slice 5: message/timeline read API。 |
| `hermes_team_mission/gateway/runtime_methods.py` | 大量 `stored_session_id` / `runtime_session_id` / DB 直写。 | Slice 7/8: participants 与 team mission activity。 |
| `hermes_team_mission/gateway/common.py` | session shell 与 runtime binding 仍使用 legacy identity aliases。 | Slice 7/8: team mission runtime binding。 |
| `hermes_state_tool_events.py` | legacy tool/read-model projection 仍携带 `stored_session_id` 和 `runtime_session_id`。 | Slice 6: `tool.*` projection isolated from canonical ledger。 |
| `hermes_state_run_event_reference.py` | run_event decode/reference 仍传播 legacy aliases。 | Slice 3/6: run_events canonical boundary。 |

## 首个推荐垂直切片

P2 不应先删除 `hermes_state.py`，也不应全仓替换 `SessionDB`。首个切片应是：

```text
gateway dispatch -> session.create/get/list -> SessionRepoImpl -> SQLite -> wire response
```

理由：

1. `SessionRepoImpl` 和 `session_methods.py` 已具备目标 API。
2. Slice 范围小于 run/message/team mission，端到端测试容易锁住。
3. 它能建立 P2 的标准迁移节奏：先 production wiring，再删除旧 owner 的同职责路径。

## 首个切片验收标准

Slice 1 结束时必须同时满足：

- `session.create/get/list` 生产 dispatch 只通过 `SessionRepoImpl`。
- 旧 `gateway/session.py` 同职责路径被删除或行为清空，不能作为 fallback 留存。
- 新增 E2E 测试证明：

```text
gateway dispatch -> SessionRepoImpl -> SQLite sessions/session_index -> wire response
```

- P2 production grep 中 `gateway/session.py` 不再直接导入或构造 `SessionDB`。
- 不新增 `stored_session_id` / `stable_session_id` / `runtime_session_id` 内部 owner。

## P2 门禁补强建议

`scripts/zero_debt/verdict.py --phase P2` 已实现为数据面所有权门禁；在
P2 正式迁移前，该门禁预期失败：

- production grep blocks `from hermes_state`, `import hermes_state`, `SessionDB`
- `INSERT INTO run_events` only allowed in `hermes_agent/domain/event_ledger.py`
- `UPDATE runs` only allowed in `hermes_agent/domain/run_terminator.py` and `hermes_agent/repositories/run_repo.py`
- identity alias allowlist remains only `hermes_agent/gateway/pipeline.py`
- phase closure still requires human sign-off

当前 preflight verdict 快照：

- `docs/audits/zero_debt_phase_p2_preflight_verdict.json`
- 状态：`fail`
- checks：10
- failed：2
- 失败门禁：
  - `p2:no_sessiondb_production`
  - `p2:no_legacy_identity_alias_internal`

首个垂直切片执行规格：

- `docs/audits/zero_debt_phase_p2_slice1_session_repo_plan.md`
- `docs/audits/zero_debt_phase_p2_slice1_session_dispatch_map.md`

完整 offender inventory：

- `scripts/zero_debt/p2_inventory.py`
- `docs/audits/zero_debt_phase_p2_offender_inventory.json`
- `docs/audits/zero_debt_phase_p2_offender_inventory.md`

重新生成：

```bash
.venv/bin/python scripts/zero_debt/p2_inventory.py --format json > docs/audits/zero_debt_phase_p2_offender_inventory.json
.venv/bin/python scripts/zero_debt/p2_inventory.py --format markdown > docs/audits/zero_debt_phase_p2_offender_inventory.md
```

## 不允许的做法

- 不允许在 DoXie 前端补偿 Hermes 数据面缺口。
- 不允许保留旧 `SessionDB` 路径作为 fallback。
- 不允许用 alias folding 扩散 `stored_session_id` / `stable_session_id` / `runtime_session_id`。
- 不允许先按文件名删除 owner，而不迁走职责。
