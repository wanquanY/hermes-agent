# 阶段 5 设计：Run 身份与会话持久化生命周期

## 目标

按当前 Hermes 分层手工吸收 R2、U6、RT5、RT6、RT7，并对 RT8 做架构等价性
重判。阶段完成后，run identity、当前 turn、tool protocol tail、restart resume 和 session
delete 都由持久事实裁决，普通、leader、`@member` 与非活跃会话不再依赖 transport
内存或调用顺序猜测。

本阶段只参考上游行为和失败场景，不 merge、rebase 或 cherry-pick。阶段起点为
`052118dc2`，执行分支为 `codex/upstream-absorption-v2026.7-all-phases`。

## 上游参考

- R2：`cd5fb760a`、`f7bf74064`。
- U6：`475922f2ce12`、`0b422559f3b2`、`50aebcbcffad`、`69fd846ef86c`、
  `8341d775a97f`。
- RT5：`81d2dc5d0`、`2d286a6d0`。
- RT6：`a7932d86c`、`dba585c17`。
- RT7：`c2db3ed7d`。
- RT8：`56255f83f`。

## 架构决策

### 1. `RunIdentity` 是 run_id 复用的唯一判定 owner

`run_id` 不能只在进程内 `WorkerPool` 唯一。身份由 run_id、conversation session、
worker、runtime scope 和 agent profile 组成，并持久化到 `runs`。同一 run_id 的重复
launch 只有完整 identity 相同才可视为幂等；任一维度不同都抛出 `CrossWiredRunError`，
Gateway 映射为稳定 `RUN_STATE_CONFLICT (5003)`。

launch 必须在追加 `run.started` 前同时校验 pool 与 SQLite；已有 row 的空 worker/profile
可在第一次 launch 原子 claim，但已 claim 的身份不能被替换。`RunRepo.create_run` 不再使用
`INSERT OR REPLACE`。兼容旧事件投影生成的未 claim placeholder：在 worker 首次 claim 前，
repository 可从 canonical `RunContext` 原子校准 session/scope；worker 一旦写入，session、scope、
worker 和 profile 全部冻结，任何后续跨接都结构化拒绝。

### 2. `TurnMessageBuffer` 与 per-agent `RLock` 共同拥有 close/flush handoff

CLI 接受输入后保留同一个 staged user dict，worker 建立 turn 时复用它；close safety-net
和 turn-start/final flush 通过 per-agent reentrant lock 串行化。持久化继续使用 copy-on-write
的 clean override，API-local model/skill/voice note 和 native multimodal payload不会污染返回
history 或 SQLite。

所有直接 `_flush_messages_to_session_db` 调用也经过同一锁；close 在结束 session row 与
memory provider 前先写入 active snapshot。显式 TurnMessageBuffer boundary 和持久键负责
幂等，不以对象别名或可变列表长度猜测已写范围。

### 3. tool protocol tail 在一个纯函数中关闭

`agent.message_sanitization.close_interrupted_tool_sequence` 是唯一 owner：只有中断且尾部是
`tool` 时追加非空 assistant close。正常工具循环、已有 assistant 尾和普通失败不修改。
finalizer 和 retry/backoff/error 的所有中断 early return 都在持久化前调用它。

### 4. provider-bound pre-API copy 承担结构兼容清洗

空或非 list 的 assistant `tool_calls` key、重复 assistant tool-call id、重复 tool result id
只在 provider-bound copy 上规范化，不改写 canonical transcript。`repair_message_sequence`
同时消费第一个匹配 result id，使恢复、压缩和 host-fed history 都满足同一不变量。

### 5. resume 使用持久 interruption marker，空自动恢复 fail-safe

`resume_pending` 的 freshness 同时接受 transcript timestamp 与持久
`last_resume_marked_at`；任一新鲜即可注入恢复说明。startup 生成的空 auto-resume turn
如果仍带 resume_pending，必须转换成非空恢复说明；普通无 caption 图片的空文本保持原样。

### 6. RT8 由“子会话脱钩保留”取代递归级联删除

上游 visited-set 修复的是旧 `_delegate_from` 递归 cascade。当前 Hermes 已把 session
lineage 和 branch request 收敛到 repository/application service，删除父会话时
`orphan_child_references` 原子清空父引用，子会话及其 transcript 保留。因此不存在需要
visited set 的递归遍历，也不能为对齐上游重新引入 cascade。

本阶段把 RT8 重判为 superseded，并用自环、双向环、多个父引用和重复删除证明 operation
只删除显式目标，不会删除父/子之外的 aggregate。

## 自动验收矩阵

1. 两个 worker/profile/scope/session 并发 claim 同一 run_id，第二个稳定得到
   `CrossWiredRun`，pool、runs row 与 `run.started` seq 均不改变。
2. 相同 identity 重试幂等；restart 后从 runs row 恢复真实 worker/profile/scope。
3. close 早于 worker、worker 早于 close、并发 direct flush、缩短 snapshot、多模态/API-local
   override 五组竞态均只持久化一个 clean user turn。
4. finalizer 与三条中断 early return 均关闭 tool tail；非中断和非 tool tail 是 no-op。
5. empty tool_calls、assistant/result 重复 id 在所有 provider 前被清理，canonical 输入不变。
6. stale transcript + fresh resume marker 仍恢复；双 stale 的空 auto-resume 也不向模型发送空 turn；
   ordinary empty input 保持。
7. session deletion 在自环、双向环和多层 lineage 下只删除显式目标，子 transcript 保留。
8. 阶段专项、migration、Hermes 全量、静态/分层/zero-debt 与 Doxie contract/gates 全通过。

## 完成条件

账本决策、实现、失败场景、收益和已知限制写入验收记录；建立本地 Phase 5 checkpoint
后进入 Phase 6。最终统一用户验收前不 push、不 merge。
