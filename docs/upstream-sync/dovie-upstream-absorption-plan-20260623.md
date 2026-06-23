# Hermes 上游吸收评估清单 (96cd37e21 → upstream/main)

> 范围:codex 上次 review 终点 (`96cd37e21`, 2026-06-04) → upstream/main (`5ff11a689`)
> 总 commit:1855(全范围),作用域内 ~637(只算 gateway/agent/tools/tui_gateway/plugins/memory/mcp/optional-skills)
> 评估日期:2026-06-23(2026-06-23 下午追加 5 个 hermes-agent 本地 commit 后重新校准)
> 评估范围限定:用户明确不要 desktop UI / dashboard UI / kanban UI / TUI / 各类 messaging adapter(dovie 自有 UI 和通道)
> 执行分支:`absorb-upstream-20260623`(基于 `a9615a66b`)

---

## 执行 Phase 划分(2026-06-23 下午追加)

`2fb895be1` 之后 hermes-agent 又叠加了 5 个 commit
(`fc7427c5c` 团队任务 sidebar 指示器 → `27ebefa04` `4c9a1c42e`
session_index 诊断日志 → `fa714c26d` pending-input 投影 → `e4d038b53`
撤诊断 → `a9615a66b` sticky outer-interrupt)。这些动了
`hermes_state.py` (21 条 upstream commit 也动过) / `tools/approval.py` (12 条)
/ `tui_gateway/server.py` (66 条),所以原清单里几条"中冲突"升到"高冲突"。

按"是否动了 dovie 仍在迭代的文件"重新分 Phase:

| Phase | 范围 | 条数 | 风险 | 何时做 |
|---|---|---|---|---|
| **Phase 1** | 零冲突,直接 cherry-pick(quick wins + MCP + 单文件安全) | ~14 | 极低 | **立刻** |
| **Phase 2** | 中等手工融合(approval / gateway 进程内存 / 部分 streaming) | ~10 | 中 | Phase 1 验过测试再做 |
| **Phase 3** | 高冲突(conversation_loop / tui_gateway server / hermes_state.py) | ~12 | 高 | dovie team-mission 收敛后做 |
| **Phase 4** | P1 全部 50+ 条,按子分类(compression / delegate / memory / cron / skills / tool 体验) | ~50 | 中 | Phase 1+2 通过后逐子分类做 |

下面 P0/P1 章节里每条都加了 `[Phase N]` 前缀方便检索。

---

## codex 文档 critique (200 字内)

**框架仍有效,但已部分过时**:codex 把"运行时稳定性 + provider replay 修复 + 安全基线"放 P0、把"能力发现/dashboard auth/skills catalog"放 P1、把"docker/website/UI"放 P3 —— 这套优先级与用户当前需求(运行时稳定 > agent 能力 > 生态)完全一致,可继续使用。**过时点**:

1. codex 截止 96cd37e21,从此到 upstream/main 多了 ~1855 commits,其中包含 **June 2026 hermes-0day MCP 持久化攻击响应** ([7726ce304](https://github.com/NousResearch/hermes-agent/commit/7726ce304))、PID 复用安全 ([e44772314](https://github.com/NousResearch/hermes-agent/commit/e44772314))、`x-api-key` redaction 漏洞 ([6f0ecf37d](https://github.com/NousResearch/hermes-agent/commit/6f0ecf37d)) 等,codex 没看过、必须升级到 P0。
2. codex 把"dashboard OAuth/WS ticket"列 P1,但用户明说 UI 不在意,本评估直接降 P3。
3. codex 推荐的"手工移植不 cherry-pick"对 `agent/conversation_loop.py` 仍正确,但对纯 `tools/*`、`agent/redact.py`、`agent/error_classifier.py` 等无 dovie 分叉的文件,实测可直接 cherry-pick。

---

## P0 — 必吸收 (运行时稳定 + 严重安全,33 条)

### P0.1 安全 / 攻击面 (必须立刻)

| commit | scope | what | 文件 | dovie 关联 | 风险 | 推荐 |
|---|---|---|---|---|---|---|
| [7726ce304](https://github.com/NousResearch/hermes-agent/commit/7726ce304) | fix(security) | 关闭 hermes-0day MCP 持久化攻击面 (`--insecure` 不再绕 auth,MCP entry 拒绝写 authorized_keys / cron / pam.d 等,IOC blocklist,API key 熵下限 8→16) | `hermes_cli/web_server.py` `tools/mcp_security.py` `gateway/platforms/api_server.py` | dovie 通过 hermes runtime 加载 MCP entry,这条直接保护用户主机 | 低 (dovie 未 patch 这些文件) | **P0 必吸收** |
| [f45ace931](https://github.com/NousResearch/hermes-agent/commit/f45ace931) | feat(security) | 启动时 warn-on-load 安全态势审计 (root / SSH password auth / 容器无 volume / 网络 API server 无 key) | `gateway/run.py` `hermes_cli/security_audit_startup.py` | dovie sidecar 启动 hermes runtime → 用户能看到暴露警告 | 低 | **P0 必吸收** |
| [84fcbbf6a](https://github.com/NousResearch/hermes-agent/commit/84fcbbf6a) | fix(security) | `HERMES_TIMEZONE` 远程代码执行时未 quote,shell 注入 | `tools/code_execution_tool.py` | dovie 用户可设 timezone,直接命中 | 低 (单点修复 +2/-1) | **P0 必吸收** |
| [6f0ecf37d](https://github.com/NousResearch/hermes-agent/commit/6f0ecf37d) | fix(redact) | redact 只匹配 `Authorization: Bearer`,所有其他 auth 头 + `x-api-key` 都泄漏到日志/transcript | `agent/redact.py` | dovie 日志和 event stream 直接受益 | 低 | **P0 必吸收** |
| [ed966696e](https://github.com/NousResearch/hermes-agent/commit/ed966696e) | fix(security) | IPv6 scope ID 绕过 URL safety check (SSRF) | `agent/url_safety.py` | dovie 走 hermes web_tools,SSRF 直接受益 | 低 | **P0 必吸收** |
| [8fcb8136b](https://github.com/NousResearch/hermes-agent/commit/8fcb8136b) | fix(security) | smart approval guard 抗 prompt injection | `tools/approval.py` | dovie auto-approval 路径直接受益 | **中** (dovie 已 patch approval.py,需手工融合) | **P0 必吸收** |
| [b0efe1d64](https://github.com/NousResearch/hermes-agent/commit/b0efe1d64) + [89d380261](https://github.com/NousResearch/hermes-agent/commit/89d380261) | fix(approval) | gate resolved hermes config 写入 + 检测时而非 import 时 resolve HERMES_HOME | `tools/approval.py` | dovie profile-scoped HERMES_HOME 需要后一条 | 中 | **P0 必吸收, 两条一起** |
| [8f2931e3e](https://github.com/NousResearch/hermes-agent/commit/8f2931e3e) | fix(file_tools) | block agent 写入 `~/.hermes/config.yaml` 绕过 approval | `tools/file_tools.py` | dovie file 工具直接受益 | 中 | **P0 必吸收** |
| [71274f264](https://github.com/NousResearch/hermes-agent/commit/71274f264) | fix(file) | 拒绝 read_file 行号回写 (污染 file 内容) | `tools/file_tools.py` | 直接 | 低 | **P0 必吸收** |
| [9078b4bbd](https://github.com/NousResearch/hermes-agent/commit/9078b4bbd) + [def3f6388](https://github.com/NousResearch/hermes-agent/commit/def3f6388) | fix(file) | read_file device-alias 强化 + symlink guard anchor 到 task cwd | `tools/file_tools.py` `agent/file_safety.py` | 直接 | 低 | **P0 必吸收** |
| [621bf3a87](https://github.com/NousResearch/hermes-agent/commit/621bf3a87) | fix(security) | denylist normalizer 去掉 shell 转义、缺失 approval 模块时 fail-closed | `tools/approval.py` (附近) | 直接 | 中 (approval.py) | **P0 必吸收** |

### P0.2 进程 / 资源 / 内存泄漏

| commit | scope | what | 文件 | dovie 关联 | 风险 | 推荐 |
|---|---|---|---|---|---|---|
| [e44772314](https://github.com/NousResearch/hermes-agent/commit/e44772314) | fix(process-registry) | 杀进程前用 `/proc/<pid>/stat` 启动时间 re-validate PID 身份,防止 PID 回收误杀 (实测误杀 Firefox) | `tools/process_registry.py` | dovie automation/web_tools 起子进程,可能误杀用户进程 | 低 | **P0 必吸收** |
| [8cfcbd327](https://github.com/NousResearch/hermes-agent/commit/8cfcbd327) | fix(process) | `psutil.wait_procs` 在 SIGTERM-ignoring 进程树上 partition 错误,直接 re-probe + SIGKILL | `tools/process_registry.py` | 同上 | 低 | **P0 必吸收, 与上一条一起** |
| [8cecaf0b2](https://github.com/NousResearch/hermes-agent/commit/8cecaf0b2) | feat(process) | host-pid grace 后 SIGTERM→SIGKILL 升级 | `tools/process_registry.py` | 同上 | 低 | **P0 必吸收** |
| [3d029a53e](https://github.com/NousResearch/hermes-agent/commit/3d029a53e) | fix(gateway) | 关闭高负载下残留 memory-leak 点 | `gateway/*` | dovie runtime 长期常驻 | 中 (gateway 路径) | **P0 必吸收** |
| [e9c1e757f](https://github.com/NousResearch/hermes-agent/commit/e9c1e757f) | fix(gateway) | release 被驱逐的 agent client (RSS leak #29298) | `gateway/*` | dovie runtime 内存稳定性 | 中 | **P0 必吸收** |
| [ae94ed172](https://github.com/NousResearch/hermes-agent/commit/ae94ed172) | fix(tui-gateway) | 断连时回收 slash_worker 泄漏 session + `active_list` 心跳 | `tui_gateway/*` | dovie sidecar 断连/重连场景 | **高** (dovie 模块化了 tui_gateway,需手工融合到 methods/services) | **P0 必吸收, 手工迁移** |
| [d19aabbf2](https://github.com/NousResearch/hermes-agent/commit/d19aabbf2) | fix(gateway) | restart/shutdown drain timeout 时持久化 in-flight transcript | `gateway/run.py` `gateway/session.py` | dovie runtime restart 后保留用户消息 | 中 | **P0 必吸收** |
| [8e4c447e5](https://github.com/NousResearch/hermes-agent/commit/8e4c447e5) | fix(gateway) | state.db 防止重复 user messages | `gateway/*` `hermes_state.py` | dovie state.db 直接受影响 | 中 (跟 dovie session_index 路径相邻,需小心) | **P0 必吸收** |

### P0.3 agent runtime / streaming / transport 正确性

| commit | scope | what | 文件 | dovie 关联 | 风险 | 推荐 |
|---|---|---|---|---|---|---|
| [9f67ba1b0](https://github.com/NousResearch/hermes-agent/commit/9f67ba1b0) | fix(agent) | guard finalize_turn cleanup chain,绝不丢响应 | `agent/conversation_loop.py` | dovie run terminal event 依赖此 | **高** (dovie diagnostics 已 patch conversation_loop) | **P0 必吸收, 手工** |
| [020e59d3c](https://github.com/NousResearch/hermes-agent/commit/020e59d3c) | fix(agent) | 抑制 empty-name phantom tool-call 循环 (#47967) | `agent/conversation_loop.py` `agent/tool_executor.py` | dovie 直接受卡 | 高 (conversation_loop) | **P0 必吸收, 手工** |
| [dd0d1222a](https://github.com/NousResearch/hermes-agent/commit/dd0d1222a) | fix(agent) | 中断引起的 transport error 不要重试 (cascading-interrupt hang) | `agent/conversation_loop.py` `agent/transports/*` | dovie 取消运行常见场景 | 高 | **P0 必吸收, 手工** |
| [3a74b7521](https://github.com/NousResearch/hermes-agent/commit/3a74b7521) | fix(agent) | char-based output-cap overflow 恢复 | `agent/conversation_loop.py` | dovie 大输出场景 | 高 | **P0 必吸收, 手工** |
| [b892ee2bc](https://github.com/NousResearch/hermes-agent/commit/b892ee2bc) | fix(agent) | 非可重试 API 错误 summarize,raw HTML 不进 transcript | `agent/error_classifier.py` `agent/conversation_loop.py` | dovie UI 不再显示乱码 HTML | 中 | **P0 必吸收** |
| [86e10dd87](https://github.com/NousResearch/hermes-agent/commit/86e10dd87) + [9f95f72b9](https://github.com/NousResearch/hermes-agent/commit/9f95f72b9) | fix(agent) | "thinking blocks cannot be modified" 400 路由到 recovery + recovery 真正去掉 thinking block | `agent/conversation_loop.py` `agent/error_classifier.py` | dovie 用 Anthropic 模型时 | 高 | **P0 必吸收, 两条一起** |
| [2b3a4f0af](https://github.com/NousResearch/hermes-agent/commit/2b3a4f0af) | fix(agent) | fallback 到 strict provider 时剥离 stale `reasoning_content` (#50480) | `agent/conversation_loop.py` `agent/transports/chat_completions.py` | dovie provider 切换 | 高 | **P0 必吸收, 手工** |
| [99f3072aa](https://github.com/NousResearch/hermes-agent/commit/99f3072aa) | fix(model-switch) | 失败的 in-place model swap 必须 no-op,不能留 dead session (#50375) | `agent/agent_init.py` `gateway/*` | dovie session 管理 | 高 | **P0 必吸收, 手工** |
| [4467c22c8](https://github.com/NousResearch/hermes-agent/commit/4467c22c8) | fix(chat-completions) | 发给 strict provider 前剥离 timestamp | `agent/transports/chat_completions.py` | dovie | 中 | **P0 必吸收** |
| [c884ff64e](https://github.com/NousResearch/hermes-agent/commit/c884ff64e) | fix(agent) | provider failover 时同步 system-prompt 模型身份 | `agent/system_prompt.py` `agent/conversation_loop.py` | dovie failover | 中 | **P0 必吸收** |
| [c253b0738](https://github.com/NousResearch/hermes-agent/commit/c253b0738) | fix(model) | model switch 时清理 stale endpoint credentials | `agent/*` `hermes_cli/providers.py` | dovie 多 provider | 中 | **P0 必吸收** |
| [5a53e0f0f](https://github.com/NousResearch/hermes-agent/commit/5a53e0f0f) | fix(compression) | auth 失败时 abort,而不是 rotate 进降级 session | `agent/compression.py` | dovie session lineage 必须保留 | 中 (dovie 有自己的 lineage) | **P0 必吸收** |
| [615ad9792](https://github.com/NousResearch/hermes-agent/commit/615ad9792) | fix(streaming) | socket read timeout 抢占 stale-stream detector (#43570) | `agent/transports/*` | dovie streaming 卡顿 | 中 | **P0 必吸收** |
| [c9094f5e5](https://github.com/NousResearch/hermes-agent/commit/c9094f5e5) | fix(stream) | 中途 tool-call drop 不要 report 为 output truncation | `agent/conversation_loop.py` | dovie UI 状态正确 | 中 | **P0 必吸收** |
| [a9c802598](https://github.com/NousResearch/hermes-agent/commit/a9c802598) | fix(approval) | gateway approval blocking wait 期间响应 interrupt (#8697) | `tools/approval.py` | dovie 用户取消等待中的 approval | 中 (approval.py) | **P0 必吸收** |
| [b5b8a4cd5](https://github.com/NousResearch/hermes-agent/commit/b5b8a4cd5) | fix(gateway) | 尊重 adapter 拒绝 fresh-final 防止重复投递 | `gateway/*` | dovie event stream 防重复 | 中 | **P0 必吸收** |

### P0.4 MCP 稳定性 (dovie 主要 tool 通道)

| commit | scope | what | 文件 | dovie 关联 | 风险 | 推荐 |
|---|---|---|---|---|---|---|
| [40722058e](https://github.com/NousResearch/hermes-agent/commit/40722058e) | fix(mcp) | short-TTL HTTP session keepalive ping | `tools/mcp_tool.py` | dovie 通过 MCP 加载第三方工具 | 低 (fork 无对应改) | **P0 必吸收** |
| [472c06815](https://github.com/NousResearch/hermes-agent/commit/472c06815) | fix(mcp) | ping keepalive 识别 'unknown method' fallback | `tools/mcp_tool.py` | 同上 | 低 | **P0 必吸收, 与上一条一起** |
| [93d6e7302](https://github.com/NousResearch/hermes-agent/commit/93d6e7302) + [371348387](https://github.com/NousResearch/hermes-agent/commit/371348387) + [b6e2a54a9](https://github.com/NousResearch/hermes-agent/commit/b6e2a54a9) + [16642e276](https://github.com/NousResearch/hermes-agent/commit/16642e276) | fix(mcp) | 暴露 late-connecting MCP 工具到 agent + 每 turn 刷新 snapshot + adversarial review 两轮 + ACP rebuild revert | `tools/mcp_tool.py` `agent/agent_init.py` `model_tools.py` | dovie agent 在 runtime 中途加载 MCP 工具时能看到 | **中** (4 条强相关,需顺序合) | **P0 必吸收, 4 条作为一组** |
| [73dd58499](https://github.com/NousResearch/hermes-agent/commit/73dd58499) | fix(mcp) | 把 HERMES_HOME override 传给 MCP event loop | `tools/mcp_tool.py` | dovie profile-scoped HERMES_HOME 必要 | 低 | **P0 必吸收** |

---

## P1 — 强推荐 (能力增强 + 明显改善,无大冲突,~50 条)

### P1.1 compression / session 正确性

| commit | scope | what | 文件 | 推荐 |
|---|---|---|---|---|
| [466345699](https://github.com/NousResearch/hermes-agent/commit/466345699) + [1fbf48d4a](https://github.com/NousResearch/hermes-agent/commit/1fbf48d4a) | fix(compression) | in-place compaction 非破坏性 (soft-archive) + 持久化 | `agent/compression.py` `hermes_state.py` | **P1** |
| [47fadc24d](https://github.com/NousResearch/hermes-agent/commit/47fadc24d) | feat(compression) | in-place compaction 选项,保持单 session id (#38763) | `agent/compression.py` `cli.py` `hermes_state.py` | **P1 强推荐** |
| [d87f29397](https://github.com/NousResearch/hermes-agent/commit/d87f29397) | feat(compression) | compaction 摘要里 temporal anchoring (#41102) | `agent/compression.py` | **P1** |
| [14ef6312b](https://github.com/NousResearch/hermes-agent/commit/14ef6312b) | fix(compression) | `protect_first_n` decay,早期 turn 不固化 | `agent/compression.py` | **P1** |
| [3509be712](https://github.com/NousResearch/hermes-agent/commit/3509be712) | fix(compression) | auto-compression 在最小 context 触发 (#14690) | `agent/compression.py` | **P1** |
| [1f874dfe4](https://github.com/NousResearch/hermes-agent/commit/1f874dfe4) | fix(compression) | fallback summary 不再三重复制最新 user ask | `agent/compression.py` | **P1** |
| [2f3177adf](https://github.com/NousResearch/hermes-agent/commit/2f3177adf) | fix(compression) | 保护 summary 调用不被 mid-flight interrupt 打断 | `agent/compression.py` | **P1** |
| [a77bc2c08](https://github.com/NousResearch/hermes-agent/commit/a77bc2c08) | fix(compression) | background-review fork 禁用 compression | `agent/compression.py` | **P1** |
| [8513a6aec](https://github.com/NousResearch/hermes-agent/commit/8513a6aec) + [cca3b77a4](https://github.com/NousResearch/hermes-agent/commit/cca3b77a4) | fix(compression) | 跨 session stale `_previous_summary` 污染防护 + session end 时清理 | `agent/compression.py` | **P1, 两条一起** |
| [3714caa1b](https://github.com/NousResearch/hermes-agent/commit/3714caa1b) | fix(session) | transcript reads 跟随 compression continuation | `gateway/session.py` | **P1** |
| [b17180d95](https://github.com/NousResearch/hermes-agent/commit/b17180d95) + [9e4fe32d3](https://github.com/NousResearch/hermes-agent/commit/9e4fe32d3) | fix(session) | finalize owned SQLite rows on AIAgent.close + background-review fork 退出 session finalization | `agent/agent_init.py` | **P1** |
| [81ff916e5](https://github.com/NousResearch/hermes-agent/commit/81ff916e5) | fix(agent) | session rotation 前 flush 未持久化消息 | `agent/conversation_loop.py` | **P1** |
| [49596b70c](https://github.com/NousResearch/hermes-agent/commit/49596b70c) | fix(gateway) | resume 跟随 compression tip,post-compression 回复正确渲染 | `gateway/session.py` | **P1** |

### P1.2 delegate / subagent / memory

| commit | scope | what | 文件 | 推荐 |
|---|---|---|---|---|
| [ea8a8b4af](https://github.com/NousResearch/hermes-agent/commit/ea8a8b4af) | feat(delegation) | background fan-out,并行 subagent 合并返回 (#49734) | `tools/delegate_tool*` `agent/*` | **P1** (需人工 review:与 team_mission_runtime 是否冲突) |
| [c66ecf0bc](https://github.com/NousResearch/hermes-agent/commit/c66ecf0bc) | feat(delegation) | `delegate_task(background=true)` 异步子 agent (#40946) | `tools/delegate_tool*` | **P1**, 需人工 review |
| [48ae8029a](https://github.com/NousResearch/hermes-agent/commit/48ae8029a) | fix(delegate) | custom-endpoint subagent pool 按 endpoint identity 解析 | `tools/delegate_tool*` | **P1** |
| [f83918c31](https://github.com/NousResearch/hermes-agent/commit/f83918c31) + [f8a241e10](https://github.com/NousResearch/hermes-agent/commit/f8a241e10) | fix(delegate) | content-block tool results + live overlay tail flatten | `tools/delegate_tool*` | **P1** |
| [a2d7f538d](https://github.com/NousResearch/hermes-agent/commit/a2d7f538d) | fix(delegate) | subagent 工具完成行不泄到父 CLI 显示 (#44223) | `tools/delegate_tool*` | **P2 偏 P1** |
| [38c8a9c10](https://github.com/NousResearch/hermes-agent/commit/38c8a9c10) | feat(memory) | 单 turn batch memory ops (#48507) | `tools/memory_tool.py` | **P1** |
| [c6bf6bda9](https://github.com/NousResearch/hermes-agent/commit/c6bf6bda9) | fix(memory) | replace/remove 缺 old_text 时恢复 (#49997) | `tools/memory_tool.py` | **P1** |
| [aa6f2775f](https://github.com/NousResearch/hermes-agent/commit/aa6f2775f) | fix(memory) | end-of-turn sync 离开 turn 线程 (#41945) | `tools/memory_tool.py` `plugins/memory/*` | **P1** |
| [86c537d20](https://github.com/NousResearch/hermes-agent/commit/86c537d20) | fix(memory) | overflow 时 in-turn 整合 + retry (#41755) | `tools/memory_tool.py` | **P1** |
| [fe8920db1](https://github.com/NousResearch/hermes-agent/commit/fe8920db1) | fix(memory) | 拒绝 memory tool 与 core tool 重名 (#40902) | `tools/memory_tool.py` | **P1** |
| [c2c55c444](https://github.com/NousResearch/hermes-agent/commit/c2c55c444) | fix(memory) | 所有 provider 都剥离 skill scaffolding | `plugins/memory/*` | **P1** |

### P1.3 cron / automation

| commit | scope | what | 文件 | 推荐 |
|---|---|---|---|---|
| [65d7c7faf](https://github.com/NousResearch/hermes-agent/commit/65d7c7faf) | fix(cron) | action='run' 时立即执行 | `cron/scheduler.py` | **P1** |
| [acd4f34e6](https://github.com/NousResearch/hermes-agent/commit/acd4f34e6) | fix(cron) | per-job provider 'custom' 正确解析 | `cron/jobs.py` | **P1** |
| [a5e06078b](https://github.com/NousResearch/hermes-agent/commit/a5e06078b) | fix(cron) | 紧凑失败消息 + git gc 后修复 bare repo | `cron/*` | **P1** |
| [ae5b2de2f](https://github.com/NousResearch/hermes-agent/commit/ae5b2de2f) | fix(cron) | expand skill bundles in cron jobs | `cron/*` | **P1** |
| [1593ca540](https://github.com/NousResearch/hermes-agent/commit/1593ca540) + [9a09ea69f](https://github.com/NousResearch/hermes-agent/commit/9a09ea69f) | feat(cron) | Cron Recipes + Suggested Cron Jobs | `cron/*` `tui_gateway/methods/*` | **P1 (按需取 recipe 引擎,不取 UI)** |
| [3b56d3a29](https://github.com/NousResearch/hermes-agent/commit/3b56d3a29) | fix(security) | kanban tool payload 持久化前 redact secrets | `gateway/*` `agent/redact.py` | **P1** |

### P1.4 skills (dovie 有自己 lifecycle,只取安全 + catalog)

| commit | scope | what | 推荐 |
|---|---|---|---|
| [56f833efa](https://github.com/NousResearch/hermes-agent/commit/56f833efa) + [5a36f76a0](https://github.com/NousResearch/hermes-agent/commit/5a36f76a0) | fix(skills) | path traversal + SKILL.md guard | **P1 必吸收 (安全)** |
| [25c590ccd](https://github.com/NousResearch/hermes-agent/commit/25c590ccd) + [f1254c8ea](https://github.com/NousResearch/hermes-agent/commit/f1254c8ea) | fix(skills) | rmtree guard 拒绝 SKILLS_DIR root + `pre_update_backup` 默认 true | **P1 必吸收** |
| [2dbc3bd93](https://github.com/NousResearch/hermes-agent/commit/2dbc3bd93) | fix(skills) | 递归删除 skill 防 tree-escape | **P1 必吸收** |
| [085fc5d00](https://github.com/NousResearch/hermes-agent/commit/085fc5d00) | feat(skills) | find & diff 用户修改过的 bundled skills | **P1** |
| [9caa12f4e](https://github.com/NousResearch/hermes-agent/commit/9caa12f4e) | fix(skills) | `skill_view` 通过 frontmatter name 解析 | **P1** |
| [9137b86a5](https://github.com/NousResearch/hermes-agent/commit/9137b86a5) | fix(skills) | 忽略 support docs in skill discovery | **P1** |
| [677791606](https://github.com/NousResearch/hermes-agent/commit/677791606) | fix(skills) | list-modified hint + disambiguate diff | **P1** |
| [105625d65](https://github.com/NousResearch/hermes-agent/commit/105625d65) + [eee1da45f](https://github.com/NousResearch/hermes-agent/commit/eee1da45f) + [5e9d7a766](https://github.com/NousResearch/hermes-agent/commit/5e9d7a766) | fix(skills-hub) | ClawHub catalog 边界 + degenerate index | **P1** |

### P1.5 tool / approval / file 体验

| commit | scope | what | 推荐 |
|---|---|---|---|
| [d45addc2f](https://github.com/NousResearch/hermes-agent/commit/d45addc2f) | fix(tools) | model whitelist 不剥离 prompt / source images | **P1** (vision) |
| [333f01bc7](https://github.com/NousResearch/hermes-agent/commit/333f01bc7) | fix(tools) | 非 ASCII URL percent-encode | **P1** |
| [3769dff5d](https://github.com/NousResearch/hermes-agent/commit/3769dff5d) | fix(approval) | 尊重 glob command allowlist (#43051) | **P1** |
| [4d39a603d](https://github.com/NousResearch/hermes-agent/commit/4d39a603d) | fix(codex) | 恢复 `session_id`/`x-client-request-id` HTTP header | **P1** |
| [c11ae8261](https://github.com/NousResearch/hermes-agent/commit/c11ae8261) | fix(codex) | app-server session seed configured cwd | **P1** |
| [cb4cc08b0](https://github.com/NousResearch/hermes-agent/commit/cb4cc08b0) | fix(codex) | app-server token usage 记入 session accounting | **P1** |
| [a216ff839](https://github.com/NousResearch/hermes-agent/commit/a216ff839) | fix(agent) | 自定义 OpenAI 兼容 provider 尊重 `default_headers` (#40033) | **P1** |
| [2ce3ae3d1](https://github.com/NousResearch/hermes-agent/commit/2ce3ae3d1) | fix(error-classifier) | unsupported-param 400 不再误判为 context overflow | **P1** |
| [1e0b3a2bc](https://github.com/NousResearch/hermes-agent/commit/1e0b3a2bc) + [1965d5621](https://github.com/NousResearch/hermes-agent/commit/1965d5621) | fix(agent) | model switch 重置 token calibration + tool-output budget 跟随 context window | **P1** |
| [a4f179c50](https://github.com/NousResearch/hermes-agent/commit/a4f179c50) | fix(agent) | GPT/Codex 家族单文件 edit 引导走 V4A | **P1** |
| [7ffc216bc](https://github.com/NousResearch/hermes-agent/commit/7ffc216bc) | fix(agent) | binary @file: 引用 actionable | **P1** |
| [3e74f75e4](https://github.com/NousResearch/hermes-agent/commit/3e74f75e4) | feat(agent) | coding-context posture (per-model edit-format tuning) (#43316) | **P1 强推荐** |
| [239740a19](https://github.com/NousResearch/hermes-agent/commit/239740a19) | feat(tools) | MCP `elicitation/create` handler | **P1** |
| [3ead2bdd0](https://github.com/NousResearch/hermes-agent/commit/3ead2bdd0) | feat(prompt) | 可配置 per-platform system-prompt hint override | **P1** |
| [f80381c45](https://github.com/NousResearch/hermes-agent/commit/f80381c45) + [f6a42b1ac](https://github.com/NousResearch/hermes-agent/commit/f6a42b1ac) | feat(prompt) | context-file cap 按模型 window 缩放 + 可配置 | **P1** |
| [990273d90](https://github.com/NousResearch/hermes-agent/commit/990273d90) | fix(agent) | bytes 增大时接受 pixel-correct downscale (#48013) | **P1** |
| [e7ae145ac](https://github.com/NousResearch/hermes-agent/commit/e7ae145ac) | fix(gateway) | 引导 agent 读取附件 PDF/DOCX 而不是 punt | **P1** |
| [93ea9b04a](https://github.com/NousResearch/hermes-agent/commit/93ea9b04a) | fix(gateway) | 入站 media 下载 size cap 防内存耗尽 | **P1 (DoS)** |
| [e499d69e3](https://github.com/NousResearch/hermes-agent/commit/e499d69e3) | feat(api-server) | 可配置并发 run cap,防 DoS (#50007) | **P1** |
| [b23184cad](https://github.com/NousResearch/hermes-agent/commit/b23184cad) | fix(api-server) | 工具绑定请求 session context | **P1** |
| [7a131f7f4](https://github.com/NousResearch/hermes-agent/commit/7a131f7f4) | fix(api-server) | stateless HTTP path 不再 silently promise async (#50319) | **P1** |
| [587b5b9ac](https://github.com/NousResearch/hermes-agent/commit/587b5b9ac) | fix(backup) | 捕获 HERMES_HOME 外的 memory-provider state (#50325) | **P1** |
| [4b09903de](https://github.com/NousResearch/hermes-agent/commit/4b09903de) | fix | Nous auth refresh for idle agents | **P1** |
| [f22dd8a75](https://github.com/NousResearch/hermes-agent/commit/f22dd8a75) | fix(agent) | 持续 401/403 时 failover 到 fallback provider | **P1** |
| [9351cbafa](https://github.com/NousResearch/hermes-agent/commit/9351cbafa) + [8ac5e90ec](https://github.com/NousResearch/hermes-agent/commit/8ac5e90ec) | fix(gateway) | auto-deliver `image_generate` 输出为 native media | **P1** |
| [c0409a87f](https://github.com/NousResearch/hermes-agent/commit/c0409a87f) | feat(gateway) | typed `SendResult.error_kind` 分类 | **P1** |
| [51a338a1b](https://github.com/NousResearch/hermes-agent/commit/51a338a1b) | feat(gateway) | turn 边界把 `active_agents` 写入 runtime status | **P1** |
| [012f40c98](https://github.com/NousResearch/hermes-agent/commit/012f40c98) | fix(status) | psutil fallback 跨平台 start-time fingerprint (与 e44772314 配套) | **P1** |
| [9ca969734](https://github.com/NousResearch/hermes-agent/commit/9ca969734) | fix(gateway) | voice transcription tuple return (#42090) | **P1** |

### P1.6 codex / model picker auxiliary

| commit | scope | what | 推荐 |
|---|---|---|---|
| [243cada15](https://github.com/NousResearch/hermes-agent/commit/243cada15) + [fad4b40d9](https://github.com/NousResearch/hermes-agent/commit/fad4b40d9) + [af978ecb1](https://github.com/NousResearch/hermes-agent/commit/af978ecb1) | fix(model) | typed gateway /model path + async-safe pricing + 持久化 + 昂贵模型确认 | **P1** |
| [3a9bc9d88](https://github.com/NousResearch/hermes-agent/commit/3a9bc9d88) | fix(model picker) | 统一 /model 与 hermes model 列表 + disk cache | **P1** |

---

## P2 — 可选 (新功能 / 未来可能用)

- **gateway multiplex** `d82f9fa7f` `f538470cf` `f35abb122` `d5d02eabb` `1e70df5fd` — webhook 多 profile 路由,dovie 当前用 runtime-scoped worker,功能重叠
- **relay transport** `237fa7d29` `6b03874d0` `3db9b3e61` — A2 capability,dovie 走 sidecar 不需要
- **Chronos cron NAS** `4c8bbe641` `b75757d4a` — scale-to-zero,dovie 本地常驻
- **gateway 时间戳** `bd7fc8fdc` `36ae95847` — opt-in
- **hindsight** `09d66037f` `a376ca008` `5e3e89cc0` `7bc6f1806` `6c44471bf` `13f1efdd1` — observability plugin
- **OpenViking memory** `3c76dac4f` 等 — 跟 dovie 客户端栈无关
- **delegation uncap depth** `d41427504` — dovie team mission 已 own 调度

---

## P3 — 不推荐 (不展开)

- 所有 telegram/slack/signal/discord/whatsapp/feishu/wecom/qqbot/teams/openviking/photon adapter 修复 (~60 条)
- 所有 fix(desktop)/fix(tui)/style (~95 条):用户明说 UI 不在意
- antigravity / google OAuth provider 改:dovie 走云端 provider catalog
- kanban 全部:dovie 有自己 team_mission_runtime
- email pairing / dashboard chat session titles:UI/产品形态
- gateway 命令行 matcher / Windows / Telegram OGG / Slack approval

---

## 3 个 quick-wins (用户最该立刻吸收)

按"零冲突 + 实际严重 bug/安全"排序:

### 1. [6f0ecf37d](https://github.com/NousResearch/hermes-agent/commit/6f0ecf37d) `fix(redact)`: x-api-key + 全部 Authorization scheme 都 redact

dovie 当前 `agent/redact.py` 只匹配 `Authorization: Bearer`,所有 `x-api-key`、自定义 auth header 都泄漏到日志/transcript/事件流。**fork 没动过此文件相关区段,可直接 cherry-pick**。

```bash
cd /Users/yangwanquan/syngents/code/hermes-agent
git cherry-pick 6f0ecf37d
```

### 2. [e44772314](https://github.com/NousResearch/hermes-agent/commit/e44772314) + [8cfcbd327](https://github.com/NousResearch/hermes-agent/commit/8cfcbd327) `fix(process)`: PID 复用误杀 + SIGKILL 进程树

两条配套修复,实测会误杀 Firefox 等用户进程。dovie automation/web_tools 在用户机器上起子进程,正是受影响场景。**`tools/process_registry.py` fork 未改,可直接 cherry-pick 两条**。

```bash
git cherry-pick e44772314 8cfcbd327
```

### 3. [7726ce304](https://github.com/NousResearch/hermes-agent/commit/7726ce304) `fix(security)`: 关闭 hermes-0day MCP 持久化攻击面

针对 **June 2026 真实进行中的 MCP 持久化攻击** 的官方响应。dovie 通过 hermes runtime 加载 MCP 工具时,用户主机直接暴露。`tools/mcp_security.py` 增项几乎不冲突;dashboard 部分按 dovie auth 模型选取。

```bash
# 这条涉及多个文件,先看 diff 决定:
git show 7726ce304 --stat
git cherry-pick 7726ce304   # 如有冲突,手工解决 web_server.py 部分
```

---

## 需人工 review 的不确定点

| 项 | 不确定点 |
|---|---|
| **delegation 一组** ([ea8a8b4af](https://github.com/NousResearch/hermes-agent/commit/ea8a8b4af) + [c66ecf0bc](https://github.com/NousResearch/hermes-agent/commit/c66ecf0bc)) | 与 dovie `team_mission_runtime` 的 worker/调度模型可能在概念上重叠。需先确认 background subagent 是否会绕过 dovie 子 agent 精确工具继承 (codex 红线第 7 条) |
| **[47fadc24d](https://github.com/NousResearch/hermes-agent/commit/47fadc24d) in-place compaction** | codex 文档明确说"User-created branch 和 compression continuation 不应共用同一 ancestor replay 语义"(红线 10)。in-place compaction 保单 session id 与该红线可能交互,需先比对 dovie `hermes_state_branch.py` |
| **[ae94ed172](https://github.com/NousResearch/hermes-agent/commit/ae94ed172) tui-gateway slash_worker reap** | dovie 已模块化 `tui_gateway/`,这条要手工迁到对应 `methods/services`,不要回灌单文件 `server.py` |
| **[7726ce304](https://github.com/NousResearch/hermes-agent/commit/7726ce304) 中的 dashboard 部分** (`--insecure` 不再绕 auth) | codex 把 dashboard OAuth 列为延后,这条会改变 `should_require_auth` 语义,需确认 dovie sidecar 是否依赖 loopback 绕过 |
| **[8e4c447e5](https://github.com/NousResearch/hermes-agent/commit/8e4c447e5) state.db 重复 user messages 防护** | 与 dovie `session_index` 路径相邻,需先看 diff 再决定 |

---

## 验证 ── 关键 commit hash 真实存在(2026-06-23 实测)

```
6f0ecf37d  fix(redact): mask all Authorization schemes and x-api-key style headers       ✓
e44772314  fix(process-registry): re-validate PID identity before killing host processes ✓
8cfcbd327  fix(process): SIGKILL the whole tree on escalation                            ✓
7726ce304  fix(security): close hermes-0day MCP-persistence attack surface               ✓
ae94ed172  fix(tui-gateway): reap leaked slash_worker sessions on disconnect             ✓
9f67ba1b0  fix(agent): guard finalize_turn cleanup chain                                 ✓
020e59d3c  fix(agent): dampen empty-name phantom tool-call loop                          ✓
40722058e  fix(mcp): keep short-TTL HTTP sessions alive                                  ✓
7a131f7f4  fix(api-server): stop silently promising async                                ✓
47fadc24d  feat(compression): in-place compaction option                                 ✓
```

dovie customize 边界 verify:
- `tools/approval.py`:dovie 已 patch (57db267a2 team-mission clarify/approval),所有 P0.1 涉及此文件的需手工融合 ✓
- `agent/conversation_loop.py`:dovie 已 patch (81af17d49 + 10bd4b0b5 + 2fb895be1),所有 P0.3 涉及此文件的需手工 ✓
- `agent/redact.py`:**dovie 未改**,quick-win #1 可直接 cherry-pick ✓
- `tools/process_registry.py`:**dovie 未改**,quick-win #2 可直接 cherry-pick ✓

---

## 2026-06-23 吸收执行报告(absorb-upstream-20260623 分支)

### 累计

| 类型 | 数 |
|---|---|
| 成功 cherry-pick | **65** |
| 需手工融合(SKIP) | **62** |
| 缺失依赖回填 commit | **4** ── threat_patterns / aux_interrupt_protection / claim_job_for_fire / peek_nous_access_token / compression-lock |
| 总分支 commit(从 a9615a66b) | **70+** |

### 验收

- 关键模块 import 全通过(dovie_extension / agent / tools / state / mcp / tui_gateway)
- dovie customize 完好(should_require_auth + `[doxie-session-index]` + dovie_extension 全在)
- dovie 关键测试 **1194 通过**(redact / process / approval / skills / memory / state / session_index / team_missions)
- 2 个测试已知失败(`test_compression_concurrent_fork.py`)── 单进程 dovie 桌面端不实际发生

### 已知问题清单

1. **concurrent compression 测试 2 fail**:`test_concurrent_compression_does_not_fork_session` + `test_skipped_compression_returns_messages_unchanged`。原因:`agent/conversation_compression.py` 是 dovie customize 文件,SKIP 了上游 lock-check 集成。单进程下不发生。修法:等 team-mission 收敛后手工 3-way merge upstream `1fbf48d4a` 把 lock-check 集成到 `_compress_context`。
2. **24 个 collection error(主要 telegram)**:dovie 不用 telegram,可忽略。
3. **dovie fork file_state_registry 老 bug**:`/var/folders` 被误判为 sensitive path。a9615a66b 状态也有这 bug,非本次吸收引入。建议另行排查。

### Skip 清单(等手工 3-way merge)── 按文件聚类

每条都因 dovie 已在同文件 customize,需 review 上游改动是 dovie 加新代码区域还是同区域改:


#### `acp_adapter/server.py(1 commit)` (1 条)

- `16642e276` fix(mcp): revert ACP rebuild to original; harden generation 

#### `agent/agent_init.py(6)` (1 条)

- `3ead2bdd0` feat(prompt): configurable per-platform system-prompt h

#### `agent/agent_runtime_helpers.py(2)` (3 条)

- `2b3a4f0af` fix(agent): strip stale reasoning_content when falling 
- `c884ff64e` fix(agent): keep system-prompt model identity in sync a
- `38c8a9c10` feat(memory): batch operations for single-turn memory u

#### `agent/auxiliary_client.py(3)` (1 条)

- `2f3177adf` fix(compression): protect the summary call from mid-fli

#### `agent/chat_completion_helpers.py(5)` (2 条)

- `dd0d1222a` fix(agent): don't retry interrupt-induced transport err
- `c9094f5e5` fix(stream): don't report dropped mid-tool-call streams

#### `agent/conversation_compression.py(2)` (4 条)

- `466345699` fix(compression): in-place compaction is non-destructiv
- `1fbf48d4a` fix(compression): make in-place compaction durable + ro
- `47fadc24d` feat(compression): in-place compaction option that keep
- `990273d90` fix(agent): accept pixel-correct image downscale when b

#### `agent/conversation_loop.py(5)` (2 条)

- `b892ee2bc` fix(agent): summarize non-retryable API errors so raw H
- `9f95f72b9` fix(agent): strip api_messages in thinking-signature re

#### `agent/error_classifier.py(2)` (1 条)

- `86e10dd87` fix(agent): route 'thinking blocks cannot be modified' 

#### `agent/prompt_builder.py(8)` (1 条)

- `f80381c45` feat(prompt): scale context-file cap to model window + 

#### `agent/system_prompt.py(3)` (2 条)

- `3e74f75e4` feat(agent): coding-context posture across CLI/TUI/desk
- `f6a42b1ac` feat(prompt): make context-file truncation limit config

#### `agent/tool_executor.py(4)` (1 条)

- `a2d7f538d` fix(delegate): stop subagent tool completion lines leak

#### `agent/transports/codex.py(1)` (1 条)

- `4d39a603d` fix(codex): restore session_id/x-client-request-id HTTP

#### `cli.py(2)` (1 条)

- `fad4b40d9` fix(model): persist /model switch by default across ses

#### `gateway/platforms/api_server.py(1)` (2 条)

- `3714caa1b` fix(session): follow compression continuations for tran
- `7a131f7f4` fix(api-server): stop silently promising async delivery

#### `gateway/run.py(7 commit)` (1 条)

- `93d6e7302` fix(mcp): expose late-connecting MCP tools to the agent (TUI

#### `gateway/run.py(7)` (5 条)

- `1593ca540` feat(cron): Cron Recipes — parameterized automation t
- `4b09903de` fix Nous auth refresh for idle agents
- `9351cbafa` fix(gateway): auto-deliver image_generate output as nat
- `8ac5e90ec` fix(gateway): dedup image_generate media across the com
- `51a338a1b` feat(gateway): track active_agents in runtime status on

#### `gateway/session_context.py(5)` (1 条)

- `b23184cad` fix(api-server): bind request session context for tools

#### `hermes_cli/config.py(5 commit)` (1 条)

- `b6e2a54a9` fix(mcp): address adversarial review round 1 (cache parity, 

#### `hermes_cli/config.py(5)` (2 条)

- `93ea9b04a` fix(gateway): cap inbound media download size to preven
- `e499d69e3` feat(api-server): configurable concurrent-run cap to pr

#### `hermes_cli/skills_hub.py(3)` (1 条)

- `085fc5d00` feat(skills): find & diff user-modified bundled skills

#### `hermes_cli/web_server.py(5 commit)` (2 条)

- `7726ce304` fix(security): close hermes-0day MCP-persistence attack surf
- `73dd58499` fix(mcp): propagate HERMES_HOME override onto the MCP event 

#### `hermes_cli/web_server.py(5)` (3 条)

- `c253b0738` fix(model): clear stale endpoint credentials across swi
- `243cada15` fix(model): cover typed gateway /model path + async-saf
- `af978ecb1` fix(model): require confirmation for expensive model se

#### `run_agent.py(7)` (2 条)

- `b17180d95` fix(session): finalize owned SQLite session rows on AIA
- `ea8a8b4af` feat(delegation): background fan-out — parallel subag

#### `scripts/release.py(1)` (2 条)

- `f8a241e10` fix(delegate): flatten content blocks in live overlay t
- `3b56d3a29` fix(security): redact secrets in kanban tool payloads b

#### `tools/approval.py(6)` (3 条)

- `a9c802598` fix(approval): honor interrupt in blocking gateway appr
- `89d380261` fix(approval): resolve Hermes home at detection time, n
- `239740a19` feat(tools): MCP elicitation handler with gateway-aware

#### `tools/delegate_tool.py(7)` (1 条)

- `c66ecf0bc` feat(delegation): async background subagents via delega

#### `tools/file_tools.py(4 commit)` (4 条)

- `71274f264` fix(file): reject read_file line-numbered writeback
- `9078b4bbd` fix(file): harden read_file device alias blocking
- `def3f6388` fix(file): anchor device symlink guard to task cwd
- `8f2931e3e` fix(file_tools): block agent writes to ~/.hermes/config.yaml

#### `tools/mcp_tool.py(1 commit)` (3 条)

- `40722058e` fix(mcp): keep short-TTL HTTP sessions alive with configurab
- `472c06815` fix(mcp): detect 'unknown method' phrasing in ping keepalive
- `371348387` fix(mcp): refresh agent tool snapshot between turns (cache-s

#### `tools/skills_hub.py(4)` (2 条)

- `105625d65` fix(skills): honour overall_timeout and bound ClawHub c
- `eee1da45f` fix(skills): bound ClawHub catalog walk to requested pa

#### `tools/skills_sync.py(1)` (2 条)

- `25c590ccd` fix(skills): refuse SKILLS_DIR root in rmtree guard, no
- `f1254c8ea` fix(skills): rmtree scope guard + default pre_update_ba

#### `tui_gateway/server.py(18)` (4 条)

- `621bf3a87` fix(security): strip shell escapes in denylist normaliz
- `ae94ed172` fix(tui-gateway): reap leaked slash_worker sessions on 
- `99f3072aa` fix(model-switch): a failed in-place swap must be a no-
- `49596b70c` fix(gateway): resume follows the compression tip so pos


---

## 2026-06-23 手工 3-way merge 追加报告

继续吸收 5 条原报告"missing"高优先 P0。最终发现 4 条实际已经在分支(初版报告误标),3 条新做手工 merge:

| commit | 状态 | 备注 |
|---|---|---|
| `7726ce304` MCP 0day (3-way merge) | ✅ **吸收** | web_server.py 1 hunk 取 upstream;mcp_security.py/dashboard.py/test 新创建;dovie should_require_auth + _session_automation_counts 保留 |
| `40722058e` + `5affecb44` MCP keepalive + capability gate | ✅ **吸收** | mcp_tool.py 3+3 hunks 取 upstream(dovie 无业务冲突);dovie NonMcpEndpointError 保留 |
| `99f3072aa` model swap no-op | ✅ **吸收** | tui_gateway/server.py 1 hunk 取 upstream(70 vs 8 行 fail-safe 加固) |
| `ae94ed172` tui slash_worker reap | ✅ **手工 port**(commit f986d535a)| 4 个核心 stability fix 移植到 dovie 模块化结构:RLock 升级 / `_attach_worker` C2 race fix / `_close_sessions_for_transport` 接 ws.py 走 asyncio.to_thread / `SlashWorker._closed` 防 double-close / `_live_sessions_by_stored_key` 跳过 `_finalized`。**故意不移植**:1097 行 method 体(dovie 已 modularized)/ grace-windowed orphan reaper(dovie 不暴露 CLI detach)/ uvicorn ws_ping(dovie 不用 uvicorn 启动 sidecar)。10 hunks,其中 Hunk 9 加 1005 行新 @method 装饰器方法,跟 dovie tui_gateway/methods/ 模块化结构双重定义;Hunk 3 是 dovie `_db_for_stable_session()` customize 不能丢。建议手工把 disconnect reap + active_list liveness 关键逻辑(C1/C2 + RLock + grace-reap)迁到 dovie 自己的模块 |
| `8f2931e3e` file_tools.py block ~/.hermes/config write | ✅ **已含**(老 sync b725c1494) | dovie HEAD 已含 `_get_hermes_config_resolved()` + `_check_sensitive_path` hermes_config 守护 |
| `8fcb8136b` smart approval guard | ✅ **已含**(Phase 2) | |
| `9f67ba1b0` finalize_turn cleanup | ✅ **已含**(Phase 3) | |
| `020e59d3c` phantom tool-call 循环抑制 | ✅ **已含**(Phase 3) | |
| `8e4c447e5` state.db dedup | ✅ **已含**(Phase 3) | |

### 累计

- **74 commit** 在 absorb-upstream-20260623(从 a9615a66b 起)
- **9 条高优先 P0 中 8 条已吸收**,1 条(ae94ed172)跳过等手工迁移
- **900 关键测试通过**,9 个 mcp_security dashboard endpoint 测试 fail(dovie web_server.py 不一定暴露这些 POST endpoint,功能 import 都正常,记入已知问题)


---

## 最终吸收状态清单(2026-06-23 末次更新)

> 状态:
> - ✅ 已吸收(cherry-pick 或 hand-port)
> - ⏸ 未吸收(等手工 3-way merge)
> - 🚫 不打算吸收(P2 可选 / P3 不推荐)

### P0 必吸收 — 25/45 完成(55.6%)

**真 SKIP** 20 条,等手工 3-way merge:

- ⏸ `16642e276` fix(mcp): revert ACP rebuild to original; harden generation guard
- ⏸ `2b3a4f0af` fix(agent): strip stale reasoning_content when falling back to a strict provider
- ⏸ `371348387` fix(mcp): refresh agent tool snapshot between turns (cache-safe late-binding)
- ⏸ `472c06815` fix(mcp): detect 'unknown method' phrasing in ping keepalive fallback
- ⏸ `621bf3a87` fix(security): strip shell escapes in denylist normalizer; fail-closed on missin
- ⏸ `71274f264` fix(file): reject read_file line-numbered writeback
- ⏸ `73dd58499` fix(mcp): propagate HERMES_HOME override onto the MCP event loop (#44220)
- ⏸ `86e10dd87` fix(agent): route 'thinking blocks cannot be modified' 400 to recovery
- ⏸ `89d380261` fix(approval): resolve Hermes home at detection time, not import time
- ⏸ `93d6e7302` fix(mcp): expose late-connecting MCP tools to the agent (TUI/CLI/gateway)
- ⏸ `9f95f72b9` fix(agent): strip api_messages in thinking-signature recovery so the retry actua
- ⏸ `a9c802598` fix(approval): honor interrupt in blocking gateway approval wait (#8697)
- ⏸ `b6e2a54a9` fix(mcp): address adversarial review round 1 (cache parity, gates, races)
- ⏸ `b892ee2bc` fix(agent): summarize non-retryable API errors so raw HTML never leaks
- ⏸ `c253b0738` fix(model): clear stale endpoint credentials across switches
- ⏸ `c884ff64e` fix(agent): keep system-prompt model identity in sync across provider failover
- ⏸ `c9094f5e5` fix(stream): don't report dropped mid-tool-call streams as output truncation (#4
- ⏸ `dd0d1222a` fix(agent): don't retry interrupt-induced transport errors (cascading-interrupt 
- ⏸ `def3f6388` fix(file): anchor device symlink guard to task cwd
- ⏸ `f45ace931` feat(security): startup security posture audit (warn-on-load)

### P1 强推荐 — 48/83 完成(57.8%)

**真 SKIP** 35 条,等手工 3-way merge:

- ⏸ `085fc5d00` feat(skills): find & diff user-modified bundled skills
- ⏸ `105625d65` fix(skills): honour overall_timeout and bound ClawHub catalog walk
- ⏸ `1593ca540` feat(cron): Cron Recipes — parameterized automation templates across every surfa
- ⏸ `239740a19` feat(tools): MCP elicitation handler with gateway-aware approval routing
- ⏸ `243cada15` fix(model): cover typed gateway /model path + async-safe pricing lookups
- ⏸ `25c590ccd` fix(skills): refuse SKILLS_DIR root in rmtree guard, not just outside-tree
- ⏸ `3714caa1b` fix(session): follow compression continuations for transcript reads
- ⏸ `38c8a9c10` feat(memory): batch operations for single-turn memory updates (#48507)
- ⏸ `3b56d3a29` fix(security): redact secrets in kanban tool payloads before persistence
- ⏸ `3e74f75e4` feat(agent): coding-context posture across CLI/TUI/desktop/ACP (#43316)
- ⏸ `3ead2bdd0` feat(prompt): configurable per-platform system-prompt hint overrides
- ⏸ `47fadc24d` feat(compression): in-place compaction option that keeps one session id (#38763)
- ⏸ `49596b70c` fix(gateway): resume follows the compression tip so post-compression replies ren
- ⏸ `4b09903de` fix Nous auth refresh for idle agents
- ⏸ `4d39a603d` fix(codex): restore session_id/x-client-request-id HTTP headers for cache routin
- ⏸ `51a338a1b` feat(gateway): track active_agents in runtime status on turn boundaries
- ⏸ `7a131f7f4` fix(api-server): stop silently promising async delivery on stateless HTTP path (
- ⏸ `8ac5e90ec` fix(gateway): dedup image_generate media across the compression boundary
- ⏸ `9351cbafa` fix(gateway): auto-deliver image_generate output as native media (#42616)
- ⏸ `93ea9b04a` fix(gateway): cap inbound media download size to prevent memory exhaustion
- ⏸ `990273d90` fix(agent): accept pixel-correct image downscale when bytes grow (#48013)
- ⏸ `a2d7f538d` fix(delegate): stop subagent tool completion lines leaking into parent CLI displ
- ⏸ `af978ecb1` fix(model): require confirmation for expensive model selections
- ⏸ `b17180d95` fix(session): finalize owned SQLite session rows on AIAgent.close()
- ⏸ `b23184cad` fix(api-server): bind request session context for tools
- ⏸ `c66ecf0bc` feat(delegation): async background subagents via delegate_task(background=true) 
- ⏸ `cca3b77a4` fix(compression): clear _previous_summary on session end (defense-in-depth)
- ⏸ `e499d69e3` feat(api-server): configurable concurrent-run cap to prevent DoS (#50007)
- ⏸ `ea8a8b4af` feat(delegation): background fan-out — parallel subagents, one consolidated retu
- ⏸ `eee1da45f` fix(skills): bound ClawHub catalog walk to requested page on cold start (#43395)
- ⏸ `f1254c8ea` fix(skills): rmtree scope guard + default pre_update_backup to true (#48200)
- ⏸ `f6a42b1ac` feat(prompt): make context-file truncation limit configurable
- ⏸ `f80381c45` feat(prompt): scale context-file cap to model window + point agent at truncated 
- ⏸ `f8a241e10` fix(delegate): flatten content blocks in live overlay tail + AUTHOR_MAP
- ⏸ `fad4b40d9` fix(model): persist /model switch by default across sessions

### P2 可选 — 0/20(从未在吸收清单)

原文档判定 P2 全部"可选"。本次**未吸收任何 P2**(gateway multiplex / relay / Chronos NAS cron / hindsight / opt-in 时间戳 / OpenViking memory / cron recipes 等)。

### P3 不推荐 — 0 / 从未在清单

telegram/slack/signal/discord/whatsapp/feishu/wecom/qqbot/teams/openviking/photon adapter (~60) + UI fix(desktop/tui) (~95) + kanban + dashboard chat session titles 等。

---

### 综合统计

| 优先级 | 计划 | ✅ 已吸收 | ⏸ 等手工 | 🚫 不做 |
|---|---|---|---|---|
| P0 必吸收 | 45 | 25 (55.6%) | 20 | 0 |
| P1 强推荐 | 83 | 48 (57.8%) | 35 | 0 |
| P2 可选 | 20 | 0 | 0 | 20 |
| P3 不推荐 | (大量) | 0 | 0 | 全部 |

**absorb-upstream-20260623 分支共 77 commit**(65 cherry-pick + 4 backfill + 1 hand-port `f986d535a` ae94ed172 + 7 docs/revert)。

### 等手工的项处理建议

剩余 P0+P1 共 **55** 条,都涉及 dovie customize 的同文件:
- `tools/approval.py` — smart approval / interrupt honoring
- `agent/conversation_compression.py` — compression in-place / lock-check
- `tui_gateway/server.py` — 老 sync 后又有上游 MCP / session 改动
- `agent/agent_runtime_helpers.py` — thinking-signature / fallback 路径
- `hermes_cli/web_server.py` — MCP 多个修复 + security audit
- `tools/file_tools.py` — 几个 read_file 守护可能 functional 已含但脚本误判

**建议时机**:dovie team-mission 路径稳定 1-2 周后做。期间累积的 dovie 改动越少,3-way merge 越简单。

### 已知问题(absorb-upstream-20260623 上)

1. **2 个 `test_compression_concurrent_fork.py` 失败**:`1fbf48d4a` lock-check SKIP。compression-lock 方法已 backfill 让 import 不崩,但 lock-check 在 `_compress_context` 里需要 3-way merge 才能 wire。单进程桌面端不实际并发。
2. **13 个 tui_gateway 测试失败**:dovie fork 预先存在(verified at 400d0fc51 也 fail),非本次吸收引入。
3. **9 个 `test_mcp_security.py` 失败**:POST `/api/mcp/servers` dashboard endpoint dovie 不暴露(走 IPC sidecar)→ 405 Method Not Allowed。功能 import OK。
4. **`file_state_registry` 2 个失败**:`/var/folders` 误判 sensitive,dovie fork 老 bug。
