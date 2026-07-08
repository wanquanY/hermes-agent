# P2 Slice 1 执行规格：Session Repository Ownership

状态：`in_progress_checkpoint_9`

## 前置门槛

本 slice 只能在以下命令通过后开始：

```bash
.venv/bin/python scripts/zero_debt/phase_closure.py --phase P1
```

P1 human sign-off 已通过，P2 已开始执行。当前 checkpoint 已完成
`gateway.SessionStore`、TUI `session.create` 的 repo-backed 写入迁移，以及
TUI `session.list/session.most_recent/session.index.list` 的 read-model owner
迁移，以及 TUI `session.title/session.status` 的 repo-backed metadata 迁移，
将 TUI `session.delete` 迁到独立 domain deletion service，并将 TUI
`session.resume` 的 stored metadata/reopen/compression-tip 解析迁到
`SessionRepoImpl`，同时将 `session_history` 中的 session row identity
解析迁到 repo。

## 目标

把 `session.create/get/list` 的生产存储所有权收口到
`hermes_agent.repositories.session_repo.SessionRepoImpl`。

当前 dispatch 入口审计：

- `docs/audits/zero_debt_phase_p2_slice1_session_dispatch_map.md`

目标链路：

```text
gateway dispatch -> session method -> SessionRepoImpl -> SQLite sessions/session_index -> wire response
```

## 当前可用目标 owner

- `hermes_agent/repositories/session_repo.py`
  - `SessionRepoImpl.create`
  - `SessionRepoImpl.get`
  - `SessionRepoImpl.list`
  - `SessionRepoImpl.update_index`
  - `SessionRepoImpl.branch`
  - `SessionRepoImpl.close`
- `hermes_agent/gateway/methods/session_methods.py`
  - `make_method_session_create`
  - `make_method_session_get`
  - `make_method_session_list`

## 当前旧 owner

- `gateway/session.py`
  - 直接构造 `SessionDB`
  - 承担 session metadata/transcript 旧职责
- 任何生产路径中直接调用 `SessionDB.create_session/get_session/list_sessions`
  的 session create/get/list 语义

## 执行步骤

1. 建立 production repo wiring
   - 找到当前 `session.*` production dispatch 入口。
   - 不能只验证 `hermes_agent/transport/stdio_daemon.py`；该路径已经
     repo-backed，但 DoXie/TUI sidecar 仍走 `dovie_extension` /
     `tui_gateway.methods.session` 旧入口。
   - 让 `session.create/get/list` 使用同一个 `SessionRepoImpl` construction path。
   - repo connection 必须来自明确的 SQLite connection provider，不能在 method
     内部临时构造 `SessionDB`。

2. 写端到端测试
   - v3 target-path tests already cover `pipeline.dispatch` +
     `SessionRepoImpl`; do not duplicate those tests.
   - New P2 Slice 1 tests must cover the DoXie/TUI production sidecar route
     currently exposed by `dovie_extension` and `tui_gateway.methods.session`.
   - 测试必须断言 SQLite `sessions` 和 `session_index` 真实落库。
   - 测试必须断言 wire response 字段：
     - `session_id`
     - `source`
     - `title`
     - `display_title`
     - `session_kind`
     - `conversation_kind`
     - `started_at`
     - `updated_at`

3. 删除旧同职责路径
   - 删除或行为清空 `gateway/session.py` 中 create/get/list 对 `SessionDB`
     的直接生产路径。
   - 不允许保留 fallback。
   - 若旧文件仍需承载非 session repository 职责，必须拆出新 owner，不能继续
     混放在 session storage owner 中。

4. 收紧 P2 verdict
   - `p2:no_sessiondb_production` 应至少移除所有 session create/get/list
     相关 offenders。
   - 如果剩余 offenders 属于 run/message/team mission 等后续 slice，必须在
     P2 preflight verdict 中可解释。

## E2E 测试用例

建议新增或扩展：

```text
tests/gateway/test_session_repository_dispatch.py
```

用例：

1. `session.create` creates `sessions` and `session_index`
2. `session.get` reads the created session via repo-backed path
3. `session.list` returns the created session with limit/source filters
4. invalid params return protocol error without DB side effect
5. no production call in this path imports or constructs `SessionDB`

## 完成定义

本 slice 完成时必须同时满足：

- `session.create/get/list` production dispatch 走 `SessionRepoImpl`
- `gateway/session.py` 不再拥有 create/get/list storage 行为
- E2E 测试证明 dispatch -> repo -> SQLite -> wire response
- `scripts/zero_debt/verdict.py --phase P2 --json` 的
  `p2:no_sessiondb_production` offender 数量下降，且没有新增 identity alias
- `scripts/zero_debt/phase_closure.py --phase P1` 已经通过

## Checkpoint 1 证据

已完成：

- `gateway/session.py` 的 session metadata create/reset/switch 不再直接通过
  `SessionDB` 写入，改由 `SessionRepoImpl` 负责。
- 新增 `hermes_agent/storage/session_repository_db.py`，为
  `SessionRepoImpl` 提供不依赖 legacy state facade 的 SQLite bootstrap。
- `tui_gateway.methods.session` 的 `session.create` 不再直接调用
  `db.create_session`，改由 `SessionRepoImpl.create` 写入 `sessions` /
  `session_index`。
- `SessionRepoImpl` 补齐 TUI create 需要的 `model/model_config/transient`
  metadata，并新增 `reopen()`，`close()` 保持第一次 terminal reason。
- 新增 `tests/gateway/test_session_repository_dispatch.py` 验证
  dispatch -> repo -> SQLite -> wire response。

已运行：

```bash
.venv/bin/pytest tests/repositories/test_session_repo_impl.py tests/gateway/test_session_repository_dispatch.py tests/gateway/test_session.py tests/tui_gateway/test_protocol.py::test_session_create_control_plane_only_persists_through_session_repo -q
.venv/bin/pytest tests/observability/test_zero_debt_gates.py -q
.venv/bin/pytest tests/tui_gateway/test_protocol.py tests/tui_gateway/test_ws_dispatch.py::test_session_list_uses_control_plane_executor tests/tui_gateway/test_ws_dispatch.py::test_control_plane_session_list_is_not_proxied_to_runtime_worker -q
.venv/bin/pytest tests/gateway/test_session_list_allowed_sources.py tests/gateway/test_session_kind_column.py tests/gateway/test_session_list_team_enrichment.py -q
.venv/bin/pytest tests/storage/test_migrations_smoke.py tests/storage/test_migrations_loader.py -q
.venv/bin/ruff check gateway/session.py tui_gateway/methods/session.py hermes_agent/repositories/session_repo.py hermes_agent/storage/session_repository_db.py tests/gateway/test_session.py tests/gateway/test_session_repository_dispatch.py tests/tui_gateway/test_protocol.py tests/repositories/test_session_repo_impl.py tests/observability/test_zero_debt_gates.py
```

当前 P2 verdict 仍应失败，剩余失败项：

- `p2:no_sessiondb_production`
- `p2:no_legacy_identity_alias_internal`

剩余工作：

- `tui_gateway.methods.session` 的 `session.list` 仍依赖
  `db.list_sessions_rich` 与富投影旧 owner，需要拆出 repo/read-model owner 后
  再切。
- `session.messages/delete/title/status/usage` 等同文件旧 DB path 属于后续
  message/read-model slice，不能混在本 checkpoint 中一次性改坏。

## 禁止事项

- 禁止在 DoXie 前端补偿 Hermes session storage 缺口。
- 禁止新增 `stored_session_id` / `stable_session_id` / `runtime_session_id`
  内部 owner。
- 禁止保留旧 `SessionDB` fallback。
- 禁止先删除 `gateway/session.py` 而没有新 owner 承担等价 production 行为。

## Checkpoint 2 证据

已完成：

- 新增 `hermes_agent/read_models/session_list.py`，由
  `SessionListReadModel` 接管 user-facing session list 富投影 SQL。
- `tui_gateway.methods.session` 的 `session.list` 不再调用
  `db.list_sessions_rich`，改为通过 `SessionListReadModel` 从同一个 SQLite
  connection 读取 `sessions/session_lineage` 并返回 wire response。
- `hermes_agent/storage/session_repository_db.py` 的 repo bootstrap 补齐
  `session_lineage`，保证 fresh profile 的 `session.list` 不依赖 legacy facade
  建表副作用。
- `pyproject.toml` package discovery 纳入 `hermes_agent.*`，避免新 owner 在
  editable 以外的安装形态下不可用。
- `tests/gateway/test_session_list_allowed_sources.py` 不再 mock
  `list_sessions_rich`，改为真实 SQLite -> read model -> TUI handler 链路。

已运行：

```bash
python -m py_compile hermes_agent/read_models/session_list.py hermes_agent/read_models/__init__.py tui_gateway/methods/session.py tests/gateway/test_session_list_allowed_sources.py hermes_agent/storage/session_repository_db.py
.venv/bin/pytest tests/gateway/test_session_list_allowed_sources.py tests/gateway/test_session_kind_column.py tests/gateway/test_session_list_team_enrichment.py tests/tui_gateway/test_protocol.py tests/tui_gateway/test_ws_dispatch.py::test_session_list_uses_control_plane_executor tests/tui_gateway/test_ws_dispatch.py::test_control_plane_session_list_is_not_proxied_to_runtime_worker -q
.venv/bin/ruff check hermes_agent/read_models/session_list.py hermes_agent/read_models/__init__.py tui_gateway/methods/session.py tests/gateway/test_session_list_allowed_sources.py pyproject.toml hermes_agent/storage/session_repository_db.py
python scripts/zero_debt/verdict.py --phase P2 --json
python scripts/zero_debt/status.py --json
```

当前验证结果：

- 定向 session-list / protocol / ws-dispatch 测试：`131 passed`
- Ruff：通过
- P2 verdict：仍失败，符合阶段内预期，失败项仍为：
  - `p2:no_sessiondb_production`
  - `p2:no_legacy_identity_alias_internal`

剩余工作：

- `session.messages/delete/title/status/usage` 等同文件旧 DB path 属于后续
  message/read-model slice。
- `gateway/session.py` transcript 相关 legacy storage 仍需在 message slice 迁出。

## Checkpoint 3 证据

已完成：

- `tui_gateway.methods.session` 的 `session.most_recent` 不再调用
  `db.list_sessions_rich`，改为复用 `SessionListReadModel` 的
  `order_by_last_active` projection。
- `session.list` 与 `session.most_recent` 共享同一套 internal source deny-list
  和 session list read owner，不再维护两套最近会话查询逻辑。
- `tests/test_tui_gateway_server.py` 的 `session.most_recent` 用例不再 fake
  `list_sessions_rich`，改为真实 SQLite `sessions` 行 -> read-model ->
  JSON-RPC handler。

已运行：

```bash
python -m py_compile tui_gateway/methods/session.py tests/test_tui_gateway_server.py
.venv/bin/pytest tests/test_tui_gateway_server.py::test_session_most_recent_returns_first_non_denied tests/test_tui_gateway_server.py::test_session_most_recent_returns_null_when_only_internal_rows tests/test_tui_gateway_server.py::test_session_most_recent_folds_db_exception_into_null_result tests/test_tui_gateway_server.py::test_session_most_recent_handles_db_unavailable -q
.venv/bin/ruff check tui_gateway/methods/session.py tests/test_tui_gateway_server.py
.venv/bin/pytest tests/test_tui_gateway_server.py::test_session_most_recent_returns_first_non_denied tests/test_tui_gateway_server.py::test_session_most_recent_returns_null_when_only_internal_rows tests/test_tui_gateway_server.py::test_session_most_recent_folds_db_exception_into_null_result tests/test_tui_gateway_server.py::test_session_most_recent_handles_db_unavailable tests/gateway/test_session_list_allowed_sources.py tests/gateway/test_session_kind_column.py tests/gateway/test_session_list_team_enrichment.py tests/tui_gateway/test_protocol.py tests/tui_gateway/test_ws_dispatch.py::test_session_list_uses_control_plane_executor tests/tui_gateway/test_ws_dispatch.py::test_control_plane_session_list_is_not_proxied_to_runtime_worker -q
python scripts/zero_debt/verdict.py --phase P2 --json
```

当前验证结果：

- 组合 session list / most_recent / protocol / ws-dispatch 测试：`135 passed`
- Ruff：通过
- P2 verdict：仍失败，符合阶段内预期，失败项仍为：
  - `p2:no_sessiondb_production`
  - `p2:no_legacy_identity_alias_internal`

剩余工作：

- `tui_gateway.methods.session` 中 `session.messages/delete/title/status/usage`
  等旧 DB path 属于后续 message/read-model slice。
- `tui_gateway/methods/insights_rollback.py` 仍有 `list_sessions_rich` 辅助查询，
  需要在对应 rollback/read-model slice 中处理。
- `tui_gateway/services/worker_supervisor.py` 仍暴露 legacy worker proxy method
  allowlist，需随 worker storage owner 迁移移除。
- `gateway/session.py` transcript 相关 legacy storage 仍需在 message slice 迁出。

## Checkpoint 4 证据

已完成：

- 新增 `hermes_agent/read_models/session_index.py`，由
  `SessionIndexReadModel` 接管 `session_index` sidebar 富投影 SQL。
- `tui_gateway.methods.session` 的 `session.index.list` 不再调用
  `db.list_session_index`，改为通过 `SessionIndexReadModel` 从 SQLite
  connection 读取。
- 保留 `_ensure_session_index_reconciled(db)` 作为阶段内显式 writer/backfill
  hook；read model 保持只读，`reconcile/repair` 后续迁到独立 writer-domain
  owner。
- 新增 `test_gateway_session_index_list_uses_read_model_not_sessiondb_method`，
  证明 handler 在 DB wrapper 不暴露 `list_session_index` 时仍能通过 `_conn`
  read-model 返回 sidebar rows。
- 新 read-model 没有增加 P2 legacy identity alias offender baseline。

已运行：

```bash
python -m py_compile hermes_agent/read_models/session_index.py hermes_agent/read_models/__init__.py tui_gateway/methods/session.py tests/gateway/test_session_kind_column.py
.venv/bin/pytest tests/gateway/test_session_kind_column.py tests/gateway/test_session_list_team_enrichment.py -q
.venv/bin/ruff check hermes_agent/read_models/session_index.py hermes_agent/read_models/__init__.py tui_gateway/methods/session.py tests/gateway/test_session_kind_column.py
.venv/bin/pytest tests/gateway/test_session_kind_column.py tests/gateway/test_session_list_team_enrichment.py tests/gateway/test_session_list_allowed_sources.py tests/tui_gateway/test_protocol.py tests/tui_gateway/test_ws_dispatch.py::test_session_list_uses_control_plane_executor tests/tui_gateway/test_ws_dispatch.py::test_control_plane_session_list_is_not_proxied_to_runtime_worker -q
.venv/bin/pytest tests/observability/test_zero_debt_gates.py -q
```

当前验证结果：

- 组合 session index/list/protocol/ws-dispatch 测试：`132 passed`
- P2 observability gate tests：`10 passed`
- Ruff：通过

剩余工作：

- `SessionDB.list_session_index` 旧方法仍存在，测试也仍有直接调用；需要在
  后续 repository/read-model parity slice 中迁移直接调用者后删除旧方法。
- `reconcile_session_index` 和 `_repair_session_index_*` 仍属于 legacy facade
  写副作用，需要拆到独立 writer-domain owner。
- `session.messages/delete/title/status/usage` 等旧 DB path 仍待迁移。

## Checkpoint 5 证据

已完成：

- `SessionRepoImpl` 新增 `get_title/get_by_title/set_title`，由 repo owner
  负责 title metadata 的读写、唯一性检查、`display_title` 同步和
  `session_index.title` 同步。
- `tui_gateway.methods.session` 的 `session.title` 不再调用
  `db.get_session_title/db.get_session_by_title/db.set_session_title`，改为
  通过 `SessionRepoImpl` 操作 `sessions/session_index`。
- `tests/test_tui_gateway_server.py` 的 `session.title` 用例迁到真实
  SQLite + `SessionRepoImpl`，不再 mock legacy title DB 方法；异常路径只
  mock repo interface。
- `tests/repositories/test_session_repo_impl.py` 增加 title repo 不变量：
  标题规范化、按 title 查找、`display_title` 同步、index 同步、auto source
  拒绝和重复 title 拒绝。

已运行：

```bash
python -m py_compile hermes_agent/repositories/session_repo.py tui_gateway/methods/session.py tests/test_tui_gateway_server.py tests/repositories/test_session_repo_impl.py
.venv/bin/pytest tests/test_tui_gateway_server.py -k session_title -q
.venv/bin/ruff check hermes_agent/repositories/session_repo.py tui_gateway/methods/session.py tests/test_tui_gateway_server.py
.venv/bin/pytest tests/test_tui_gateway_server.py -k "session_title or session_status or session_delete" tests/tui_gateway/test_protocol.py tests/tui_gateway/test_ws_dispatch.py::test_session_title_uses_control_plane_executor tests/tui_gateway/test_ws_dispatch.py::test_control_plane_session_title_is_not_proxied_to_runtime_worker tests/repositories/test_session_repo_impl.py tests/gateway/test_session_repository_dispatch.py -q
.venv/bin/pytest tests/observability/test_zero_debt_gates.py -q
```

当前验证结果：

- Session title/repo/protocol/routing 组合测试：`22 passed`
- P2 observability gate tests：`10 passed`
- Ruff：通过

剩余工作：

- `session.delete/status/usage/resume/messages` 仍有 legacy DB facade 依赖。
- `SessionDB.set_session_title/get_session_title/get_session_by_title` 旧方法
  仍存在，待所有非 gateway 调用者迁移后删除。

## Checkpoint 6 证据

已完成：

- `tui_gateway.methods.session` 的 `session.status` 不再调用
  `db.get_session/db.get_session_by_title` 读取 stored metadata，改由
  `SessionRepoImpl.get/get_by_title` 读取 title/created/updated。
- `session.status` 的运行态仍通过 `_session_run_snapshot/run_control`
  projection 负责；本 checkpoint 不把 run-state owner 混进 session repo。
- `SessionRepoImpl` 增加 legacy `sessions` schema projection 支持：旧库缺少
  `updated_at/session_kind/conversation_kind` 时，分别通过
  `last_active/start_at` 与明确表达式派生，避免 repo 接入真实旧库时报
  `OperationalError`。
- `tests/test_tui_gateway_server.py::test_session_status_reads_live_gateway_agent`
  改成真实 SQLite/repo-backed stored metadata。
- `tests/repositories/test_session_repo_impl.py` 增加 legacy schema projection
  regression test。

已运行：

```bash
python -m py_compile hermes_agent/repositories/session_repo.py tui_gateway/methods/session.py tests/test_tui_gateway_server.py
.venv/bin/pytest tests/test_tui_gateway_server.py::test_session_status_reads_live_gateway_agent tests/gateway/test_session_list_allowed_sources.py::test_session_status_reads_stored_profile_session_without_runtime tests/tui_gateway/test_protocol.py::test_session_status_returns_machine_readable_run_state tests/repositories/test_session_repo_impl.py -q
.venv/bin/ruff check hermes_agent/repositories/session_repo.py tui_gateway/methods/session.py tests/test_tui_gateway_server.py
.venv/bin/pytest tests/test_tui_gateway_server.py -k "session_status or session_title" tests/gateway/test_session_list_allowed_sources.py::test_session_status_reads_stored_profile_session_without_runtime tests/tui_gateway/test_protocol.py::test_session_status_returns_machine_readable_run_state tests/tui_gateway/test_ws_dispatch.py::test_run_status_uses_control_plane_executor tests/repositories/test_session_repo_impl.py -q
.venv/bin/pytest tests/observability/test_zero_debt_gates.py -q
python scripts/zero_debt/verdict.py --phase P2 --json
```

当前验证结果：

- Session status/title/repo/routing 组合测试：`13 passed`
- P2 observability gate tests：`10 passed`
- Ruff：通过
- P2 verdict：仍失败，符合阶段内预期，失败项仍为：
  - `p2:no_sessiondb_production`
  - `p2:no_legacy_identity_alias_internal`

剩余工作：

- `session.delete/usage/resume/messages` 仍有 legacy DB facade 依赖。
- `session.status` 的 run-state projection 仍经 `run_control`，后续应在
  RunState owner slice 中统一收口。

## Checkpoint 7 证据

已完成：

- 新增 `hermes_agent/domain/session_deletion.py`，由
  `SessionDeletionService` 接管 session destructive lifecycle：
  `sessions` 行删除、`session_index` 清理、`messages` 删除、
  `session_lineage` 断链、`session_branch_requests` 清理，以及
  `HERMES_HOME/sessions` 下 transcript/request dump 文件清理。
- `SessionDeletionService` 刻意不放入 `SessionRepoImpl`：
  deletion 跨越 session metadata、message transcript、branch lineage、
  sidebar index 和文件系统，属于 destructive domain service，不是单一
  repository aggregate 的 CRUD 方法。
- `tui_gateway.methods.session` 的 `session.delete` 不再调用
  `db.delete_session` 或 `db.delete_session_index`；active-session fail-closed
  检查仍在 gateway 入口执行，真正删除交给 `SessionDeletionService`。
- `tests/test_tui_gateway_server.py` 的 `session.delete` 用例从 fake legacy DB
  method 迁到真实 SQLite/service 链路；成功路径断言 `sessions`、
  `session_index` 和 transcript 文件均被清理。
- 新增 `tests/domain/test_session_deletion.py`，覆盖完整 graph/file cleanup、
  orphan `session_index` cleanup，以及 minimal legacy schema。

已运行：

```bash
python -m py_compile hermes_agent/domain/session_deletion.py tui_gateway/methods/session.py tests/test_tui_gateway_server.py tests/domain/test_session_deletion.py
.venv/bin/pytest tests/domain/test_session_deletion.py tests/test_tui_gateway_server.py -k session_delete -q
.venv/bin/ruff check hermes_agent/domain/session_deletion.py tui_gateway/methods/session.py tests/test_tui_gateway_server.py tests/domain/test_session_deletion.py
.venv/bin/pytest tests/test_tui_gateway_server.py -k "session_delete or session_status or session_title" tests/repositories/test_session_repo_impl.py tests/gateway/test_session_list_allowed_sources.py::test_session_status_reads_stored_profile_session_without_runtime -q
.venv/bin/pytest tests/observability/test_zero_debt_gates.py -q
python scripts/zero_debt/verdict.py --phase P2 --json
```

当前验证结果：

- Session deletion domain/gateway 定向测试：`8 passed`
- Session delete/status/title/repo 组合测试：`20 passed`
- P2 observability gate tests：`10 passed`
- Ruff：通过
- P2 verdict：仍失败，符合阶段内预期，失败项仍为：
  - `p2:no_sessiondb_production`
  - `p2:no_legacy_identity_alias_internal`

剩余工作：

- `session.usage/resume/messages` 仍有 legacy DB facade 依赖。
- `SessionDB.delete_session/delete_session_index` 旧方法仍存在，待所有直接
  production 调用者迁移后删除。
- `session.status` 的 run-state projection 仍经 `run_control`，后续应在
  RunState owner slice 中统一收口。

## Checkpoint 8 证据

已完成：

- `SessionRepoImpl` 新增 `resolve_resume_session_id()`，接管
  compression-continuation tip 解析和“空 parent 沿 child 找到首个有消息
  session”的 resume target 语义。
- `SessionRepoImpl.reopen()` 改为 schema-aware：真实旧库缺少
  `updated_at/end_reason` 时仍能通过 `last_active/ended_at` 安全 reopen，
  不再依赖 legacy `SessionDB.reopen_session`。
- `tui_gateway.methods.session` 的 `session.resume` 不再调用
  `db.get_session`、`db.get_session_by_title`、`db.resolve_resume_session_id`
  或 `db.reopen_session`；stored metadata、title lookup、resume target
  re-anchor 和 reopen 均由 `SessionRepoImpl` 负责。
- 本 checkpoint 明确不迁 transcript/message reader：`history_reader` 仍暂时
  读取 legacy message projection，后续由 message read-model slice 统一接管。
- `tests/test_tui_gateway_server.py` 和 `tests/tui_gateway/test_protocol.py`
  的 `session.resume` 用例从 fake legacy DB 方法迁到真实 SQLite +
  `SessionRepoImpl` 链路。
- `tests/repositories/test_session_repo_impl.py` 增加 resume target 和 legacy
  reopen schema 回归测试。

已运行：

```bash
python -m py_compile hermes_agent/repositories/session_repo.py tui_gateway/methods/session.py tests/test_tui_gateway_server.py tests/tui_gateway/test_protocol.py tests/repositories/test_session_repo_impl.py
.venv/bin/pytest tests/test_tui_gateway_server.py -k session_resume -q
.venv/bin/pytest tests/tui_gateway/test_protocol.py -k session_resume -q
.venv/bin/pytest tests/repositories/test_session_repo_impl.py -q
.venv/bin/pytest tests/test_tui_gateway_server.py -k "session_resume or session_status or session_title or session_delete" tests/tui_gateway/test_protocol.py -k "session_resume or session_status or session_title" tests/gateway/test_session_list_allowed_sources.py::test_session_status_reads_stored_profile_session_without_runtime -q
.venv/bin/ruff check hermes_agent/repositories/session_repo.py tui_gateway/methods/session.py tests/test_tui_gateway_server.py tests/tui_gateway/test_protocol.py tests/repositories/test_session_repo_impl.py
.venv/bin/pytest tests/observability/test_zero_debt_gates.py -q
python scripts/zero_debt/verdict.py --phase P2 --json
```

当前验证结果：

- `session.resume` gateway/protocol 定向测试：`6 passed`
- `SessionRepoImpl` 完整测试：`21 passed`
- Session resume/status/title/delete 组合测试：`19 passed`
- P2 observability gate tests：`10 passed`
- Ruff：通过
- P2 verdict：仍失败，符合阶段内预期，失败项仍为：
  - `p2:no_sessiondb_production`
  - `p2:no_legacy_identity_alias_internal`

剩余工作：

- `session.messages` / resume display history / prompt history reader 仍使用
  legacy message projection，需要独立 message read-model owner。
- `session.usage` 本身不依赖 DB，不是当前 P2 storage owner blocker。
- `gateway/run.py`、`cli.py`、`run_agent.py` 仍存在直接 `SessionDB` 生产路径，
  属于 P2 后续垂直切片。

## Checkpoint 9 证据

已完成：

- `tui_gateway.methods.session_history` 新增 repo-backed session row resolver，
  `session.messages`、`session.events`、`session.message_metadata.merge` 和
  stored `session.recall_turn` 不再调用 `db.get_session` /
  `db.get_session_by_title` 做 session identity 解析。
- 该 checkpoint 只迁 session identity owner，不迁 message projection：
  `get_messages_page_as_conversation`、`list_run_events`、
  `merge_message_metadata`、`replace_messages` 仍属于后续 message/event
  read/write owner slice。
- 相关测试从 fake legacy session metadata DB 迁到真实 SQLite +
  `SessionRepoImpl`，保留 message/run-event reader fake 作为测试关注点。
- 修正一次 helper 命名，避免新增 legacy identity alias inventory
  offender。

已运行：

```bash
python -m py_compile tui_gateway/methods/session_history.py tests/gateway/test_session_list_allowed_sources.py tests/tui_gateway/test_protocol.py
.venv/bin/pytest tests/tui_gateway/test_profile_data_context.py tests/gateway/test_session_list_allowed_sources.py -k session_messages -q
.venv/bin/pytest tests/tui_gateway/test_protocol.py -k "session_messages or message_metadata or recall" -q
.venv/bin/pytest tests/tui_gateway/test_protocol.py tests/tui_gateway/test_profile_data_context.py tests/gateway/test_session_list_allowed_sources.py tests/tui_gateway/test_ws_dispatch.py::test_control_plane_session_messages_are_not_proxied_to_runtime_worker -q
.venv/bin/ruff check tui_gateway/methods/session_history.py tests/gateway/test_session_list_allowed_sources.py tests/tui_gateway/test_protocol.py tests/tui_gateway/test_profile_data_context.py
.venv/bin/pytest tests/observability/test_zero_debt_gates.py -q
python scripts/zero_debt/verdict.py --phase P2 --json
```

当前验证结果：

- Session history / protocol / profile-data / ws-dispatch 组合测试：
  `122 passed`
- P2 observability gate tests：`10 passed`
- Ruff：通过
- P2 verdict：仍失败，符合阶段内预期，失败项仍为：
  - `p2:no_sessiondb_production`
  - `p2:no_legacy_identity_alias_internal`

剩余工作：

- `session_history` 的 message page、message metadata merge、stored recall
  rewrite 仍需要迁到 message read/write owner。
- `session.history` 和 `session.resume` 的 display/history hydration 仍经
  legacy message projection。
