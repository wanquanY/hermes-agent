# Hermes Agent v3.0.3 架构落地目标执行计划

## 文档目的

本文档用于定义 Hermes Agent v3.0.3 架构重构的完整执行目标、阶段顺序、不可妥协门禁和验收标准。

核心原则：Hermes 必须拥有权威运行时与存储契约；DoXie 只能消费 Hermes 下发的 canonical 契约，不能继续替 Hermes 的 storage、seq、identity、gateway 缺口做前端兜底。

## 目标架构

- `run_events` 是唯一 canonical timeline 与 runtime event ledger。
- `seq_counter` 是唯一运行时 seq 分配来源。
- `tool_events` 只作为 legacy/read-model projection；前端 timeline API 必须从 `run_events` 读取 canonical tool frames。
- `team_mission_events` 只作为 audit projection；runtime activity replay 不允许依赖它。
- `v3_activities` 是面向前端 item projection 与 mission/team activity state 的 v3 原生活动表。
- interaction 生命周期通过 `InteractionRegistry` 持久化为 `_internal.interaction.*` run events，默认 canonical event list 必须过滤 internal events。
- 可见 transcript 身份使用 `session_id` / `conversation_session_id`；运行域身份使用 `runtime_scope_key` / `execution_scope_key`。
- 生产 gateway dispatch 使用唯一 registry / pipeline / auth / error model。
- `SessionDB` 逐步退为 compatibility facade；repository/domain services 拥有生产写契约。

## 不可妥协门禁

- 不允许用 DoXie/前端补偿 Hermes 上游缺陷。
- 不允许新增 runtime `MAX(seq)` 分配。
- 不允许新 domain service 失败后静默 fallback 到旧 legacy 写路径。
- 不允许 canonical timeline 从 `tool_events` 或 `team_mission_events` 输出。
- 不允许测试专属生产 schema。
- 不允许在明确 compatibility adapter 之外继续扩散 `stored_session_id` / `stable_session_id`。
- 不允许通过新增迁移越过冻结的 `0047`，导致未来 `0047` 无法正常启用。
- 不允许文件无限膨胀；文件接近 2000 行前必须拆分职责。

## Goal 0：冻结当前基线

### 目标

在继续大规模切换前，把当前 dirty worktree 固定成可验证、可追踪的基线。

### 工作项

- 记录当前未提交改动分组：storage、domain、gateway、worker、tests、docs。
- 跑核心回归测试并记录当前 pass/fail 状态。
- 将审查报告中的发现分类为：
  - 当前仍成立
  - 当前工作区已修复
  - 因后续实现已过时
  - 明确延期
- 本阶段不做功能改动，只做审计、分组和风险标注。

### 验收

- `git diff --check`
- 当前核心 storage/domain/gateway 测试有明确基线结果。
- `docs/audits/` 下存在一份简短审查分流记录。

## Goal 1：L1 Storage 契约收口

### 目标

保证生产 schema 支撑所有 repository/domain 写路径。测试中不能存在生产 schema 没有的私有表或私有列。

### 当前状态

已部分完成：

- `v3_activities` 已加入生产 `SCHEMA_SQL`。
- `0046_team_mission_event_seq_counter.py` 已创建 `v3_activities`。
- `TeamMissionRepoImpl` 已增加真实 `SessionDB` production schema 集成测试。

### 剩余工作

- 扫描所有 `RepoImpl` 对表和列的依赖。
- 将依赖与生产 `SCHEMA_SQL` / 当前 migrations 对齐。
- 删除或解释所有与生产 schema 不一致的手写测试 DDL。
- 为全部 v3 repositories 增加 production-schema integration tests。

### 验收

- `pytest tests/storage tests/repositories -q`
- 没有 RepoImpl 依赖 test-only table 或 test-only column。
- `git diff --check`

## Goal 2：EventLedger 权威化

### 目标

让 EventLedger 成为 canonical event 持久化与 replay 的权威 API。

### 当前状态

已部分完成：

- J3 guard 限制直接 `INSERT INTO run_events`。
- J4 guard 限制直接 `UPDATE runs SET status`。
- `session.messages` 不再用 `list_tool_events` row model 兜底输出 `toolEvents`。
- `SessionDB.list_tool_events_as_canonical()` 已从 `run_events` 输出 canonical tool frames。

### 剩余工作

- 将 `SessionDB.list_run_events` 收敛到 EventLedger 或 thin EventLedger adapter。
- 确保 recovery 路径没有绕过 EventLedger 直接写 `run_events`。
- 增加 gateway timeline API 守卫，禁止从 `tool_events` 输出 timeline item。
- 保持 `tool_events` 只作为 projection/read model 维护。

### 验收

- `pytest tests/domain/test_event_ledger.py tests/domain/test_j3_event_ledger_single_writer.py tests/test_pr3_tool_events_canonical.py tests/test_pr12_e2e_symptom_regression.py -q`
- grep/AST guard 证明 timeline API 不调用 legacy `list_tool_events`。

## Goal 3：将 `team_mission_events` 从 runtime replay 退役

### 目标

把 mission activity replay 和 watermarks 从 `team_mission_events` 迁移到 canonical `run_events` / activity indexes。`team_mission_events` 只保留为显式审计 API。

### 当前状态

核心切换已完成：

- `runtime.activity.subscribe` 的 mission activity replay 已改为读取 canonical `run_events` activity index。
- structural mission events 与 conversation status events 会写入 `run_events` 的 `mission:<id>` activity index，同时保留 `team_mission_events` audit projection。
- canonical mission activity rows 不写入可见 team transcript session，避免污染聊天 transcript。
- `conversation.render_snapshot` 的 mission activity watermark 已改为 `source="run_events"`。
- 新增 observability guard，禁止 runtime subscribe/snapshot watermark 重新读取 `team_mission_events`。

### 为什么这是高优先级目标

这是当前剩余问题里对 timeline 顺序和 inactive conversation 恢复影响最大的点。它直接影响 DoXie 历史恢复、mission activity 面板、replay cursor 和 completed mission 恢复。

### 工作项

- 识别所有生产读取 `team_mission_events` 的路径。
- 将读取路径分类为：
  - audit API
  - runtime activity replay
  - snapshot watermark
  - diagnostics/storage maintenance
- 将 `runtime.activity.subscribe` 的 mission replay 迁移到 canonical `run_events`，并使用稳定 `activity_id`。
- 将 snapshot activity watermarks 迁移到 canonical seq。
- 保留 `team_mission.events`，但明确标记为 `audit_only=True`。
- 从 runtime-facing snapshot/watermark 输出中移除 `source="team_mission_events"`。

### 验收

- `runtime.activity.subscribe` 不再通过 `list_team_mission_events` 做 runtime replay。
- `conversation.render_snapshot` 不再发布来源为 `team_mission_events` 的 mission activity watermark。
- `team_mission.events` 仍可读取审计数据，并返回 `audit_only=True`。
- `pytest tests/test_runtime_activity_subscribe.py tests/test_run_events_authoritative.py tests/test_hermes_team_mission_gateway.py -q`

### 当前验证

- `pytest tests/observability/test_team_mission_events_audit_only.py tests/test_runtime_activity_subscribe.py tests/test_run_events_authoritative.py tests/test_hermes_team_mission_gateway.py -q`
- 结果：`134 passed`

## Goal 4：Interaction Registry 正式收口

### 目标

interaction lifecycle persistence 由单一 domain service 负责，不再由 prompt/respond/worker 各处散写 DB。

### 当前状态

已完成本轮收口：

- `InteractionRegistry` 对 `requested/resolved/expired` 写入失败改为 fail-fast；非 interaction lifecycle 事件保持 no-op。
- registry 内部使用 `session_id` 作为权威持久化锚点，`stored_session_id` 仅保留为边界兼容字段。
- `PendingRegistry` 不再吞 publish/persist 异常；register/resolve/expire 都在持久化成功后提交本地状态。
- `prompt_respond` 的 in-process unblock 改为先持久化 resolved，再释放等待者。
- worker interaction frame 持久化失败会阻断前端 delivery，避免制造“前端已显示、后端未落库”的假状态。
- clarify gateway 增加按 request id 读取 pending entry 的正式 API，`clarify.respond` 会带上真实 `session_key` 进入 registry。
- 新增 observability guard，禁止生产代码绕过 `InteractionRegistry` 直接拥有 `_internal.interaction.*` run_event 写入。

### 工作项

- 保持 `InteractionRegistry` 作为 `requested/resolved/expired` 的唯一持久化路径。
- 内部变量逐步使用 `session_id`，legacy aliases 只在边界解析。
- `prompt_respond` 与 worker 路径中 interaction persistence 失败必须 fail-fast。
- pending recovery 只读取 internal run events。

### 验收

- `pytest tests/gateway/test_interaction_persistence.py tests/gateway/test_worker_runtime.py tests/gateway/test_worker_frame_router.py -q`
- 默认 `list_run_events` 过滤 `_internal.*`。
- pending recovery 排除 resolved/expired requests。

### 当前验证

- `pytest tests/gateway/test_interaction_persistence.py tests/gateway/test_worker_runtime.py tests/gateway/test_worker_frame_router.py tests/observability/test_interaction_registry_single_owner.py tests/test_pr5_respond_contract.py -q`
- 结果：`93 passed`
- `pytest tests/tools/test_clarify_gateway.py tests/test_pr4_interactive_request_id.py -q`
- 结果：`89 passed`

## Goal 5：Identity 统一

### 目标

停止应用层扩散 `stored_session_id` 和 `stable_session_id`。

### 工作项

- 创建中央 identity adapter，集中解析 legacy wire aliases。
- domain/service/repository 内统一使用：
  - `session_id` 表示可见 transcript anchor
  - `conversation_session_id` 表示明确 conversation owner
  - `runtime_scope_key` 表示执行域
- 按模块替换应用层 `stored_session_id` / `stable_session_id`。
- 增加 grep gate，只允许 docs/tests/migrations/compatibility adapter 使用 legacy names。

### 验收

- 生产应用层 `stored_session_id|stable_session_id` grep 逐步归零。
- 普通 chat、leader chat、member chat、inactive conversation recovery 全部通过。
- 不允许用机械改名隐藏行为变化。

## Goal 6：SessionDB 到 Repository 的生产切换

### 目标

让 repositories 成为生产 storage boundary，并让 `SessionDB` 退为 compatibility facade。

### 工作项

- 引入组合式 storage facade。
- 生产写路径切到：
  - `RunRepoImpl`
  - `MessageRepoImpl`
  - `SessionRepoImpl`
  - `TeamMissionRepoImpl`
  - `AgentProfileRepoImpl`
- `SessionDBRunMixin` 和其他 mixins 逐步降级为 compatibility wrappers。
- 继续新增行为前，必须拆分超大文件。

### 验收

- 核心生产 flows 使用 repository-backed storage。
- `hermes_state_runs.py` 行数持续下降。
- 现有 storage 与 gateway 回归测试通过。

## Goal 7：WorkerPool 单实现

### 目标

将生产 runtime execution 切到新 WorkerPool，并退役旧 `tui_gateway/services/worker_pool.py` 路径。

### 工作项

- 将 gateway submit/run control 接到新 WorkerPool。
- 用 sharded lock 替换旧 process-local DB RPC locks。
- 删除旧 active-runs 状态源。
- 增加 lease、timeout、recovery、并发 submit 测试。

### 验收

- 旧 WorkerPool 不再被生产 import。
- worker lifecycle、run submit/cancel/recovery 测试通过。

## Goal 8：Gateway 单 Registry

### 目标

生产 gateway 只使用一套 registry，删除 method override dispatch。

### 工作项

- 将 `tui_gateway/server.py` dispatch 接入 `hermes_agent/gateway` registry。
- 删除 `DOVIE_GATEWAY_METHOD_OVERRIDES`。
- 将 read-only method metadata 移入 registry definition。
- snake_case/camelCase 归一只保留在边界层。
- auth/error handling 对齐新 pipeline。

### 验收

- gateway permission 与 contract tests 通过。
- 生产 grep 中 `DOVIE_GATEWAY_METHOD_OVERRIDES` 为 0。
- 生产 dispatch 路径走新 registry。

## Goal 9：旧 `gateway/` 目录退役

### 目标

移除 `tui_gateway/` 对根目录旧 `gateway/` 的 runtime 依赖。

### 工作项

- 盘点所有 `tui_gateway/` 对根目录 `gateway/` 的 import。
- 将仍活跃的平台代码迁到明确维护包。
- 删除或归档死代码。
- 测试同步调整到新 import 边界。

### 验收

- `tui_gateway/` import 根目录 `gateway/` 为 0。
- 旧根目录 `gateway/` 被删除，或明确归档为非运行时代码。

## Goal 10：最终 V3 门禁

### 目标

只有当新旧双轨不再同时竞争生产主干时，才能声明 v3.0.3 架构真正落地。

### 验收矩阵

- `run_events`：canonical source of truth。
- `seq_counter`：唯一 seq allocator。
- `tool_events`：projection/read model only。
- `team_mission_events`：audit only。
- `InteractionRegistry`：唯一 interaction persistence owner。
- repositories：生产 storage boundary。
- WorkerPool：单一生产实现。
- Gateway：单一 registry。
- Identity：应用层 legacy names 清零。
- Silent fallback scans：覆盖全仓。
- 文档与实现一致。

## 推荐下一执行目标

建议你下一个设定的目标是：

**Goal 5：Identity 统一**

原因：

- Goal 3 的核心 runtime replay 切换已经完成并有回归测试保护。
- Interaction lifecycle 仍处在“已集中但命名/边界仍有 legacy alias”的过渡状态。
- 在继续做 identity 全量统一前，应先把 interaction 的 domain service 边界锁死。

## 标准验证命令

```bash
pytest tests/storage/test_identity_fk_migration.py tests/storage/test_migrations_smoke.py tests/storage/test_migrations_loader.py -q
pytest tests/domain/test_event_ledger.py tests/domain/test_j3_event_ledger_single_writer.py tests/domain/test_j4_run_state_machine_single_writer.py -q
pytest tests/gateway/test_interaction_persistence.py tests/gateway/test_worker_runtime.py tests/gateway/test_worker_frame_router.py -q
pytest tests/test_pr3_tool_events_canonical.py tests/test_pr12_e2e_symptom_regression.py tests/test_tool_events_read_model.py -q
pytest tests/test_runtime_activity_subscribe.py tests/test_run_events_authoritative.py -q
git diff --check
```
