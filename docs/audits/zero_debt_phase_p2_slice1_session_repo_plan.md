# P2 Slice 1 执行规格：Session Repository Ownership

状态：`blocked_by_p1_human_signoff`

## 前置门槛

本 slice 只能在以下命令通过后开始：

```bash
.venv/bin/python scripts/zero_debt/phase_closure.py --phase P1
```

当前 P1 human sign-off 仍为 pending，因此本文档只是可执行规格，不代表 P2
生产迁移已经开始。

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

## 禁止事项

- 禁止在 DoXie 前端补偿 Hermes session storage 缺口。
- 禁止新增 `stored_session_id` / `stable_session_id` / `runtime_session_id`
  内部 owner。
- 禁止保留旧 `SessionDB` fallback。
- 禁止先删除 `gateway/session.py` 而没有新 owner 承担等价 production 行为。
