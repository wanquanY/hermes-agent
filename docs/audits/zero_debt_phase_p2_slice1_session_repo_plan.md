# P2 Slice 1 执行规格：Session Repository Ownership

状态：`in_progress_checkpoint_3`

## 前置门槛

本 slice 只能在以下命令通过后开始：

```bash
.venv/bin/python scripts/zero_debt/phase_closure.py --phase P1
```

P1 human sign-off 已通过，P2 已开始执行。当前 checkpoint 已完成
`gateway.SessionStore`、TUI `session.create` 的 repo-backed 写入迁移，以及
TUI `session.list/session.most_recent` 的 read-model owner 迁移。

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
