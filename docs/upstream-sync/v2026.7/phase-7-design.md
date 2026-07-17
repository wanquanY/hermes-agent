# 阶段 7 设计：Approval 统一治理

## 目标与范围

按当前 Hermes 分层手工吸收 A1-A5。阶段完成后，command、execute-code、model tool、
registry/executor、plugin hook 与 MCP elicitation 的人审编排只存在一个 application seam；
CLI、Gateway、TUI RPC、worker proxy 和 Team Mission 只负责携带相同的 session/profile
上下文及投影结果，不能拥有第二套 allow/deny 语义。

本阶段只参考上游行为和失败场景，不 merge、rebase 或 cherry-pick。阶段起点为
`c6040112a`，执行分支为 `codex/upstream-absorption-v2026.7-all-phases`。

## 上游参考

- A1：`f512d6f02`、`117f49b7d`，通用 approval gate 与 hook 升级。
- A2：`36308f066`，tool 级持久审批 rule key。
- A3：`e2fe529ef`，用户 deny 配置硬拦。
- A4：`cb6c47af0`，拒绝原因回传 agent。
- A5：`4a7a6fd40`，用户可见 approval prompt 脱敏。

## 架构决策

### 1. 状态 owner 与编排 owner 分离，但只有一个执行通道

`tools.approval` 继续唯一拥有危险命令检测、hardline/deny policy、session/permanent
集合、pending queue、Gateway blocking primitive 与持久化。新建 `tools.approval_gate`
作为唯一 application orchestration owner，集中完成：

- 缓存命中与 bypass 裁决；
- 用户可见字段脱敏；
- CLI/Gateway/pending responder 选择；
- request、wait、resolve、timeout 与 notify failure 收敛；
- session/permanent choice 持久化；
- tool-facing deny 结果塑形。

`check_dangerous_command` 只保留兼容 facade，不能再形成一条独立审批流。静态 owner
门禁禁止生产代码绕过 `approval_gate` 直接调用 wait/prompt primitive，并限制 owner 文件
大小，防止统一 seam 再退化成 monolith。

### 2. Plugin `approve` 是升级人审，不是授权结果

`pre_tool_call` 的 canonical directive 为 `block | approve`。`block` 立即终止；`approve`
只把 tool name、reason 和可选 stable `rule_key` 送入统一 gate。没有 human responder、hook
解析失败或 gate 异常全部 fail closed，不能把组织策略的“需要批准”误读成“已批准”。

model tool、registry/executor 的 sequential/concurrent 路径和 runtime helper 都调用
`resolve_pre_tool_block`；旧 `get_pre_tool_call_block_message` 只保留 ABI adapter，避免旧插件
调用面成为第二事实源。

### 3. 任意 tool rule 具有稳定、可撤销的 scope key

显式 key 写为 `plugin_rule:{rule_key}`；未提供 key 时写为
`plugin_rule:{tool_name}:{sha256(reason)[:12]}`。同一 key 的 `session` 选择只在当前
approval session 生效，换 session/profile 后重新询问；`always` 进入已有 permanent
allowlist persistence，撤销和配置重载继续走同一 owner。原始 reason 不作为 key 明文，
避免把敏感策略内容写入配置。

MCP elicitation 复用同一 gate，但明确 `persist_decision=false`，因为协议 consent 是单次
交互，不得被一次批准永久跳过。

### 4. `approvals.deny` 是 approval bypass 之下的硬边界

用户 deny 规则使用 case-insensitive shell glob，同时匹配 raw 与 canonical command
variant。它在 yolo、`mode: off`、smart approval、session/permanent allowlist 之前运行；
命中直接返回结构化 `user_deny`，不创建 pending、不调用 responder，也不能从另一个
transport 覆盖。hardline blocklist 仍先于 deny，二者共同组成 always-on policy floor。

### 5. 拒绝原因与展示脱敏使用统一合同

`/deny [all] [reason]`、TUI `prompt.respond` 和 worker protocol 都把可选 reason 传回同一
approval entry。reason 先折叠为空白单行，再截断到 500 字符；只有 deny 会携带 reason，
approve 不能注入伪拒绝文本。agent 收到稳定字段 `denial_reason` 和不可重试提示，可据此
选择其他方案，而不是反复换命令绕过用户拒绝。

command、tool target、description 与 MCP message 在 notify/pending/CLI prompt 之前统一
经过 display-only redaction；policy detection 与真实 tool execution 仍使用原值。脱敏器
异常时显示边界 fail closed 为固定占位符，不能回落显示 raw secret。

### 6. Transport 只投影同一 pending state

Gateway slash command、TUI RPC request-id/legacy session route 与 worker dict payload 最终
都调用 `resolve_gateway_approval`。request id 负责并发 winner 定位，session key 负责所有
会话类型的 scope；普通、leader、`@member` 和后台 Activity 继承同一上下文，因此 Team
Mission 不再拥有独立批准缓存。旧只含 choice 的 worker payload 保留兼容读取，canonical
payload 为 `{choice, reason}`。

## 自动验收矩阵

1. model/registry/sequential/concurrent/runtime helper 都只经 `resolve_pre_tool_block`；
   wait/prompt primitive 只有 `approval_gate` 可调用。
2. plugin `approve` 可升级人审；无 responder、hook/gate 异常 fail closed；`block` 不退化。
3. explicit rule key、reason hash、session cache、跨 session 隔离和 permanent persistence
   语义一致。
4. user deny 在 yolo、mode off、session/permanent allowlist 之前生效，命中时没有 pending
   或 handler 调用，大小写与 command variant 一致。
5. Gateway `/deny`、TUI request-id/legacy route 和 worker proxy 都传递同一 bounded
   single-line reason；并发 resolve 仍只有一个 winner。
6. command/description/MCP display 字段在 notify 前脱敏，原始 policy detection 和执行输入
   不被 mutation。
7. CLI、Gateway、worker、Team Mission、MCP、plugin 与 Doxie approval contract 回归通过。
8. 阶段专项、Hermes 全量、静态/分层/zero-debt、账本与 Doxie gates 全通过。

## 完成条件

账本、实现、失败场景、安全收益和已知限制写入验收记录；建立本地 Phase 7 checkpoint
后进入 Phase 8。最终统一用户验收前不 push、不 merge。
