# Hermes 上游同步检查清单

这是一份仓库内使用的检查文档，用来在同步 Hermes 上游最新代码之后，核对本地为 Doxie / 桌面端 / TUI gateway 做的实现是否仍然完整。

只要发生下面这些情况，都建议按这份清单检查一遍：

- 从上游拉取大量更新并合并到本地分支
- 把本地 `dev` / 稳定分支上的定制功能迁移到新同步分支
- 手工解决完上游同步冲突之后
- 准备把同步分支再合回本地稳定分支之前

## 目标

确认上游同步没有把本地定制功能静默覆盖、删掉，或者改出行为回归。

## 推荐同步流程

建议不要在日常开发分支上直接同步上游，先切到专门的同步分支处理。

### 1. 先拉取上游最新代码

先确认远端。当前仓库已有 `origin` 指向本地 fork；如果还没有 `upstream`，应先添加官方 Hermes 上游远端。

```sh
git remote -v
git fetch origin
git fetch upstream
git log --oneline --decorate -n 20 upstream/main
```

如果暂时没有 `upstream` 远端，就先不要盲目 `pull`。应先确认官方上游 URL 和本地 fork 的分支关系。

### 2. 切到专门处理同步的本地分支

推荐把同步工作放在单独分支，例如：

```sh
git checkout dev
git checkout -b codex/sync-upstream-main-YYYYMMDD
```

这里的基线通常应该是当前真正要保留本地能力的分支，例如本地 `dev` 或稳定分支，而不是盲目从 `upstream/main` 起新分支。

### 3. 合并上游到同步分支

在同步分支上执行：

```sh
git merge upstream/main
```

注意：

- 不要把“没有冲突”等同于“没有回归”
- 即使自动 merge 成功，也必须继续做下面的范围、语义和验证检查
- 如果出现冲突，优先保留本地 Doxie / TUI gateway / 桌面端协议语义，再吸收上游结构调整，不要机械地选 `ours` 或 `theirs`

### 4. 解决冲突后，立刻按这份清单做检查

推荐顺序：

1. 先看本地功能清单里的涉及文件，确认高风险文件没有被上游重写
2. 再做语义检查，确认关键 RPC、工具 schema、上下文隔离、持久化字段仍然存在
3. 最后跑测试和必要的手工回归

如果检查没做完，不要急着把同步分支合回本地稳定分支。

## 本地实现功能清单

每次同步后都要检查下面这些模块。以后如果本地又新增新的定制功能，也要及时补进这份文档。

### 0. TUI gateway 模块化注册与长任务调度

涉及文件：

- `tui_gateway/server.py`
- `tui_gateway/core/method_registration.py`
- `tui_gateway/core/panic.py`
- `tui_gateway/methods/*.py`
- `tui_gateway/methods/run.py`
- `tui_gateway/services/*.py`
- `tests/test_tui_gateway_server.py`
- `tests/tui_gateway/test_protocol.py`

必须确认：

- `tui_gateway/server.py` 仍然是轻量协调层，RPC 处理逻辑仍然分拆在 `tui_gateway/methods/` 和 `tui_gateway/services/`
- `@method(...)` 注册机制仍然会加载并暴露 `run.*`、`events.*`、`prompt.submit`、`session.*`、`config.*`、`model.*`、`voice.*`、`platforms.manage`、`workspace.current`、`artifacts.list` 等本地方法
- `_LONG_HANDLERS` 仍然包含会阻塞 dispatcher 的重操作，尤其是 `platforms.manage`、session resume / branch / compress、CLI 执行等
- panic hook 仍然写入 `$HERMES_HOME/logs/tui_gateway_crash.log`，并把一行摘要发到 stderr，避免 TUI gateway 崩溃时没有诊断信息
- 兼容性 patch point 仍然保留，例如 `_SlashWorker = SlashWorker`
- `session.messages` 这类只读 profile data 方法不能获取 profile env lock，避免历史读取阻塞正在运行的 profile runtime

推荐验证：

```sh
scripts/run_tests.sh tests/test_tui_gateway_server.py tests/tui_gateway/test_protocol.py tests/tui_gateway/test_profile_data_context.py -q
```

### 0.1 Run control、runtime lease 与事件订阅协议

涉及文件：

- `tui_gateway/methods/run.py`
- `tui_gateway/services/run_control.py`
- `tui_gateway/services/runtime_pool.py`
- `tui_gateway/services/status_events.py`
- `tui_gateway/services/session_info.py`
- `tui_gateway/services/transcript_messages.py`
- `hermes_state_runs.py`
- `tui_gateway/methods/prompt.py`
- `tui_gateway/methods/session.py`
- `tests/test_tui_gateway_server.py`
- `tests/tui_gateway/test_protocol.py`
- `tests/tui_gateway/test_runtime_pool.py`
- `tests/test_hermes_state_runs.py`

必须确认：

- `prompt.submit` 仍然是兼容入口；没有 `_run_registry_reserved` 时必须转发到 `run.submit`，由 run registry 统一做 busy gate 和状态持久化
- `run.submit`、`run.reserve`、`run.fail`、`run.status`、`run.list`、`run.cancel` 仍然完整注册并可通过 stored session id 调用
- `runtime_scope_key` 仍然是 Doxie profile / draft runtime 的隔离键；同一个 stored session 在不同 profile scope 下不能错误复用旧 runtime
- `acquire_runtime_lease()` 仍然只复用可执行 runtime；scope 不匹配时必须通过 `session.resume(... hydrate=none, _runtime_attach=True)` 重建轻量 runtime
- `run_events` 仍然是 append-only、单调 `seq`，`events.subscribe` 必须支持 `after_seq` replay 和 `active_only`
- `runtime_scope_key` 必须同时落在 `runs` 和 `run_events` 上；`events.subscribe(runtime_scope_key=...)`、`session.messages(include_run_events=true, runtime_scope_key=...)` 和 `SessionDB.list_run_events(..., runtime_scope_key=...)` 都必须按 scope 过滤 replay
- `events.unsubscribe` 必须清理 transport subscription；WebSocket / stdio 断开后不能继续向死 transport 写事件
- `run.cancel` 在没有 live runtime 但有持久 run state 时，仍然能发布 cancelled terminal event，而不是让 UI 永远显示 running
- `transient` / `temporary` / `ephemeral` run 和 control-plane-only session 不能持久化 active run，也不能出现在普通 `session.list` 里
- gateway 启动或恢复时，`fail_orphaned_active_runs` 仍然会把死进程 owner 的 active runs 标记为 failed，同时保留当前 pid 的 live runs

推荐验证：

```sh
scripts/run_tests.sh tests/test_tui_gateway_server.py tests/tui_gateway/test_protocol.py tests/tui_gateway/test_runtime_pool.py tests/test_hermes_state_runs.py -q
```

### 0.2 WebSocket dispatch、控制面优先级与 interrupt 响应

涉及文件：

- `tui_gateway/ws.py`
- `tui_gateway/server.py`
- `tui_gateway/doxie_sidecar.py`
- `tui_gateway/services/run_control.py`
- `tests/test_tui_gateway_ws.py`
- `tests/tui_gateway/test_ws_dispatch.py`
- `tests/tui_gateway/test_protocol.py`

必须确认：

- WebSocket 仍然复用 `tui_gateway.server.dispatch`，不能引入第二套 RPC 行为
- 控制面方法仍然走独立 executor，尤其是 `run.status`、`run.reserve`、`run.fail`、`events.subscribe/unsubscribe`、`session.interrupt`、`session.messages`、approval / clarify / sudo / secret respond
- WS transport 写出仍然用优先级队列，JSON-RPC response 必须优先于高频 `message.delta` / tool progress event
- 从 event loop 线程调用 `write()` 时不能死锁；从 worker 线程写 WS frame 必须有超时保护
- `HERMES_INTERRUPT_TRACE` 诊断只在显式开启时输出，不能污染普通 stderr
- WebSocket 断开时必须 detach run-control transport，避免订阅泄漏
- `run.cancel` 必须属于 profile-context bypass 和 WS control-plane 方法，取消运行不能被目标 profile env lock 或后台队列阻塞
- `tui_gateway/doxie_sidecar.py` 仍然只暴露 `/api/ws`，必须用 query `token` 做 constant-time 校验；启动时要注册 gateway methods 并启动 Doxie cron ticker，退出时停止 ticker

推荐验证：

```sh
scripts/run_tests.sh tests/test_tui_gateway_ws.py tests/tui_gateway/test_ws_dispatch.py tests/tui_gateway/test_protocol.py -q
```

### 1. Workspace / Artifact 持久化与 Doxie 成果视图协议

涉及文件：

- `docs/Hermes/workspace-artifacts-architecture.md`
- `tui_gateway/methods/workspace_artifacts.py`
- `tui_gateway/services/persistence/gateway_store.py`
- `tui_gateway/services/workspaces/`
- `tui_gateway/services/artifact_registry/`
- `tui_gateway/services/artifacts.py`
- `tui_gateway/services/workspace.py`
- `tui_gateway/server.py`
- `tests/tui_gateway/test_artifacts.py`
- `tests/tui_gateway/test_workspace_context.py`

必须确认：

- `session.create`、`session.resume`、`session.branch` 仍然会规范化 `cwd`，解析 workspace，并持久化 session-workspace binding
- 显式 workspace 存在时，`cwd` 必须在 workspace 根目录内；没有显式 workspace 时，默认 workspace 根目录为规范化后的 `cwd`
- 工具完成事件仍然能从 `write_file`、`patch` 等工具结果提取目标文件，并只记录 workspace 内真实存在的文件
- artifact 在发出 `artifact.created` 事件前必须先落库，避免客户端重连后成果丢失
- artifact 去重仍然基于 `(workspace_id, path)`，重复写同一文件应更新原记录，而不是制造重复成果
- `workspace.current`、`workspace.list`、`artifacts.list` 仍然能在 gateway 重启后从 `$HERMES_HOME/tui-gateway/state.db` 恢复状态

推荐验证：

```sh
scripts/run_tests.sh tests/tui_gateway/test_artifacts.py tests/tui_gateway/test_workspace_context.py -q
```

### 2. Prompt turn 元数据、附件、模型描述符与中断持久化

涉及文件：

- `tui_gateway/methods/prompt.py`
- `tui_gateway/methods/session.py`
- `tui_gateway/server.py`
- `run_agent.py`
- `hermes_state.py`
- `tests/test_tui_gateway_server.py`
- `tests/tui_gateway/test_protocol.py`
- `tests/run_agent/test_model_descriptor_vision.py`
- `tests/test_hermes_state.py`
- `tests/test_hermes_state_messages.py`
- `tests/tui_gateway/test_profile_data_context.py`

必须确认：

- `prompt.submit` 仍然接受并返回 `run_id`、`turn_id`、`client_message_id`，并把当前 turn 的 pending metadata 写入 session
- `prompt.submit` 不能绕开 run registry；新客户端应使用 `run.submit`，老客户端的 `prompt.submit` 必须自动进入同一条 run-control 路径
- user / assistant / tool 消息落库时仍然保留 `metadata_json`，conversation replay 时也能恢复 `metadata`
- API 请求发给模型前仍然会剥离内部 `metadata` 字段，避免严格 provider 报错
- `model_descriptor.vision_enabled` 仍然可以覆盖主模型视觉能力判断，用于 Doxie runtime 模型元数据
- 附件仍然会被规范化为 `attachments` metadata；图片附件仍然按当前模型能力走 native image input 或 vision pre-analysis
- 中断、recall turn、session busy、running runtime 复用等场景，不能把已经撤回或被打断的 turn 错误追加成完整历史
- agent 初始化失败、runtime agent 缺失、runtime auth rebind 失败、model switch 失败、session busy 等错误路径，必须发布 failed terminal run event，不能留下挂起 run
- transient session / run 的失败路径不能写入持久 run event，避免临时 control-plane runtime 污染用户历史
- `approval_policy` / `permission_mode` 仍然只接受 `default` 和 `full_access`；`full_access` 必须通过 `tools.approval.enable_session_yolo(stored_session_id)` 对当前 session 生效，`default` 必须关闭该 override
- goal follow-up 自触发回合也必须分配新的 run / turn id 和 runtime scope，不能复用上一轮 active run

推荐验证：

```sh
scripts/run_tests.sh tests/test_tui_gateway_server.py tests/tui_gateway/test_protocol.py tests/run_agent/test_model_descriptor_vision.py tests/test_hermes_state.py tests/test_hermes_state_messages.py -q
```

### 3. Session context、HERMES_HOME 上下文隔离与 cwd 语义

涉及文件：

- `gateway/session_context.py`
- `hermes_constants.py`
- `run_agent.py`
- `tools/code_execution_tool.py`
- `tui_gateway/server.py`
- `tui_gateway/services/workspace.py`
- `tests/tui_gateway/test_workspace_context.py`

必须确认：

- `TERMINAL_CWD`、`HERMES_DOXIE_PRODUCT_CONTEXT` 等会话变量仍然走 ContextVar，不退回成纯 `os.environ` 进程全局状态
- `get_hermes_home()` 仍然支持 context-local override，长生命周期 gateway 里不同 profile / session 不能互相污染
- agent context files、checkpoint destructive command cwd、code execution project cwd 仍然优先读取 session context
- session context 清理路径仍然会 reset ContextVar，避免并发 session 或 cron job 串上下文

### 4. Runtime provider、Doxie runtime auth 与 secure-store 占位符

涉及文件：

- `hermes_cli/runtime_provider.py`
- `hermes_cli/model_switch.py`
- `hermes_cli/env_loader.py`
- `hermes_cli/config.py`
- `tui_gateway/server.py`
- `tui_gateway/methods/model.py`
- `gateway/run.py`
- `tests/hermes_cli/test_runtime_provider_resolution.py`
- `tests/hermes_cli/test_model_switch_custom_providers.py`
- `tests/hermes_cli/test_env_loader.py`
- `tests/tui_gateway/test_make_agent_provider.py`
- `tests/gateway/test_runtime_auth_response.py`

必须确认：

- named custom provider 暴露给 live Agent 为 `provider="custom"` 后，后续 rebind / `/model` switch 仍然保留原始 requested provider 作为凭据权威
- 同 provider 切模型时，不能把已有有效 API key 降级成 `no-key-required`
- Doxie runtime token 轮换后，`_ensure_agent_runtime_current` 仍然能重新 resolve runtime provider 并调用 `agent.switch_model`
- `.env` 和 config 中的 `<secure-store>` / `<已隐藏>` 只表示安全存储占位符，不能作为真实凭据写入环境或返回给 provider
- runtime auth 失败时，gateway 返回本地友好的 Doxie 登录凭证过期提示，而不是空 assistant response 或低层异常

推荐验证：

```sh
scripts/run_tests.sh tests/hermes_cli/test_runtime_provider_resolution.py tests/hermes_cli/test_model_switch_custom_providers.py tests/hermes_cli/test_env_loader.py tests/tui_gateway/test_make_agent_provider.py tests/gateway/test_runtime_auth_response.py -q
```

### 5. Doxie 分身设计工具与后端桥接

涉及文件：

- `tools/doxie_agent_profile_tool.py`
- `toolsets.py`
- `hermes_cli/tools_config.py`
- `model_tools.py`
- `gateway/session_context.py`
- `tests/tools/test_doxie_agent_profile_tool.py`
- `tests/hermes_cli/test_tools_config.py`
- `tests/test_model_tools.py`

必须确认：

- `doxie` toolset 仍然是 internal toolset：可供 Doxie runtime 定向开启，但不能出现在普通 tools picker、默认 toolset 扫描或平台配置保存结果里
- `design_agent_profile operation=inspect_context` 仍然先返回真实 Doxie 模板、Hermes toolsets、avatar assets、workflow rules，模型不能凭空编造 tools / skills
- `inspect_context` 默认只返回 compact overview；详细 catalog 必须通过 `catalog_kind=toolsets|skills|templates|avatars`、`query`、`limit` 分页获取，避免把完整 skill/tool catalog 一次性塞进上下文
- system toolsets catalog 必须过滤 internal toolsets，并优先使用 `_get_effective_configurable_toolsets()` 作为可展示来源
- 后端桥接环境 `DOXIE_BACKEND_BRIDGE_URL` / `DOXIE_BACKEND_BRIDGE_TOKEN` 可用时，draft create / update / revision / resolve / list / prepare runtime 仍然走 Doxie backend bridge
- `HERMES_DOXIE_PRODUCT_CONTEXT` 仍然会注入 design mode、source session / turn / message、workspace、active draft、target profile 等上下文
- `test_agent_profile` 在有 backend bridge 时可以省略 `draft_id`，由 Doxie 解析 active 或匹配 draft；无 bridge 时仍然要求本地 draft runtime 文件存在
- `operation=install_skill` 和 `install_skill_to_agent_profile_draft` 仍然只安装已经存在于当前 Hermes 的真实 skill；必须先让 Doxie backend `prepare_runtime` 返回目标 draft home，再把完整 skill package 复制进 draft profile 的 Hermes home
- 安装 skill 到 draft 后，draft `recommendedSkills` 必须加入该 skill，`missingCapabilities` 中对应“技能未安装 / skill missing”条目必须被清掉
- 分身测试仍然用 parent agent 创建隔离 runtime，不应污染当前主 session 的配置和历史；测试子代理必须设置 transient session 并 suppress child progress，不能把草稿测试的 subagent delta / fake thinking 暴露到主会话 UI
- `model_tools.handle_function_call(... parent_agent=...)` 和 `run_agent.py` 的调用链必须继续把当前 agent 传给 registry tool，否则 `test_agent_profile` 等需要 parent context 的工具会退化失败

推荐验证：

```sh
scripts/run_tests.sh tests/tools/test_doxie_agent_profile_tool.py tests/hermes_cli/test_tools_config.py tests/test_model_tools.py -q
```

### 6. 文档解析工具与附件读取能力

涉及文件：

- `tools/document_parse_tool.py`
- `toolsets.py`
- `model_tools.py`
- `tools/lazy_deps.py`
- `tests/tools/test_document_parse_tool.py`

必须确认：

- core/file toolset 仍然包含 `parse_document`
- `parse_document` 仍然支持 PDF、Office 文档、表格、CSV/TSV、纯文本、Markdown、JSON/XML/YAML/TOML/RTF 等常见附件类型
- PDF / PPT / PPTX 在配置 Doxie MinerU proxy 时仍然优先走 `DOXIE_MINERU_PROXY_URL` + `DOXIE_LLM_RUNTIME_TOKEN`
- 本地路径仍然走 `tools.file_tools._resolve_path_for_task` 和 `agent.file_safety.get_read_block_error`，不能绕过读文件安全策略
- 最大字符数限制和 fallback parser 仍然存在，不能把大文档无限注入上下文
- parser 返回 JSON 字符串，符合 Hermes registry handler 约定

推荐验证：

```sh
scripts/run_tests.sh tests/tools/test_document_parse_tool.py -q
```

### 7. Browser native CDP 多标签页能力

涉及文件：

- `tools/browser_tool.py`
- `toolsets.py`
- `tests/tools/test_browser_cdp_override.py`

必须确认：

- core browser toolset 仍然包含 `browser_tabs`、`browser_new_tab`、`browser_select_tab`、`browser_close_tab`
- 多标签页操作只在 native CDP provider 下启用；Camofox 或没有 `BROWSER_CDP_URL` 时应返回 unsupported，而不是误操作
- active CDP target 仍然按 task/session key 记录，后续 snapshot / navigate / click 前会重新 activate 当前 target
- 关闭 active tab 后，仍然会选择另一个可用 tab 或创建 blank tab，不能让 session 卡在不存在的 target 上

推荐验证：

```sh
scripts/run_tests.sh tests/tools/test_browser_cdp_override.py -q
```

### 8. Computer Use 多窗口 / 多显示器目标绑定

涉及文件：

- `tools/computer_use/backend.py`
- `tools/computer_use/cua_backend.py`
- `tools/computer_use/schema.py`
- `tools/computer_use/tool.py`
- `tests/tools/test_computer_use.py`

必须确认：

- `computer_use` schema 仍然支持 `action="list_targets"` 和 `target_id`
- capture result 仍然包含 `target_id`、pid、window_id、display_id、window/capture bounds、coordinate_space、scale_factor、warnings 等元数据
- pixel coordinate 语义仍然是 capture response 的 coordinate space，不要退回成含混的全局屏幕坐标
- `list_targets` 是 safe action，不需要 approval；click/type/key/scroll/drag 等控制类动作仍然走 approval
- backend 切换 `HERMES_COMPUTER_USE_BACKEND` 时会重建 backend，避免测试或运行时复用错误 backend 实例
- CUA backend 仍然通过 cua-driver 暴露窗口和显示器 catalog，并按 target_id capture / act

推荐验证：

```sh
scripts/run_tests.sh tests/tools/test_computer_use.py -q
```

### 9. Doxie cron 管理、approval policy 与平台连接管理

涉及文件：

- `tui_gateway/methods/integrations.py`
- `tui_gateway/services/doxie_cron_jobs.py`
- `tui_gateway/services/doxie_cron_runtime.py`
- `tui_gateway/services/platform_connections.py`
- `tui_gateway/services/tool_events.py`
- `tui_gateway/services/status_events.py`
- `tui_gateway/doxie_sidecar.py`
- `cron/jobs.py`
- `cron/scheduler.py`
- `tools/approval.py`
- `tests/test_tui_gateway_server.py`
- `tests/tui_gateway/test_doxie_cron_jobs.py`
- `tests/cron/test_jobs.py`

必须确认：

- `cron.manage` 仍然支持 status/list/add/update/remove/run/runs/pause/resume，并能把 Doxie metadata 写入 cron job
- Doxie sidecar runtime 必须启动 `doxie_cron_runtime` ticker；`cron.status.scheduler` 应反映真实 ticker 状态，而不是硬编码 healthy
- `cron.manage add` 带 `wakeMode=now` 和 `cron.manage run` 必须调用 `trigger_job()` 并 `request_cron_tick()`，让任务立即执行而不是等下一个固定 tick
- cron scheduler 每次运行必须把真实 `_runtime_session_id` 写入 job，`mark_job_run(... session_id=...)` 持久化为 `last_session_id`
- `cron.manage runs` 返回的 run entry 必须优先用 `last_session_id` 作为 `sessionId/sessionKey`，同时保留 Doxie 目标会话为 `targetSessionId`
- cron job 的 `doxie.approval_policy` 仍然能通过 ContextVar 覆盖该 job 的 approval mode，job 结束后必须 reset
- `approval.pending.list` 仍然能让客户端重连后恢复等待中的 approval UI
- `platforms.manage` 仍然是长任务 handler，并能通过 service 层管理平台连接，不要把平台管理逻辑重新塞回 server 主文件
- `plugins.list`、`tools.list/show/configure`、`toolsets.list`、`agents.list`、`skills.manage/reload` 等 integration RPC 仍然可用
- `tools.prepare` 对 `agent_browser/browserbase` 的 ready/installable/config-required 判断必须区分 local browser provider 与 Browserbase，并且 `requires_configuration` 不能压过可安装状态
- tool progress event 仍然通过 `GatewayToolEventBridge` 输出，且 session 已 interrupt 时不能继续发 `tool.start` / `tool.complete`
- Doxie structured tool result 仍然只对白名单工具透传结构化结果，例如 `design_agent_profile`、draft create/revision、`test_agent_profile`
- retry / rate-limit / stream reconnect / fallback 等 provider status 文本仍然会被 `status_events.classify_status_update()` 归一化为客户端可稳定渲染的结构化状态

推荐验证：

```sh
scripts/run_tests.sh tests/tui_gateway/test_doxie_cron_jobs.py tests/cron/test_jobs.py tests/test_tui_gateway_server.py -q
```

### 10. Feishu 依赖与 websocket 代理兼容

涉及文件：

- `gateway/platforms/feishu.py`
- `pyproject.toml`
- `uv.lock`

必须确认：

- Feishu optional dependency 仍然包含 `python-socks`
- `check_feishu_requirements()` 即使 `lark-oapi` 已经可 import，也仍然会 lazy-install / 校验完整 Feishu dependency set
- SOCKS proxy 环境下 websocket mode 不能因为缺少 `python-socks` 失败

### 11. Session 列表、删除、历史与 Doxie/TUI transcript 文件

涉及文件：

- `hermes_state.py`
- `hermes_state_messages.py`
- `hermes_state_platform.py`
- `hermes_state_runs.py`
- `hermes_state_search.py`
- `tui_gateway/methods/session.py`
- `tui_gateway/services/session_info.py`
- `tui_gateway/services/transcript_messages.py`
- `tests/gateway/test_session_list_allowed_sources.py`
- `tests/test_hermes_state.py`
- `tests/test_hermes_state_schema.py`
- `tests/test_hermes_state_messages.py`
- `tests/test_hermes_state_runs.py`
- `tests/test_hermes_state_search.py`
- `tests/tui_gateway/test_protocol.py`

必须确认：

- `hermes_state.py` 仍然只是组合入口；message、platform、run、search 逻辑分别留在 mixin 模块中，避免重新膨胀成单文件巨物
- session DB messages 表仍然包含 `metadata_json` 并能迁移旧库
- `runs` / `run_events` 表和索引仍然存在，旧库缺少 `runtime_scope_key` 时必须能迁移并回填
- 旧 `run_events` 表迁移时必须从 `event_json.runtime_scope_key`、`payload_json.runtime_scope_key` 或 session id 回填 `runtime_scope_key`，并创建 `idx_run_events_scope_seq`
- structured / multimodal message content 仍然通过 sentinel JSON 编码落 SQLite，读取时恢复 list/dict，不能重新触发 sqlite bind list/dict 错误
- FTS5 搜索、CJK LIKE fallback、session list allowed sources 仍然走拆分后的 search/platform mixin，不能在拆分后丢行为
- 删除 session 时，除了 `{session_id}.json` / `{session_id}.jsonl`，也要清理 Doxie/TUI `session_{session_id}.json` / `session_{session_id}.jsonl`
- `session.list` 仍然只返回允许来源的 sessions，不能把内部或不该展示的来源混进 Doxie UI
- `session.list` 必须排除 `sessions.transient=1` 和无内容占位行，但不能因为测试 stub 或旧 DB 缺少 `message_count/title/preview` 字段就误删真实历史
- `session.messages` 仍然提供分页 transcript，支持 cursor / limit，并且只读 profile 数据时不进入 profile env lock
- `session.messages(include_run_events=true)` 必须返回 `runEvents`，并支持按 `runtime_scope_key` 过滤，方便客户端一次恢复 transcript 和同 scope run 事件
- `session.history`、`session.recall_turn`、`session.undo` 等 Doxie 依赖的方法仍然按 turn metadata 和 stored session id 正确工作
- 如果同一个 stored session id 同时有 idle runtime 和 running runtime，方法解析应优先使用 running runtime

推荐验证：

```sh
scripts/run_tests.sh tests/test_hermes_state.py tests/test_hermes_state_schema.py tests/test_hermes_state_messages.py tests/test_hermes_state_runs.py tests/test_hermes_state_search.py tests/gateway/test_session_list_allowed_sources.py -q
```

### 12. ACP / tool schema / terminal 小型但高风险修复

涉及文件：

- `acp_adapter/tools.py`
- `tools/file_tools.py`
- `tools/terminal_tool.py`
- `tools/delegate_tool.py`
- `agent/prompt_builder.py`
- `agent/image_routing.py`
- `tests/agent/test_image_routing.py`
- `tests/agent/test_subagent_progress.py`
- `tests/tools/test_delegate.py`
- `tests/tools/test_document_parse_tool.py`

必须确认：

- ACP 工具定义、Hermes tool schemas 和 core toolset 对新增工具保持一致
- terminal / code execution / file tools 仍然尊重 session cwd 和 workspace 语义
- `tools.terminal_tool.get_environment(config, task_id=...)` 仍然是 prompt builder remote backend probe 的入口；不要退回到从 `tools.environments` 直接 import，避免导入路径不一致
- delegate tool 仍然与本地 toolset / session metadata 改动兼容；子代理普通 assistant 内容和 quiet spinner 不能被重新标记成 `_thinking`
- `delegate_task` 必须支持 parent opt-out 标志 `_delegate_child_progress_suppressed` 和 `_delegate_child_transient_session`，草稿测试等内部执行不能落主 session DB，也不能流式输出到主 UI
- image routing 仍然能在模型描述符、provider 配置和附件类型之间做正确选择

推荐验证：

```sh
scripts/run_tests.sh tests/agent/test_subagent_progress.py tests/tools/test_delegate.py tests/tools/test_doxie_agent_profile_tool.py -q
```

### 13. Skills Hub inspect 与 skill 文件预览

涉及文件：

- `hermes_cli/skills_hub.py`
- `tools/skill_package_lifecycle.py`
- `tools/skills_hub.py`
- `tools/skills_tool.py`
- `tui_gateway/methods/integrations.py`
- `tests/tools/test_skill_package_lifecycle.py`
- `tests/tools/test_skills_hub.py`
- `tests/tui_gateway/test_protocol.py`
- `tests/test_tui_gateway_server.py`

必须确认：

- `inspect_skill()` 仍然返回 `skill_md_preview`，并额外包含 bundle `files` 列表
- 文件 payload 必须包含 `path`、`content`、`truncated`、`is_binary`、`size`
- 二进制文件不能按文本硬解；应返回二进制占位说明，并标记 `is_binary=True`
- 单文件内容仍然有字符上限，默认 `max_chars=96000`，避免 inspect skill 一次性把大文件塞爆上下文
- `browse_skills()` 和 `skills.manage action=search` 仍然使用 `parallel_search_sources(... overall_timeout=6)`，不能退回串行或无限等待 marketplace
- `skills.list` 必须只列本地已安装技能，不依赖 marketplace router；返回项需包含 `install_path`、`modified_at`、`managed`、`can_delete`、`can_update`、`can_toggle`
- gateway 在 Doxie profile context 下执行 `skills.list/manage/reload` 前，必须把已 import 的 `tools.skills_tool`、`tools.skill_manager_tool`、`tools.skills_sync`、`tools.skills_hub` 模块级 `HERMES_HOME/SKILLS_DIR/.hub` 路径同步到当前 profile home
- `skills.manage action=copy_installed|copy_local` 必须通过 `copy_installed_skill_to_home()` 把完整 skill package 复制到目标 Hermes home，并保留 scripts/templates/assets 等辅助文件
- `skills.manage action=import_archive` 必须只接受单 skill `.zip` 包；拒绝多 `SKILL.md`、路径穿越和 symlink；安装前仍然运行本地 skill security scan
- `skills.manage action=delete` / `delete_skill_package()` 只能删除本地 skills root 下的 local 或 hub skill；必须拒绝 builtin skill、`.hub` 内部目录和 skills root 之外路径
- `copy_installed_skill_to_home()` / `import_skill_archive_to_home()` 完成后必须清理 skill prompt cache 并 reload skills

推荐验证：

```sh
scripts/run_tests.sh tests/tools/test_skill_package_lifecycle.py tests/tools/test_skills_hub.py tests/tui_gateway/test_protocol.py -q
```

### 14. Feishu 身份映射与 workspace authority 元数据

涉及文件：

- `gateway/platforms/feishu.py`
- `tests/gateway/test_feishu.py`
- `tui_gateway/services/workspaces/service.py`
- `tests/tui_gateway/test_workspace_context.py`

必须确认：

- Feishu `SessionSource.user_id` 仍然优先使用 `open_id`，因为 QR onboarding allowlist 存的是 open_id；`user_id_alt` 才携带 `union_id` 或 tenant `user_id`
- bot name lookup 仍然使用 `open_id`，不能切到 union_id / tenant user_id 导致官方接口查名失败
- workspace payload 仍然标记 `authority: "doxie"`（显式 workspace id）或 `authority: "hermes_runtime_cache"`（无显式 id fallback），并始终带 `runtime_cache: true`
- Hermes gateway 只缓存 runtime/session artifact 所需 workspace 信息；Doxie 仍然是产品级 workspace 名称、默认 profile、last-used profile 等元数据权威

### 15. Doxie-managed web search / page parse tools

涉及文件：

- `tools/doxie_web_tools.py`
- `toolsets.py`
- `hermes_cli/tools_config.py`
- `tests/tools/test_doxie_web_tools.py`

必须确认：

- `doxie_web` toolset 仍然是可配置 toolset，包含且只包含 `jina_web_parser_tool` 和 `serper_search_tool`
- `serper_search_tool` 必须通过 `DOXIE_SERPER_PROXY_URL` 调用 Doxie llm-proxy，使用 `DOXIE_LLM_RUNTIME_TOKEN` bearer auth；不能把 SERPER key 放进 Hermes runtime
- `jina_web_parser_tool` 必须通过 `DOXIE_WEB_PARSE_PROXY_URL` 调用 Doxie-managed web reader，使用相同 runtime token
- web proxy timeout 必须有边界：默认 45 秒，最小 5 秒，最大 60 秒，并允许 `DOXIE_WEB_PROXY_TIMEOUT` 作为默认覆盖
- 代理返回非 2xx、非 JSON、超时或 URL error 时，工具仍然返回 Hermes tool error JSON，而不是抛出未捕获异常
- `serper_search_tool` 应继续透传 query、location、gl、hl、page、num、time_range；`jina_web_parser_tool` 应继续透传 url、output_format、include_links、include_images、timeout

推荐验证：

```sh
scripts/run_tests.sh tests/tools/test_doxie_web_tools.py -q
```

## 推荐总体验证

同步完成后，至少跑下面这些测试。范围较大时可以分批执行。

```sh
scripts/run_tests.sh \
  tests/test_tui_gateway_server.py \
  tests/test_tui_gateway_ws.py \
  tests/tui_gateway/test_protocol.py \
  tests/tui_gateway/test_ws_dispatch.py \
  tests/tui_gateway/test_runtime_pool.py \
  tests/tui_gateway/test_profile_data_context.py \
  tests/tui_gateway/test_doxie_cron_jobs.py \
  tests/tui_gateway/test_make_agent_provider.py \
  tests/tui_gateway/test_artifacts.py \
  tests/tui_gateway/test_workspace_context.py \
  tests/run_agent/test_model_descriptor_vision.py \
  tests/agent/test_subagent_progress.py \
  tests/test_hermes_state.py \
  tests/test_hermes_state_schema.py \
  tests/test_hermes_state_messages.py \
  tests/test_hermes_state_runs.py \
  tests/test_hermes_state_search.py \
  tests/tools/test_document_parse_tool.py \
  tests/tools/test_doxie_agent_profile_tool.py \
  tests/tools/test_doxie_web_tools.py \
  tests/tools/test_browser_cdp_override.py \
  tests/tools/test_computer_use.py \
  tests/tools/test_delegate.py \
  tests/hermes_cli/test_runtime_provider_resolution.py \
  tests/hermes_cli/test_model_switch_custom_providers.py \
  tests/hermes_cli/test_env_loader.py \
  tests/hermes_cli/test_tools_config.py \
  tests/test_model_tools.py \
  tests/gateway/test_runtime_auth_response.py \
  tests/gateway/test_feishu.py \
  tests/gateway/test_session_list_allowed_sources.py \
  tests/cron/test_jobs.py \
  tests/tools/test_skill_package_lifecycle.py \
  tests/tools/test_skills_hub.py \
  -q
```

如果同步触碰 TUI gateway、run control、workspace/artifact、Doxie runtime provider 或 computer-use，不能只跑单测。还应手工验证：

- Doxie 桌面端能创建 / resume session，且 `cwd` 和 workspace 显示正确
- Doxie 桌面端能通过 `run.submit` 发起回合，通过 `events.subscribe(after_seq=...)` 重连 replay，并能 cancel running run
- agent 写入文件后，成果视图能收到并恢复 artifact
- 分身设计能 inspect context、保存 draft、测试 draft runtime
- 已创建或已安装的 Hermes skill 能被安装进 Doxie draft profile，draft runtime home 下能看到完整 skill package，draft recommendedSkills 同步更新
- Doxie profile / draft scope 下的技能列表、删除、导入 zip、复制到 target home 都作用在目标 Hermes home，而不是主进程默认 home
- Doxie-managed SERPER 搜索和网页解析工具通过后端 proxy 正常工作，Hermes 侧不持有第三方搜索/解析密钥
- Doxie sidecar 启动后 cron ticker 正常运行，`wakeMode=now` 和手动 run 能立即唤醒执行，运行记录能打开真实 cron runtime session
- runtime token 失效后，客户端能看到登录过期提示，并在刷新后恢复
- native CDP browser 多标签页操作不会串 tab
- computer-use 在多窗口场景下能 list target、capture 指定窗口、按窗口坐标操作
- WebSocket 客户端在高频 streaming 时仍能快速收到 `run.status`、`session.interrupt`、approval respond 等控制面响应
