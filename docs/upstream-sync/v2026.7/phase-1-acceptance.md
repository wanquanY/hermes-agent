# 阶段 1 验收记录：数据完整性与异步 I/O 边界

## 状态

**自动验收完成，等待全阶段统一用户验收。**

2026-07-16 用户明确授权后续阶段连续实施，并在全部吸收完成后统一测试验收。
本阶段允许建立本地 checkpoint commit，但统一验收前不推送、不合并回主开发分支。

## 吸收结论

| 账本项 | 决策 | 本地实现证据 |
|---|---|---|
| U1 | absorb | `hermes_agent/storage/sqlite_wal.py` 统一 durability policy，并由真实连接工厂复用 |
| R4 | absorb | `hermes_agent/composition/async_sqlite.py` 是 async SQLite 唯一 owner；Gateway、worker、Team Mission 与 sidecar 已接入 |
| U4 | superseded by R4 | 上游 gateway 同步 DB 修复族按当前拆分架构落到 R4，不恢复上游 monolith |
| B1 | superseded by R4 | 旧 branch 目标由当前 composition/gateway/orchestration owner 覆盖 |

本阶段没有 merge、rebase 或 cherry-pick 上游提交。上游代码只用于识别行为目标、
失败模式和验收场景。

## 自动验收证据

### 专项行为

| 行为 | 证据 |
|---|---|
| 统一 durability policy | `tests/storage/test_sqlite_durability_policy.py` |
| 慢 SQLite 不阻塞 event loop | `tests/repositories/test_async_sqlite_boundary.py`、`tests/gateway/test_async_sqlite_boundary.py` |
| cancellation、timeout、drain、并发 shutdown | `tests/repositories/test_async_sqlite_boundary.py` |
| async handler 不直调同步 storage | `tests/observability/test_async_sqlite_boundary.py` |
| session index 锁外写与 revision 防倒退 | `tests/gateway/test_session.py` |
| `/new` cleanup 锁外、有界、无重复释放 | `tests/gateway/test_session_model_reset.py` |
| worker DB RPC 单 owner、慢 publish 不阻塞 loop | `tests/test_worker_db_proxy.py`、`tests/gateway/test_worker_frame_router.py` |
| 真实 close 与 shutdown lifecycle | Gateway shutdown、restart、clean marker、session store 回归集 |

macOS durability 用 child process 写真实 WAL 后发送 `SIGKILL`，随后 reopen，校验
数据、PRAGMA 与 `PRAGMA integrity_check`。测试不是 mock-only 证明。

### 最终门禁

| 门禁 | 最终结果 |
|---|---|
| Phase 1 专项回归 | SQLite policy、async boundary、session/shutdown、worker DB、subscription lifecycle 等专项矩阵全部通过；最终稳定性复跑 252 passed，0 failed |
| Hermes 全量回归 | `scripts/run_tests.sh`：1548 files，28,789 passed，0 failed；28 workers，822.0s |
| Hermes 静态与架构 | `ruff check .`、`compileall`、`lint-imports`、zero-debt、silent fallback、async SQLite boundary、absorption ledger 与 `git diff --check` 全部通过 |
| Doxie Vitest | 583 suites passed；1,970 passed，3 skipped，0 failed |
| Doxie Node 回归 | Electron Node 391 passed；Source Node 19 passed；0 failed |
| Doxie Gateway ABI | 205 methods in sync；98 active desktop methods covered by 135 requiredMethods |
| Doxie 其余门禁 | `vue-tsc --build`、ESLint（0 error，70 个既有 warning）与 frontend boundary check 通过；`test:gates` 总退出码 0 |

全量复跑期间发现并从根因关闭了两类生命周期竞态：subscription poll 在 owner
关闭数据库前现在必须释放 poll lease；session race 测试在退出 fixture 前等待完整
agent build 收尾。并行测试 runner 的私有临时目录也改为产品无关名称，避免测试
进程之间扫描到彼此的临时 fixture。

Doxie 首轮集成门禁还暴露出一条阶段 0 工具合同调整后的陈旧测试：它仍把
`payload.status` 当作可见详情。生产合同保持不变，只把测试输入改为正式
`payload.detail`；相关 104 项定向回归与完整 `test:gates` 均通过。

## 用户实机验收

### 启动命令

```bash
pkill -f "hermes-agent-upstream-absorption-v2026.7/.venv.*tui_gateway"

cd /Users/yangwanquan/Personal_projects/AIGC_dev/doxie && \
DOVIE_HERMES_RUNTIME_MODE=source \
DOVIE_HERMES_SOURCE_DIR=/Users/yangwanquan/syngents/code/hermes-agent-upstream-absorption-v2026.7 \
DOVIE_STREAM_TRACE=1 \
DOVIE_HERMES_GATEWAY_WS_FRAME_TRACE=1 \
corepack pnpm desktop:dev 2>&1 | tee /tmp/dovie-phase1-dev.log
```

必须指向 Phase 1 worktree，不能指向主 Hermes checkout；否则验收的不是本阶段
代码。

### 重点场景

1. 普通对话连续发送消息，确认 streaming、思考过程与工具卡片持续刷新，没有整页
   卡住或数秒无 heartbeat。
2. 触发 clarify 和 approval，确认交互卡片正常出现、可以提交，状态不是被吞成普通
   system message；提交期间其他 UI 仍响应。
3. 启动一个 subagent 或 Team Mission，同时观察主会话流和成员事件；确认慢持久化
   不阻断事件投影，最终结果和历史可重放。
4. 在 agent 刚启动时快速发送第二条消息，再测试 `/stop`；第二条只能排队，不能启动
   重复 agent，`/stop` 后会话可继续使用。
5. 运行产生 terminal/browser 等资源的 turn 后执行 `/new`；确认 reset 有界完成，旧
   资源被释放，新会话可以立即继续。
6. 一条消息完成后强制退出 Dovie/Hermes，再启动同一环境；确认历史仍在、最后一条
   已完成消息没有消失、会话可以继续发送。

### 通过标准

- 上述 6 个场景均无 Gateway freeze、`SQLite objects created in a thread`、
  `database is locked`、历史倒退或 shutdown 卡死。
- clarify/approval、stream、interrupt 和 Team Mission 状态与阶段 0 已验收行为一致。
- `/tmp/dovie-phase1-dev.log` 没有持续重复的 SQLite、executor 或 close 异常。

## 收益确认

本阶段验收通过只能证明数据完整性和 async I/O 底座落地：

- macOS 异常终止后的数据库恢复更可靠；
- 慢 SQLite 和资源释放不再直接占用事件循环；
- transaction cancellation 与 shutdown drain 有确定语义；
- 后续阶段可以复用唯一 I/O owner。

它不代表工具合同、安全底座、runtime lifecycle 或本轮全部上游吸收已经完成，也不
构成 GA 判断。

## 执行模式变更

原定“每阶段停止等待人工验收”已由用户于 2026-07-16 明确调整为：每阶段继续保留
独立设计、自动门禁、收益证据和本地 checkpoint，但阶段之间不等待人工操作；
Phase 2-12 全部完成后统一实机验收。此变更不降低任何机器门禁，也不授权提前
push 或 merge。
