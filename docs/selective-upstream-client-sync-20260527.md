# 选择性吸收上游更新清单 - 客户端接入

## 目标

本分支用于评估并选择性吸收 `upstream/main` 在 `1264fab15..4feb181eb` 范围内，对当前客户端接入真正有价值的更新。

当前分支：

```text
codex/selective-upstream-client-sync-20260527
```

结论先行：

```text
不做全量 merge。
优先选择性吸收影响客户端稳定性、API 能力发现、认证安全、streaming 正确性的上游修复。
继续保留 Doxie 本地客户端接入边界。
```

## 当前吸收状态

截至本分支当前工作区，已开始按 P0/P1 执行选择性吸收，未执行 `upstream/main` 全量合并。

已吸收：

- P0 streaming / Codex / provider request stability：已吸收清单中的全部 P0 提交。
- P1 API 能力发现：已吸收 `/v1/skills`、`/v1/toolsets` 以及 `skills_api` capability。
- P1 Skills / file / plugin / media 安全：已吸收 symlink bundle 拒绝、lock install path 防护、dashboard plugin path/env 防护、sensitive store 权限、gateway media path 防护。
- P1 Dashboard / WebSocket 安全：已吸收 Host/Origin 校验、loopback WS 限制、gated mode 下 loopback peer bypass、`should_require_auth` 和 `app.state.auth_required`。

有意未全量吸收：

- 完整 Dashboard OAuth 登录页面、WS ticket、SPA AuthWidget 和 re-auth envelope：这些需要先和 Doxie sidecar token / runtime worker bridge 的认证关系做设计，不在本轮直接套入。
- API server session controls / media session chat 的完整实现：本轮只吸收能力发现和安全相关部分，避免绕过 Doxie run control 与 prompt attachment contract。
- TUI session orchestrator、Docker/s6、website/i18n、optional skills/MCP 大量新增：仍按 P2/P3 暂缓。

## 现状更新（2026-06-02）

本分支在 `doxie-upstream-sync-20260527` 之后继续保留 Doxie 客户端接入边界，并补齐两项本地 runtime 行为：

- Doxie automation 的 create / update / remove 工具结果现在可以走 direct tool response。`agent/direct_tool_response.py` 只对白名单内的 Doxie 自动化工具、单个 tool call、结构化成功事件生效；`agent/conversation_loop.py` 在工具执行后直接生成最终 assistant 回复并结束本轮，避免为了“已创建/已更新/已删除任务”这种确定性结果再请求模型 follow-up，从而减少延迟和二次流式卡住风险。
- `session.list` 和 `session.most_recent` 现在把 `cron` 与 `tool` 一起视为内部 runtime source。Doxie 自动化任务的用户可见结果应通过 current-session / new-session result binding 投影到目标会话；raw cron execution session 不应作为独立聊天出现在 Doxie 会话侧栏，否则会暴露内部 cron prompt 并造成重复会话。

这两项都不是对上游 API session controls 或原生 `cronjob` 的吸收。它们是本地 Doxie contract 的收敛：自动化任务仍由 `tools/doxie_automation_task_tool.py`、`tui_gateway/services/doxie_cron_jobs.py` 和 Doxie run/event binding 管理，模型不重新获得原生 `cronjob` 工具作为主入口。

## 现状更新（2026-06-04，prodv0.9.0）

本地工作区在 `doxie-upstream-sync-20260602` 之后继续把 Hermes 作为 Doxie 的 agent engine，而不是切换到上游 API server session controls。当前改动集中在生产版客户端运行态可观测性和子 agent 权限边界：

- `delegate_task` 子 agent 的工具继承从 `tools/delegate_tool.py` 拆到 `tools/delegate_tool_access.py`。父 agent 可以通过 `enabled_tools` 把已经解析过的精确工具名传给子 agent，子 agent 只获得父级真实加载且未被阻断的工具；请求 `web` / `search` 等语义别名时，也会先映射到 Doxie 托管 web 工具再和父级工具面求交，避免因 toolset 粒度过粗获得 sibling tools。
- 子 agent 身份和展示名由 `tools/subagent_identity.py` 统一清洗、生成和人类可读化。`delegate_task` 现在把 `delegate_call_id`、`agent_name`、`role`、`context`、`dispatch_message`、tool running/completed 状态以及子 agent output delta 一起投递到 gateway event stream，Doxie 右侧运行面板可以稳定关联一次 delegate tool call 下的子任务。
- `hermes_state_runs.py` 对 token 级 stream delta 做持久化合并，并新增 `list_run_events_filtered()` 与 `compact_run_events()`。这使 `run_events` 既能支撑实时流，又不会因为 message / reasoning / subagent delta 激增而让历史回放和 Doxie hydration 退化。
- TUI Gateway 新增 `subagent.runs.list`、`subagent.events.list` 和 `events.compact`，实现位于 `tui_gateway/services/subagent_snapshots.py` 与 `tui_gateway/methods/run.py`。客户端可以按 session / runtime scope 拉取子 agent 快照和详情事件，而不是重放整段主会话事件流。
- Doxie sidecar 通过 `DOXIE_SIDECAR_PARENT_PID` 增加父进程 watchdog；runtime proxy 会把该 PID 传给 sidecar。父 runtime 退出或 sidecar 被错误收养时，sidecar 会主动退出，避免本地桌面客户端留下孤儿 WebSocket 进程。
- `session.recall_turn` 支持用 `turn_id`、`run_id` 或 `client_message_id` 定位用户轮次；中断运行中 turn 时会为活跃子 agent 补发 `subagent.complete`，避免客户端右侧面板留下永远 running 的子任务。

这些是 Doxie 客户端 contract 的本地生产化收敛，不是对上游 TUI session orchestrator、API server session controls 或完整 dashboard OAuth / WS ticket 链路的吸收。后续合并上游时必须保留：精确工具面继承、runtime-scoped subagent event API、run event compaction、sidecar 父进程生命周期，以及 Doxie 对 recall / interrupt 的子 agent 完结投影。

## 现状更新（2026-06-05，0.9.2）

本地工作区在 `prodv0.9.0` 之后继续按“手工迁移、保留 Doxie contract”的方式吸收上游 `4feb181eb..96cd37e21` 中的稳定性和安全修复。详细分析见 `UPSTREAM_UPDATE_REPORT_2026-06-05.md`。

当前 0.9.2 发布节点包括：

- P0 稳定性和安全：TUI/Gateway 并发锁与 resume/close race、Zombie agent/session reset 清理、approval 与 file tools 对 Hermes config/env 写入防护、Cron 非阻塞 tick / parallel pool / profile cwd、State/WAL 可靠性、MCP probe/shutdown fast-fail、Vision pixel cap。
- P1 Branch / Session / Search：`messages.active` soft-delete、Doxie `session_lineage` branch primitives、`session.branch` 模块化实现、`/undo [N]` 和 `/rewind` prefill 合同、SQL-bounded session-id search、Web session search 的 ID 优先和 compression-lineage 去重。
- P1/P2 局部工具和客户端体验：Doxie visible browser bridge source URL、`system.search`、MCP HTML/non-MCP endpoint preflight、Vision native provider shrink recovery、slash command prefill 在 TUI 和 Web 前端统一。

仍未吸收：上游 React Desktop、Bootstrap installer、完整 Dashboard OAuth / remote gateway auth、Channels UI、Skills 全量 catalog 和 progressive tool disclosure。这些需要另行按 Doxie profile/runtime_scope、云端模型代理和技能市场生命周期设计。

## 逐项核对结果（2026-05-27）

本节记录按本文 P0/P1 清单逐项核对当前工作区后的真实状态。后续再同步上游时，优先看这里，而不是只看提交是否 cherry-pick 成功。

### P0 核对：必须吸收项

结论：P0 已全部落地。核对时曾发现 `4243b6dc4` 只列入文档但 TTFB 首包超时分支未带 silent-hang hint，本轮已补齐代码和测试。

| 提交 | 状态 | 当前证据 | 覆盖测试 |
|---|---|---|---|
| `775a17284` | 已吸收 | `agent/transports/chat_completions.py` 清理 Hermes internal scaffolding keys | `tests/agent/transports/test_chat_completions.py` |
| `20b3703a4` | 已吸收 | `agent/conversation_loop.py` 对 partial-stream length continuation 使用专门续写提示 | `tests/run_agent/test_partial_stream_finish_reason.py` |
| `9140be7c2` | 已吸收 | `agent/chat_completion_helpers.py` 为 text-only partial-stream stub 标记 `finish_reason=length` | `tests/run_agent/test_partial_stream_finish_reason.py` |
| `ac5359a3f` | 已吸收 | `hermes_constants.py` 定义 `PARTIAL_STREAM_STUB_ID`，conversation loop 识别 mid-tool-call partial stub | `tests/run_agent/test_partial_stream_finish_reason.py` |
| `2d422720b` | 已吸收 | `run_agent.py` request/stale timeout resolver，`agent/transports/codex.py` 透传 Responses timeout | `tests/agent/transports/test_codex_transport.py` |
| `8601c4d44` | 已吸收 | `agent/chat_completion_helpers.py` Codex TTFB watchdog | `tests/agent/test_codex_ttfb_watchdog.py` |
| `43a3f119f` | 已吸收 | `agent/codex_runtime.py` / `agent/codex_responses_adapter.py` 处理 null output stream | `tests/run_agent/test_run_agent_codex_responses.py` |
| `b6ca56f65` | 已吸收 | `agent/error_classifier.py` 和 `agent/conversation_loop.py` 支持 `invalid_encrypted_content` 恢复 | `tests/agent/test_error_classifier.py`, `tests/run_agent/test_codex_xai_oauth_recovery.py` |
| `b1a46b304` | 已吸收 | `agent/codex_responses_adapter.py` replay 时丢弃 transient `rs_tmp` state | `tests/run_agent/test_codex_xai_oauth_recovery.py` |
| `9c69204d8` | 已吸收 | `agent/transports/codex.py` 标记 issuer，adapter/replay 丢弃 foreign issuer reasoning | `tests/run_agent/test_codex_xai_oauth_recovery.py` |
| `4243b6dc4` | 已吸收 | `run_agent.py` 新增 `_codex_silent_hang_hint()`，TTFB 和 stale timeout 都会带可操作 workaround | `tests/agent/test_codex_ttfb_watchdog.py`, `tests/run_agent/test_codex_silent_hang_hint.py` |
| `38b8d0da8` | 已吸收 | `agent/conversation_loop.py` guardrail halt 前发出明确终止消息 | `tests/run_agent/test_run_agent_codex_responses.py` |
| `8edeebe6d` | 已吸收 | `agent/conversation_loop.py` / `gateway/run.py` 保留 plugin transformed response，不被 streaming suppression 吞掉 | `tests/test_transform_tool_result_hook.py` |

### P1 核对：API 能力发现

结论：能力发现已吸收；session controls / media session chat 未作为 Doxie 客户端主链路替代方案吸收。

| 提交 | 状态 | 当前证据 | 说明 |
|---|---|---|---|
| `25f43d38d` | 已吸收 | `gateway/platforms/api_server.py` 注册 `/v1/skills`、`/v1/toolsets` | 用于客户端能力发现，不替代 Doxie gateway contract |
| `96223265b` | 已吸收 | `gateway/platforms/api_server.py` capability 中包含 `skills_api: true` | capability 与 API 路由一致 |
| `f7527b0fd` | 未作为 P1 直接吸收 | API server 当前已有部分 session capability 字段，但未采纳完整 session controls 作为客户端主控路径 | 继续以 `tui_gateway/services/run_control.py` 为 Doxie run control 边界 |
| `464b51d45` | 未作为 P1 直接吸收 | 当前只保留本地 prompt attachments / document parse contract | 不用上游 media session chat 替换 Doxie attachment enrichment |

### P1 核对：Dashboard / WebSocket 安全

结论：安全基线已吸收；完整 OAuth / WS ticket / SPA 重登录链路未吸收，需另做 Doxie token 与 dashboard auth 的统一设计。

| 提交 | 状态 | 当前证据 | 说明 |
|---|---|---|---|
| `8773bbf18` | 已吸收 | `hermes_cli/web_server.py::should_require_auth()` | auth gate 判断独立可测 |
| `949ad95e4` | 已吸收 | `app.state.auth_required` | dashboard runtime 可读取 gated 状态 |
| `53736b392` | 已吸收 | gated mode fail-closed，auth provider 为空时拒绝启动；开启 gated 时启用 proxy headers | 未带入完整 OAuth UI |
| `c3104195b` | 已吸收 | loopback WS peer 在 gated mode 下保留可用 | 仅限本地 peer 例外 |
| `2e66eefbc` | 已吸收 | `_ws_host_origin_is_allowed()` 校验 Host / Origin | 覆盖 `/api/pty`、`/api/ws/channel`、`/api/pub/ws`、`/api/events/ws` |
| `973255986` | 已吸收 | `_ws_client_is_allowed()` 限制 dashboard WS 为 loopback | 防远程直接滥用 WS |
| `b26d81d53` | 部分吸收 | `X-Forwarded-Prefix` 相关处理已在 `web_server.py` 中存在 | secure cookie/OAuth cookie 细节未完整吸收，因为完整 OAuth 栈未吸收 |
| `5b17eab67` | 未吸收 | 无 `/auth/*`、`/login` 完整 gate middleware | 需先设计与 Doxie sidecar token 的关系 |
| `b69fce9c8` | 未吸收 | 无 `/api/auth/ws-ticket`、无 single-use WS ticket store | 不能直接套入以免阻断 runtime worker bridge |
| `b2360ba44` | 未吸收 | WS 入口使用 Host/Origin/loopback guard，不使用 ticket auth | 等统一 WS auth 设计后再做 |
| `8971e9483` | 未吸收 | 前端未引入 `getWsTicket()` / `buildWsAuthParam()` | native client 不一定走 SPA ticket 模型 |
| `5e9308b5b` | 未吸收 | 未引入 401 re-auth envelope | native client 需要不同错误模型 |
| `2fc4615fc` | 未吸收 | 未引入 AuthWidget / 完整 `/api/status` auth fields | dashboard 登录展示暂不作为本轮目标 |

### P1 核对：安全和本地文件边界

结论：文档/skills/dashboard plugin/sensitive store/gateway media 的安全修复已吸收。

| 提交 | 状态 | 当前证据 | 覆盖测试 |
|---|---|---|---|
| `5744b1757` | 已吸收 | `web/src/components/Markdown.tsx` 限制 link scheme，`gateway/platforms/wecom_callback.py` 使用 `defusedxml` | `tests/hermes_cli/test_web_server.py` 相关 markdown/API 测试 |
| `c26af4681` | 已吸收 | `tools/skills_hub.py` quarantine install 拒绝 symlink bundle | `tests/tools/test_skills_hub.py` |
| `b82608a6f` | 已吸收 | `tools/skills_hub.py` lock install path normalize/redirect guard，`gateway/pairing.py` pending lock 展示防护 | `tests/tools/test_skills_hub.py` |
| `3b9b9a7ad` | 已吸收 | `tools/skills_hub.py` uninstall 使用 `_resolve_lock_install_path()` | `tests/tools/test_skills_hub.py` |
| `30928f945` | 已吸收 | `hermes_cli/web_server.py` dashboard plugin asset allowlist / env denylist | `tests/hermes_cli/test_web_server.py` |
| `8bf99227f` | 已吸收 | `hermes_cli/web_server.py::_safe_plugin_api_relpath()` 防 path traversal | `tests/hermes_cli/test_web_server.py` |
| `09f85f2cf` | 已吸收 | `hermes_cli/web_server.py` project plugin gate 使用 truthy env semantics | `tests/hermes_cli/test_web_server.py` |
| `3bace071b` | 已吸收 | `hermes_cli/config.py`、`gateway/platforms/api_server.py`、`hermes_cli/webhook.py` 设置敏感文件权限 | `tests/hermes_cli/test_config.py`, `tests/hermes_cli/test_webhook_cli.py` |
| `41d2c758c` | 已吸收 | `gateway/platforms/base.py`、`cron/scheduler.py`、`gateway/run.py`、`tools/send_message_tool.py`、`tools/yuanbao_tools.py` 使用安全 media path 交付 | `tests/gateway/test_platform_base.py`, `tests/gateway/test_tts_media_routing.py`, `tests/cron/test_scheduler.py`, `tests/tools/test_send_message_tool.py` |

## 当前本地客户端接入边界

这些能力是本地实现，不是上游等价功能，合并时必须保留：

| 能力 | 本地关键路径 | 上游是否等价 |
|---|---|---:|
| Doxie gateway contract / capabilities | `doxie_extension/*`, `tui_gateway/core/method_registration.py` | 否 |
| profile-scoped runtime worker | `runtime.ensure`, `tui_gateway/services/runtime_proxy.py` | 否 |
| run control / event replay | `tui_gateway/services/run_control.py`, `hermes_state_runs.py` | 否 |
| workspace / artifact API | `tui_gateway/services/workspaces/*`, `artifact_registry/*` | 否 |
| prompt attachments / document parse | `doxie_extension/prompt_attachments.py`, `document_parse_tool.py` | 否 |
| desktop visible browser bridge | `doxie_extension/browser_bridge.py`, `tools/browser_tool.py` | 否 |
| Doxie automation | `tools/doxie_automation_task_tool.py`, `tui_gateway/services/doxie_cron_jobs.py`, `agent/direct_tool_response.py` | 否 |
| 子 agent 精确工具继承 | `tools/delegate_tool_access.py`, `model_tools.py`, `agent/agent_init.py` | 否 |
| 子 agent run snapshot / detail API | `tui_gateway/services/subagent_snapshots.py`, `tui_gateway/methods/run.py`, `hermes_state_runs.py` | 否 |
| Doxie sidecar 父进程生命周期 | `tui_gateway/doxie_sidecar.py`, `tui_gateway/services/runtime_proxy.py` | 否 |
| runtime-scoped control RPC | approval / cron / skills / tools proxy to runtime worker | 否 |
| Doxie session sidebar filtering | `tui_gateway/methods/session.py` 隐藏 `tool` / `cron` 内部 runtime sessions | 否 |

因此，本次不是“上游已经提供客户端接入能力，必须合并”的情况。正确动作是按下面列表挑选。

## 吸收优先级

### P0：必须优先吸收，直接影响客户端稳定性

这些修复直接影响客户端是否卡 running、是否丢 partial assistant、是否错 replay reasoning、provider 请求是否带内部字段。

| 提交 | 内容 | 建议方式 | 原因 | 本地风险点 |
|---|---|---|---|---|
| `775a17284` | strip Hermes-internal scaffolding keys before chat.completions | 手工移植 | 防止内部 metadata 进入 provider 请求 | 不能删除 DB/event 中的 Doxie metadata |
| `20b3703a4` | tailor length-continuation prompt for partial stream | 手工移植 | partial stream 后续补全更稳定 | 不能破坏 run terminal event |
| `9140be7c2` | emit `finish_reason=length` on text-only partial-stream stub | 手工移植 | 客户端可识别需要 continuation | 和 Doxie structured delta 合并 |
| `ac5359a3f` | route mid-tool-call partial-stream-stub through length continuation | 手工移植 | 工具调用中断时减少挂起 | 保留 tool.start/tool.complete 边界 |
| `2d422720b` | size and propagate timeouts for Responses API | 手工移植 | 避免无响应卡住 | timeout event 要进入 run events |
| `8601c4d44` | Codex time-to-first-byte watchdog | 手工移植 | 首包超时更快显式失败 | 失败前要通知客户端 terminal event |
| `43a3f119f` | recover Codex streams with null output | 手工移植 | 空 output stream 不应导致客户端永远 running | 与 partial assistant persistence 对齐 |
| `b6ca56f65` | recover from `invalid_encrypted_content` | 手工移植 | Responses replay 容错 | 不能重放 foreign Doxie runtime metadata |
| `b1a46b304` | drop transient `rs_tmp` reasoning replay state | 手工移植 | replay 更干净 | 保留本地 turn/run metadata |
| `9c69204d8` | drop foreign-issuer reasoning on replay | 手工移植 | 防止跨 issuer reasoning 污染 | profile/runtime scope 不能串 |
| `4243b6dc4` | update silent-hang workaround hint | 可 cherry-pick 或手工 | 用户可见错误更明确 | 文案不影响 contract |
| `38b8d0da8` | emit guardrail halt message before closing stream | 手工移植 | 客户端能收到明确终止原因 | 需要进入 run event log |
| `8edeebe6d` | plugin transformed response survives streaming suppression | 手工移植 | 插件改写回复不应被流式路径吞掉 | Doxie extension hook 结果不能丢 |

建议落地顺序：

1. 先移植 provider 请求清洗：`775a17284`。
2. 再移植 partial stream / length continuation：`20b3703a4`、`9140be7c2`、`ac5359a3f`。
3. 再移植 Codex timeout/null output/replay 修复：`2d422720b`、`8601c4d44`、`43a3f119f`、`b6ca56f65`、`b1a46b304`、`9c69204d8`。
4. 最后补客户端可见 halt/error 事件：`38b8d0da8`、`8edeebe6d`。

验证：

```sh
scripts/run_tests.sh \
  tests/run_agent/test_run_agent.py \
  tests/run_agent/test_run_agent_codex_responses.py \
  tests/agent \
  tests/test_tui_gateway_server.py \
  tests/tui_gateway/test_ws_dispatch.py
```

额外手工验证：

- WebSocket `prompt.submit` 后中断，客户端收到 terminal event。
- provider 超时或空 stream 时，UI 不再无限 running。
- assistant partial 保存后，`session.resume` 能看到同一轮 metadata。

### P1：建议吸收，提升客户端 API 能力发现

这些不是 Doxie 客户端接入的前置条件，但对客户端动态展示可用能力有价值。

| 提交 | 内容 | 建议方式 | 原因 | 本地风险点 |
|---|---|---|---|---|
| `25f43d38d` | API server `GET /v1/skills` and `/v1/toolsets` | 手工移植设计，不直接照搬 | 客户端可展示技能和工具集 | 必须包含 Doxie extension tools/toolsets |
| `96223265b` | mark `skills_api` capability true | 手工移植 | capability 暴露给客户端 | 不能让能力声明早于 Doxie tools 注册 |
| `f7527b0fd` | API server session controls | 暂不直接吸收，先设计映射 | 有外部 session control 价值 | 不能绕过 Doxie run control |
| `464b51d45` | media in session chat API | 只吸收附件模型思想 | 与 Doxie document attachments 有重叠 | 不能破坏 `parse_document` 提示增强 |

建议设计：

```text
客户端能力发现优先走 Doxie gateway contract。
如果移植 /v1/skills 和 /v1/toolsets，底层必须读取当前 runtime scope 下已注册工具，而不是只读 upstream core registry。
```

建议新增/调整本地方法：

| 客户端需求 | 建议方法 | 数据来源 |
|---|---|---|
| 查询当前可用 skills | `skills.list` 或 API server `/v1/skills` | profile-scoped skills hub + Doxie extension |
| 查询当前可用 toolsets | `toolsets.list` 或 API server `/v1/toolsets` | `toolsets.py` + extension registered toolsets |
| 查询 runtime 能力 | `gateway.capabilities` | Doxie manifest + current worker |
| 提交多媒体/文档 | 继续走 `prompt.submit.attachments` | Doxie attachment enrichment + `parse_document` |

验证：

```sh
scripts/run_tests.sh \
  tests/gateway/test_api_server_toolset.py \
  tests/test_doxie_gateway_contract.py \
  tests/test_model_tools.py \
  tests/tools/test_doxie_agent_profile_tool.py
```

### P1：建议吸收，提升 Dashboard / WebSocket 安全

这组上游提交对“公网 dashboard / 反代 / OAuth 登录 / WS 认证”有价值。如果当前客户端只走本地 sidecar token，不是立即必需；如果 Doxie 客户端要走 Portal 或公网 dashboard，应进入 P1。

| 提交 | 内容 | 建议方式 | 原因 | 本地风险点 |
|---|---|---|---|---|
| `8773bbf18` | `should_require_auth` predicate | 手工移植 | auth gate 判断可复用 | 不能误伤本地 sidecar |
| `949ad95e4` | stash `auth_required` on app.state | 手工移植 | 前端可知道是否需要登录 | 与 Doxie auth state 合并 |
| `5b17eab67` | auth gate middleware + `/auth/*` + `/login` | 暂缓全量，拆设计 | 功能完整但侵入大 | 可能覆盖 Doxie token auth |
| `53736b392` | gated mode fail-closed / proxy headers | 手工吸收安全策略 | 反代安全 | 本地开发 loopback 不应误拦 |
| `b69fce9c8` | single-use WS tickets + `/api/auth/ws-ticket` | 手工设计性吸收 | WS 鉴权更安全 | Doxie runtime worker bridge 要兼容 |
| `b2360ba44` | ticket auth on all WS endpoints | 手工设计性吸收 | 所有 WS 入口一致 | 不要阻断 sidecar internal WS |
| `8971e9483` | SPA `getWsTicket()` + `buildWsAuthParam()` | 借鉴前端实现 | 客户端 WS 连接安全 | Doxie native client 可能不用 SPA |
| `5e9308b5b` | 401 re-auth envelope + `next=` propagation | 可选 | 前端重登录体验 | native client 需不同错误模型 |
| `2fc4615fc` | AuthWidget + `/api/status` auth fields | 可选 | Web dashboard 展示登录状态 | Doxie 客户端可能自己展示状态 |
| `c3104195b` | bypass loopback WS peer check in gated mode | 手工吸收 | 本地开发可用 | 避免扩大公网例外 |
| `b26d81d53` | `X-Forwarded-Prefix` + secure cookie handling | 可选吸收 | 反代部署需要 | cookie/path 与 Doxie domain 设计相关 |
| `2e66eefbc` | validate WebSocket Host and Origin | 应吸收 | WS 安全基线 | 需要允许 Doxie local origins |
| `973255986` | restrict dashboard websockets to loopback clients | 应吸收但加 Doxie allowlist | 防远程滥用 WS | Doxie desktop/client origin 白名单 |

建议设计：

```text
不要直接把 upstream OAuth gate 套到 Doxie sidecar 上。
先定义三类调用方：
1. local dashboard browser
2. Doxie native/desktop client
3. profile-scoped runtime worker bridge

每类调用方分别确认 token、cookie、WS ticket、origin/host 的验证顺序。
```

验证：

```sh
scripts/run_tests.sh \
  tests/test_tui_gateway_ws.py \
  tests/tui_gateway/test_ws_dispatch.py \
  tests/test_doxie_gateway_contract.py
```

手工验证：

- 本地 Doxie desktop client 能连接 gateway WS。
- 浏览器 dashboard 在 auth gated mode 下能拿到 WS ticket。
- runtime worker bridge 事件不被 dashboard auth middleware 拦截。
- 非 allowlisted Origin/Host 被拒绝。

### P1：建议吸收，安全和本地文件边界

这些安全修复不一定是客户端功能，但生产接入应该吸收。

| 提交 | 内容 | 建议方式 | 原因 | 本地风险点 |
|---|---|---|---|---|
| `5744b1757` | markdown link scheme restriction + `defusedxml` | 手工移植 | 文档/网页/解析安全 | Doxie document parse 要继承 |
| `c26af4681` | reject symlinks in skill bundles before install | 手工移植 | skill 安装安全 | 与本地 ZIP install 合并 |
| `b82608a6f` | path traversal guard in uninstall / lock pending | 手工移植 | skills/pairing 安全 | 不能破坏 invalid path repair |
| `3b9b9a7ad` | guard uninstall lock paths | 手工移植 | skills uninstall 安全 | profile home 要正确 |
| `30928f945` | dashboard plugin assets allowlist + env denylist | 手工移植 | dashboard plugin 安全 | Doxie plugin/static assets 要检查 |
| `8bf99227f` | plugin API path traversal + project RCE | 手工移植 | 高风险安全修复 | 不能绕过 project plugin gate |
| `09f85f2cf` | truthy env semantics for project-plugin gate | 手工移植 | gate 行为更明确 | Doxie env bridge 是否设置 |
| `3bace071b` | sensitive store file permissions | 手工移植 | 本地凭据安全 | profile-scoped state 权限 |
| `41d2c758c` | unsafe gateway media path delivery | 手工移植 | media/file 交付安全 | Doxie artifact path 同步校验 |

验证：

```sh
scripts/run_tests.sh \
  tests/tools/test_skill_package_lifecycle.py \
  tests/tools/test_skills_hub.py \
  tests/tools/test_skills_sync.py \
  tests/tools/test_skills_tool.py \
  tests/gateway
```

### P2：可选吸收，TUI 用户体验

这些对 Hermes Ink TUI 有价值，但不是 Doxie 客户端接入的基础能力。如果 Doxie 客户端主要走自己的 UI + TUI Gateway，则优先级低于 P0/P1。

| 提交 | 内容 | 建议方式 | 原因 | 风险 |
|---|---|---|---|---|
| `0a83247e9` | TUI session orchestrator | 暂缓，先评估 | 多 session 体验好 | 涉及 `tui_gateway/server.py`，侵入大 |
| `50aaf0c4a` | delineate assistant responses from details | 手工吸收思想 | transcript 展示更清晰 | Doxie structured stream 已有 tool events |
| `874c2b1fe` | ignore late thinking deltas after completion | 手工移植 | 防止完成后 UI 被晚到事件污染 | 要和 Doxie event seq 对齐 |
| `026f64f8e` | commit composer input bursts immediately | 可选 | TUI 输入体验 | 对 native client 无关 |
| `511b8e232` | sticky resize remount fix | 可选 | TUI resize 稳定 | UI-only |
| `0277194e3` | preserve transcript tail across resizes | 可选 | TUI resize 稳定 | UI-only |
| `4fea02cc1` | refresh virtual transcript on viewport resize | 可选 | TUI resize 稳定 | UI-only |
| `a627981a6` | slash dropdown char fix | 可选 | TUI slash UX | UI-only |

建议：

```text
不要在本轮选择性同步里合入完整 ui-tui orchestrator。
只挑 late thinking delta / response-details 分隔这类会影响事件解释的修复。
```

验证：

```sh
cd ui-tui && npm run type-check && npm test
```

### P2：可选吸收，Cron / Automation 安全补强

本地 Doxie automation 已经替代原生 `cronjob` 工具，这组上游更新只吸收安全/日志部分，不改变 Doxie automation contract。

| 提交 | 内容 | 建议方式 | 原因 | 本地风险点 |
|---|---|---|---|---|
| `d952b377a` | cron API provenance logging | 手工移植 | 审计更完整 | Doxie result binding metadata 要保留 |
| `2c3ca475c` | reject id mutation + validate output paths | 手工移植 | 防误改和路径风险 | Doxie output/artifact path 要纳入 |
| `9863a07af` | layer `agent.disabled_toolsets` onto cron baseline | 手工移植 | cron 继承禁用工具集 | Doxie automation 默认继承当前 runtime |
| `ccd899318` | split scanner into two tiers | 可选 | 降低 cron scanner 误判 | 不影响客户端主链路 |

验证：

```sh
scripts/run_tests.sh \
  tests/cron/test_jobs.py \
  tests/cron/test_scheduler.py \
  tests/tui_gateway/test_doxie_cron_jobs.py
```

### P2：可选吸收，Browser / Computer-use 辅助修复

当前客户端接入里最关键的是 Doxie desktop visible browser bridge。上游这次没有等价桥接能力，但有少量通用 browser/vision 修复。

| 提交 | 内容 | 建议方式 | 原因 | 本地风险点 |
|---|---|---|---|---|
| `22f3f5a75` | browser daemon cleanup process-tree termination | 手工移植 | 防 browser daemon 泄漏 | 不要影响 Doxie visible session |
| `3d66787a0` | auxiliary vision provider=openai routing | 手工移植 | vision routing 更正确 | Doxie model descriptor vision 覆盖保留 |
| `83f6a83b2` | TUI images with codex app-server | 可选 | 图片展示体验 | 与 Doxie attachments 关系弱 |

验证：

```sh
scripts/run_tests.sh \
  tests/tools/test_doxie_desktop_browser_bridge.py \
  tests/tools/test_browser_cdp_override.py \
  tests/tools/test_computer_use.py \
  tests/tools/test_computer_use_vision_routing.py
```

### P3：暂缓，不建议本轮吸收

这些改动价值不低，但和“当前客户端接入稳定”关系弱，或者侵入太大，建议等 P0/P1 稳定后另开分支。

| 范围 | 原因 |
|---|---|
| Docker `s6-overlay` 全套迁移 | 部署层变化大，除非当前客户端部署已经跑容器 profile gateway |
| website / i18n 大量生成产物 | 不影响客户端接入 runtime |
| 完整 dashboard typography / contrast pass | UI polish，不应和接入稳定性混合 |
| 完整 TUI session orchestrator | 涉及大量 ui-tui 和 `tui_gateway/server.py`，容易冲击本地模块化 |
| model provider catalog 大量变更 | 除非当前接入需要 OpenAI live models 或移除 Vercel |
| ntfy / platform plugin 新增 | 与 Doxie 客户端接入无直接关系 |
| optional skills / optional MCP 大量新增 | 能力扩展，不是接入稳定性修复 |

## 推荐执行批次

### 批次 1：客户端 streaming 稳定性

目标文件：

```text
agent/conversation_loop.py
agent/codex_responses_adapter.py
agent/transports/codex.py
agent/transports/chat_completions.py
run_agent.py
tui_gateway/methods/prompt.py
tui_gateway/services/run_control.py
hermes_state.py
hermes_state_runs.py
```

吸收提交：

```text
775a17284
20b3703a4
9140be7c2
ac5359a3f
2d422720b
8601c4d44
43a3f119f
b6ca56f65
b1a46b304
9c69204d8
38b8d0da8
8edeebe6d
```

执行方式：

```text
逐提交 `git show`，手工移植关键逻辑。
不建议直接 cherry-pick，因为本地 conversation/run/event metadata 已经分叉。
```

验收：

```text
客户端不会因为 provider 空流/超时/guardrail/partial stream 卡 running。
provider 请求中不带 Doxie/Hermes 内部 metadata。
DB/event stream 保留 turn_id、run_id、runtime_scope_key、client_message_id。
```

### 批次 2：客户端能力发现 API

目标文件：

```text
gateway/platforms/api_server.py
tui_gateway/doxie_gateway_contract.py
tui_gateway/core/method_registration.py
model_tools.py
toolsets.py
doxie_extension/manifest.py
```

吸收提交：

```text
25f43d38d
96223265b
```

参考但不直接合并：

```text
f7527b0fd
464b51d45
```

验收：

```text
客户端能查询当前 runtime 下 skills/toolsets。
Doxie extension tools 和 Doxie toolsets 被包含。
API session control 不绕过 Doxie run control。
```

### 批次 3：WebSocket / Dashboard 认证安全

目标文件：

```text
hermes_cli/web_server.py
web/src/lib/api.ts
web/src/pages/ChatPage.tsx
tui_gateway/ws.py
tui_gateway/services/runtime_proxy.py
tui_gateway/doxie_sidecar.py
```

优先吸收：

```text
2e66eefbc
973255986
c3104195b
8773bbf18
949ad95e4
53736b392
```

设计性参考：

```text
b69fce9c8
b2360ba44
8971e9483
5e9308b5b
2fc4615fc
5b17eab67
b26d81d53
```

验收：

```text
Doxie local client、dashboard browser、runtime worker bridge 三类连接都能通过正确认证。
非 allowlisted Host/Origin 被拒绝。
开启 auth gate 时 WS 不回退到裸 token。
关闭 auth gate 或本地 sidecar 模式时不误拦客户端。
```

### 批次 4：Skills / 文件安全

目标文件：

```text
tools/skills_hub.py
tools/skills_sync.py
tools/skills_tool.py
tools/skill_manager_tool.py
tools/skill_package_lifecycle.py
hermes_constants.py
doxie_extension/document_parse_tool.py
tui_gateway/services/artifact_registry/*
```

吸收提交：

```text
5744b1757
c26af4681
b82608a6f
3b9b9a7ad
30928f945
8bf99227f
09f85f2cf
3bace071b
41d2c758c
```

验收：

```text
skills path 自愈仍然生效。
skill ZIP / bundle symlink 被拒绝。
Doxie artifact/document/media path 不允许 traversal。
profile home 下状态/凭据文件权限正确。
```

### 批次 5：Doxie automation / browser 辅助修复

目标文件：

```text
cron/scheduler.py
tools/doxie_automation_task_tool.py
tools/cronjob_tools.py
tools/browser_tool.py
doxie_extension/browser_bridge.py
tools/computer_use/*
agent/auxiliary_client.py
```

吸收提交：

```text
d952b377a
2c3ca475c
9863a07af
22f3f5a75
3d66787a0
```

验收：

```text
Doxie automation 仍默认绑定当前会话。
原生 cronjob 不重新暴露给模型。
Doxie desktop browser 可用时 navigate/snapshot/action 仍优先走 visible session。
vision routing 仍遵守 Doxie model descriptor。
```

## 不建议直接 cherry-pick 的范围

这些范围建议手工移植，不建议直接 cherry-pick：

| 范围 | 原因 |
|---|---|
| `run_agent.py` / `agent/conversation_loop.py` | 本地 run/turn metadata 和 event contract 已分叉 |
| `tui_gateway/server.py` | 本地已模块化，直接 cherry-pick 会把逻辑重新塞回 server |
| `hermes_cli/web_server.py` | upstream dashboard auth 和 Doxie sidecar auth 需要重新设计边界 |
| `gateway/platforms/api_server.py` | upstream API session control 可能绕过 Doxie run control |
| `toolsets.py` | 本地 Doxie toolset / cronjob 替换语义必须保留 |
| `tools/skills_hub.py` | 本地 skills path repair 和 package lifecycle 要合并语义 |
| `web/src/pages/SessionsPage.tsx` | 本地 automation badge 和 upstream dashboard UI 大改易冲突 |

## 可以直接 cherry-pick 的候选

只有满足“文件冲突小、语义不碰 Doxie contract”的提交才考虑直接 cherry-pick：

```text
4243b6dc4  silent-hang workaround hint
c2aa23532  outer-loop exception traceback logging
c3104195b  loopback WS peer check bypass in gated mode
af3d4a687  ChatPage cleanup closes WS via wsRef.current
2517917de  fallback paste collapse restore
3b9b9a7ad  skills uninstall lock path guard
```

即使 cherry-pick，也要先 `git show --stat <sha>` 确认没有带入大范围无关文件。

## 最小测试矩阵

每个批次完成后至少跑对应测试；全部批次完成后跑下面组合：

```sh
git diff --check
python -m compileall agent hermes_cli gateway tools tui_gateway doxie_extension

scripts/run_tests.sh \
  tests/test_doxie_gateway_contract.py \
  tests/test_tui_gateway_server.py \
  tests/test_tui_gateway_ws.py \
  tests/tui_gateway/test_ws_dispatch.py \
  tests/tui_gateway/test_runtime_pool.py \
  tests/tui_gateway/test_tool_events.py \
  tests/tui_gateway/test_workspace_context.py \
  tests/tui_gateway/test_artifacts.py \
  tests/tui_gateway/test_doxie_cron_jobs.py \
  tests/tools/test_doxie_desktop_browser_bridge.py \
  tests/tools/test_doxie_agent_profile_tool.py \
  tests/tools/test_doxie_web_tools.py \
  tests/tools/test_skill_package_lifecycle.py \
  tests/cron/test_scheduler.py \
  tests/run_agent/test_run_agent_codex_responses.py
```

## 手工验收

1. 启动 Doxie 客户端，调用 `gateway.capabilities`，确认 Doxie required methods 全部存在。
2. 调用 `runtime.ensure`，确认 worker ready，`runtime.status.runtime_proxy` 有 worker/bridge 状态。
3. WebSocket 提交带 `runtime_scope_key` 的 prompt，确认 response/event 都从 worker bridge 返回。
4. 触发 provider timeout / interrupt / guardrail，确认客户端不会无限 running。
5. 上传文档附件，确认 prompt 被 Doxie attachment context 增强，模型能调用 `parse_document`。
6. 调用 `session.list`、`workspace.list`、`artifacts.list`，确认只读查询不会创建空 DB。
7. Doxie desktop browser 可用时，`browser_navigate` / `browser_snapshot` / `browser_click` 结果带 `provider=doxie_desktop`。
8. 创建 automation，确认默认 result binding 到当前会话，模型看不到原生 `cronjob`。
