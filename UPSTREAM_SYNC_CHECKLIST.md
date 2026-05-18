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
- `tui_gateway/services/*.py`
- `tests/test_tui_gateway_server.py`
- `tests/tui_gateway/test_protocol.py`

必须确认：

- `tui_gateway/server.py` 仍然是轻量协调层，RPC 处理逻辑仍然分拆在 `tui_gateway/methods/` 和 `tui_gateway/services/`
- `@method(...)` 注册机制仍然会加载并暴露 `prompt.submit`、`session.*`、`config.*`、`model.*`、`voice.*`、`platforms.manage`、`workspace.current`、`artifacts.list` 等本地方法
- `_LONG_HANDLERS` 仍然包含会阻塞 dispatcher 的重操作，尤其是 `platforms.manage`、session resume / branch / compress、CLI 执行等
- panic hook 仍然写入 `$HERMES_HOME/logs/tui_gateway_crash.log`，并把一行摘要发到 stderr，避免 TUI gateway 崩溃时没有诊断信息
- 兼容性 patch point 仍然保留，例如 `_SlashWorker = SlashWorker`

推荐验证：

```sh
scripts/run_tests.sh tests/test_tui_gateway_server.py tests/tui_gateway/test_protocol.py -q
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

必须确认：

- `prompt.submit` 仍然接受并返回 `run_id`、`turn_id`、`client_message_id`，并把当前 turn 的 pending metadata 写入 session
- user / assistant / tool 消息落库时仍然保留 `metadata_json`，conversation replay 时也能恢复 `metadata`
- API 请求发给模型前仍然会剥离内部 `metadata` 字段，避免严格 provider 报错
- `model_descriptor.vision_enabled` 仍然可以覆盖主模型视觉能力判断，用于 Doxie runtime 模型元数据
- 附件仍然会被规范化为 `attachments` metadata；图片附件仍然按当前模型能力走 native image input 或 vision pre-analysis
- 中断、recall turn、session busy、running runtime 复用等场景，不能把已经撤回或被打断的 turn 错误追加成完整历史

推荐验证：

```sh
scripts/run_tests.sh tests/test_tui_gateway_server.py tests/tui_gateway/test_protocol.py tests/run_agent/test_model_descriptor_vision.py tests/test_hermes_state.py -q
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
- `gateway/session_context.py`
- `tests/tools/test_doxie_agent_profile_tool.py`

必须确认：

- `doxie` toolset 仍然暴露 `design_agent_profile` 和 `test_agent_profile`
- `design_agent_profile operation=inspect_context` 仍然先返回真实 Doxie 模板、Hermes toolsets、avatar assets、workflow rules，模型不能凭空编造 tools / skills
- 后端桥接环境 `DOXIE_BACKEND_BRIDGE_URL` / `DOXIE_BACKEND_BRIDGE_TOKEN` 可用时，draft create / update / revision / resolve / list / prepare runtime 仍然走 Doxie backend bridge
- `HERMES_DOXIE_PRODUCT_CONTEXT` 仍然会注入 design mode、source session / turn / message、workspace、active draft、target profile 等上下文
- `test_agent_profile` 在有 backend bridge 时可以省略 `draft_id`，由 Doxie 解析 active 或匹配 draft；无 bridge 时仍然要求本地 draft runtime 文件存在
- 分身测试仍然用 parent agent 创建隔离 runtime，不应污染当前主 session 的配置和历史

推荐验证：

```sh
scripts/run_tests.sh tests/tools/test_doxie_agent_profile_tool.py -q
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
- `tui_gateway/services/platform_connections.py`
- `cron/scheduler.py`
- `tools/approval.py`
- `tests/test_tui_gateway_server.py`

必须确认：

- `cron.manage` 仍然支持 status/list/add/update/remove/run/runs/pause/resume，并能把 Doxie metadata 写入 cron job
- cron job 的 `doxie.approval_policy` 仍然能通过 ContextVar 覆盖该 job 的 approval mode，job 结束后必须 reset
- `approval.pending.list` 仍然能让客户端重连后恢复等待中的 approval UI
- `platforms.manage` 仍然是长任务 handler，并能通过 service 层管理平台连接，不要把平台管理逻辑重新塞回 server 主文件
- `plugins.list`、`tools.list/show/configure`、`toolsets.list`、`agents.list`、`skills.manage/reload` 等 integration RPC 仍然可用

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
- `tui_gateway/methods/session.py`
- `tests/gateway/test_session_list_allowed_sources.py`
- `tests/test_hermes_state.py`
- `tests/tui_gateway/test_protocol.py`

必须确认：

- session DB messages 表仍然包含 `metadata_json` 并能迁移旧库
- 删除 session 时，除了 `{session_id}.json` / `{session_id}.jsonl`，也要清理 Doxie/TUI `session_{session_id}.json` / `session_{session_id}.jsonl`
- `session.list` 仍然只返回允许来源的 sessions，不能把内部或不该展示的来源混进 Doxie UI
- `session.history`、`session.recall_turn`、`session.undo` 等 Doxie 依赖的方法仍然按 turn metadata 和 stored session id 正确工作
- 如果同一个 stored session id 同时有 idle runtime 和 running runtime，方法解析应优先使用 running runtime

### 12. ACP / tool schema / terminal 小型但高风险修复

涉及文件：

- `acp_adapter/tools.py`
- `tools/file_tools.py`
- `tools/terminal_tool.py`
- `tools/delegate_tool.py`
- `agent/prompt_builder.py`
- `agent/image_routing.py`
- `tests/agent/test_image_routing.py`
- `tests/tools/test_document_parse_tool.py`

必须确认：

- ACP 工具定义、Hermes tool schemas 和 core toolset 对新增工具保持一致
- terminal / code execution / file tools 仍然尊重 session cwd 和 workspace 语义
- delegate tool 仍然与本地 toolset / session metadata 改动兼容
- image routing 仍然能在模型描述符、provider 配置和附件类型之间做正确选择

## 推荐总体验证

同步完成后，至少跑下面这些测试。范围较大时可以分批执行。

```sh
scripts/run_tests.sh \
  tests/test_tui_gateway_server.py \
  tests/tui_gateway/test_protocol.py \
  tests/tui_gateway/test_make_agent_provider.py \
  tests/tui_gateway/test_artifacts.py \
  tests/tui_gateway/test_workspace_context.py \
  tests/run_agent/test_model_descriptor_vision.py \
  tests/test_hermes_state.py \
  tests/tools/test_document_parse_tool.py \
  tests/tools/test_doxie_agent_profile_tool.py \
  tests/tools/test_browser_cdp_override.py \
  tests/tools/test_computer_use.py \
  tests/hermes_cli/test_runtime_provider_resolution.py \
  tests/hermes_cli/test_model_switch_custom_providers.py \
  tests/hermes_cli/test_env_loader.py \
  tests/gateway/test_runtime_auth_response.py \
  tests/gateway/test_session_list_allowed_sources.py \
  -q
```

如果同步触碰 TUI gateway、workspace/artifact、Doxie runtime provider 或 computer-use，不能只跑单测。还应手工验证：

- Doxie 桌面端能创建 / resume session，且 `cwd` 和 workspace 显示正确
- agent 写入文件后，成果视图能收到并恢复 artifact
- 分身设计能 inspect context、保存 draft、测试 draft runtime
- runtime token 失效后，客户端能看到登录过期提示，并在刷新后恢复
- native CDP browser 多标签页操作不会串 tab
- computer-use 在多窗口场景下能 list target、capture 指定窗口、按窗口坐标操作

