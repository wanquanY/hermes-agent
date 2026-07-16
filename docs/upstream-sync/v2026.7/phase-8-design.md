# 阶段 8 设计：Subagent 统一执行生命周期与有界上下文

## 目标与账本范围

阶段 8 手工吸收 `C2`、`CB3`，并验证架构决策 `D2`：Subagent 的同步或异步由
主 Agent 按因果关系显式选择，runtime 负责校验和执行，不把顶层 delegation 一律改成
后台，也不维护同步、daemon background 和 worker dispatch 三套事实源。

上游只作为失败场景与语义参考：

- `eab208db7`：hook 注入超大 context 时保留有界 preview，并把完整内容安全 spill。
- `35a0803a3`：fan-in summary 按父上下文剩余 headroom 分配预算，完整结果可追溯。
- 本地 Activity/Run、worker IPC、RunContext 与 approval owner 是最终架构约束；禁止
  merge、rebase 或 cherry-pick 上游实现。

## 阶段前事实与不能保留的旧行为

1. `delegate_task(background=false)` 同步运行 child，`background=true` 则进入
   `tools.async_delegation` 的进程内 daemon executor 和 module-level record registry。
2. 后台 completion 通过 `process_registry.completion_queue` 回流，Gateway/TUI 将其格式化
   后再次 submit 成 synthetic message；它不是 Activity terminal event，会污染消息语义。
3. `dispatch_agent_async`、`dispatch_team_async` 已持久化 Activity，并经 worker/Team Mission
   runtime 执行，是当前异步生命周期的正确方向。
4. background fan-out 被直接拒绝；现有绿色测试锁定的是旧限制，不能作为阶段完成证据。
5. hook context 和多个 child summary 都可无界进入父 prompt，可能触发重复压缩或 429。

## 唯一 owner 设计

### `SubagentExecutionService`

新增 application service，拥有以下唯一职责：

- 将外部 `background` 兼容字段归一化为 `ExecutionMode.SYNC | ASYNC`；无效值返回结构化
  policy error，绝不静默切换模式。
- 为一次 delegation 创建 parent Activity，为每个 child 创建 child Activity/Run；fan-out
  的 parent 只在全部 child terminal 后聚合状态和输入顺序结果。
- 同步模式等待同一 child runner 的 terminal 状态并返回当前 tool result；父 turn cancel
  传播到尚未 terminal 的同步 child。
- 异步模式将同一 runner 提交到有界 executor，立即返回 typed handle；Activity/Run 是
  唯一状态源，executor 只负责调度，不能再保存第二份业务 record。
- detached async 不响应父 turn 的普通 interrupt，但响应显式 activity cancel、runtime
  shutdown 和 policy cancellation；资源在 terminal path 统一释放。
- terminal result 通过 `activity.*` / run event 发布；不得再伪装成 user/system message，
  不得插入 assistant/tool role pairing 中间。

`tools.delegate_tool` 负责 child spec、权限和 runner 构造；`dispatch_agent_async` 与
`dispatch_team_async` 保持显式永远异步，并复用同一 execution-mode/handle 合同。
`tools.async_delegation` 只能保留薄兼容 adapter，不能拥有 registry、completion queue、
消息注入或独立 persistence。

### 身份、权限与层级

- parent conversation、parent Activity、participant/profile、execution scope、toolsets 与
  approval owner 从父 RunContext/Agent 显式继承；child Run 使用自己的 execution session。
- leaf 永远不能派生；orchestrator 仅在配置 depth 内同步派生。任何异步派生都必须进入
  Activity service，禁止嵌套 daemon tree。
- child tool surface 默认是父 Participant 已授权能力的子集；扩大权限必须先过阶段 7
  的统一 approval gate。
- `dispatch_agent_async` / `dispatch_team_async` API 名称已经声明 ASYNC，传入同步意图是
  validation error，不允许降级。

### 取消、重启与 backpressure

- executor 使用有界 worker 和有界 pending capacity；达到上限返回 `capacity_exceeded`，
  不隐式回退同步，也不无限排队。
- activity cancel 通过唯一 execution registry 定位 interrupt handle；terminal transition
  只有一个 winner，迟到 completion 不能覆盖 cancelled。
- shutdown 先停止接收新任务，再 cancel/drain 有界时间，最后将未收敛 Activity 明确写成
  cancelled/failed；线程不阻止进程退出。
- 进程重启时，持久 Activity/Run 不假装仍可恢复内存 runner：reconciler 必须明确接管
  可恢复 worker Activity，或把本地不可恢复 runner 标记为 interrupted，不留永久 running。

## 有界上下文设计

### C2 hook spill

hook 与 delegation 共用安全的 context-spill primitive：小内容原样返回；超限内容以
`0600` 原子文件保存，prompt 只保留 head/tail preview、原始长度与路径。session 目录名
必须去路径语义；写入失败仍返回有界 preview，不能回退完整 raw 内容。每 session 有数量
和年龄清理上限，防止磁盘成为新泄漏点。

`pre_llm_call` 聚合是唯一 hook context cap owner，覆盖 Python plugin 和 shell hook；
`hooks.output_spill` 是配置子段，不得被解析为 hook event。

### CB3 delegation headroom

所有 child terminal 后、结果进入父 tool result 前统一计算预算：

- 动态预算取父 context 剩余输入 headroom 的固定安全比例，再按实际 summary 数量均分。
- `delegation.max_summary_chars` 提供静态单项 ceiling；有效 cap 取动态与静态最小值。
- 超限 summary 使用相同安全 spill primitive，父上下文保留 head/tail、完整路径和分页提示；
  parent Activity 的 canonical result 保存预算后的投影及完整路径，不复制大文本。
- fan-out 结果始终按输入 `task_index` 排序，预算或完成顺序都不能改变语义顺序。

## 可验收退出条件

1. sync/async 相同 task spec 共用同一 Activity/Run state machine，显式选择得到不同等待
   方式但相同 identity、permission、usage 与 terminal result contract。
2. async 单任务和 fan-out 都立即返回 typed Activity handle；parent/child Activity 可见，
   parent 终态正确汇总 completed/partial failure/cancelled，结果顺序稳定。
3. 普通、Leader、`@member` 与非活跃会话只接收 typed activity/run event；不存在
   `async_delegation` synthetic prompt、伪 user/system message或普通工具 `completed` 投影。
4. 父 interrupt 取消 sync child、不误杀 detached async；显式 cancel 和 shutdown 能终止
   async child，迟到 completion 不能反转终态。
5. depth、role、capacity、toolset narrowing、approval widening、worker restart 和不可恢复
   local runner reconciliation 均有故障注入行为测试。
6. hook context 与 delegation fan-in 的小内容不变；超限内容完整 spill、preview 有界、路径
   不可穿越，write failure fail bounded，数量/年龄清理有效。
7. 阶段专项、Hermes 全量、静态/分层/账本门禁及 Doxie Activity/Gateway `test:gates`
   全绿后，才允许建立 Phase 8 本地 checkpoint。

## 阶段收益

- 主 Agent 能按任务因果关系正确选择同步或后台，不以丢失当前 turn 正确性换并发。
- Doxie 看到的是可持久、可取消、可重连、可归属的 Activity，不再看到合成系统消息。
- fan-out、独立异步 agent 和 Team Mission 共享一种 handle/terminal 语义，消除状态漂移。
- 大 hook 与多 child 汇总不会击穿父上下文或 prompt cache，完整证据仍可按路径读取。
