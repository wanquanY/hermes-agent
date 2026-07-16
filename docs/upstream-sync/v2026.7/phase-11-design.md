# 阶段 11 设计：Automation execution claim 与统一 active-work drain

## 目标与账本范围

阶段 11 手工吸收 `U10`、`U11`、`RT3`。参考的上游失败模式包括：

- `dabae386e`、`9b72995a1`、`ffa525754`、`cd5371876`：one-shot/长脚本 claim
  必须绑定 owner、持续 heartbeat，running-set 检查异常时不能把可能仍活着的任务当死任务。
- `1da89a5f3`、`8f18fa104`、`021ee3454`、`915f1bf1b`：run control、SSE
  stream retention 和 process active-work 生命周期必须分离；drain 要在接受 work 的原子边界生效。
- `e581740aa`：同 board 的 dispatcher tick 必须是跨进程单 writer，loser 明确 skipped。
- `9884b4faa`：Chronos JWKS client 不能每次 fire 重建并触发 fetch storm。

禁止复制上游 cron 产品模型，禁止 merge/rebase/cherry-pick。Dovie automation adapter 仍是产品投影
owner；Hermes 只拥有调度执行事实、claim 和进程生命周期。

## 阶段前根因

1. `cron.jobs.claim_job_for_fire` 是明确标注的单机 no-op；而 gateway tick、manual run、standalone
   scheduler 即使在同一台机器也可能是多个进程，当前会重复执行。
2. `jobs.json` 的多数 read-modify-write 只有 `threading.Lock`，跨进程 claim 即使补上字段也会被并发
   写回覆盖。
3. scheduler 的 `_running_job_ids` 只解决单进程重复提交；长 agent/script 没有 store-level owner
   heartbeat，stale runner 也没有 owner compare-and-refresh。
4. Gateway 只 drain `_running_agents`；API chat/Responses、`/v1/runs`、cron 与 TUI worker 各自维护
   活跃集合，restart 时可能漏掉 work，或把 SSE retention 误当成 run liveness。
5. Kanban task claim 是 per-task CAS，但两个 dispatcher 仍可同时执行整轮 reclaim/spawn/write；当前
   没有 board-scoped dispatch owner。

## 唯一 owner 设计

### 跨进程锁 primitive

`hermes_agent.storage.process_lock` 提供唯一的跨平台 advisory exclusive lock：POSIX 使用 `flock`，
Windows 使用 byte-range lock。它只区分 acquired/contended；打开、加锁或平台能力异常都抛出结构化
错误，调用方必须 fail closed，不能无锁继续写。

- cron 的 jobs read-modify-write 使用 process lock + process-local `RLock`。
- Kanban dispatcher 使用同一 primitive 的 non-blocking、board-scoped sidecar lock。
- 数据文件仍由既有 atomic replace/SQLite transaction owner 持久化；锁不复制业务逻辑。

### Automation execution claim

`cron.jobs` 是 job store 与 claim CAS owner：

- `claim_job_for_fire(job_id, owner)` 在一次跨进程锁内检查旧 claim、写入 owner/timestamp，并为 recurring
  job 原子推进 `next_run_at`。manual、gateway 与 standalone tick 共用。
- scheduler 为每次 execution attempt 生成唯一 owner 并把它携带到 `run_one_job`；完成、失败、drain
  timeout 和 heartbeat 都 compare owner。旧 runner 不能刷新或完成 replacement owner。
- cron 线程不能被 Python 安全强杀；drain deadline 只记录 timeout 事实并保留 claim，不会假装任务已终态
  或释放 owner。线程正常结束时按 owner 终态化；进程退出后由 TTL 路径接管，避免旧线程仍有外部副作用时
  新 scheduler 重入同一 execution body。
- long agent 和 long script 均 heartbeat；无限 agent timeout 也使用低成本 monitor poll，不再永久阻塞
  在 future.result。
- claim 尚新时不进入 due；claim 过期但本进程 running-set 仍活跃时保留。running-set 查询异常按“可能
  活跃”处理并告警，优先不重复副作用。
- exactly-once 的严格边界是“同一 scheduled execution attempt 只进入一次 Hermes execute body”。外部
  下游若不支持 idempotency key，无法在进程崩溃于外部副作用后、落 terminal result 前提供数学意义的
  exactly-once；该限制必须可见，不能伪称已解决。

`9884b4faa` 对当前仓库是 `skip-with-reason` 子项：本地没有 Chronos provider/JWKS verifier，创建未被
任何入口使用的 cache 会成为 dead code。未来引入 Chronos 时，JWKS client 必须由 provider lifecycle
持有并有 TTL/size 边界，不能每 fire 构造。

### Kanban dispatcher single owner

新增小型 `hermes_cli.kanban_dispatch_lock`，不继续扩大已超过 6000 行的 `kanban_db.py`：

- lock key 是 resolved board DB path 的 `.dispatch.lock` sibling，不同 board 可并行。
- `dispatch_once` 只负责取得 non-blocking lease，然后调用原有 body；loser 返回
  `DispatchResult(skipped_locked=True)` 且零 DB/spawn side effect。
- path resolution、open、lock 或 unlock 异常均 fail closed 并返回明确 reason/日志；不采用上游
  “锁失败则无锁 dispatch”的降级。

### Transport-neutral active-work registry

新增 `hermes_agent.application.active_work_registry`，作为一个进程内所有 work 的生命周期事实源；
surface 自有 map 只保留查找/取消投影，不再决定 drain 是否完成。

- 状态机：`accepting -> draining -> stopped`。`begin_drain` 与 `register` 在同一锁下线性化；drain
  开始后所有新 work 得到结构化 `WorkRejected`。
- lease 包含稳定 work id、kind、surface、started_at，以及可选 persist-timeout/cancel callback。
  completion/release 幂等。
- drain 先拒绝新 work并等待期限；超时先持久化 terminal/recovery 状态，再在锁外取消，最后有界等待。
  callback 异常被逐项记录，不能阻止其他 work 收敛。
- Gateway message、API chat/Responses 与 `/v1/runs`、cron、TUI worker 共用同一 API。API stream queue
  retention 不计作 active run；run terminal 后即释放 lease，即便客户端尚未消费 SSE。
- gateway/worker 启动显式开启一个 process generation；测试与同进程 restart 只有在上一 generation
  已无 active work 时才能 reopen，避免静默跨代接收。

## 可验收退出条件

1. 两个进程/线程同时 claim 同一 job，只有一个 execution owner；stale owner 不能 heartbeat/complete
   replacement，long agent/script claim 不过期。
2. running-set 检查异常保留 one-shot；owner 死亡且 claim 过期后可安全接管；recurring next run 与 claim
   在同一写入事务推进。
3. 两个 dispatcher 同 board 只有一个 tick 执行；不同 board 可并行；锁 primitive 异常 fail closed。
4. Gateway/API/TUI/worker/cron 的 registry 行为测试覆盖：drain 后拒绝新 work、期限内完成、超时先
   persist 后 cancel、callback 异常隔离、run terminal 与 stream TTL 解耦。
5. restart/shutdown 仍保留现有 session resume_pending、agent interrupt、approval release 与 subprocess
   cleanup；统一 registry 不能成为第二套业务状态库。
6. cron、Kanban、Gateway/API、TUI worker、Dovie automation contract、Hermes 全量、静态/分层/
   observability 和 Doxie `test:gates` 全部通过。

## 阶段收益

- 同机多进程、gateway/standalone/manual 竞争时，Automation 不再重复进入执行体；长任务不会因为
  claim TTL 被第二个 scheduler 误抢。
- Gateway restart/升级围绕一个 active-work 定义排空，不再遗漏 API、cron 或 worker work，也不把
  已结束 run 的 SSE 保留时间误算成活跃执行。
- 同 board Kanban dispatcher 成为可证明的单 writer，避免重复 spawn 与多 writer SQLite 风险。
- Doxie 继续拥有 Automation 产品语义，可靠性能力留在 Hermes execution kernel，职责没有倒置。
