# 阶段 8 验收：Subagent 统一生命周期与有界上下文

## 自动验收结论

阶段 8 已完成 `C2`、`CB3` 和架构决策 `D2` 的手工吸收。同步和异步 delegation
不再拥有两套业务状态；唯一 `SubagentExecutionService` 持久化 parent/child Activity 与
Run，异步 executor 只负责有界调度。Gateway、TUI 和 worker 不再把后台完成结果重新
注入 synthetic user/system message，Doxie 只消费 typed Activity/Run event。

机器门禁于 2026-07-16 完成：

- Phase 8 聚合专项：626 passed、0 failed。
- Hermes 全量：29,044 passed、0 failed。第二次全量持续观测至 99.2% 零失败；测试进程
  结束后，将未保留汇总屏的 5 个长尾文件独立复跑，236 passed、0 failed。
- `ruff check .`、`lint-imports`、zero-debt、aggregate single-owner、`git diff --check`
  与 tracked production Python compile：全部通过。
- Doxie desktop `test:gates`：退出码 0；205 个 Gateway 方法同步，98 个活跃桌面方法被
  135 个 required method 覆盖，type-check、lint（0 error）和 frontend boundary 全绿。

第一次 Doxie 门禁曾错误继承实机启动用的 `DOVIE_HERMES_RUNTIME_MODE=source` 与
`DOVIE_HERMES_SOURCE_DIR`，覆盖 Electron 测试自己的临时 runtime 环境并造成 49 项失败；
清除这两个运行态变量后完整门禁通过。该失败属于验收命令污染，不是产品回归。

## 统一实机验收期间补充修复（2026-07-16）

实机异步 subagent 测试发现：parent 已返回并完成时，child 的工具执行和最终模型调用实际
已经完成，但右侧面板仍停留 `running`。根因不是异步 executor 卡住，而是 child 的
`subagent.complete` 在发出时重新读取 parent session 的可变 `active_run_id` / `active_turn_id`；
parent terminal 已清空这些字段，或者下一回合已覆盖这些字段，导致迟到终态无法持久化，
甚至可能被错误归属到新回合。

修复后，delegation 在派发时一次性冻结 parent 的 `run_id`、`turn_id`、
`client_message_id`、`runtime_scope_key` 与 `activity_id`。child 的 progress、output、reasoning
和 terminal event 始终携带该不可变来源；Gateway 优先使用事件来源，只在同步兼容路径缺少
来源时回退 session 当前值。因此 parent 先结束、下一回合已开始、断线后回放三种时序都
收敛到原 Run/Activity，Doxie 不需要超时猜测或伪造完成状态。

补充自动证据：

- Hermes dispatch-origin、Gateway routing、terminal replay 与真实 prompt worker identity scope
  新增 4 条回归，4 passed。最初实现曾在 prompt worker 误用该作用域不存在的局部变量，实机
  重启后立即暴露；现改为从本回合 `turn_metadata.client_message_id` 读取，并由真实
  `prompt.submit -> agent.run_conversation` 测试覆盖，避免回调单测掩盖作用域错误。
- Hermes subagent progress、async delegation、execution service 与 Gateway 相关门禁
  64 passed、0 failed。
- Doxie session event controller、subagent reducer/store 51 passed，`vue-tsc --build` 与
  定向 ESLint 退出码 0；验证 `message.complete` 之后到达或回放的 `subagent.complete`
  仍会把面板收敛为 terminal。
- 一次扩大执行的 158 条用例中有 2 条既有顺序污染失败；两条失败单独复跑均通过，且不在
  本次异步事件代码路径。该结果不冒充全量门禁，本补充修复以定向合同门禁为验收证据。

## 桌面工具展示合同补充（2026-07-17）

Doxie 继续以 Hermes registry 为事实源完成工具展示合同对账：当前 101 个 Hermes 内建工具
由生成契约锁定，另对实机安装的 7 个 Spotify 集成工具建立明确展示定义；本轮补齐 42 个
原先没有 exact display entry 的工具。`get_activity` 现投影为“查询异步任务”状态卡，只展示
状态、任务类型、关联标识和任务/结果摘要，不再把 `result_json`、parent activity 等内部字段
渲染成“结构化数据”。

补充自动证据：

- 展示注册表、结果 presenter 与真实 Vue 工具卡片定向门禁：45 passed、0 failed。
- Doxie desktop 完整 `test:gates`：Vitest 1,984 passed、3 skipped；Electron 391 passed；
  source node spec 19 passed；205 个 Gateway 方法、101 个 Hermes 内建工具合同同步；
  `vue-tsc --build`、ESLint（0 error）和 frontend boundary 全绿。
- 全量 ESLint 的 70 条 warning 均为仓库既有项，本次变更文件没有新增 warning。

## 已锁定的行为合同

- `execution_mode=sync|async` 为显式合同；旧 `background` 仅作兼容归一，无效或冲突意图
  fail closed。`dispatch_agent_async` / `dispatch_team_async` 始终返回 typed async handle。
- sync/async 共用 task spec、RunContext、权限收窄、usage 与 terminal state machine；
  orchestrator 不能把父级没有的 `delegate_task` 能力授予 child。
- async 单任务与 fan-out 先持久化再返回；root 和单 child 均可取消，取消一个 child 不误杀
  sibling，迟到 completion 不得反转 cancelled，外部 Activity cancel 会收敛对应 Run。
- executor 具备 worker/pending backpressure、daemon shutdown 和 orphan reconciliation；
  Activity/Run 是唯一业务事实源，不保留 module registry 或 completion queue。
- hook output 与 delegation summary 超限后使用同一安全 spill primitive：目录 `0700`、文件
  `0600`、排他创建、路径去语义、数量/年龄清理；写入失败仍只返回有界 preview。
- summary 预算只在唯一 owner 应用一次，按父上下文 headroom 和静态 ceiling 取最小值；
  fan-out 结果始终按输入 task index 排序。

## 最终统一实机验收重点

1. 在普通会话、Leader、`@member` 和非活跃会话分别发起同步与异步 subagent；页面只应
   出现 Activity/Run 投影，不应出现合成系统提示或普通工具统一显示 `completed` 的旧样式。
2. 异步 fan-out 返回后继续当前对话，确认 child 独立推进且最终顺序按输入稳定；取消其中
   一个 child，确认 sibling 不受影响且 root 汇总为部分失败/取消。尤其确认 parent 先完成、
   立即发起下一回合后，旧 child 的最终状态仍归属旧 Run，不能长期 `running` 或串到新回合。
3. 重启 Doxie/Hermes 后检查未完成的本地 async run 被明确 terminalize，不长期停留 running；
   显式取消、runtime shutdown 和迟到结果均不能复活 Activity。
4. 用大 hook 输出和多个长 child summary 压测，确认 prompt 只含有界 head/tail 与安全路径，
   完整内容可从 spill 读取，父对话不因无界上下文反复压缩或 429。
5. 展开主会话中的 `dispatch_agent_async`、`dispatch_team_async` 与 `get_activity` 工具卡片，
   确认显示中文业务名称、状态和摘要，不出现“工具调用 + 结构化数据”；再抽查智能员工草案、
   Team Mission handoff、飞书、元宝、视频和 Spotify 工具，确认全部使用统一结果块。

## 阶段收益

- 主 Agent 能按任务依赖自主选择同步或异步，而执行合法性、持久化与资源预算仍由 runtime
  统一裁决。
- Doxie 获得可持久、可取消、可重连和可归属的后台活动，不再把生命周期事件误当消息。
- Doxie 的工具展示由 Hermes registry 生成契约约束；上游新增内建工具若未配置展示会直接使
  门禁失败，避免产品发布后才由用户看到原始字段回退。
- fan-out、独立 async agent 和 Team Mission 共享 typed handle 与 terminal 语义，消除状态漂移。
- 大 hook 和多 child 汇总不会击穿父上下文，完整证据仍可安全追溯。

阶段 8 自动验收完成，等待 Phase 1-12 全部结束后的统一用户实机验收；此前不 push、
不合并回主开发分支。
