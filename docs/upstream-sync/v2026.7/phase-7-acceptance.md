# 阶段 7 验收：Approval 统一治理

## 结论

阶段 7 的 A1-A5 已按当前 Hermes 架构手工吸收。command、execute-code、model tool、
registry/executor、plugin hook 与 MCP elicitation 的人审编排现在共用唯一
`tools.approval_gate`；用户 deny 成为 transport/backend 无关的硬边界；拒绝原因和展示
脱敏在 CLI、Gateway、TUI RPC 与 worker 之间具有同一合同。最终用户实机验收与
Phase 1-12 统一进行；在此之前不 push、不合并回主开发分支。

## 条目决策

| ID | 决策 | 当前架构落点 | 验收结果 |
|---|---|---|---|
| A1 | absorb | `tools.approval_gate`、`resolve_pre_tool_block`、command/execute-code/MCP adapter | model、sequential/concurrent executor、runtime helper、CLI、Gateway 与 worker 只经单一 seam；无 responder/异常 fail closed |
| A2 | absorb | `plugin_rule:{explicit_key}`、`plugin_rule:{tool}:{reason_hash}`、既有 session/permanent state owner | session cache、跨 session 隔离、permanent persistence 与无明文 reason key 均有行为测试 |
| A3 | absorb | `approvals.deny`、raw/canonical command variant、command 与 execute-code 前置门禁 | 先于 yolo、mode off、allowlist 和 isolated-backend skip；命中不创建 pending、不调用 responder |
| A4 | absorb | Gateway `/deny [all] [reason]`、TUI `prompt.respond`、worker approval payload | reason 折叠成单行、最多 500 字符，只在 deny 传递；request-id/legacy session route 一致 |
| A5 | absorb | `approval_gate._redact_user_visible`、统一 notify/pending/CLI display boundary | command、description 与 MCP message 在展示前脱敏；检测和真实执行仍使用原值；脱敏异常不回退 raw secret |

## 唯一 owner 与兼容边界

`tools.approval` 只保留 detection/state/persistence/blocking primitive；
`tools.approval_gate` 负责 state lookup、display redaction、responder selection、等待、结果
塑形和 persistence orchestration。旧 `check_dangerous_command` 与
`get_pre_tool_call_block_message` 仅作为 ABI facade，生产调用面不能复制审批流程。

MCP elicitation 走同一 gate 但强制 non-persistent；plugin `approve` 只表示升级到人审，
不是 allow。通用 tool 的组织硬禁止仍使用 plugin `block`；`approvals.deny` 明确是 terminal
和 execute-code command glob，不伪装成一套未设计的通用 RBAC。

## 自动验收证据

### Hermes 阶段专项

- Approval 聚合：22 files、960 passed、0 failed，耗时 18.4s；覆盖 command/execute-code、
  CLI、Gateway、TUI、worker、plugin、MCP、run-agent、Team Mission observer 与静态 owner。
- 单一 owner 与账本：6 passed；静态验证生产 import、wait/prompt primitive owner 和
  `tools.approval.py` 小于 2000 行。
- user deny 专项包含 mode off、session yolo、permanent allowlist、大小写、isolated backend
  与 execute-code backend skip：6 passed。
- Kanban daemon 原有测试在全仓高负载下暴露 300ms wall-clock 调度假设；已改为等待
  第二个 tick 的行为事件，同时保持 2s 收敛上限。该文件 164 passed、1 skipped。

### Hermes 全量与静态

- 最终完整隔离门禁：1570 files、29,016 passed、0 failed，耗时 560.2s，
  28 workers，2 个声明式 serial tail 文件；包含 backend-independent deny 和 Kanban
  高负载调度根因修复后的最终状态。
- `ruff check .`：通过。
- tracked production Python compile：通过。
- import architecture：162 files、328 dependencies、1 kept contract、0 broken。
- 账本/单一 owner 6 passed，`git diff --check` 通过。

### Doxie 联调

- Gateway contract：205 methods in sync；98 个活跃桌面方法由 135 个
  `requiredMethods` 覆盖，source 指向本阶段 worktree。
- desktop `test:gates`：Vitest、Electron Node（391 passed）、source Node（19 passed）、
  Gateway contract、`vue-tsc --build`、ESLint 与 frontend boundary 全部退出码 0；ESLint
  为 0 error、70 条既有 warning。

### 最终 checkpoint 复核

- 2026-07-16 最终复核完成：Hermes 全量 29,016 passed、0 failed；Ruff、production
  compile、import architecture、账本/单一 owner 与 `git diff --check` 全绿；Doxie
  desktop `test:gates` 退出码 0。满足建立 Phase 7 本地 checkpoint 的条件。

## 阶段收益

- 同一 tool policy 不再因 registry、并发 executor、Gateway 或 Team Mission 路径不同而
  得到不同审批结果；transport 只投影同一 pending state。
- plugin 无法把“需要人审”误当成授权，无人响应、notify 失败和 policy seam 异常都会
  明确拒绝，避免静默执行。
- 用户 deny 在 yolo、mode off、永久 allowlist 和 isolated backend 之前生效，真正成为
  operator-controlled hard boundary。
- stable rule key 让一次、会话和永久批准可预测且可撤销；reason hash 避免敏感策略明文
  进入持久配置。
- 用户可以给 agent 一个可行动的拒绝原因，agent 同时收到禁止重试/换命令绕过的明确
  语义；所有可见 prompt 在 transport 前脱敏，不暴露 credential。

## 最终实机验收重点

1. 普通会话、Team Leader、`@member` 与后台 Activity 各触发一次同一 plugin approval，
   确认只显示一个标准审批卡，批准一次/本会话/永久的行为一致。
2. 配置 `approvals.deny: ["git push *"]` 后分别打开 yolo、mode off 和永久 allowlist，
   确认仍直接硬拒绝且没有 pending approval。
3. 对一次审批执行 `/deny 请改用 staging`，确认 agent 收到该原因并选择替代方案；输入
   超长、多行 reason，确认展示为 500 字符以内单行。
4. 在 command、description 或 MCP elicitation 中放入测试 credential，确认审批卡和历史
   不出现原值，而实际 policy matching 不受脱敏 mutation 影响。
5. approval pending 期间刷新/重连 Doxie，再按 request id resolve，确认只有一个 winner，
   历史投影不显示普通 system message 或伪造的 `completed` 工具状态。
