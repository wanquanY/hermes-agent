# 阶段 1 设计：数据完整性与异步 I/O 边界

## 目标

在不合并或 cherry-pick 上游代码的前提下，吸收 U1、R4 的工程收益：统一
Hermes 的 SQLite durability policy，并让 async Gateway、worker 与 Dovie sidecar
不再在事件循环上执行同步 SQLite 或资源回收。

本阶段只建立数据与 I/O 底座，不吸收阶段 2 的工具合同、批次 deadline 或
计量能力。

## 输入与边界

- 阶段起点：`d78d83fe7cf7afd79bdc15973a78e6360137e99f`。
- 执行分支：`codex/upstream-absorption-v2026.7-phase-1`。
- 执行 worktree：`/Users/yangwanquan/syngents/code/hermes-agent-upstream-absorption-v2026.7`。
- 上游行为参考：U1 `9aba95b05317`；R4 `022548036`、`811df74a1`。
- U4 的六个上游提交只作为失败模式参考，由当前 R4 owner 覆盖；B1 同样由
  R4 取代。
- 禁止 merge、rebase、cherry-pick `upstream/main`；实现必须服从当前
  Hermes composition/storage/gateway/orchestration 分层。

## 架构决策

### 1. durability policy 只有一个 owner

`hermes_agent.storage.sqlite_wal` 是连接策略唯一 owner。连接工厂统一应用：

- `busy_timeout`；
- `foreign_keys=ON`；
- WAL，以及不支持 WAL 时的显式 DELETE fallback；
- macOS `synchronous=FULL`；
- macOS `checkpoint_fullfsync=1`。

业务 repository 不重复设置或覆盖这些 PRAGMA。策略同时覆盖 session/profile、
Gateway store、Kanban、API response store、Holographic memory、stdio daemon 与
runtime state merge 等真实连接入口。

### 2. 同步 repository 保持同步，async runtime 只过一个 façade

`hermes_agent.composition.async_sqlite` 提供进程级单一
`AsyncSQLiteBoundary`：

- 专用单线程 owner 保证已接收 SQLite 工作有序执行；
- `contextvars` 跨线程保留；
- caller cancellation/timeout 只停止等待，不在 commit 中途强杀线程；
- shutdown 先拒绝新工作，再 drain 已接收工作；并发 shutdown 共享同一完成屏障；
- Gateway、Team Mission、worker DB RPC 和 Dovie sidecar 共用同一 owner。

storage/repository 的事务和 row mapping 仍是同步代码，不引入第二套 async
repository，也不在业务模块散落 `asyncio.to_thread`。

### 3. 锁只保护内存，不包围 I/O

`SessionStore` 的 sessions index 使用以下协议：

1. 文件读取由独立 load lock 串行，读取时不持有 metadata lock；
2. metadata lock 内只更新 `_entries` 并生成不可变、带 revision 的 snapshot；
3. 原子文件写在 metadata lock 外执行；
4. write lock 和 revision 阻止旧 snapshot 覆盖新状态。

agent cache 的 pop/clear 在锁内完成，agent cleanup、SQLite close、memory/http
client shutdown 和 subprocess cleanup 都在锁外或事件循环外执行。

### 4. 连接与进程生命周期必须真实关闭

- `SessionStore` 只关闭自己创建的 connection，外部注入 connection 的所有权
  保持在调用方。
- Gateway shutdown 关闭真实 `_session_db` 和 `session_store`，不再只查找已退役
  的 `_db` 属性。
- close 通过 async SQLite owner 排队，随后 drain 并关闭 owner thread。
- `/new`、expiry、hygiene 与 shutdown 的资源释放有界执行；超时可以继续控制流，
  但已接收的 SQLite transaction 不被半途取消。

### 5. turn persistence 是独立 owner

完成 turn 的 overflow 分类、transcript 写入、session update 和 compression
exhaustion reset 从 `agent_turn_runtime.py` 提取到
`agent_turn_persistence.py`。一次 turn 的同步持久化工作合并为一次 async boundary
crossing，避免细粒度线程切换，同时保持原有 transcript 与自动 reset 语义。

## 验收要求

1. 真实 SQLite 慢查询期间 event-loop heartbeat 继续运行。
2. cancellation、timeout 与并发 shutdown 不产生半提交事务、线程亲和错误或
   未 drain 工作。
3. macOS child process 在 WAL 写入期间被 `SIGKILL` 后，数据库可 reopen，
   `integrity_check` 通过，durability PRAGMA 仍符合策略。
4. Session index 的慢写不持有 metadata lock，旧 revision 不能覆盖新 revision。
5. async handler 静态门禁禁止直接调用已知同步 storage helper。
6. Hermes 全量回归、ruff、compileall、import-linter、zero-debt、silent fallback、
   absorption ledger 和 `git diff --check` 全绿。
7. Doxie Gateway contract 与 desktop `test:gates` 全绿。

## 阶段收益

- macOS 强制退出、断电式终止或 checkpoint 中断时，降低已确认状态丢失和主库
  损坏风险。
- 慢磁盘、checkpoint、大查询与资源 cleanup 不再冻结 Gateway heartbeat、stream、
  clarify/approval、interrupt 和 Team Mission event routing。
- SQLite 工作、连接 close 与 shutdown drain 有唯一顺序，消除同一进程内多套
  offload executor 和锁内 I/O 的竞态。
- 后续工具、compression、automation 与 lifecycle 阶段建立在可验证的数据与
  终止语义上。

## 阶段状态

阶段设计原定实现完成后停止等待用户验收。用户于 2026-07-16 明确改为全阶段连续
实施、最终统一验收，因此本阶段自动门禁全绿后允许建立本地 checkpoint 并开始
阶段 2；最终统一验收前仍不 push、不合并回主开发分支。
