# Hermes 上游同步检查清单

这份文档用于每次把 Hermes upstream/main 同步到本地分支后，快速判断 Doxie / 桌面端 / TUI Gateway 的本地实现有没有被上游结构调整打散。

重点不是罗列所有功能，而是把最容易在同步后回归的边界固定下来：先看症状，再查对应模块，最后跑最小验证包。

## 本次同步暴露的问题

当前分支的暂存改动显示，这次同步后的问题集中在几个边界：

- TUI Gateway 从 `server.py` 中抽离了大量实现，上游改动容易重新把 Doxie 的 RPC 覆盖、profile context、run event、session store 行为打坏。
- Doxie 相关能力需要通过 `doxie_extension` 注册，而不是散落在 Hermes core 里；同步时最容易漏掉 extension hook。
- `run_id` / `turn_id` / `runtime_scope_key` / `stored_session_id` / `runtime_session_id` 的边界被多处代码共同维护，任何一处回退都会导致 UI running 卡住、历史串 profile、事件 replay 不完整。
- prompt submit、interrupt、recall turn、partial assistant 持久化和 delta streaming 是一个整体；只修其中一段会制造重复消息、丢 metadata 或取消不落库。
- tool 调用链现在依赖 `parent_agent`、session cwd、model descriptor、Doxie runtime credentials；上游同步后如果退回旧调用方式，会让 computer use、delegate、profile test、vision routing 出问题。
- document parse、prompt attachments、display transcript sanitization、desktop browser bridge 已经属于 Doxie extension 边界；同步时要确认它们仍通过 extension 注册和使用。

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
- 如果 `run_agent.py`、`agent/conversation_loop.py`、`agent/tool_executor.py`、`model_tools.py` 有改动，优先检查 metadata、parent agent、tool invocation 和 extension tool registration。
- 如果 `hermes_state.py`、`hermes_state_runs.py` 有改动，优先检查 run registry、run events、message metadata、transient session migration。
- 如果 `tools/browser_tool.py`、`tools/computer_use/*`、`tools/delegate_tool.py`、`tools/vision_tools.py` 有改动，优先检查 Doxie desktop bridge、vision routing、child progress suppression。
- 如果新增或移动 Doxie 文件，确认它们在 `doxie_extension` 或 Doxie service 边界内，不要把产品专属逻辑重新混进 Hermes upstream core。

## 同步红线

这些行为不能因为吸收上游代码而回退。

### 1. Doxie Extension 是本地能力边界

必须保留：

- `doxie_extension.load_extension()`。
- `DoxieHermesExtension.register_gateway_methods()`。
- `DoxieHermesExtension.gateway_method_overrides()`。
- `DoxieHermesExtension.register_tools()`。
- `model_tools.py` 中 extension tool registration。
- `tui_gateway/core/method_registration.py` 中 extension gateway method registration。
- `tui_gateway/server.py` 中 `_EXTRACTED_METHOD_OVERRIDES` 对 extracted method 的优先注册。

检查方式：

```sh
rg "load_extension|gateway_method_overrides|register_tools|register_gateway_methods" doxie_extension model_tools.py tui_gateway
```

常见坏症状：

- `gateway.capabilities` 显示缺 Doxie required methods。
- `parse_document` 不再可用。
- `prompt.submit`、`run.*`、`session.*` 回到 upstream 默认行为。
- Doxie UI 调用某些 RPC 返回 method not found。

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

检查方式：

```sh
rg "_get_db|_active_hermes_home|ensure_agent_runtime_current|ensure_session_turn_toolsets|GatewayToolEventBridge" tui_gateway
```

常见坏症状：

- profile A 的历史、配置或 DB 被 profile B 读到。
- Doxie runtime token 更新后，旧 agent 继续用过期凭据。
- tool events 不带 run / turn / runtime scope。
- `session.info`、`runtime.status`、`model.set` 行为和 Doxie UI 不一致。

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

重点路径：

- `tui_gateway/methods/prompt.py`
- `agent/conversation_loop.py`
- `run_agent.py`
- `hermes_state.py`

检查方式：

```sh
rg "turn_metadata|persist_user_message|metadata_json|client_message_id|attachments" tui_gateway agent run_agent.py hermes_state.py
```

常见坏症状：

- transcript 里同一轮用户消息重复。
- interrupt 后 partial assistant 没有保存，或保存成完整回答。
- recall turn 删除了错误范围。
- provider 请求里混入内部 `metadata` 字段。
- 附件存在但 agent 不知道该调用 `parse_document`。

### 5. Doxie 文档和 transcript 处理在 extension 层

文档附件、文档解析、cron 提示清洗不应该重新分散进 Hermes core。

必须保留：

- `doxie_extension/prompt_attachments.py`
- `doxie_extension/document_parse_tool.py`
- `doxie_extension/display_transcript.py`
- `tui_gateway/methods/prompt.py` 调用 `enrich_prompt_with_document_attachments(...)`
- `tui_gateway/methods/session.py` 调用 `sanitize_transcript_messages(...)` 和 `sanitize_session_list_item(...)`

检查方式：

```sh
rg "enrich_prompt_with_document_attachments|sanitize_transcript_messages|parse_document|Doxie attached documents" doxie_extension tui_gateway model_tools.py toolsets.py
```

常见坏症状：

- 用户上传 PDF/Office 文件后，模型直接回答而不是先解析。
- cron job 的内部系统提示出现在 Doxie UI transcript。
- `parse_document` 从 toolset 消失。
- Doxie MinerU proxy token / URL 没有生效。

### 6. Tool 调用链必须保留 parent_agent 和 session cwd

上游同步很容易把工具调用退回到直接 `handle_function_call(...)`，导致工具拿不到当前 agent 的 runtime 信息。

必须保留：

- `agent._invoke_tool(...)` 调用路径。
- `model_tools.handle_function_call(... parent_agent=...)` 的能力。
- destructive command checkpoint cwd 顺序：tool workdir -> `agent.session_cwd` -> `DOXIE_WORKSPACE_ROOT` -> `TERMINAL_CWD`。
- `AIAgent(..., cwd=...)` 和 `agent.session_cwd`。
- `model_descriptor.vision_enabled` 对 vision routing 的覆盖。

检查方式：

```sh
rg "_invoke_tool|parent_agent|session_cwd|DOXIE_WORKSPACE_ROOT|vision_enabled" agent run_agent.py model_tools.py tools tui_gateway
```

常见坏症状：

- `test_agent_profile` 找不到 parent context。
- computer use 截图错误地走主模型 vision，或非 vision 模型收到图片。
- destructive checkpoint 落到进程 cwd，而不是用户 workspace。
- delegate 子代理进度泄漏到主会话。

### 7. Desktop Browser Bridge 优先于本地 CDP fallback

Doxie desktop browser 可用时，多标签页操作必须通过 Doxie bridge；只有 bridge 不可用时才 fallback 到 native CDP。

重点路径：

- `doxie_extension/browser_bridge.py`
- `tools/browser_tool.py`

检查方式：

```sh
rg "browser_bridge|browser_use_list_sessions|browser_use_create_tab|browser_use_activate_tab|browser_use_close_tab" doxie_extension tools/browser_tool.py
```

常见坏症状：

- Doxie 桌面浏览器打开了 tab，但 Hermes `browser_tabs` 看不到。
- `browser_new_tab` 在桌面端错误返回 “native CDP required”。
- tab id / target id 混乱，后续 snapshot 操作跑到旧标签。

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
- Doxie 临时 runtime 出现在普通 session list。
- message metadata 丢失导致 recall / interrupt / client message mapping 失效。

### 10. Doxie Cron / Automation Task 不能脱离 run event 和 approval policy

重点保留：

- `tui_gateway/services/doxie_cron_jobs.py`
- `tools/doxie_automation_task_tool.py`
- cron job 的 Doxie metadata。
- cron 运行时的 approval policy override。
- job run 与 session/run event 的关联。

检查方式：

```sh
rg "doxie_automation|approval_policy|cron.manage|trigger_job|request_cron_tick|last_session_id" tui_gateway cron tools tests
```

常见坏症状：

- Doxie 创建的定时任务不立即执行。
- cron run 找不到对应 session。
- cron job 的 full_access/default approval policy 不生效。
- automation task tool 不在正确 toolset 中。

## 症状到检查点

| 症状 | 先查 |
| --- | --- |
| Doxie UI method not found | `doxie_extension/gateway_methods.py`、`tui_gateway/core/method_registration.py`、`gateway.capabilities` |
| UI 一直 running | `tui_gateway/methods/run.py`、`tui_gateway/methods/prompt.py`、`tui_gateway/services/run_control.py`、`hermes_state_runs.py` |
| 重连后 tool progress 丢失 | `run.events`、`events.subscribe`、`append_run_event`、`runtime_scope_key` |
| 不同 profile 历史串了 | `profile_context.py`、`session_store.py`、`_get_db()`、`get_hermes_home()` |
| 文档附件没被解析 | `prompt_attachments.py`、`document_parse_tool.py`、`model_tools.py`、`toolsets.py` |
| transcript 出现 cron 内部提示 | `display_transcript.py`、`session.messages`、`session.list` |
| browser 多 tab 在桌面端不可用 | `browser_bridge.py`、`tools/browser_tool.py` |
| computer use 抓错窗口或坐标错 | `tools/computer_use/cua_backend.py`、`tools/computer_use/tool.py` |
| 非 vision 模型收到图片 | `agent/image_routing.py`、`tools/computer_use/vision_routing.py`、`model_descriptor.py` |
| delegate/profile test 泄漏子代理输出 | `tools/delegate_tool.py`、`tools/doxie_agent_profile_tool.py`、`agent/conversation_loop.py` |
| model switch 后凭据失效 | `runtime_credentials.py`、`methods/model.py`、`agent/auxiliary_client.py` |
| session list 多出临时会话 | `hermes_state.py`、`methods/session.py`、allowed sources tests |

## 最小验证包

按改动范围选包跑。不要每次都盲目全量，先跑最可能坏的包。

### Gateway ABI / Run Control

```sh
scripts/run_tests.sh \
  tests/test_doxie_gateway_contract.py \
  tests/test_tui_gateway_server.py \
  tests/tui_gateway/test_protocol.py \
  tests/tui_gateway/test_profile_data_context.py \
  tests/tui_gateway/test_tool_events.py \
  -q
```

### State / Session / Run Events

```sh
scripts/run_tests.sh \
  tests/test_hermes_state.py \
  tests/test_lazy_session_regressions.py \
  tests/gateway/test_session_list_allowed_sources.py \
  tests/tui_gateway/test_workspace_context.py \
  -q
```

### Prompt / Attachments / Doxie Extension

```sh
scripts/run_tests.sh \
  tests/test_doxie_document_parse_tool.py \
  tests/test_doxie_display_transcript.py \
  tests/tui_gateway/test_protocol.py \
  tests/test_tui_gateway_server.py \
  -q
```

### Agent / Tool Invocation / Delegate

```sh
scripts/run_tests.sh \
  tests/run_agent/test_run_agent.py \
  tests/tools/test_delegate.py \
  tests/tools/test_doxie_agent_profile_tool.py \
  tests/tools/test_computer_use.py \
  tests/tools/test_computer_use_vision_routing.py \
  -q
```

### Runtime Model / Credentials

```sh
scripts/run_tests.sh \
  tests/tui_gateway/test_make_agent_provider.py \
  tests/agent/test_auxiliary_named_custom_providers.py \
  tests/tools/test_parse_env_var.py \
  -q
```

### Doxie Cron / Automation

```sh
scripts/run_tests.sh \
  tests/tui_gateway/test_doxie_cron_jobs.py \
  tests/tui_gateway/test_tool_events.py \
  -q
```

## 同步后的人工检查

自动测试通过后，再做这些人工检查：

1. 启动 Doxie / TUI Gateway，调用 `gateway.capabilities`，确认 `ok=true`，`missingCapabilities=[]`。
2. 创建一个普通 session，提交一轮 prompt，确认返回 `run_id`、`turn_id`，并能通过 `run.events` replay。
3. 中断一轮正在运行的 prompt，确认 transcript 中有 partial assistant 或 cancelled terminal event，UI 不再 stuck running。
4. 上传一个 PDF 或 Office 文档，确认 prompt 中出现 Doxie document attachment context，agent 会调用 `parse_document`。
5. 切换 Doxie profile 或 draft runtime，确认 session list、history、run events 没有串到另一个 profile。
6. 在桌面浏览器 bridge 可用时，跑 `browser_tabs` / `browser_new_tab` / `browser_select_tab`。
7. 跑一次 `computer_use capture`，确认返回窗口元数据和 warnings 字段。

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

- 暴露问题：上游同步后，暂存修复集中在 TUI Gateway 拆分、Doxie extension 注册、run event/runtime scope、prompt metadata、tool invocation、computer use/browser bridge 等边界。
- 根因边界：本地 Doxie 能力和 Hermes upstream core 的职责边界不够显式，旧文档按功能罗列，无法指导同步后快速定位。
- 修复涉及：`doxie_extension/`、`tui_gateway/server.py`、`tui_gateway/methods/`、`tui_gateway/services/`、`hermes_state.py`、`hermes_state_runs.py`、`agent/`、`run_agent.py`、`model_tools.py`、`tools/`。
- 下次优先检查：extension hook、run control、profile-scoped DB、turn metadata、parent_agent 工具调用、Doxie document/browser/computer-use 边界。
- 最小验证：先跑 Gateway ABI / Run Control 包，再根据 diff 跑 State、Prompt、Agent/Tools、Runtime Credentials、Cron 包。
