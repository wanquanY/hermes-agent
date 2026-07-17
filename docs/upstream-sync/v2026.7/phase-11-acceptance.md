# 阶段 11 验收：Automation ownership 与统一 active-work drain

## 结论

阶段 11 的 `U10`、`U11`、`RT3` 已按 Hermes 当前架构手工吸收，专项、标准全量、
静态/分层/可观测性与 Doxie 桌面门禁全部通过。`9884b4faa` 对应的 Chronos JWKS
生命周期按 `skip-with-reason` 处理：当前仓库不存在 Chronos provider，不能为未接入入口制造
dead code。阶段状态为“实施完成，待 Phase 1-12 统一用户实机验收”。最终统一验收前不 push、
不合并回主开发分支。

## 已关闭的执行与生命周期缺口

### U10：owner-bound Automation execution claim

- cron jobs store 的 read-modify-write 共用跨进程 advisory lock；锁打开、获取或平台能力异常均
  fail closed，不会退化成无锁写入。
- `claim_job_for_fire` 在一次 store 临界区内绑定 execution owner，并为 recurring job 原子推进
  `next_run_at`；gateway、standalone scheduler 和 manual fire 共用同一入口。
- 长 agent/script 持续 heartbeat；heartbeat、正常完成、失败与恢复都 compare owner，旧 runner
  无权刷新或完成 replacement owner。
- drain deadline 只持久化 `drain_timeout_at`，继续保留 owner；不能安全强杀的 Python 线程不会因
  超时被伪装成 terminal 并提前释放 claim。线程最终返回后才按 owner 完成，进程崩溃后由 claim TTL
  恢复。
- 本阶段证明的是“同一 scheduled execution attempt 只进入一次 Hermes execute body”。若外部系统
  不支持 idempotency key，进程在外部副作用后、terminal 落库前崩溃时，不能承诺数学意义的
  end-to-end exactly-once；该限制保留为显式合同。

`9884b4faa` 未直接吸收：本地没有 Chronos provider/JWKS verifier。未来接入 Chronos 时，JWKS
client 必须由 provider lifecycle 持有，并具备 TTL/size 边界；不能每次 fire 重建。

### RT3：Kanban board-scoped single writer

- `hermes_agent.storage.process_lock` 提供唯一跨平台锁 primitive；Kanban 只新增 board-scoped
  dispatch lease，不复制数据库业务逻辑。
- 同一 resolved board DB path 的两个 dispatcher 只有一个进入 tick；loser 返回
  `skipped_locked=True` 且零 DB/spawn 副作用。不同 board 可并行。
- 路径解析、打开、加锁或解锁异常都明确失败，不采用“锁坏了继续 dispatch”的危险降级。

### U11：transport-neutral active-work registry

- `ActiveWorkRegistry` 是进程内活跃执行的唯一生命周期事实源，状态机为
  `accepting -> draining -> stopped`；`register` 与 `begin_drain` 在线性化边界竞争，drain 后新 work
  得到结构化拒绝。
- Gateway message、API chat/Responses、`/v1/runs`、cron 与 TUI worker 统一注册 lease；surface map
  只承担查找/取消投影，不再决定 shutdown 是否完成。
- drain 报告区分 `deadline_expired` 与取消后仍残留的 `timed_out`：即使强制取消最终释放全部 work，
  只要越过优雅期限就不是 clean shutdown，恢复 marker 不会被错误删除。
- persist-timeout、cancel callback 均在 registry 锁外逐项执行并隔离异常；原有 session
  `resume_pending`、agent interrupt、approval release 和 subprocess cleanup 保持 owner 不变。
- API run terminal 即释放 lease；SSE queue 的 terminal retention 仅服务迟到客户端，不再被误算为
  活跃执行。

## 自动化验收证据

### 阶段专项与相邻回归

| 范围 | 结果 | 证明内容 |
|---|---:|---|
| API / Gateway / TUI 聚合 | 275 passed | drain 拒绝、API run/stream 解耦、worker 与兼容入口 |
| Gateway 全目录 | 6,024 passed，53 skipped | message/API/run、shutdown、session 与恢复投影 |
| Cron / Kanban / registry / storage 聚合 | 1,176 passed，1 skipped | claim、heartbeat、single writer、process lock 与 drain |
| Cron timeout 修正专项 | 152 passed | timeout 保留 owner，正常 terminal 后清理 timeout 事实 |
| active registry / clean marker | 13 passed | graceful/forced drain 与真实 clean marker 回归 |

### 标准全量与架构门禁

| 门禁 | 结果 |
|---|---|
| `scripts/run_tests.sh`（默认 28 workers + serial tail） | 1,589 files，29,129 passed，0 failed，586.1s |
| `.venv/bin/ruff check`（阶段文件） | passed |
| production package `compileall` | passed |
| `.venv/bin/lint-imports --config .importlinter` | 1 contract kept，0 broken |
| `pytest -q tests/observability` | 86 passed，13 skipped，1 xfailed |
| `git diff --check` | passed |

### Doxie 集成门禁

在显式移除本地 source-runtime 环境变量后执行：

```bash
env -u DOVIE_HERMES_RUNTIME_MODE -u DOVIE_HERMES_SOURCE_DIR \
  corepack pnpm --filter @dovie/desktop test:gates
```

结果：退出码 0。Electron Node 391 passed、source Node 19 passed、Gateway ABI 205 methods
in sync；Vitest、type-check、ESLint（0 errors，70 个既有 warnings）和 frontend boundary
全部通过。

## 收益确认

- 同机多个 gateway/scheduler/manual 入口竞争时，同一 execution attempt 不再重复进入执行体；长任务
  不会仅因 claim TTL 被第二个 scheduler 误抢。
- Gateway restart/升级围绕一个 active-work 定义排空，能同时覆盖 message、API、cron 和 worker，且
  不把已完成 run 的 SSE 保留窗口误判为活跃任务。
- 同 board Kanban dispatcher 成为可证明的单 writer，消除重复 spawn 与并发整轮写入风险。
- Doxie 继续拥有 Automation 产品投影，Hermes 只拥有执行、claim 与进程生命周期，职责边界没有倒置。

## 统一实机验收重点

1. 同时启动 gateway cron 与 standalone scheduler，令二者竞争同一 one-shot/recurring job；确认只有
   一个 owner、一次执行和一个 terminal result。
2. 运行超过 claim TTL 的 agent 与 script，再启动第二 scheduler；确认 heartbeat 阻止误抢。随后模拟
   进程崩溃并等待 TTL，确认新 owner 可恢复接管。
3. 保持 `/v1/runs` terminal SSE 未消费后触发 restart；确认 terminal run 不阻塞 drain，迟到客户端仍
   可读取保留事件。
4. 分别让 Gateway work 在期限内完成和越过期限；确认前者写 clean marker，后者保留恢复 marker 并先
   persist 再 cancel。
5. 同时启动两个 Kanban dispatcher 指向同一 board，再用两个不同 board 复测；确认同 board 单 writer、
   不同 board 可并行。
6. TUI worker 运行长任务时触发 shutdown；确认停止接收新任务、旧任务按期限收敛，UI 无假 completed。

## 回滚与限制

- 回滚必须整体撤销 process lock、owner-bound claim、Kanban dispatch lease 与 active-work registry；不能
  只移除 heartbeat 或 drain 注册，否则会制造半套所有权协议。
- advisory lock 只协调遵守该协议的本机进程，不是跨主机分布式锁；多主机部署需要独立的 durable
  coordination owner。
- Python 线程无法安全强杀，因此 cron drain timeout 保留 claim 而非伪造终态；外部副作用的最终
  exactly-once 仍要求下游接受 idempotency key。
- Chronos JWKS 生命周期未制造无入口实现；未来引入 provider 时必须单独补 provider-scoped cache 与
  生命周期测试。
