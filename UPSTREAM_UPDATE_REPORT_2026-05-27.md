# Hermes upstream/main 更新报告 - 2026-05-27

## 范围

本报告分析的是本地执行 `git fetch upstream` 后，Hermes 上游 `main` 分支相对上一次已知上游基线的增量。

当前本地分支：

```text
codex/merge-upstream-main-20260522-141935
```

上游增量范围：

```text
1264fab15..4feb181eb
```

范围两端：

```text
1264fab15 fix(tui): surface verbose tool details (#30225)
4feb181eb chore(release): map sir-ad + rdasilva1016-ui in AUTHOR_MAP
```

本报告只做 fetch 后的上游更新分析；当前没有执行 `merge`、`rebase` 或代码合并。

## 更新规模

```text
494 non-merge commits
1235 files changed
181654 insertions(+)
10416 deletions(-)
```

按顶层目录统计的变化文件数：

```text
539 website
313 tests
58 hermes_cli
55 web
51 ui-tui
38 agent
35 tools
33 plugins
21 gateway
16 optional-skills
16 locales
14 docker
8 .github
5 infographic
3 skills
3 scripts
3 nix
2 optional-mcps
2 cron
1 uv.lock
1 tui_gateway
1 toolsets.py
1 setup-hermes.sh
1 run_agent.py
1 pyproject.toml
1 hermes_state.py
1 hermes_constants.py
1 docs
1 cli.py
1 acp_adapter
```

目录热区：

```text
tests/gateway/                     7.0%
tests/hermes_cli/                  6.0%
tests/tools/                       3.8%
hermes_cli/                        3.7%
agent/                             2.6%
website/docs/user-guide/features/  2.5%
tools/                             2.3%
tests/agent/                       2.3%
```

## 总体结论

这次上游更新不是单一功能迭代，而是一次横跨运行时、Dashboard、Docker、Gateway、Provider、Skills、安全加固和文档站的综合更新。

对本地 Doxie 分支来说，最需要关注的不是普通文本冲突，而是这些架构边界是否会在合并时被上游新实现覆盖：

- Dashboard 认证和 WebSocket ticket 机制会影响本地 Doxie dashboard / sidecar / PTY bridge 的认证假设。
- Docker 从 `tini` 切到 `s6-overlay`，会改变容器内主进程、profile gateway、dashboard 进程和 runtime lifecycle 的监督方式。
- Codex Responses / streaming / reasoning replay 有大量修复，会影响 conversation loop、tool call continuation、内部 metadata 过滤和 partial stream 处理。
- Gateway API server 新增 session control、media session chat、skills/toolsets API，会和本地 TUI Gateway / Doxie run control / session API 有重叠。
- Skills Hub、MCP catalog、安全扫描、skill bundle symlink 防护持续增强，会影响本地 skill package lifecycle 和 profile-aware skills 初始化逻辑。
- `tui_gateway/server.py` 上游虽然只在一个文件有变化，但本地分支已经把 TUI Gateway 模块化；合并时要避免把 extracted methods/services 的本地边界打回单文件结构。

## 面向客户端接入的合并判断

如果只从当前 Doxie 客户端接入是否必须立刻合并来看，结论是：

```text
不建议为了客户端接入立刻全量合并本次 upstream/main。
```

理由是本次上游没有直接提供能替代本地 Doxie 客户端接入的核心能力：

- 没有上游等价的 `doxie_extension` gateway method / tool registration 边界。
- 没有上游等价的 `runtime.ensure` profile-scoped worker proxy。
- 没有上游等价的 Doxie run control / run event replay / client run state contract。
- 没有上游等价的 workspace / artifact 一等客户端 API。
- 没有上游等价的 Doxie document attachment + `parse_document` 集成。
- 没有上游等价的 Doxie desktop visible browser bridge。
- 没有上游等价的 Doxie automation current-session result binding。

所以，如果目标是让当前客户端稳定接入，本次上游更新不是“必须马上吃掉”的前置条件。反过来，全量合并会直接触碰本地接入的核心文件，短期风险高于收益。

但是，这次上游确实有几类和客户端接入相关的更新，适合后续选择性吸收。

### 本地现状更新（2026-06-02）

截至 2026-06-02，本地 Doxie 接入在选择性吸收 P0/P1 后又补齐了两项客户端稳定性行为：

- Doxie automation create / update / remove 工具在返回结构化成功事件后，可以由 `agent/direct_tool_response.py` 直接生成最终 assistant 回复。这个路径只对单个白名单 Doxie automation tool call 生效，失败或非结构化结果仍回到普通模型 follow-up，避免为了确定性工具结果再次请求模型导致额外延迟或 running 状态卡住。
- TUI Gateway 的会话列表把 `cron` 与 `tool` 一起作为内部 runtime source 排除。Doxie 自动化任务的用户可见结果通过当前会话或新会话 result binding 展示，raw cron execution session 不再污染客户端会话侧栏。

这两项进一步强化了原报告的判断：Doxie automation 和 session/run state 仍是本地客户端 contract，不应被 upstream API server session controls 或原生 cron 流程替代。后续吸收上游时，仍应优先保留本地 run/event binding、profile-scoped runtime worker 和 Doxie automation 工具边界。

### 对客户端接入有直接价值的上游更新

1. Dashboard / WebSocket 认证链路：

```text
dashboard auth gate
single-use WS tickets
AuthWidget
/api/status auth fields
X-Forwarded-Prefix
__Host-/__Secure- cookie handling
```

价值：

- 如果 Doxie 客户端要走 Hermes dashboard 的公网/反代/Portal 登录，这部分值得合并或借鉴。
- 如果当前 Doxie 客户端只走本地 sidecar token 和 profile-scoped runtime proxy，这部分不是立即必要。

风险：

- 可能和本地 sidecar token、PTY bridge、runtime worker WS bridge 的认证模型冲突。
- 需要设计清楚 Doxie auth 与 upstream OAuth gate 的顺序，而不是机械套上。

2. API Server session controls / media session chat：

```text
GET /v1/skills
GET /v1/toolsets
API server session controls
media in session chat API
skills_api capability
```

价值：

- 上游开始给外部客户端暴露更明确的 API surface。
- `skills/toolsets` API 对客户端动态展示可用能力有参考价值。
- media session chat 和 Doxie document attachment 有模型重叠，后续可以统一附件 contract。

风险：

- 本地 Doxie 已有更强的 `prompt.submit`、`run.submit`、`run.events`、runtime scope 和 artifact/session contract。
- 如果直接采用 upstream API server session controls，可能绕过本地 run control，导致客户端 running 状态、取消、事件 replay 不一致。

3. TUI session orchestrator / active session switcher：

```text
feat: add TUI session orchestrator
activeSessionSwitcher
session lifecycle tests
virtual transcript resize fixes
late thinking delta fixes
```

价值：

- 对 Hermes Ink TUI 客户端体验有价值。
- 对 Doxie 如果嵌入真实 `hermes --tui` 或复用 TUI Gateway event model，有参考意义。

风险：

- 当前 Doxie 客户端接入主要依赖 `tui_gateway` JSON-RPC、WebSocket bridge、run events，不是直接依赖 Ink UI。
- 上游 `tui_gateway/server.py` 仍是单文件演进，本地已经模块化；合并时容易把本地 method/service 边界打散。

4. Streaming / Codex Responses / partial output 修复：

```text
null output stream recovery
time-to-first-byte watchdog
finish_reason=length continuation
invalid_encrypted_content recovery
foreign-issuer reasoning replay filtering
internal scaffolding key stripping
```

价值：

- 这部分对客户端最终体验有实际价值：减少挂起、空流、provider replay 异常、工具调用中断后的状态错误。
- 对 Doxie run control 来说，尤其要吸收“空 stream / partial stream / length continuation”的正确性修复。

风险：

- 这些代码在 `run_agent.py`、`agent/conversation_loop.py`、`agent/codex_responses_adapter.py`，正好也是本地 turn metadata、run metadata、tool event 的核心路径。
- 合并策略应该是以上游修复为基础，再保留本地 metadata/event contract，不能简单覆盖。

5. Skills Hub / security hardening：

```text
skill bundle symlink rejection
skills hub freshness / health checks
promptware defense
path traversal / .env / plugin asset hardening
```

价值：

- 对 Doxie 客户端接入不是功能前置，但对生产稳定性和安全很重要。
- 本地 skills 目录自愈和 skill package lifecycle 应该对齐这些安全语义。

风险：

- 本地 ZIP skill 导入、profile home skills 初始化不能绕过上游 quarantine / scan / symlink 防护。

### 对本地已开发功能的影响判断

| 本地功能 | 上游这次有没有等价更新 | 合并必要性 | 风险 |
|---|---:|---:|---|
| Doxie gateway contract / capabilities | 没有 | 不必须 | 覆盖 method registration |
| `runtime.ensure` / runtime worker proxy | 没有 | 不必须 | scoped RPC 退回 control plane |
| run control / run events / client running state | 部分相关：API session controls、streaming fix | 建议选择性吸收 | run state contract 被绕过 |
| workspace / artifact API | 没有 | 不必须 | 只读查询创建空 DB |
| prompt attachment / document parse | 部分相关：media session chat、安全解析 | 建议后续对齐 | 附件模型分叉 |
| desktop visible browser bridge | 没有，只有 browser daemon cleanup | 不必须 | 回退到 headless/browser agent |
| Doxie automation | 部分相关：cron provenance/output validation | 建议选择性吸收 | 原生 cronjob 暴露给模型 |
| Dashboard session badge / web UI | 有大量 dashboard/web 更新 | 看 Doxie 是否复用 web dashboard | session badge 被 upstream SessionsPage 覆盖 |
| skills path 自愈 / package lifecycle | 有安全和 hub 更新 | 建议吸收安全部分 | 绕过 scan/quarantine |

### 建议决策

当前阶段建议：

```text
暂不做全量合并；先 cherry-pick / 手工移植和客户端稳定性直接相关的修复。
```

优先级：

1. 优先吸收 streaming / Codex Responses / provider request filtering 修复，因为它们会直接影响客户端是否卡 running、是否丢 assistant partial、是否 replay 错 reasoning。
2. 选择性吸收 API server 的 `GET /v1/skills`、`GET /v1/toolsets` 思路，但不要让它绕过 Doxie gateway contract。
3. 选择性吸收 dashboard auth / WS ticket 设计，前提是先明确 Doxie sidecar token 与 upstream OAuth gate 的关系。
4. 吸收 skills/security hardening，但保持本地 profile-aware skills 自愈和 Doxie skill lifecycle。
5. 暂缓 Docker/s6、website/i18n、完整 dashboard 重构，除非当前客户端部署已经依赖这些场景。

如果要做下一步技术动作，建议开一个“小同步分支”，只处理客户端接入相关修复，而不是直接把 `upstream/main` 全量 merge 到当前接入分支。

## 主要更新主题

### 1. Dashboard Auth 和 Portal 集成

代表提交：

```text
a890389b6 feat(dashboard-auth): HERMES_DASHBOARD_PUBLIC_URL / dashboard.public_url override
61dcc3389 feat(dashboard-auth): config.yaml as canonical surface for dashboard.oauth
b26d81d53 feat(dashboard-auth): honour X-Forwarded-Prefix + __Host-/__Secure- cookies
034ad95fe fix(dashboard-auth): propagate next= through login page + PKCE cookie
b3dc53930 feat(dashboard-auth): Nous plugin always-on; default portal URL; specific error messages
2fc4615fc feat(dashboard-auth): Phase 7 - SPA AuthWidget + /api/status auth fields
5e9308b5b feat(dashboard-auth): Phase 6 - 401 re-auth envelope + next= propagation
8971e9483 feat(dashboard-auth): SPA WS auth - getWsTicket() + buildWsAuthParam()
b2360ba44 feat(dashboard-auth): _ws_auth_ok helper + ticket auth on all 4 WS endpoints
b69fce9c8 feat(dashboard-auth): single-use WS tickets + POST /api/auth/ws-ticket
848baeb0a feat(dashboard-auth): plugins/dashboard_auth/nous - contract-compliant Nous OAuth provider
```

新增/重点文件：

```text
hermes_cli/dashboard_auth/*
plugins/dashboard_auth/nous/*
web/src/components/AuthWidget.tsx
hermes_cli/web_server.py
hermes_cli/config.py
```

影响判断：

- Dashboard 从简单 token/本地访问模型，推进到 OAuth provider、PKCE、cookie prefix、single-use WebSocket ticket、SPA re-auth envelope。
- 这会影响本地 Doxie dashboard 嵌入、`/api/pty`、WebSocket bridge、sidecar 鉴权、public URL 和 reverse proxy prefix 处理。
- 合并时不能只解决 `hermes_cli/web_server.py` 行冲突，还要重新核对 Doxie sidecar token 与 upstream dashboard auth 是否并行、互斥或需要桥接。

本地核对点：

```sh
rg "dashboard_auth|ws-ticket|X-Forwarded-Prefix|AuthWidget|api/status|api/pty" hermes_cli web tui_gateway doxie_extension
```

### 2. Docker 运行时切换到 s6-overlay

代表提交：

```text
e0e9c895d feat(docker)!: replace tini with s6-overlay as PID 1
0abf661f7 feat(service_manager): add S6ServiceManager for runtime gateway supervision
2afefc501 feat(docker): per-profile s6 supervision + container-restart reconciliation
4b4c36cb6 feat(docker): remove gosu from bundled image; s6-setuidgid handles privilege drop
d4e452b67 fix(docker): SHA256-verify s6-overlay tarballs
f7893df4d fix(docker): support multi-arch s6-overlay install
4f416fc40 fix(docker): make s6 lifecycle work for unprivileged hermes user
27a29ee54 feat(docker): upgrade Node to 22 LTS
fb298a958 fix(docker): mkdir HERMES_HOME as root before chown
```

新增/重点文件：

```text
Dockerfile
docker/cont-init.d/*
docker/s6-rc.d/*
docker/stage2-hook.sh
hermes_cli/container_boot.py
hermes_cli/service_manager.py
tests/docker/*
```

影响判断：

- 容器内进程管理从单进程入口转为 s6 service tree。
- 上游加入 per-profile s6 supervision 和 container restart reconciliation。
- 本地如果依赖 Doxie runtime worker、sidecar gateway、dashboard 子进程，需要重新确认这些进程由谁拉起、谁监督、如何清理。
- `HERMES_HOME` 创建/权限、profile home、container boot hook 会影响 Doxie profile-scoped runtime。

本地核对点：

```sh
rg "S6ServiceManager|container_boot|s6|HERMES_HOME|profile" Dockerfile docker hermes_cli tui_gateway doxie_extension
```

### 3. 安全加固和 Promptware 防御

代表提交：

```text
249534e47 plugins: add security-guidance
5744b1757 harden: restrict markdown link schemes; parse untrusted XML with defusedxml
0dee92df2 feat(security): promptware defense - shared threat patterns + memory load-time scan + tool-result delimiters
c26af4681 fix(skills): reject symlinks in skill bundles before install
de76f4dbc fix(secrets): only apply external secrets once per HERMES_HOME per process
30928f945 fix(dashboard): suffix-allowlist plugin assets + denylist subprocess-influencing env vars
bd2756dd2 fix(update): reject symlink members in update ZIP
5f20322d2 fix(tts): reject '..' traversal in output_path
46d8b5dad fix(profile): reject symlinks in distributions
0d55315c3 fix(backup): skip symlinked files in zip archives
95848b1cb fix(transcription): reject symlinked audio inputs
ee59ef194 fix: reject read_file symlinks to blocking devices
b7b8bec80 fix(security): block /proc/*/environ, cmdline, maps from file read
ba3c45091 fix(security): block read_file on project-local .env files
79fc92e9c fix(security): tighten .env file permissions to 0600
4cb3eb03c fix(approval): harden YOLO bypass, LLM parsing, auto-approve audit, pipe pattern
3ab7e2aa9 harden(env_passthrough): apply GHSA-rhgp-j443-p4rf filter
5faea3f61 fix(file_tools): reject '..' traversal in V4A patch headers
00bd24e27 fix(security): expand memory content scanning
```

新增/重点文件：

```text
plugins/security-guidance/*
agent/file_safety.py
agent/system_prompt.py
agent/redact.py
hermes_cli/security_audit.py
tools/file_tools.py
tools/skills_hub.py
```

影响判断：

- 上游继续把不可信输入、防 symlink、防 path traversal、防 prompt injection 扩展到 memory、skills、profile distribution、backup、TTS、transcription、read_file、dashboard plugin assets。
- 本地 Doxie 的文档解析、artifact registry、workspace 文件展示、skill package lifecycle、desktop browser bridge 都要继承这些安全语义。
- 合并时不能用本地旧实现绕过上游的安全入口，尤其是 ZIP install、附件解析、workspace artifact path、profile-scoped HERMES_HOME。

本地核对点：

```sh
rg "symlink|path traversal|promptware|security|defusedxml|read_file|\\.env|artifact|parse_document" agent tools hermes_cli tui_gateway doxie_extension tests
```

### 4. Codex Responses、Streaming 和 Conversation Loop 修复

代表提交：

```text
9c69204d8 fix(codex_responses_adapter): drop foreign-issuer reasoning on replay
b1a46b304 fix(codex): drop transient rs_tmp reasoning replay state
4920f8437 test(codex): cover null output stream terminal events
4243b6dc4 fix(codex): update silent-hang workaround hint
cb38ce28c refactor(codex): drop SDK responses.stream() helper; consume events directly
b6ca56f65 fix(codex-responses): gracefully recover from invalid_encrypted_content
43a3f119f fix(agent): recover Codex streams with null output
8601c4d44 fix(codex): add time-to-first-byte watchdog
ac5359a3f fix(streaming): route mid-tool-call partial-stream-stub through length continuation
2d422720b fix(codex): size and propagate timeouts
9140be7c2 fix(streaming): emit finish_reason=length on text-only partial-stream stub
20b3703a4 fix(conversation-loop): tailor length-continuation prompt
775a17284 fix(transport): strip Hermes-internal scaffolding keys before chat.completions
3d66787a0 fix(vision): route auxiliary.vision.provider=openai to api.openai.com, skip text-only main
```

重点文件：

```text
agent/codex_responses_adapter.py
agent/transports/codex.py
agent/transports/chat_completions.py
agent/conversation_loop.py
agent/tool_executor.py
agent/tool_dispatch_helpers.py
run_agent.py
```

影响判断：

- 这部分直接影响 Doxie run control、partial assistant persistence、tool events、conversation metadata、reasoning replay 和内部 scaffold 字段过滤。
- 本地 `turn_id` / `run_id` / `runtime_scope_key` / `client_message_id` 等 metadata 不能进入 provider 请求，但必须继续留在 DB、event stream 和 UI transcript 里。
- 上游加入 null output stream、time-to-first-byte watchdog、invalid encrypted content recovery，合并后要确认本地结构化流不会重新吞掉 terminal events 或造成 UI running 卡住。

本地核对点：

```sh
rg "reasoning|rs_tmp|finish_reason|partial-stream|metadata|turn_id|run_id|runtime_scope_key|time-to-first-byte" agent run_agent.py tui_gateway hermes_state.py doxie_extension
```

### 5. Gateway API、Messaging 平台和平台插件迁移

代表提交：

```text
464b51d45 Support media in session chat API
f7527b0fd feat: add API server session controls
25f43d38d feat(api-server): add GET /v1/skills and /v1/toolsets
96223265b chore(api-server): mark skills_api capability True
f05a47309 fix(gateway): refresh cached agent tools on /reload-mcp
60f84c6c2 gateway: quiet Telegram operational chatter
efa952531 fix: ignore Telegram start pings
8807b1c72 fix(gateway): hide telegram compaction status noise
```

结构变化：

```text
gateway/platforms/discord.py    -> plugins/platforms/discord/adapter.py
gateway/platforms/mattermost.py -> plugins/platforms/mattermost/adapter.py
plugins/platforms/ntfy/*
```

重点文件：

```text
gateway/platforms/api_server.py
gateway/run.py
gateway/session.py
gateway/session_context.py
gateway/stream_consumer.py
hermes_cli/plugins.py
hermes_cli/plugins_cmd.py
```

影响判断：

- API server session controls 和 media chat API 与本地 Doxie session/run/attachment 语义有交集。
- Discord、Mattermost 迁移到 bundled platform plugins，说明上游继续把平台能力从 core gateway 抽出去。
- 本地若有 Doxie 平台或 gateway hook，不应重新塞进 core；应继续沿用 extension/plugin 边界。
- `/reload-mcp` 工具缓存刷新会影响当前 agent tool schema 的动态更新，本地 Doxie extension tools 也应纳入核对。

本地核对点：

```sh
rg "session controls|media|skills_api|toolsets|reload-mcp|platforms" gateway hermes_cli plugins tui_gateway doxie_extension model_tools.py
```

### 6. Skills Hub、MCP Catalog 和可选能力扩展

代表提交：

```text
8b69ec03a feat(mcp): Nous-approved MCP catalog with interactive picker
d8703e27f feat(skills-hub): health checks, freshness badge, and watchdog cron
cea87d913 fix(skills-hub): show every catalog source on /docs/skills
263e008d6 feat(skills): add web-pentest optional skill
386f245d9 feat(skills): add optional openhands skill
5671461c0 feat(skills): add code-wiki skill
```

新增/重点文件：

```text
hermes_cli/mcp_catalog.py
hermes_cli/mcp_picker.py
optional-mcps/linear/manifest.yaml
optional-mcps/n8n/manifest.yaml
optional-skills/security/web-pentest/*
optional-skills/autonomous-ai-agents/openhands/SKILL.md
optional-skills/software-development/code-wiki/*
tools/skills_ast_audit.py
tools/skills_hub.py
```

影响判断：

- 上游 Skills Hub 不只是安装列表，已经包含健康检查、freshness badge、watchdog cron、catalog source 展示和 MCP picker。
- 本地曾经补过 skills 目录初始化自愈、skill package lifecycle、Doxie profile-aware skills 行为；合并时要接入上游 canonical lifecycle，而不是保留一套平行安装语义。
- 新 optional skills 和 MCP catalog 会增加 docs/website 生成内容，合并时要接受对应站点产物或重新生成。

本地核对点：

```sh
rg "skills_hub|SkillBundle|watchdog|freshness|optional-mcps|mcp_catalog|ensure_directory_path|skill package" hermes_cli tools tui_gateway doxie_extension tests
```

### 7. Provider、Model Catalog、TTS/STT/Image Gen

代表提交：

```text
a6b0414ea feat(providers): extend openai-api with live /v1/models fetch + gpt-5.5-pro
aeb87508c feat(providers): add OpenAI API provider option
ccd3d04fc chore(models): swap qwen3.6-plus -> qwen3.7-max
f0fdb5e67 feat(catalog): add qwen3.7-max
febc4cfec remove Vercel AI Gateway and Vercel Sandbox
2fc77c53f feat(opencode-go): route qwen3.7-max via anthropic_messages
```

新增/重点文件：

```text
agent/tts_provider.py
agent/tts_registry.py
agent/transcription_provider.py
agent/transcription_registry.py
plugins/image_gen/fal/*
plugins/model-providers/opencode-zen/*
hermes_cli/providers.py
agent/usage_pricing.py
```

影响判断：

- 上游 provider surface 更插件化，OpenAI API provider 有 live model fetch。
- Vercel AI Gateway / Sandbox 被删除，本地如果还有配置或文档引用需要清理。
- 新 TTS/STT registry 说明多媒体 provider 开始走统一 provider 抽象；Doxie 如果有语音/转写/图片代理，后续应该对齐 registry，而不是绕开。

本地核对点：

```sh
rg "openai-api|gpt-5.5-pro|qwen3.7|max|Vercel|tts_registry|transcription_registry|image_gen" agent hermes_cli plugins tools tests
```

### 8. TUI、ui-tui 和 TUI Gateway

代表提交：

```text
0a83247e9 feat: add TUI session orchestrator
50aaf0c4a fix(tui): delineate assistant responses from details
874c2b1fe fix(tui): ignore late thinking deltas after completion
```

重点文件：

```text
ui-tui/src/*
tui_gateway/server.py
web/src/pages/ChatPage.tsx
```

影响判断：

- `ui-tui` 有大量活跃 session switcher、status rule、virtual history、hotkey、text input、Ctrl-J、mouse handling 相关更新。
- `tui_gateway` 本次上游变化列表里只有 `tui_gateway/server.py`，但这个文件对本地分支风险最高，因为本地已经把 server 行为拆到 `methods/` 和 `services/`。
- 合并时要把上游新增 TUI 行为迁移到 extracted method/service 边界，而不是直接用上游 `server.py` 覆盖本地结构。

本地核对点：

```sh
rg "session orchestrator|active session|thinking|message.delta|approval|slash.exec|runtime.ensure|method_registration" ui-tui tui_gateway doxie_extension tests
```

### 9. Website、Docs、i18n

变化规模：

```text
539 website files
16 locales files
大量 website/i18n/zh-Hans 内容
```

重点变化：

- 文档站新增大量当前功能页和中文本地化内容。
- Dashboard auth、Docker s6、API server、providers、skills、messaging、security 等主题有新文档。
- `scripts/build_skills_index.py` 和 skills index workflow 更新，说明 docs 产物与 skills catalog 更紧密绑定。

影响判断：

- 合并时文档产物体量很大，但大部分不是本地运行时冲突核心。
- 如果本地 Doxie 文档也改动 website/docs，需避免被生成产物覆盖。
- 若接受上游 docs 产物，建议后续单独补一份 Doxie fork 同步说明，而不要在大 merge 中混入产品文档重写。

## 高风险文件清单

下面这些文件在本地分支中通常承载 Doxie 关键行为，同时也在本次上游更新中发生变化。合并时应逐个核对，不能只按冲突标记机械解决。

```text
run_agent.py
agent/agent_init.py
agent/auxiliary_client.py
agent/codex_responses_adapter.py
agent/conversation_loop.py
agent/file_safety.py
agent/prompt_builder.py
agent/system_prompt.py
agent/tool_dispatch_helpers.py
agent/tool_executor.py
cron/jobs.py
cron/scheduler.py
gateway/platforms/api_server.py
gateway/run.py
gateway/session.py
gateway/session_context.py
hermes_cli/config.py
hermes_cli/plugins.py
hermes_cli/plugins_cmd.py
hermes_cli/tools_config.py
hermes_cli/web_server.py
hermes_constants.py
hermes_state.py
tools/browser_tool.py
tools/computer_use/backend.py
tools/computer_use/tool.py
tools/skills_hub.py
tools/skills_sync.py
tui_gateway/server.py
toolsets.py
ui-tui/src/*
web/src/*
```

## 对本地 Doxie 实现的合并风险

### 风险 A：Dashboard Auth 覆盖本地 sidecar / WebSocket 鉴权

上游现在有 OAuth、PKCE、cookie prefix、WS ticket 和 SPA re-auth。Doxie 本地 sidecar 如果继续依赖自己的 token auth，需要明确两套机制的层级：

- Doxie desktop / sidecar 内部请求如何通过 upstream dashboard auth。
- `runtime.ensure`、profile-scoped worker bridge、approval / cron control RPC 是否应该走 WS ticket。
- `/api/pty` 的 auth query 参数和 Doxie token 是否冲突。

### 风险 B：s6 service manager 改变 runtime 生命周期

上游新增 `S6ServiceManager` 和 per-profile supervision。Doxie runtime worker 不能继续假设所有长生命周期进程都由当前 Python process 直接拥有。

需要重新核对：

- profile runtime worker 启动、停止、重启是否落到正确 profile。
- control plane 与 runtime worker 的 WebSocket bridge 是否在 container restart 后恢复。
- Docker 权限、`HERMES_HOME`、profile home、logs 目录是否仍 profile-aware。

### 风险 C：Conversation Loop 修复覆盖 Doxie turn metadata

上游修复了 provider replay、stream continuation 和 internal scaffold filtering。本地需要保留：

- provider 请求前剥离 Hermes/Doxie 内部字段。
- DB/event/transcript 内保留 `turn_id`、`run_id`、`runtime_scope_key`、`client_message_id`。
- partial assistant、terminal event、tool event 都能按 run/turn 关联。

### 风险 D：API Server Session Controls 与 Doxie Run Control 重叠

上游 API server 新增 session control 和 media session chat。本地 Doxie run control 已有更强的 run registry/event replay/interrupt/cancel 语义。

合并时要明确：

- 上游 API server 的 session controls 是否应调用 Doxie run control。
- media session chat 和 Doxie prompt attachments/document parse 如何共享附件模型。
- `GET /v1/skills`、`GET /v1/toolsets` 是否应包含 Doxie extension tools/toolsets。

### 风险 E：Skills Hub 安全生命周期与本地 skill package lifecycle 重复

上游已经强化 bundle provenance、quarantine、scan、symlink 防护和 watchdog。本地 ZIP 导入、跨 home 复制、skills 目录自愈应尽量复用上游生命周期。

不能接受的退化：

- 直接移动 ZIP 解包结果绕过 scan/quarantine。
- 遇到 `$HERMES_HOME/skills` 损坏成文件时直接删除用户数据。
- Doxie profile skills 与默认 Hermes home 混写。

### 风险 F：TUI Gateway 单文件上游更新覆盖本地模块化

上游 `tui_gateway/server.py` 是本次唯一 TUI Gateway 文件变化，但本地能力散布在 `methods/`、`services/` 和 `doxie_extension`。

合并原则：

- 接受上游行为修复，但落到本地 extracted method/service。
- 保留 `_EXTRACTED_METHOD_OVERRIDES`、extension method override、runtime proxy、run control、profile context。
- `server.py` 继续作为协调层，不重新膨胀成巨型行为文件。

## 建议合并顺序

1. 先只合并上游基础，不做 Doxie 新需求。
2. 优先解决 `hermes_state.py`、`run_agent.py`、`agent/conversation_loop.py`、`tui_gateway/server.py`、`hermes_cli/web_server.py`、`toolsets.py`。
3. Dashboard auth 和 Doxie sidecar 分开验证，避免一次性混合调试 OAuth、PTY、runtime worker。
4. Docker/s6 单独验证容器启动，不要和普通本地 venv 测试混在一起定位。
5. Skills Hub/skill package lifecycle 单独核对安全路径，确保 symlink/path traversal 防护没有被本地 wrapper 绕开。
6. Gateway API/session/media 与 Doxie run control 做契约对齐，再补测试。

## 最小验证清单

合并后建议先跑这些快速检查：

```sh
git diff --check
python -m compileall agent hermes_cli gateway tools tui_gateway doxie_extension
```

TUI Gateway / Doxie runtime：

```sh
scripts/run_tests.sh tests/test_tui_gateway_server.py tests/test_tui_gateway_ws.py tests/tui_gateway
```

Conversation / Codex / streaming：

```sh
scripts/run_tests.sh tests/agent tests/run_agent
```

Gateway API / platform：

```sh
scripts/run_tests.sh tests/gateway
```

Skills / MCP：

```sh
scripts/run_tests.sh tests/tools/test_skills_sync.py tests/tools/test_skills_hub.py tests/hermes_cli
```

Tools / Browser / Computer use：

```sh
scripts/run_tests.sh tests/tools/test_computer_use.py tests/tools/test_file_sync.py tests/tools
```

Docker/s6 如果改到容器路径：

```sh
scripts/run_tests.sh tests/docker
```

前端如果接受 `web/` 或 `ui-tui/` 大量变化：

```sh
cd ui-tui && npm run type-check && npm test
```

## 后续追踪命令

查看完整非 merge 提交：

```sh
git log --oneline --no-merges 1264fab15..upstream/main
```

查看文件级变化：

```sh
git diff --name-status 1264fab15..upstream/main
```

按目录看影响面：

```sh
git diff --dirstat=files,0 1264fab15..upstream/main
```

只看本地高风险文件：

```sh
git diff --name-status 1264fab15..upstream/main -- \
  run_agent.py \
  hermes_state.py \
  hermes_cli/web_server.py \
  tui_gateway \
  agent \
  gateway \
  tools \
  toolsets.py \
  cron
```

## 本次报告生成依据

使用的本地命令：

```sh
git status --short --branch
git log --oneline --decorate --no-merges 1264fab15..upstream/main
git rev-list --count 1264fab15..upstream/main
git diff --shortstat 1264fab15..upstream/main
git diff --dirstat=files,0 1264fab15..upstream/main
git diff --name-status 1264fab15..upstream/main
git diff --name-only 1264fab15..upstream/main
```
