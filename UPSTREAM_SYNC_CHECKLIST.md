# Hermes 上游同步检查清单

这份文档用于每次把 Hermes upstream/main 同步到本地分支后，快速判断 Dovie / 桌面端 / TUI Gateway 的本地实现有没有被上游结构调整打散。

重点不是罗列所有功能，而是把最容易在同步后回归的边界固定下来：先看症状，再查对应模块，最后跑最小验证包。

## 文档管理

同步文档按职责拆分，避免把历史审计、架构设计和发布快照继续堆到一个文件里：

- `UPSTREAM_SYNC_CHECKLIST.md`：只放同步后的快速分诊、红线和最小验证包。
- `docs/upstream-sync/README.zh-CN.md`：同步文档入口和更新规则。
- `docs/upstream-sync/dovie-runtime-snapshot-YYYYMMDD.md`：每次大批量本地 staged 代码的 Dovie runtime 快照。
- `docs/selective-upstream-client-sync-20260527.md`：P0/P1 选择性吸收历史审计。
- `docs/team-mission-runtime-architecture.zh-CN.md`：Team Mission 原生运行时长期架构。

## 本次同步暴露的问题

当前分支的暂存改动显示，这次同步后的问题集中在几个边界：

- TUI Gateway 从 `server.py` 中抽离了大量实现，上游改动容易重新把 Dovie 的 RPC 覆盖、profile context、run event、session store 行为打坏。
- Dovie 相关能力需要通过 `dovie_extension` 注册，而不是散落在 Hermes core 里；同步时最容易漏掉 extension hook。
- `run_id` / `turn_id` / `runtime_scope_key` / `stored_session_id` / `runtime_session_id` 的边界被多处代码共同维护，任何一处回退都会导致 UI running 卡住、历史串 profile、事件 replay 不完整。
- prompt submit、interrupt、recall turn、partial assistant 持久化和 delta streaming 是一个整体；只修其中一段会制造重复消息、丢 metadata 或取消不落库。
- tool 调用链现在依赖 `parent_agent`、session cwd、model descriptor、Dovie runtime credentials；上游同步后如果退回旧调用方式，会让 computer use、delegate、profile test、vision routing 出问题。
- document parse、prompt attachments、display transcript sanitization、desktop browser bridge 已经属于 Dovie extension 边界；同步时要确认它们仍通过 extension 注册和使用。
- profile-scoped runtime 现在可由 control plane 通过 `runtime.ensure` 启动 sidecar worker，并把 scoped 请求经 WebSocket bridge 代理到 worker；同步时要特别防止请求被错误留在 control plane 或桥接连接提前关闭。
- 只读 Gateway 查询不应为了空 profile 创建 SQLite state；否则 Dovie UI 仅查看 session/workspace/artifact 时会制造空 DB 和假状态。
- Dovie automation 已经替代原生 `cronjob` 模型工具；默认结果绑定当前会话，能力继承当前 Dovie runtime，只有显式 advanced override 才持久化 skills/toolsets。
- TUI/WebSocket 结构化流使用 tool events 表达工具边界，不能再把 legacy 文本流的工具后空行注入 message delta 或 transcript。
- Dashboard session 列表会显示 automation badge；session 与 job 的关联来自 Dovie owner/result binding metadata。
- Skills 目录初始化必须能自愈：如果 `$HERMES_HOME/skills` 或 `.hub` 相关路径被历史损坏成文件，应重命名为 `.invalid-*` 再创建目录，不能直接失败或删除用户数据。
- Dovie desktop browser bridge 不只负责 tab 操作；navigate、snapshot、click、type、scroll、press、back/forward 都必须优先走 Dovie 可见桌面 browser session。
- profile-scoped approval 和 cron control RPC 必须代理到 runtime worker；否则 approval policy、pending approval 和 cron 管理会读写 control plane，而不是目标 Dovie profile。
- `session_index` 是 Dovie 侧栏的索引化读模型；同步后要确认 `session.index.list` 仍在 control plane 本地读主库，不能被 runtime proxy 误代理到 worker。
- Team Mission conversation 删除必须同时清理 `session_index`，否则会出现正式数据已删但侧栏仍显示且点开报错的幽灵会话。
- 图片附件必须先走 Dovie `attachments` 归一化，再提取 image paths 做 vision enrichment；不要回退到只读平铺 `image_paths` 的旧入口。

## 10 分钟快速分诊

每次处理完 merge conflict 或大规模自动合并后，先跑这一段，不要直接进入全量测试。

```sh
git status --short --branch
git diff --cached --stat
git diff --cached --name-only
git diff --cached --check
```

快速判断：

- 如果 `tui_gateway/server.py`、`tui_gateway/core/method_registration.py`、`tui_gateway/methods/*.py`、`tui_gateway/services/*.py` 有改动，优先检查 Gateway ABI 和 run control。
- 如果 `tui_gateway/services/runtime_proxy.py`、`tui_gateway/ws.py`、`tui_gateway/methods/system.py` 有改动，优先检查 `runtime.ensure`、runtime proxy、WS bridge 生命周期和 control-plane 方法分流。
- 如果 `session.index.list`、`tui_gateway/services/runtime_proxy.py` 或 `tui_gateway/methods/session.py` 有改动，优先检查侧栏索引查询仍在 control plane、不会触发 scoped worker cold start。
- 如果 `run_agent.py`、`agent/conversation_loop.py`、`agent/tool_executor.py`、`model_tools.py` 有改动，优先检查 metadata、parent agent、tool invocation 和 extension tool registration。
- 如果 `hermes_state.py`、`hermes_state_runs.py` 有改动，优先检查 run registry、run events、message metadata、transient session migration。
- 如果 `hermes_state.py` 的 SQLite 初始化顺序有改动，优先检查 `auto_vacuum=INCREMENTAL` 仍在建表前设置，启动时 bounded `incremental_vacuum` 没有被移除。
- 如果 `tui_gateway/services/persistence/gateway_store.py`、`tui_gateway/services/session_store.py`、`tui_gateway/services/workspaces/service.py`、`tui_gateway/services/artifact_registry/service.py` 有改动，优先检查只读查询不会创建空 DB。
- 如果 `cron/scheduler.py`、`tools/cronjob_tools.py`、`tools/dovie_automation_task_tool.py`、`tui_gateway/services/dovie_cron_jobs.py`、`toolsets.py` 有改动，优先检查 Dovie automation contract 是否仍替代原生 cronjob tool。
- 如果 `run_agent.py`、`tui_gateway/methods/prompt.py`、`dovie_extension/display_transcript.py` 有改动，优先检查结构化 stream delta 和 transcript 展示不会引入工具边界空白。
- 如果 `hermes_cli/web_server.py` 或 `web/src/pages/SessionsPage.tsx` 有改动，优先检查 session automation badge 是否仍基于 job metadata，而不是猜测 transcript 内容。
- 如果 `hermes_constants.py`、`tools/skills_hub.py`、`tools/skills_sync.py`、`tools/skills_tool.py`、`tools/skill_manager_tool.py` 有改动，优先检查 skills path 自愈和 profile-aware skills 目录。
- 如果 `tools/browser_tool.py` 或 `dovie_extension/browser_bridge.py` 有改动，优先检查 Dovie desktop bridge 是否覆盖可见会话的 navigation、snapshot 和 action 操作。
- 如果 `tools/computer_use/*`、`tools/delegate_tool.py`、`tools/vision_tools.py` 有改动，优先检查 vision routing、child progress suppression 和 computer-use 元数据。
- 如果新增或移动 Dovie 文件，确认它们在 `dovie_extension` 或 Dovie service 边界内，不要把产品专属逻辑重新混进 Hermes upstream core。

## 同步红线

这些行为不能因为吸收上游代码而回退。

### 1. Dovie Extension 是本地能力边界

必须保留：

- `dovie_extension.load_extension()`。
- `DovieHermesExtension.register_gateway_methods()`。
- `DovieHermesExtension.gateway_method_overrides()`。
- `DovieHermesExtension.register_tools()`。
- `model_tools.py` 中 extension tool registration。
- `tui_gateway/core/method_registration.py` 中 extension gateway method registration。
- `tui_gateway/server.py` 中 `_EXTRACTED_METHOD_OVERRIDES` 对 extracted method 的优先注册。
- `runtime.ensure` 在 Dovie manifest、gateway method overrides 和 `tui_gateway.methods.system` 中同时存在。

检查方式：

```sh
rg "load_extension|gateway_method_overrides|register_tools|register_gateway_methods|runtime.ensure" dovie_extension model_tools.py tui_gateway
```

常见坏症状：

- `gateway.capabilities` 显示缺 Dovie required methods。
- `parse_document` 不再可用。
- `runtime.ensure` 不在 `gateway.capabilities` 中。
- `prompt.submit`、`run.*`、`session.*` 回到 upstream 默认行为。
- Dovie UI 调用某些 RPC 返回 method not found。

### 2. Gateway Server 只做协调，具体行为在 methods/services

`tui_gateway/server.py` 不应重新膨胀成一个大文件。同步时如果上游往 server 塞逻辑，要优先迁移到已有 service 或 method 模块。

重点保留：

- crash logging: `tui_gateway/services/crash_logging.py`
- profile context: `tui_gateway/services/profile_context.py`
- session DB per home: `tui_gateway/services/session_store.py`
- session info: `tui_gateway/services/session_info.py`
- model descriptor: `tui_gateway/services/model_descriptor.py`
- runtime credentials rebind: `tui_gateway/services/runtime_credentials.py`
- tool event bridge: `tui_gateway/services/tool_events.py`
- turn toolset scope: `tui_gateway/services/toolset_scope.py`
- notification poller: `tui_gateway/services/notification_poller.py`
- runtime proxy: `tui_gateway/services/runtime_proxy.py`

检查方式：

```sh
rg "_get_db|_active_hermes_home|ensure_agent_runtime_current|ensure_session_turn_toolsets|GatewayToolEventBridge|runtime_proxy" tui_gateway
```

常见坏症状：

- profile A 的历史、配置或 DB 被 profile B 读到。
- Dovie runtime token 更新后，旧 agent 继续用过期凭据。
- tool events 不带 run / turn / runtime scope。
- `session.info`、`runtime.status`、`model.set` 行为和 Dovie UI 不一致。
- `runtime.status` 不包含 runtime proxy pool snapshot。

### 3. Run Control 是 UI 状态的事实来源

所有长任务必须通过 run registry 和 run event log 表达状态。不要让 `prompt.submit`、`run.submit`、`run.cancel`、`session.interrupt` 各自维护一套状态。

必须保留：

- `run.reserve`
- `run.submit`
- `run.cancel`
- `run.status`
- `run.events`
- `events.subscribe`
- `events.unsubscribe`
- `run_control.publish_run_terminal_event(...)`
- event payload 中的 `stored_session_id`、`runtime_session_id`、`run_id`、`turn_id`、`runtime_scope_key`、`seq`

检查方式：

```sh
rg "runtime_scope_key|publish_run_terminal_event|run.events|events.subscribe|create_run_if_session_idle|append_run_event" tui_gateway hermes_state.py hermes_state_runs.py
```

常见坏症状：

- UI 一直显示 running，但后端已经结束。
- 取消请求返回成功，但 transcript 没有 cancelled terminal event。
- 重连后丢失 tool progress / run terminal event。
- 同一个 stored session 在不同 profile/draft runtime 下事件互串。

### 4. Prompt Turn 必须完整保留 metadata

Gateway 发起的一轮对话至少要保留：

- `turn_id`
- `run_id`
- `client_message_id`
- `attachments`
- `runtime_scope_key`
- clean prompt / persisted prompt 的区分
- assistant / tool message 继承当前 user turn 的 `turn_id` / `run_id` / `client_message_id`
- `message.complete` 返回可定位的 persisted assistant `message_id`

重点路径：

- `tui_gateway/methods/prompt.py`
- `agent/conversation_loop.py`
- `run_agent.py`
- `hermes_state.py`

检查方式：

```sh
rg "turn_metadata|persist_user_message|metadata_json|client_message_id|attachments|message_id" tui_gateway agent run_agent.py hermes_state.py dovie_extension
```

常见坏症状：

- transcript 里同一轮用户消息重复。
- interrupt 后 partial assistant 没有保存，或保存成完整回答。
- recall turn 删除了错误范围。
- provider 请求里混入内部 `metadata` 字段。
- 附件存在但 agent 不知道该调用 `parse_document`。
- assistant/tool 消息没有 turn metadata，导致客户端无法按 turn 更新、recall 或定位最终 assistant message。

### 5. Dovie 文档和 transcript 处理在 extension 层

文档附件、文档解析、cron 提示清洗不应该重新分散进 Hermes core。

必须保留：

- `dovie_extension/prompt_attachments.py`
- `dovie_extension/document_parse_tool.py`
- `dovie_extension/display_transcript.py`
- `tui_gateway/methods/prompt.py` 调用 `enrich_prompt_with_document_attachments(...)`
- `tui_gateway/methods/session.py` 调用 `sanitize_transcript_messages(...)` 和 `sanitize_session_list_item(...)`

检查方式：

```sh
rg "enrich_prompt_with_document_attachments|sanitize_transcript_messages|parse_document|Dovie attached documents" dovie_extension tui_gateway model_tools.py toolsets.py
```

常见坏症状：

- 用户上传 PDF/Office 文件后，模型直接回答而不是先解析。
- cron job 的内部系统提示出现在 Dovie UI transcript。
- `parse_document` 从 toolset 消失。
- Dovie MinerU proxy token / URL 没有生效。

### 6. Tool 调用链必须保留 parent_agent 和 session cwd

上游同步很容易把工具调用退回到直接 `handle_function_call(...)`，导致工具拿不到当前 agent 的 runtime 信息。

必须保留：

- `agent._invoke_tool(...)` 调用路径。
- `model_tools.handle_function_call(... parent_agent=...)` 的能力。
- destructive command checkpoint cwd 顺序：tool workdir -> `agent.session_cwd` -> `DOVIE_WORKSPACE_ROOT` -> `TERMINAL_CWD`。
- `AIAgent(..., cwd=...)` 和 `agent.session_cwd`。
- `model_descriptor.vision_enabled` 对 vision routing 的覆盖。

检查方式：

```sh
rg "_invoke_tool|parent_agent|session_cwd|DOVIE_WORKSPACE_ROOT|vision_enabled" agent run_agent.py model_tools.py tools tui_gateway
```

常见坏症状：

- `test_agent_profile` 找不到 parent context。
- computer use 截图错误地走主模型 vision，或非 vision 模型收到图片。
- destructive checkpoint 落到进程 cwd，而不是用户 workspace。
- delegate 子代理进度泄漏到主会话。

### 7. Desktop Browser Bridge 优先于本地 CDP fallback

Dovie desktop browser 可用时，浏览器可见会话操作必须通过 Dovie bridge；只有 bridge 不可用时才 fallback 到 agent-browser / native CDP。

重点路径：

- `dovie_extension/browser_bridge.py`
- `tools/browser_tool.py`

必须保留：

- `browser_session_id()` 先读 `DOVIE_BROWSER_SESSION_ID`，再读 session context，最后才用 `browser:electron:default`。
- `browser_navigate` 通过 `browser_use_navigate` 导航，再通过 `browser_use_observe_embedded` 返回 snapshot。
- `browser_snapshot` 通过 `browser_use_observe_embedded` 返回 Dovie desktop snapshot payload。
- `browser_click`、`browser_type`、`browser_scroll`、`browser_press` 通过 `browser_use_action_embedded` 执行。
- `browser_back` / `browser_forward` 通过 Dovie bridge 的 history command 执行。
- Dovie bridge 返回 payload 必须带 `provider="dovie_desktop"` 和 `browser_session_id`，方便客户端和测试分辨路径。
- ref 入参要兼容 `@e1` 和 `e1`，发给 Dovie backend 前去掉 `@`。
- observation 要格式化成可读 snapshot，保留 `@ref`、tag、role、state、name/text。
- bridge 不可用或失败时才进入 agent-browser / Camofox / CDP fallback。

检查方式：

```sh
rg "browser_bridge|browser_use_navigate|browser_use_observe_embedded|browser_use_action_embedded|browser_use_list_sessions|browser_use_create_tab|browser_use_activate_tab|browser_use_close_tab" dovie_extension tools/browser_tool.py tests
```

常见坏症状：

- Dovie 桌面浏览器打开了 tab，但 Hermes `browser_tabs` 看不到。
- Dovie 桌面浏览器已经可用，但 `browser_navigate` 仍启动 agent-browser。
- `browser_snapshot` 读取的是 headless/agent-browser，而不是用户正在看的桌面页面。
- click/type/scroll/press 跑到不可见 browser session。
- `browser_new_tab` 在桌面端错误返回 “native CDP required”。
- tab id / target id 混乱，后续 snapshot 操作跑到旧标签。
- tool result 缺 `provider=dovie_desktop`，客户端无法确认是否走可见桌面浏览器。

### 8. Computer Use 必须保留窗口选择和 vision routing 修复

重点保留：

- structured `list_windows` 解析。
- off-screen / non-current-space warning。
- pid、window_id、window_bounds、capture_bounds、scale_factor、warnings。
- follow-up capture 时继续传 `parent_agent`。
- aux vision cache dir 创建。
- model descriptor 对 `supports_vision_override` 的影响。

检查方式：

```sh
rg "window_bounds|capture_bounds|scale_factor|supports_vision_override|parent_agent" tools/computer_use tools/vision_tools.py agent/image_routing.py
```

常见坏症状：

- 指定 app 时抓到前台错误窗口。
- capture 返回图片但坐标、窗口 id 或 warning 丢失。
- 无 vision 主模型收到 multimodal payload。
- macOS Space / off-screen 窗口没有诊断信息。

### 9. Hermes State 必须保留 run 和 metadata schema

重点保留：

- `hermes_state_runs.py`
- `runs` 表。
- `run_events` 表。
- `runtime_scope_key` 迁移和索引。
- messages 的 `metadata_json`。
- transient session 过滤。
- session list allowed sources。

检查方式：

```sh
rg "metadata_json|runtime_scope_key|run_events|create_run_if_session_idle|append_run_event|transient" hermes_state.py hermes_state_runs.py tests
```

常见坏症状：

- 老库迁移失败。
- run events replay 顺序错乱。
- Dovie 临时 runtime 出现在普通 session list。
- message metadata 丢失导致 recall / interrupt / client message mapping 失效。

### 10. Dovie Cron / Automation Task 不能脱离 run event、会话绑定和能力继承

重点保留：

- `tui_gateway/services/dovie_cron_jobs.py`
- `tools/dovie_automation_task_tool.py`
- `cron/scheduler.py`
- cron job 的 Dovie metadata。
- cron 运行时的 approval policy override。
- job run 与 session/run event 的关联。
- `manage_cron(...)` 在 control plane 中必须通过 active `HERMES_HOME` override 读写当前 profile 的 cron store，不需要先启动 runtime worker。
- 新建 automation 默认 `resultBinding={"mode": "current-session", "sessionId": sourceSessionId}`，并把 `sessionTarget` 归一为 `main`。
- 显式 `resultBinding={"mode": "new-session"}` 才创建独立 automation session。
- future run 结果要按 result binding 写回当前会话或新会话，并保存 trigger user message 与 assistant result metadata。
- cron job 默认继承 `HERMES_TUI_TOOLSETS` / 当前 agent runtime capabilities；只有显式 `capabilityOverride` 才持久化 `skills` / `enabledToolsets`。
- `capabilityOverride={}` 必须能清除任务级 override，恢复 agent-runtime inheritance。
- `cronjob` 原生模型工具不能再注册给模型；`cronjob` toolset 只暴露 `dovie_automation_task_*`。

检查方式：

```sh
rg "dovie_automation|result_binding|capabilityOverride|HERMES_TUI_TOOLSETS|cronjob|dovie_automation_task" tui_gateway cron tools toolsets.py model_tools.py tests
```

常见坏症状：

- Dovie 创建的定时任务不立即执行。
- cron run 找不到对应 session。
- cron job 的 full_access/default approval policy 不生效。
- automation task tool 不在正确 toolset 中。
- automation 结果只进 run log，不回到当前会话。
- 模型同时看到原生 `cronjob` 和 Dovie automation tools，创建出不受 Dovie 约束的任务。
- 从对话创建的任务错误默认成 isolated/new-session。
- 用户没有显式高级设置时，任务把当前 runtime skills/toolsets 固化进 job，后续 profile 能力更新不生效。
- `cron.manage` 在 control plane list/add 时读写 default home，而不是当前 profile home。

### 11. Runtime Proxy / Scoped Worker 是 profile 执行边界

Dovie profile-scoped 请求不能长期跑在 control plane 进程里。control plane 负责启动、探活、代理和回收 worker；真正的 scoped runtime 在 sidecar worker 中执行。

重点保留：

- `runtime.ensure` 控制面方法。
- `dovie_extension` manifest 和 method overrides 中的 `runtime.ensure`。
- `tui_gateway/services/runtime_proxy.py` 的 `RuntimeWorkerPool`、`RuntimeProxyBridge`、`should_proxy_to_runtime(...)`。
- `tui_gateway/ws.py` 在普通 dispatch 前先调用 `proxy_to_runtime(...)`。
- worker 启动环境中的 `HERMES_HOME`、`DOVIE_HERMES_RUNTIME_SCOPE_KEY`、`DOVIE_AGENT_PROFILE_ID`。
- WebSocket bridge 读取 worker 的 response/event 后原样写回客户端，并在客户端 WS 关闭时释放 bridge。
- `bridge_count > 0` 时 idle reclaim 不能杀 worker；idle timeout 到期且无活动 bridge 时才回收。
- control-plane 方法默认不代理，但 `approval.pending.list`、`approval.policy.get`、`approval.policy.set`、`approval.respond`、`cron.manage`、`skills.reload`、`tools.configure`、`run.cancel`、`session.create(control_plane_only)`、clarify/sudo/secret respond 等 scoped 控制动作要按规则处理。

检查方式：

```sh
rg "runtime.ensure|RuntimeWorkerPool|RuntimeProxyBridge|should_proxy_to_runtime|proxy_to_runtime|approval.policy|approval.pending|cron.manage|skills.reload|tools.configure|DOVIE_HERMES_RUNTIME_SCOPE_KEY|bridge_count" tui_gateway dovie_extension tests
```

常见坏症状：

- `prompt.submit` 带 `runtime_scope_key` 后仍在 control plane 执行。
- `session.list` / `session.messages` 被错误代理到 worker。
- worker emit 的 `message.delta` 到不了客户端。
- 首个 response 后 bridge 被关闭，后续 stream event 丢失。
- 用户关闭 WebSocket 后 worker bridge 计数不释放，idle worker 永远不回收。
- worker 启动时没有 scoped `HERMES_HOME`，导致 profile 数据写到 control plane home。
- profile-scoped approval policy 设置到了 control plane session，目标 worker 仍然保持默认 approval mode。
- `approval.pending.list` 在 Dovie profile 下返回空，但 worker 里实际有 pending approval。
- `cron.manage` 在 profile runtime 下读写了 control plane cron store。
- `skills.reload` 留在 control plane，导致目标 profile worker 的 skill cache 没刷新。
- `tools.configure` 留在 control plane，导致工具配置写到错误 profile 或 worker runtime 未生效。

### 12. 只读 Gateway 查询不能创建空状态库

Dovie UI 会频繁读取 session、workspace、artifact、run event 等状态。只读查询不应因为目标 profile 还没有 DB，就创建空 `state.db` 或 `tui-gateway/state.db`。

重点保留：

- `tui_gateway/server.py` 的 `_READ_ONLY_DB_METHODS` 和 `_current_method`。
- `tui_gateway/services/session_store.py` 的 `create_if_missing=False`。
- `tui_gateway/services/persistence/gateway_store.py` 的 read-only store path。
- `workspace_for_session(...)`、`list_workspaces(...)`、`list_artifacts(...)` 在 store 不存在时返回空结果。
- `session.list` 在 DB 不存在时返回空列表；`session.messages` 在 DB 不存在时返回 session not found。

检查方式：

```sh
rg "create_if_missing|_READ_ONLY_DB_METHODS|_current_method|get_gateway_state_store" tui_gateway tests
```

常见坏症状：

- 只是打开 profile 下拉或 workspace 视图，就在 profile home 下创建空 DB。
- 空 DB 被当成真实历史，Dovie UI 出现无意义 session/workspace。
- read-only 方法在缺 DB 时返回 500，而不是空结果或 not found。

### 13. 结构化流式输出不能混入 legacy 展示空白

Legacy CLI 文本流需要在工具调用后插入段落空行，但 TUI Gateway / WebSocket 已经用 `tool.start` / `tool.complete` 表达边界。结构化流里再注入空行，会污染 Dovie transcript 和最终消息内容。

重点保留：

- `agent._stream_inject_tool_breaks` 默认 `True`，但 `tui_gateway/methods/prompt.py` 调用 agent 时临时设为 `False`。
- `_MessageDeltaNormalizer` 暂存 trailing newlines，只有后续文本到来才一起输出；遇到 tool boundary / `feed(None)` 时丢弃 pending trailing newlines。
- renderer 使用 normalized delta，而不是原始 delta。
- `dovie_extension/display_transcript.py` 对 assistant text/reasoning 去掉首尾结构性空行，但保留正文内部缩进和换行。

检查方式：

```sh
rg "_stream_inject_tool_breaks|_MessageDeltaNormalizer|discard_pending_trailing_newlines|sanitize_assistant_display_text" agent run_agent.py tui_gateway dovie_extension tests
```

常见坏症状：

- Dovie UI 中工具调用后 assistant 消息以多余空白开头。
- streamed delta 中出现仅用于 legacy display 的 `\n\n`。
- 工具边界后的第一段正文 offset 错误，客户端 reducer 拼接错乱。
- transcript sanitization 把正文内部格式也剥掉。

### 14. Dashboard Session Automation Badge 依赖 job metadata

Dashboard 的 session 列表只展示 session 是否有关联 automation task，不负责推断任务语义。

重点保留：

- `hermes_cli/web_server.py` 的 `_session_automation_counts()`。
- `/api/sessions` 返回 `has_automation_tasks` 和 `automation_task_count`。
- job 关联来源包括 Dovie owner `sourceSessionId`、result binding `sessionId`、legacy `dovie.session_id`。
- `web/src/lib/api.ts` 的 session 类型字段。
- `web/src/pages/SessionsPage.tsx` 的 compact icon badge。

检查方式：

```sh
rg "automation_task_count|has_automation_tasks|_session_automation_counts|AutomationBadge" hermes_cli web tests
```

常见坏症状：

- 当前会话已有 automation task，但 session list 没有标记。
- 一个 session 同时作为 owner 和 result target 时计数漏算。
- Dashboard 通过扫描消息内容推断 automation，导致历史文案变化影响 UI。

### 15. Skills 目录创建必须自愈非目录路径

用户的 profile home 是长期数据目录，旧版本、手工编辑或异常写入可能把 `skills`、`.hub`、manifest/cache 等目录路径变成普通文件。同步上游时不能退回裸 `mkdir(parents=True, exist_ok=True)`，否则 skill hub、sync、list、install 都会在 profile 初始化阶段失败。

重点保留：

- `hermes_constants.ensure_directory_path(...)`。
- 遇到非目录路径时重命名为 `<name>.invalid-<timestamp>-<id>`，再创建目录。
- `tools/skills_hub.py`、`tools/skills_sync.py`、`tools/skills_tool.py`、`tools/skill_manager_tool.py` 创建目录时使用该 helper。
- 只移动无效 filesystem node，不删除用户数据。
- helper 必须 import-safe，不能引入 heavy deps 或 logging setup。

检查方式：

```sh
rg "ensure_directory_path|invalid-" hermes_constants.py tools/skills_hub.py tools/skills_sync.py tools/skills_tool.py tools/skill_manager_tool.py tests/tools
```

常见坏症状：

- `$HERMES_HOME/skills` 被文件占用时，`skills list` / `skills sync` 直接 `FileExistsError`。
- `.hub` 相关路径异常时，skill search/install 失败且没有自愈。
- 修复时直接删除冲突文件，造成用户数据不可恢复。

## 症状到检查点

| 症状 | 先查 |
| --- | --- |
| 改了某个 `@method` handler 完全没生效 | 同名 handler 是否在 `tui_gateway/methods/*.py` 和 `tui_gateway/server.py` 同时注册（methods/* 强制覆盖、server.py 是死代码，编辑 server.py 那份无效） |
| 侧栏出现幽灵「新会话」（空标题）删了重启又有 | `session.index.list`（控制面侧栏读）是否过滤空草稿——它要镜像 `session.list` 的 `_is_empty_stored_conversation`（仅非运行时过滤）；`session.create` 控制面路径会给每个刚打开 composer 的空会话急投影一行 session_index，靠读侧过滤隐藏（投影必须保留，它是会话进 session_index 的机制，reconcile 只是一次性 backfill）；`session_kind=hermes_session` 即与团队功能无关 |
| `DOVIE_AUTH_REQUIRED` / HTTP 401 但 runtime token 未过期 | `_persist_model_switch` 是否把 `provider` 写成通用 `custom`；`config.model.provider` 是否被污染；`agent.api_key` 是否为 `no-key-required`；`_resolve_named_custom_runtime` 的 base_url 自愈分支 |
| 默认模型变成 gpt-5.5 / 每次对话都弹切模型 marker | `_make_agent` 模型优先级（`model_override → _persisted_session_runtime → 全局`）；`session.create` 是否携带并持久化 model 到 DB row；`_apply_model_switch` same-model short-circuit |
| 压缩后 resume 历史是旧的 / 压缩前回复不见了 | `session.resume`（`methods/session.py`）是否调 `db.resolve_resume_session_id` 重锚到续接 tip |
| 前端聊天里显示原始报错 / 内部符号 / ip:port | run error 是否走 `humanizeHermesError`；`isHermesRuntimeAuthError` 是否匹配 message 形态（非仅 error_code） |
| Dovie UI method not found | `dovie_extension/gateway_methods.py`、`tui_gateway/core/method_registration.py`、`gateway.capabilities` |
| `runtime.ensure` 缺失或失败 | `dovie_extension/manifest.py`、`methods/system.py`、`services/runtime_proxy.py`、`tests/test_dovie_gateway_contract.py` |
| scoped prompt 没有进 profile worker | `runtime_proxy.should_proxy_to_runtime`、`ws.proxy_to_runtime`、`DOVIE_HERMES_RUNTIME_SCOPE_KEY` |
| scoped approval/cron 留在 control plane | `_RUNTIME_SCOPED_CONTROL_METHODS`、`should_proxy_to_runtime`、`tests/tui_gateway/test_ws_dispatch.py` |
| scoped skills/tools mutation 留在 control plane | `_RUNTIME_SCOPED_CONTROL_METHODS`、`skills.reload`、`tools.configure`、`tests/tui_gateway/test_ws_dispatch.py` |
| worker stream 只收到首包 | `RuntimeProxyBridge._read_loop`、`WSTransport.runtime_bridge`、bridge close/release 逻辑 |
| idle worker 不回收 | `RuntimeWorkerPool.reclaim_idle`、`bridge_count`、WS close cleanup |
| UI 一直 running | `tui_gateway/methods/run.py`、`tui_gateway/methods/prompt.py`、`tui_gateway/services/run_control.py`、`hermes_state_runs.py` |
| 重连后 tool progress 丢失 | `run.events`、`events.subscribe`、`append_run_event`、`runtime_scope_key` |
| 不同 profile 历史串了 | `profile_context.py`、`session_store.py`、`_get_db()`、`get_hermes_home()` |
| 只读查询创建空 DB | `_READ_ONLY_DB_METHODS`、`session_store.create_if_missing`、`gateway_store.create_if_missing` |
| 文档附件没被解析 | `prompt_attachments.py`、`document_parse_tool.py`、`model_tools.py`、`toolsets.py` |
| transcript 出现 cron 内部提示 | `display_transcript.py`、`session.messages`、`session.list` |
| assistant 完成事件不能定位消息 | `_latest_assistant_message_id_for_turn`、`run_agent.py` metadata merge、`display_transcript.py` metadata merge |
| browser 多 tab 在桌面端不可用 | `browser_bridge.py`、`tools/browser_tool.py` |
| Dovie browser navigate/snapshot 跑到 headless | `browser_bridge.navigate/observe/action`、`browser_tool._dovie_browser_bridge`、`tests/tools/test_dovie_desktop_browser_bridge.py` |
| Dovie browser action 点不到可见页面 | `browser_use_action_embedded`、ref normalization、`browser_click/type/scroll/press` |
| computer use 抓错窗口或坐标错 | `tools/computer_use/cua_backend.py`、`tools/computer_use/tool.py` |
| 非 vision 模型收到图片 | `agent/image_routing.py`、`tools/computer_use/vision_routing.py`、`model_descriptor.py` |
| delegate/profile test 泄漏子代理输出 | `tools/delegate_tool.py`、`tools/dovie_agent_profile_tool.py`、`agent/conversation_loop.py` |
| model switch 后凭据失效 | `runtime_credentials.py`、`methods/model.py`、`agent/auxiliary_client.py` |
| session list 多出临时会话 | `hermes_state.py`、`methods/session.py`、allowed sources tests |
| Dovie automation 结果没回当前会话 | `cron.scheduler._deliver_dovie_bound_result`、`result_binding`、`SessionDB.append_message` |
| 模型仍能调用原生 `cronjob` | `tools/cronjob_tools.py`、`toolsets.py`、`model_tools._LEGACY_TOOLSET_MAP`、`tests/test_model_tools.py` |
| automation 固化了 runtime 能力 | `capabilityOverride`、`capabilitySource`、`HERMES_TUI_TOOLSETS`、`dovie_cron_jobs.py` |
| 工具后消息前有多余空行 | `_stream_inject_tool_breaks`、`_MessageDeltaNormalizer`、`sanitize_assistant_display_text` |
| Dashboard session 没显示 automation 标记 | `_session_automation_counts`、`has_automation_tasks`、`AutomationBadge` |
| skills 初始化遇到 FileExistsError | `ensure_directory_path`、skills hub/sync/list/install 目录创建路径 |

## 最小验证包

按改动范围选包跑。不要每次都盲目全量，先跑最可能坏的包。

### Gateway ABI / Run Control

```sh
scripts/run_tests.sh \
  tests/test_dovie_gateway_contract.py \
  tests/test_tui_gateway_server.py \
  tests/tui_gateway/test_protocol.py \
  tests/tui_gateway/test_profile_data_context.py \
  tests/tui_gateway/test_tool_events.py \
  tests/tui_gateway/test_ws_dispatch.py
```

### State / Session / Run Events

```sh
scripts/run_tests.sh \
  tests/test_hermes_state.py \
  tests/test_lazy_session_regressions.py \
  tests/gateway/test_session_list_allowed_sources.py \
  tests/tui_gateway/test_workspace_context.py
```

### Prompt / Attachments / Dovie Extension

```sh
scripts/run_tests.sh \
  tests/test_dovie_document_parse_tool.py \
  tests/test_dovie_display_transcript.py \
  tests/tui_gateway/test_protocol.py \
  tests/test_tui_gateway_server.py
```

### Agent / Tool Invocation / Delegate

```sh
scripts/run_tests.sh \
  tests/run_agent/test_run_agent.py \
  tests/tools/test_delegate.py \
  tests/tools/test_dovie_agent_profile_tool.py \
  tests/tools/test_computer_use.py \
  tests/tools/test_computer_use_vision_routing.py
```

### Runtime Model / Credentials

```sh
scripts/run_tests.sh \
  tests/tui_gateway/test_make_agent_provider.py \
  tests/agent/test_auxiliary_named_custom_providers.py \
  tests/tools/test_parse_env_var.py
```

### Dovie Cron / Automation

```sh
scripts/run_tests.sh \
  tests/cron/test_scheduler.py \
  tests/tui_gateway/test_dovie_cron_jobs.py \
  tests/tui_gateway/test_tool_events.py \
  tests/test_model_tools.py \
  tests/gateway/test_api_server_toolset.py
```

### Structured Streaming / Transcript Display

```sh
scripts/run_tests.sh \
  tests/run_agent/test_run_agent_codex_responses.py \
  tests/tui_gateway/test_protocol.py \
  tests/test_dovie_display_transcript.py
```

### Dashboard Automation Badge

```sh
scripts/run_tests.sh \
  tests/hermes_cli/test_web_session_automation_badges.py
```

### Dovie Desktop Browser Bridge

```sh
scripts/run_tests.sh \
  tests/tools/test_dovie_desktop_browser_bridge.py
```

### Skills Directory Repair

```sh
scripts/run_tests.sh \
  tests/tools/test_skills_hub.py \
  tests/tools/test_skills_sync.py \
  tests/tools/test_skills_tool.py
```

### Runtime Proxy / WebSocket Bridge

```sh
scripts/run_tests.sh \
  tests/test_dovie_gateway_contract.py \
  tests/tui_gateway/test_ws_dispatch.py \
  tests/tui_gateway/test_protocol.py
```

## 同步后的人工检查

自动测试通过后，再做这些人工检查：

1. 启动 Dovie / TUI Gateway，调用 `gateway.capabilities`，确认 `ok=true`，`missingCapabilities=[]`。
2. 创建一个普通 session，提交一轮 prompt，确认返回 `run_id`、`turn_id`，并能通过 `run.events` replay。
3. 中断一轮正在运行的 prompt，确认 transcript 中有 partial assistant 或 cancelled terminal event，UI 不再 stuck running。
4. 上传一个 PDF 或 Office 文档，确认 prompt 中出现 Dovie document attachment context，agent 会调用 `parse_document`。
5. 切换 Dovie profile 或 draft runtime，确认 session list、history、run events 没有串到另一个 profile。
6. 调用 `runtime.ensure`，确认返回 `ready=true`，`worker.running=true`，且 `runtime.status.runtime_proxy` 能看到 worker。
7. 通过 WebSocket 提交带 `runtime_scope_key` 的 `prompt.submit`，确认 response 和后续 stream events 都从 worker bridge 回到客户端。
8. 在只读 profile 上调用 `session.list`、`workspace.list`、`artifacts.list`，确认不会创建空 DB。
9. 从一个已有对话创建 automation，确认默认绑定当前 session，任务完成后 assistant result 回写该 session。
10. 检查模型工具列表，确认没有原生 `cronjob`，只有 `dovie_automation_task_*`。
11. 在 TUI/WebSocket 跑一次“工具调用后继续回答”的流式场景，确认消息开头没有多余空行。
12. 打开 Dashboard sessions，确认有关联 automation 的 session 显示 badge。
13. 在 profile-scoped WebSocket 下调用 approval policy/pending/respond 和 `cron.manage`，确认请求代理到 worker。
14. 在桌面浏览器 bridge 可用时，跑 `browser_navigate` / `browser_snapshot` / `browser_click` / `browser_type`，确认 payload 带 `provider=dovie_desktop`，页面就是用户可见桌面页面。
15. 在桌面浏览器 bridge 可用时，跑 `browser_tabs` / `browser_new_tab` / `browser_select_tab`。
16. 临时把测试 profile 的 `skills` 路径做成普通文件，确认 skills sync/list 会把它移动为 `.invalid-*` 并重建目录。
17. 跑一次 `computer_use capture`，确认返回窗口元数据和 warnings 字段。

## 文档维护规则

每次同步结束后，只更新三类内容：

- 新增的高风险边界。
- 已经真实发生过的坏症状。
- 能直接定位问题的检查命令或测试包。

不要把文档重新写成：

- 全仓库文件清单。
- 所有功能的产品说明。
- 只有“应该正常工作”的泛泛描述。
- 没有对应测试或检查命令的愿望列表。

推荐每次同步后补一小段：

```md
### YYYY-MM-DD 同步记录

- 暴露问题：
- 根因边界：
- 修复涉及：
- 下次优先检查：
- 最小验证：
```

## 当前同步记录

### 2026-05-24 同步记录

- 暴露问题：上游同步后，暂存修复集中在 TUI Gateway 拆分、Dovie extension 注册、run event/runtime scope、prompt metadata、tool invocation、computer use/browser bridge 等边界。
- 根因边界：本地 Dovie 能力和 Hermes upstream core 的职责边界不够显式，旧文档按功能罗列，无法指导同步后快速定位。
- 修复涉及：`dovie_extension/`、`tui_gateway/server.py`、`tui_gateway/methods/`、`tui_gateway/services/`、`hermes_state.py`、`hermes_state_runs.py`、`agent/`、`run_agent.py`、`model_tools.py`、`tools/`。
- 下次优先检查：extension hook、run control、profile-scoped DB、turn metadata、parent_agent 工具调用、Dovie document/browser/computer-use 边界。
- 最小验证：先跑 Gateway ABI / Run Control 包，再根据 diff 跑 State、Prompt、Agent/Tools、Runtime Credentials、Cron 包。

### 2026-05-26 同步记录

- 暴露问题：新增 profile-scoped runtime worker proxy、`runtime.ensure`、WS bridge streaming、只读 DB 查询和 turn metadata/message id 补齐后，同步风险从单进程 Gateway 扩展到 control plane 与 worker 的请求分流。
- 根因边界：Dovie profile runtime 必须由 control plane 管理 worker 生命周期，但 scoped 执行必须进入 worker；同时 Dovie UI 的只读恢复路径不能因为 profile 尚无状态库而创建假数据。
- 修复涉及：`tui_gateway/services/runtime_proxy.py`、`tui_gateway/ws.py`、`tui_gateway/methods/system.py`、`tui_gateway/server.py`、`tui_gateway/services/session_store.py`、`tui_gateway/services/persistence/gateway_store.py`、`tui_gateway/services/workspaces/service.py`、`tui_gateway/services/artifact_registry/service.py`、`run_agent.py`、`dovie_extension/display_transcript.py`、`dovie_extension/manifest.py`、`dovie_extension/gateway_methods.py`。
- 下次优先检查：`runtime.ensure` 是否仍在 gateway contract；`should_proxy_to_runtime` 是否正确区分 control-plane 和 scoped 方法；WS bridge 是否保持 streaming event 通道；只读 session/workspace/artifact 查询是否使用 `create_if_missing=False`；assistant/tool metadata 是否继承当前 turn identity。
- 最小验证：优先跑 Runtime Proxy / WebSocket Bridge 包，再跑 Gateway ABI / Run Control、Prompt / Attachments / Dovie Extension、State / Session / Run Events 包。

### 2026-05-26 V0.6.5 同步记录

- 暴露问题：Dovie automation 从原生 Hermes cronjob 语义切到会话绑定任务语义后，风险集中在默认 result binding、runtime capability inheritance、toolset 暴露、结构化流式空白和 Dashboard session 标记。
- 根因边界：automation 是 Dovie 产品能力，不应让模型直接创建原生 `cronjob`；任务结果默认应回到发起会话，任务能力默认跟随当前 agent runtime，只有用户显式高级配置才持久化 override。
- 修复涉及：`cron/scheduler.py`、`tui_gateway/services/dovie_cron_jobs.py`、`tools/dovie_automation_task_tool.py`、`tools/cronjob_tools.py`、`toolsets.py`、`model_tools.py`、`run_agent.py`、`tui_gateway/methods/prompt.py`、`dovie_extension/display_transcript.py`、`hermes_cli/web_server.py`、`web/src/lib/api.ts`、`web/src/pages/SessionsPage.tsx`。
- 下次优先检查：原生 `cronjob` 是否仍未注册；`cronjob` toolset 是否只含 Dovie automation tools；current-session result binding 是否默认生效；`capabilityOverride` 是否只在显式请求时持久化；TUI structured stream 是否禁用工具边界空白注入；session automation badge 是否基于 job metadata。
- 最小验证：优先跑 Dovie Cron / Automation、Structured Streaming / Transcript Display、Dashboard Automation Badge 三组测试，再按 diff 补跑 Gateway ABI / Run Control 包。

### 2026-05-27 同步记录

- 暴露问题：最近提交修复了三个同步后容易遗漏的边界：skills 目录路径被文件占用时需要自愈；Dovie desktop browser 不应只代理 tab API，还要代理 navigate/snapshot/action 到可见桌面 browser session；profile-scoped approval/cron/skills/tools control RPC 必须进 runtime worker。
- 根因边界：profile home、desktop browser、approval/cron state 和 runtime skills/tools 配置都是 Dovie 本地运行态的一部分。上游同步如果退回裸目录创建、headless agent-browser 路径或 control-plane scoped mutation 处理，会让 profile 初始化失败、browser tool 操作不可见会话，或让 approval/cron/skills/tools 状态写错 runtime。
- 修复涉及：`hermes_constants.py`、`tools/skills_hub.py`、`tools/skills_sync.py`、`tools/skills_tool.py`、`tools/skill_manager_tool.py`、`dovie_extension/browser_bridge.py`、`tools/browser_tool.py`、`tui_gateway/services/runtime_proxy.py`、`tests/tools/test_skills_hub.py`、`tests/tools/test_skills_sync.py`、`tests/tools/test_skills_tool.py`、`tests/tools/test_dovie_desktop_browser_bridge.py`、`tests/tui_gateway/test_ws_dispatch.py`。
- 下次优先检查：skills path 创建是否仍用 `ensure_directory_path`；无效 skills 路径是否移动到 `.invalid-*` 而不是删除；Dovie bridge 可用时 `browser_navigate/snapshot/click/type/scroll/press` 是否仍优先走 `browser_use_*` backend command；Dovie browser tool result 是否带 `provider=dovie_desktop` 和 `browser_session_id`；`_RUNTIME_SCOPED_CONTROL_METHODS` 是否仍包含 approval policy/pending/respond、`cron.manage`、`skills.reload`、`tools.configure`。
- 最小验证：优先跑 Dovie Desktop Browser Bridge、Skills Directory Repair、Runtime Proxy / WebSocket Bridge 三组测试。

### 2026-06-24 同步记录（absorb-upstream-20260623）

这一轮把 P0/P1 全量吸收到 `absorb-upstream-20260623` 后，问题集中暴露在「模块化抽离不彻底 + 运行态/配置边界」上，下面按根因记录，避免后续同步重复踩坑。

- 暴露问题（六类）：
  1. **重复 `@method` handler 静默分叉（最高优先）**：同一方法在 `tui_gateway/server.py` 和 `tui_gateway/methods/*.py` 各注册一次；装饰器规则是 methods 模块 `_methods[name] = fn` 强制覆盖、`server.py` 用 `setdefault`，且 methods 模块在 `server.py` 之后加载，所以 **methods/* 永远是 live、server.py 那份是死代码**。模块化迁移时 live 版被不完整复制，悄悄丢了 dead 版的逻辑：`config.set` 丢 lazy-agent-build + confirm；`session.resume` 丢压缩续接 tip；`session.create` 丢 model/messages/title/close_on_disconnect；`session.save` 丢 Hermes-home 存储和 session_id/session_start/system_prompt 字段；`reload.mcp` 丢 `refresh_agent_mcp_tools`；`config.get` 丢 `project` 分支。本轮已把 5 处逻辑移植回 methods/*，并删除 server.py 中 21 个死 handler。
  2. **模型切换持久化把命名 provider 退化成 `custom` → 污染 config → 401**：桌面端每次对话强制 `/model`（无 `--session`）→ `persist_global=True` → `_persist_model_switch` 把 `result.target_provider`（dovie-cloud 暴露给 Agent 的通用标签 `"custom"`）写进 `config.model.provider`。一旦 `provider: custom`，`_resolve_named_custom_runtime` 跳过命名 provider 查找（`provider∈{auto,custom}`）→ 落到不读 `key_env` 的 openrouter fallback → `api_key="no-key-required"` → LLM proxy 返回 `DOVIE_AUTH_REQUIRED`（HTTP 401）。token 本身有效（JWT 7 天未过期），是凭证**够不着**而非过期。
  3. **`_make_agent` 用全局 `config.model.default` 构建，无视 session/profile 模型**：默认成 `gpt-5.5` 而非用户配置的 profile 模型，前端只能靠每次对话强制切换补救——这正是 marker、slash worker churn 和上面 config 污染链的源头。
  4. **`session.resume` 丢压缩续接 tip 解析**：上下文压缩会 end 当前 session 并 fork 续接子会话（`agent.session_id` 轮转），resume 父 id 必须先 `db.resolve_resume_session_id` 重锚到 tip，否则读到压缩前的陈旧历史、还会漏掉按 tip 键的 live session。
  5. **抽离不完整导致的 undefined symbol / 缺失 helper**：`_restart_slash_worker(sid, session)` 签名与上游不一致；`_persist_live_session_runtime` / `_persist_live_session_system_prompt` / `_append_model_switch_marker` / `_runtime_model_config` 等 helper 在非 P0/P1 提交里、被引用却没吸收；`conversation_loop` 的 `_retry.*`、`error_classifier` 的 `_REQUEST_VALIDATION_PATTERNS` / `_CONTENT_POLICY_BLOCKED_PATTERNS`、`file_tools` 的若干 sentinel/helper 都需 AST 级系统排查后回填。
  6. **前端把后端原始异常直接渲染**：run error 事件里 `errorText = payload.message`（原始 401/内部符号/ip:port）直接进 `oc-error-bar` + `antMessage.error`，没 humanize；`isHermesRuntimeAuthError` 只匹配 error_code `dovie_auth_required`，匹配不到 message 形态 `Dovie authentication required`。
- 根因边界：（a）`tui_gateway/server.py` 只该做协调，handler 实现全在 `methods/*`——server.py 里任何 `@method` 与 methods 同名即死代码，编辑它无效；（b）模型切换的持久化绝不能把运行时通用标签 `custom` 写回 `config.model.provider`，命名 provider 的身份是它的 `base_url` + `key_env`；（c）每个会话的 agent 应从一开始就建在它自己的模型上（control-plane create 持久化到 DB row → `_make_agent` 经 `_persisted_session_runtime` 读回），而不是建在全局默认再 per-turn 切换；（d）凭证解析在 `provider` 退化时应能按 `base_url` 自愈到命名 provider；（e）任何用户可见文本必须经 `humanizeHermesError`，原始报文只进日志。
- 修复涉及：`tui_gateway/methods/config.py`（config.set lazy-build + confirm、config.get project）、`tui_gateway/methods/system.py`（reload.mcp refresh_agent_mcp_tools）、`tui_gateway/methods/session.py`（session.create model_override/messages/title/close_on_disconnect、control-plane create 用 per-session model、session.save 完整字段、session.resume 压缩 tip 重锚）、`tui_gateway/server.py`（`_persist_model_switch` 不退化 provider、`_apply_model_switch` same-model short-circuit、`_make_agent` 优先级 model_override→`_persisted_session_runtime`→全局、`_attach_worker` 返回 bool 防双重 close、删除 21 个死 handler、回填缺失 helper）、`hermes_cli/runtime_provider.py`（`_resolve_named_custom_runtime` 按 base_url 自愈命名 provider）、`agent/error_classifier.py`、`agent/conversation_loop.py`、`tools/file_tools.py`、`tools/approval.py`；桌面端 `apps/desktop/src/services/hermes/gateway/auth.ts`（isHermesRuntimeAuthError 匹配 prose 形态）、`apps/desktop/src/composables/chat/useHermesSessionEventController.ts`（错误 humanize）、`apps/desktop/src/services/desktop/hermes-error-friendly.ts`（DOVIE_AUTH 规则）、`apps/desktop/src/services/hermes/gateway/runtime.ts` + `composables/chat/useHermesSendSessionController.ts` + `useHermesComposerSubmitController.ts`（session.create 携带 model）。
- 下次优先检查：（1）**任何 `@method` 改动先确认 live 版在 `methods/*` 还是 `server.py`**——grep `@method("X")` 看两边是否同时存在，编辑 methods 版；（2）`_persist_model_switch` 是否仍不把 `custom` 写回 `model.provider`；（3）`_resolve_named_custom_runtime` 的 base_url 自愈分支是否还在；（4）`_make_agent` 模型优先级是否仍 `model_override → 持久化 DB-row → 全局`；（5）`session.resume` 是否仍调 `db.resolve_resume_session_id` 重锚；（6）吸收后跑一遍「AST 级 undefined symbol 审计」（`py_compile` + 导入所有 `tui_gateway.methods.*` + 全量测试）；（7）前端新增 error surface 是否走 `humanizeHermesError`。
- 最小验证：`pytest tests/test_tui_gateway_server.py tests/hermes_cli/test_runtime_provider_resolution.py tests/hermes_cli/test_model_switch_custom_providers.py`（本轮 367 passed），桌面端 `vitest run src/services/desktop/__tests__/hermes-error-friendly.spec.ts src/composables/chat/__tests__/` + `type-check`。注意 `tests/gateway/test_telegram_*` 的 collection error（缺 `plugins.platforms.telegram`）是既有环境问题，与本轮无关。

#### 补充（同日，模型解析 / 上下文压缩边界）

- 暴露问题（续）：
  7. **`_apply_model_switch` 写进程级 `os.environ[HERMES_MODEL/HERMES_TUI_PROVIDER/...]` → 跨会话污染**：上游是「一进程一会话」，写 env 给 `/new` 重新解析用；dovie 是「一进程多会话」，写 env 会把 A 会话的 `/model` 切换泄漏给 B 会话的下次 agent 构建。正确做法是把选择记到 `session["model_override"]`（**不写 env**，api_key 不记，避免 dovie-cloud 轮转 token 被记成陈旧值），`_make_agent` 重建时读它。`tests/tui_gateway/test_make_agent_provider.py::test_apply_model_switch_does_not_leak_process_env` 守这条；上游遗留的 `test_config_set_model_syncs_*provider_env`（断言必须写 env）与之**直接冲突**，已改写为断言 `session["model_override"]`。
  8. **`_make_agent` 丢了 `model_override` 参数**：dovie 设计是 `_make_agent(..., model_override=dict)`，且 override 带 concrete `base_url/api_key/api_mode`（切换已解析的具体凭证应在重建中存活，避免 re-resolve 回全局端点）。吸收时参数丢失，`test_make_agent_honors_per_session_model_override` 因此红。已加回参数（参数优先 → 否则 `session["model_override"]` → 否则 `_persisted_session_runtime` → 否则全局）。
  9. **上下文压缩的 `context_length` 没用 admin 模型配置**：admin 模型配置带 `context_window`，桌面端 model catalog（`useHermesModelCatalog`）已读它并通过 `model_descriptor.context_window` 随每次 prompt.submit 发到后端，但后端只把 descriptor 存到 `agent.model_descriptor`，**没喂给 compressor** → compressor 回退 `model_metadata` 的家族猜测（如改名模型命中 1,048,576），阈值（context_length × threshold_percent，默认 0.50）按错误窗口校准，proactive 压缩太晚、只靠 reactive 报错兜底。已在 `tui_gateway/services/model_descriptor.py::set_session_model_descriptor` 用 descriptor 的 `context_window` 校准 compressor（已 reactive 探到更小真实上限时不再撑大）。压缩本身是生效的（`compression_enabled` 默认 True，preflight + turn 内逐迭代 + reactive 三处触发都接入 `agent/conversation_loop.py`）。
- 下次优先检查（续）：（8）`_apply_model_switch` 是否仍只记 `session["model_override"]`、不写 `os.environ`；（9）`_make_agent` 是否仍有 `model_override` 参数且 concrete 凭证存活；（10）`set_session_model_descriptor` 是否仍用 descriptor 的 `context_window` 校准 compressor —— 避免压缩阈值退回家族猜测。
- 既有失败（与本轮无关，基线即红）：`tests/tui_gateway/test_protocol.py::test_block_and_respond` / `test_clear_pending`、`tests/tui_gateway/test_review_summary_callback.py` 两项 —— 另外的吸收 casualty，待单独处理。
