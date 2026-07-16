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
   一个 child，确认 sibling 不受影响且 root 汇总为部分失败/取消。
3. 重启 Doxie/Hermes 后检查未完成的本地 async run 被明确 terminalize，不长期停留 running；
   显式取消、runtime shutdown 和迟到结果均不能复活 Activity。
4. 用大 hook 输出和多个长 child summary 压测，确认 prompt 只含有界 head/tail 与安全路径，
   完整内容可从 spill 读取，父对话不因无界上下文反复压缩或 429。

## 阶段收益

- 主 Agent 能按任务依赖自主选择同步或异步，而执行合法性、持久化与资源预算仍由 runtime
  统一裁决。
- Doxie 获得可持久、可取消、可重连和可归属的后台活动，不再把生命周期事件误当消息。
- fan-out、独立 async agent 和 Team Mission 共享 typed handle 与 terminal 语义，消除状态漂移。
- 大 hook 和多 child 汇总不会击穿父上下文，完整证据仍可安全追溯。

阶段 8 自动验收完成，等待 Phase 1-12 全部结束后的统一用户实机验收；此前不 push、
不合并回主开发分支。
