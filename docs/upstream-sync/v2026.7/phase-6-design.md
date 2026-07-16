# 阶段 6 设计：Compression 与流稳定状态机

## 目标与范围

按当前 Hermes 分层手工吸收 U5、M4、C1。阶段完成后，自动 compression 是否继续、
provider 是否仍值得重试、follow-up 是否可中断当前 turn，都由持久状态和明确策略裁决，
不再依赖单个 `AIAgent` 实例里的临时计数或 transport 调用时序。

本阶段只参考上游行为和失败场景，不 merge、rebase 或 cherry-pick。阶段起点为
`d1003b326`，执行分支为 `codex/upstream-absorption-v2026.7-all-phases`。

稳定账本是阶段归属事实源：C2（hook 大输出 spill）归 Phase 8，B2/B3（silent marker、
monotonic cooldown）归 Phase 12；Doxie 总计划在 Phase 6 的概括不会改变这些条目的唯一
实施阶段，避免重复实现和阶段验收串线。

## 上游参考

- U5：`76381e2a8e3a`、`32f30d2a4f95`、`83000c7295a0`、`5ce827cac9f9`、
  `af7dceaf77bb`、`2627933f337b`、`8f29c9f4e302`、`fd461b58cad4`。
- M4：`985e19c11`、`d43863f00`、`2985d16be`。
- C1：`5eaccf580`。

## 架构决策

### 1. `SessionRuntimeStabilityService` 是稳定状态唯一持久化 owner

新建独立 `session_runtime_stability` aggregate，不把 breaker 字段继续摊进 `sessions`，
也不允许 agent/gateway 直接写 SQL。repository 只负责 typed snapshot 的原子 upsert；
application service 负责 compression、stream-stale、carry-forward 与 reset 语义。

状态跟随真实 `session_id`。compression rotation 创建 child 后，由 service 在同一逻辑边界
把 verdict/cooldown/streak carry 到 child；in-place compression 继续使用原 row。新会话、
显式 reset 和 route 变化清除旧判定，restart/resume 则从 SQLite 恢复。

### 2. Compression 采用“触发阈值 + 完成边界 + 真实 usage verdict”三段式

- 小于 512K 的 context window 把低配置阈值 raise 到 75%，同时扣除明确的 output token
  reservation；最小 context 的退化窗口在 85% input budget 触发，避免 100% 才压缩。
- no-op、不可压缩窗口立即记一次 ineffective；成功 rewrite 只 arm pending verdict。
- 下一次 provider 真实 `prompt_tokens` 才裁决是否回到 threshold 以下；usage 缺失只消费
  pending，不拿后续无关响应补判。
- deterministic fallback 独立累计并持久化；连续两次 fallback 或 ineffective 后自动
  compression 熔断。手动 `/compress` 可重试并清除 failure cooldown，但不伪造成功 verdict。
- legacy compressor 与 Codex-native compaction 都调用同一个 completed-boundary seam。

跨进程 compression lease 是唯一排他事实。缺少 lease capability 属于部署错误；竞争返回
“本轮跳过”，未知 probe/acquire 错误 fail closed。取得 ownership 后的所有正常与异常退出
统一经过 release seam；该 seam 用 `finally` 停止 refresher、清理本地 marker，并释放旧
session lease。

### 3. Stream-stale breaker 同时覆盖 streaming 与 non-streaming

连续 stale kill 按 session + route fingerprint 持久化；达到可配置阈值后在 open window 内
fail fast，避免每个 turn 重复消耗完整 stale timeout 与 retry budget。成功响应、显式 `/retry`、
新 session、model/provider/fallback route 变化会清零；open window 到期进入一次 half-open
尝试，成功关闭，失败重新打开。

异常文本只包含稳定诊断信息，不包含 credential/base URL；route 只持久化 hash。

### 4. Compression 中的普通 follow-up 降级为 FIFO queue

Gateway busy runtime 先看运行 agent 的本地 in-flight marker，再在线程池查询持久 lease。
确认压缩中或 probe 未知时，普通 `busy_input_mode=interrupt` follow-up 改为 queue，压缩完成
后只消费一次。缺少 capability 的旧测试 double 视为“没有压缩能力”；真实 I/O/状态异常
fail closed。显式 `/stop`、`/new` 走 control path，不受降级影响。

## 自动验收矩阵

1. 128K/256K/512K 窗口、output reservation 与 model switch 的 threshold 计算正确。
2. no-op、真实 usage 未过 threshold、正常 summary、连续 fallback、usage 缺失的 verdict
   只更新一次，restart 与 rotation 后状态不丢。
3. compression lease 竞争、缺失 capability、unexpected acquire/probe error、异常退出与 refresher
   生命周期均 fail closed 且无遗留 lock。
4. streaming/non-streaming stale 达阈值后 fail fast；restart 仍生效；成功、`/retry`、route
   变化和 policy expiry 正确 half-open/reset。
5. compression in flight 时 Gateway follow-up FIFO 排队且不调用 `interrupt()`；压缩结束后
   正常 interrupt 恢复；显式 stop 仍强制终止。
6. legacy 与 Codex-native compaction 都写同一 verdict seam。
7. 阶段专项、migration、Gateway、Hermes 全量、静态/分层/zero-debt 与 Doxie gates 全通过。

## 完成条件

账本、实现、失败场景、性能/重试收益和已知限制写入验收记录；建立本地 Phase 6
checkpoint 后进入 Phase 7。最终统一用户验收前不 push、不 merge。
