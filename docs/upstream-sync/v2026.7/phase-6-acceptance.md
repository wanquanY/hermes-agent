# 阶段 6 验收：Compression 与流稳定状态机

## 结论

阶段 6 的 U5、M4、C1 已按当前 Hermes 架构手工吸收。自动 compression 的触发、
完成边界、真实 usage verdict、失败冷却和跨进程排他不再依赖单个 agent 实例；
stream-stale 能跨 turn、restart 和 worker 进程止损；压缩中的普通 follow-up 会保持
FIFO，而显式控制命令仍可中断。最终用户实机验收与 Phase 1-12 统一进行；在此之前
不 push、不合并回主开发分支。

## 条目决策

| ID | 决策 | 当前架构落点 | 验收结果 |
|---|---|---|---|
| U5 | absorb | `SessionRuntimeStabilityService`、`ContextCompressor`、compression lease、legacy/Codex completion seam | threshold、真实 usage、fallback/ineffective、restart/rotation、lease fail-closed 全覆盖 |
| M4 | absorb | `agent.runtime_stability`、streaming/non-streaming API 边界、CLI/Gateway retry 与 model switch | session + route breaker 跨 turn 持久化；expiry half-open；成功、retry、route change 可恢复 |
| C1 | absorb | Gateway busy runtime、本地 in-flight marker、持久 lease probe、Codex native compaction tracker | legacy/Codex 压缩期间普通文本不调用 interrupt，按 FIFO 后续 turn 消费；`/stop` 不受影响 |

C2 仍归 Phase 8，B2/B3 仍归 Phase 12；它们没有因早期计划中的 Phase 6 概括被
提前实现或重复计入收益。

## 唯一状态与跨进程边界

`session_runtime_stability` 是独立 aggregate，repository 只做 typed 原子读写，application
service 拥有 compression、stream breaker 与 child carry-forward 语义。schema 版本由 52
升级到 53；session 删除依靠外键级联清理该 aggregate。

worker 不直接打开 SQLite。`runtime_stability` 已成为显式 worker DB component，其五个
可调用方法逐项进入 IPC 白名单；dataclass 经 IPC 变为 JSON projection 后，agent adapter
仍恢复完全相同的 typed state 语义。该补强来自全量门禁的架构覆盖，而不是在 worker
路径增加旁路数据库连接。

## 自动验收证据

### Hermes 专项与架构门禁

- agent、run_agent、Gateway、domain、migration、CLI store 与 unit-of-work 标准隔离聚合：
  554 files，11,184 passed，0 failed，耗时 128.5s。
- worker DB component、白名单和 JSON projection 修复专项：17 passed；后台进程 timeout
  负载敏感项隔离连续 3 轮、24 passed。
- observability、aggregate single-owner 与 cross-aggregate isolation：88 passed；另有一条
  预期 xfail，不作为通过数伪装。
- `ruff check .`：通过。
- tracked production Python compile：通过。
- import architecture：162 files、328 dependencies、1 kept contract、0 broken。

### Hermes 全量

- 标准完整隔离门禁：1567 files、28,994 passed、0 failed，耗时 548.8s，使用
  28 workers；2 个带 `hermes-test-runner: serial` 声明的进程时序文件在并行主体后串行
  收尾。
- `tests/tools/test_local_background_child_hang.py` 保持 `<4s` 业务阈值不变；标准运行器
  通过声明式 serial tail 隔离共享机器负载，连续 3 轮隔离验证 24 passed。

### Doxie 联调

- Gateway contract：205 methods in sync；98 个活跃桌面方法由 135 个
  `requiredMethods` 覆盖，退出码 0。
- desktop `test:gates`：Vitest、Electron Node（391 passed）、source Node（19 passed）、
  Gateway contract、`vue-tsc --build`、ESLint 与 frontend boundary 全部退出码 0；ESLint
  为 0 error、70 条既有 warning。
- 最终静态复核：`ruff check .`、tracked production Python compile、import architecture
  （162 files、328 dependencies、1 kept、0 broken）、账本测试（3 passed）与
  `git diff --check` 全部通过。

## 阶段收益

- 长会话不再因为低收益 summary 或 deterministic fallback 每个 turn 反复消耗模型调用；
  成功 rewrite 也必须由下一次真实 provider usage 证明已回到阈值以下。
- provider 持续不返回首 token/完整响应时，streaming 与 non-streaming 共用持久 breaker，
  达阈值后立即给出可操作诊断，不再重复支付完整 timeout 和 retry budget。
- restart、resume、compression child rotation 和 worker IPC 不会遗失稳定状态；route 变化、
  `/retry` 和成功响应又能明确解除旧判定。
- legacy summarizer 与 Codex-native compaction 共用完成边界；压缩期间用户连续输入不会
  打断正在提交的摘要或乱序，普通 interrupt 能在压缩结束后自动恢复。

## 最终实机验收重点

1. 用 128K/256K 长会话逼近输入预算，确认约 75% effective window 开始压缩，且一次有效
   压缩后不会立即重复触发。
2. 让 summarizer 连续两次失败或只产出低收益结果，重启 Hermes 后继续对话，确认不会
   进入压缩死循环；手动 `/compress` 仍能显式恢复。
3. 对不可响应 provider 分别走 streaming/non-streaming，达到阈值后确认下一 turn 立即
   fail-fast；`/retry`、切换 model/provider 或等待 policy expiry 后可再试。
4. 在 legacy compression 与 Codex native compaction 中各连续发送两条普通消息，确认
   当前压缩不被 interrupt，两条消息按 FIFO 进入后续 turn，且只消费一次。
5. 压缩中执行 `/stop`，确认 control path 仍会终止；压缩结束后再发送普通 interrupt，
   确认原有 interrupt 行为恢复。
