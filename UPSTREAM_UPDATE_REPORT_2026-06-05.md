# Hermes upstream/main 更新报告 - 2026-06-05

## 范围

本报告分析的是本地重新执行 `git fetch upstream main --tags` 后，Hermes 上游 `main` 分支相对上一轮已知上游基线的增量。

当前本地分支：

```text
codex/selective-upstream-client-sync-20260527
```

上游增量范围：

```text
4feb181eb..96cd37e21
```

范围两端：

```text
4feb181eb 2026-05-27 02:41:24 -0700 chore(release): map sir-ad + rdasilva1016-ui in AUTHOR_MAP
96cd37e21 2026-06-04 19:50:33 -0700 fix(dashboard): reap orphaned embedded-chat sessions to stop slash_worker leak
```

当前确认的 upstream/main：

```text
96cd37e21 fix(dashboard): reap orphaned embedded-chat sessions to stop slash_worker leak
v2026.5.29-834-g96cd37e21
```

本报告初始版本只做 fetch 后的上游更新分析和 Dovie 产品吸收建议；创建报告时没有执行 `merge`、`rebase` 或代码合并。后续吸收进度见下方“实施进度”。

## 实施进度

截至 2026-06-05，第一批 P0 稳定性和安全已按“手工迁移、不全量覆盖”的方式吸收到本地代码：

- 已完成：TUI/Gateway 并发锁和恢复、Zombie agent / session reset race、审批安全、Cron 非阻塞和清理、State/WAL 可靠性、MCP shutdown/probe 稳定、Vision pixel cap。
- 已验证：P0 聚焦测试 `560 passed, 11 skipped`；完整 `tests/tools/test_mcp_tool.py` 为 `196 passed`；`py_compile`、`git diff --check`、`uv lock --check` 通过。
- 第二批 P1 Branch / Session / Search 已完成：吸收 `messages.active` + rewind/undo 软删除原语、`/undo [N]`/`/rewind` TUI prefill 合同、SQL-bounded session-id search、Web session search 的 ID 优先和 compression-lineage 去重；Dovie `session_lineage` branch 可见性通过回归测试保护，没有引入上游 `_branched_from` 标记或简单 ancestor replay。
- 第二批已验证：`tests/test_hermes_state.py` 为 `241 passed`；P1 合并验证 `397 passed`；TUI Vitest `57 passed`；`py_compile`、`git diff --check` 通过。`npm run type-check` 仍失败在既有 `packages/hermes-ink/src/utils/execFileNoThrow.ts` Node child_process overload 类型问题，和本批 slash/prefill 改动无关。
- 第三批 P1/P2 Tool / Vision / Dashboard 局部稳定性已完成：吸收 MCP non-MCP endpoint / HTML content-type fast-fail、approval 和 file_tools 对 Hermes config/env 写入防护、Vision pixel cap 和 native provider 缩图恢复、Dovie visible browser bridge source URL 透传、Web session search API、`system.search`、session undo/rewind command prefill，以及 cron parallel pool / profile cwd 稳定性。
- 当前发布节点：`0.9.2-20260605`。本节点仍是选择性吸收结果，不代表 full merge upstream/main；未进入的内容包括上游 React Desktop、Bootstrap installer、完整 Dashboard OAuth / remote gateway auth、Channels UI、Skills 全量 catalog 和 progressive tool disclosure。

## 更新规模

```text
989 commits
2159 files changed
223468 insertions(+)
46023 deletions(-)
```

按顶层目录统计的主要变化：

```text
apps                 430 files   80040 insertions
tests                815 files   52675 insertions
package-lock.json      1 file    22226 insertions
hermes_cli            80 files   19339 insertions
web                   74 files    9660 insertions
website              263 files    6048 insertions
tools                 48 files    5889 insertions
plugins               59 files    4939 insertions
gateway               28 files    4659 insertions
agent                 51 files    4026 insertions
ui-tui                59 files    3019 insertions
tui_gateway            3 files    1684 insertions
hermes_state.py        1 file     1105 insertions
```

提交类型分布：

```text
535 fix
123 feat
67 chore
58 test
58 docs
26 refactor
22 style
8 perf
7 ci
```

高频主题：

```text
71 fix(desktop)
51 fix(gateway)
37 fix(docker)
33 feat(desktop)
21 fix(tui)
21 style(desktop)
16 fix(dashboard)
10 fix(mcp)
10 fix(auth)
10 fix(tools)
9 fix(vision)
7 feat(skills)
6 fix(security)
6 fix(cron)
```

## Release 节点

本次范围跨过两个明确 release tag：

### v2026.5.28 / v0.15.0

上游 release note 摘要：

```text
The Velocity Release.
run_agent.py 16k -> 3.8k LOC refactor
kanban grows into a multi-agent platform
cold-start perf wave
session_search 4500x faster
promptware defense
Bitwarden Secrets Manager
Krea + FAL plugin
Nous-approved MCP catalog
OpenHands skill
ntfy as 23rd platform
deep xAI round
```

对 Dovie 的含义：

- Agent loop、tool execution、provider streaming 继续大规模重构，必须选择性吸收稳定性修复，不能直接覆盖本地 Dovie event contract。
- `session_search`、MCP catalog、promptware defense、Secrets Manager 和 image/video provider 对产品有价值，但需要经过 Dovie 的权限、分身、技能广场和云代理边界。

### v2026.5.29 / v0.15.1

上游 release note 摘要：

```text
Same-day hotfix for v0.15.0.
dashboard infinite-reload loop in loopback mode
kanban worker SIGTERM
/model picker unification
/yolo session bypass
skills.sh full catalog
.md media delivery restore
gateway probe-stepdown safety
web URL redaction passthrough
kanban worker vision on referenced images
hindsight observation-default
Docker hardening
```

对 Dovie 的含义：

- Dashboard loopback、gateway probe、media delivery、model picker 和 yolo/approval 相关修复应进入候选吸收清单。
- Docker/kanban/hindsight 只在 Dovie 产品路线需要时再吸收。

## 总体结论

这次上游更新不是一次小补丁，而是跨 Desktop、Dashboard、Gateway、TUI、Docker、Installer、Agent runtime、State、Cron、MCP、Skills、Provider、Vision、安全和官网文档的综合推进。

对 Dovie 来说，当前仍不建议全量合并 `upstream/main`。主要原因：

- 上游新增了完整 React Desktop 和 Dashboard 产品面，和 Dovie 当前 Vue/Electron 客户端信息架构不一致。
- 上游 `tui_gateway/server.py` 仍承载大量单文件实现，而本地 Dovie fork 已经将 Gateway 拆成 `methods/services` 并建立了 Dovie extension、runtime proxy、run control、artifact、workspace 和 profile-scoped worker 边界。
- 上游新增 `session.branch` 语义，但实现偏向 TUI 内部历史复制；Dovie 当前正在实现更强的 branch-point、workspace、runtime scope、idempotency 和 session lineage contract，不能被上游简单实现覆盖。
- 上游 Dashboard / OAuth / WS ticket / remote gateway auth 很活跃，但 Dovie 客户端还有自己的登录态、本地 sidecar token、profile worker token 和云端模型代理策略，需要先统一设计。

正确策略仍是：

```text
不做全量 merge。
按 P0/P1 选择性吸收运行时稳定性、安全修复、会话恢复、审批、cron、skills/tools/provider 能力。
继续保留 Dovie 本地客户端接入边界。
```

## 本地 0.9.2 发布工作区提醒

本地 Hermes fork 当前 staged 改动已经从单一 `session.branch` 扩展为 0.9.2 选择性吸收批次。后续同步上游时要优先保护：

- `hermes_state_branch.py` 和 `hermes_state.py` 中的 Dovie branch lineage、branch request idempotency、`messages.active` soft-delete、compression continuation 与 user-created branch 的区分。
- `tui_gateway/methods/session_branch.py`、`tui_gateway/methods/session.py`、`tui_gateway/methods/prompt.py`、`tui_gateway/methods/system.py` 中模块化后的 branch / undo / rewind / search / prefill contract。
- `dovie_extension/gateway_methods.py` 和 `dovie_extension/manifest.py` 中暴露给 Dovie 客户端的 method manifest / overrides。
- `cron/scheduler.py` 中非阻塞 tick、parallel pool、profile cwd/env 传播和 completed output 清理逻辑；Dovie automation 仍不回退到原生 cron 主入口。
- `tools/approval.py`、`tools/file_tools.py`、`tools/mcp_tool.py`、`tools/vision_tools.py` 中的安全和稳定性修复，特别是 Hermes config/env 写入防护、MCP preflight fast-fail、Vision pixel cap / shrink recovery。
- `hermes_cli/web_server.py` 的 session search API 和 `web/src/lib/slashExec.ts` / `ui-tui` 的 slash prefill 合同；前端收到 undo/rewind prefill 后只填充 composer，不自动提交。

## 上游更新分组

### 1. Desktop / Dashboard / Installer 大规模新增

代表变化：

- 新增 `apps/desktop/**`，包含 Electron main/preload、React app、Chat、Sidebar、Profile rail、Settings、Command Palette、Cron、Messaging、Skills、Right Sidebar、Artifacts、Providers 等完整桌面客户端。
- 新增 `apps/bootstrap-installer/**`，Tauri installer、安装/更新脚本、Windows/macOS 图标与打包配置。
- Dashboard 管理面扩展：Channels、MCP、Pairing、System、Webhooks、Sessions、Skills Hub、Profiles 等。
- Desktop 交互修复：Cmd+K / Cmd+P、session search by id、profile rail、remote gateway login、approval/clarify prompt 展示、composer Enter/IME/scroll、thinking block 流式状态、session rename、profile switching、remote backend auth 等。

对 Dovie 的判断：

- 不吸收上游 React Desktop 代码。Dovie 已有自己的 Vue/Electron 产品面和视觉规范。
- 可选择性借鉴交互设计：Cmd+K 跳会话、session search by id、background needs-input indicator、approval/clarify 不被折叠隐藏、remote gateway 登录失败提示。
- Installer / update 逻辑只在 Dovie 后续要做 Hermes runtime 独立安装器时再研究，不作为当前客户端前置项。

### 2. Gateway / TUI / 会话恢复稳定性

代表提交：

```text
5bcb63e40 fix(tui): add thread-safety locks for _sessions and prompt dicts
98903d031 fix(tui): reuse live session on resume
bd6d09876 fix(tui): keep resumed live history current
8077e7d2f fix(tui): narrow resume lock to avoid blocking session.close
e7a7872a8 fix(tui_gateway): dedup re-queued process notifications flooding TUI
693f4c7e9 fix(gateway): clear zombie agent slot when session_reset races in-flight run
787936d13 feat(gateway): structured stream-event protocol + Telegram draft formatting parity
```

对 Dovie 的价值：

- 直接影响客户端 running 状态、历史恢复、后台会话切回、进程通知、session reset race。
- Dovie 的 `runtime-scoped run events`、`events.subscribe`、`session.resume/messages` 可以吸收这些修复背后的并发策略。

吸收方式：

- 手工迁移语义和测试，不直接 cherry-pick 覆盖 `tui_gateway/server.py`。
- 把锁、resume、session close、notification dedup 放入现有 `tui_gateway/methods/*` 和 `tui_gateway/services/*`。

### 3. Session branch / undo / rewind / search

代表提交：

```text
3e59be0c4 feat(state): add messages.active flag + rewind primitives
243e836dc feat(tui): wire /rewind through command.dispatch + prefill payload
3f7d1c801 feat(undo): /undo [N] backs up N user turns with prefill + soft-delete
a3fb48b2c fix(state): keep /branch sessions visible after parent reopen
580d92409 perf(desktop): make session-id search SQL-bounded, not O(n)
9ecc331be feat(desktop): search sessions by id
```

上游 `session.branch` 当前形态：

- 从当前 runtime history 复制消息到新 session。
- `parent_session_id` 指向旧 session。
- 用 `_branched_from` marker 保证 branch 在 session list 中可见。
- 没有 Dovie 当前需要的明确 branch point、workspace binding、runtime scope、idempotency request、branch lineage 表。

对 Dovie 的价值：

- branch 可见性、search by id、undo/rewind soft delete 语义有参考价值。
- 上游的 `parent_session_id` 处理会和 compression continuation 混在一起，Dovie 不能直接照搬。

吸收方式：

- 保留本地 Dovie `session_lineage` / `session_branch_requests` 设计。
- 上游 `_branched_from` 可见性思路可映射为 Dovie session list item metadata。
- `session.search by id` 和 SQL bounded 查询可吸收到 Dovie 历史列表和 branch picker。

### 4. 安全 / 审批 / 凭据边界

代表提交：

```text
25742372e fix(approval): check is_approved in execute_code guard (#39275)
4e9d886d9 fix(approval): pair terminal-side gate for ~/.hermes/config.yaml writes
8f2931e3e fix(file_tools): block agent writes to ~/.hermes/config.yaml to prevent silent approval bypass
a6a4e6f9d fix(approval): gate perl/ruby -i in-place edits of Hermes config/env
b04c6e95f fix(approval): catch perl/ruby -i as a separate flag token
95b5b7240 fix(security): block AWS SDK creds from subprocess env
6bebab476 fix(security): narrow Bedrock subprocess strip to inference bearer token only
c60952ba9 fix(web): run URL SSRF checks off the event loop in async paths
1495f0cc3 fix(file-safety): extend sandbox-mirror guard to cover inner-container path
162c7856c fix(file-safety): add sandbox-mirror soft guard for writes to per-task .hermes mirrors
```

对 Dovie 的价值：

- 这些属于生产客户端安全基线，应优先吸收。
- 特别是 approval guard 与 config/env 写入防护，直接影响用户本地 Hermes home、Dovie profile home、runtime config 的安全性。

吸收方式：

- 手工迁移到 `tools/approval.py`、`tools/code_execution_tool.py`、`tools/file_tools.py`、`agent/file_safety.py`。
- Dovie 需要额外确认 profile-scoped Hermes home / version runtime home 是否也被同等保护。

### 5. Cron / 自动化任务

代表提交：

```text
eb9cde734 fix(cron): decouple job dispatch from completion in tick()
9fbfeb31b fix(cron): make sequential jobs non-blocking too + sweep MCP after jobs finish
30412a977 fix(cron): re-validate stale cron-output entries before deletion
2c0d64839 fix(cron): sanitize invisible unicode in vetted skill content instead of hard-blocking
bd72d333d fix(gateway,cron): reuse existing _HERMES_GATEWAY marker; tighten cron regex
5cd6c1717 fix(gateway,cron): prevent agent restart loops via self-targeting gateway commands
ae5b2de2f fix: expand skill bundles in cron jobs
```

对 Dovie 的价值：

- Dovie 自动化任务目前是产品级 contract，不应回退到原生 `cronjob` 工具主入口。
- 但 scheduler 非阻塞、MCP sweep、stale output revalidation、gateway self-targeting 防循环都很有价值。

吸收方式：

- 迁移 scheduler 行为，不改变 `tools/dovie_automation_task_tool.py` 和 `tui_gateway/services/dovie_cron_jobs.py` 的产品边界。
- 保持 raw cron sessions 对 Dovie session list 隐藏。
- 自动化任务结果仍通过 Dovie current-session / new-session result binding 展示。

### 6. Tools / MCP / Skills

代表提交：

```text
369075dc9 feat(tools): progressive tool disclosure for MCP and plugin tools
7427b9d58 fix(tool-search): scope bridge catalog + dispatch to the session's toolsets
fb9f3a4ef fix(skills): pull full ClawHub catalog into the skills index (200 -> 20k+)
7050c052e fix(skills): pull full skills.sh catalog via sitemap (858 -> 19,932)
38d3c49aa refactor(skills): clean up bundled skill set + add environments: relevance gate
751b91446 fix(mcp): ensure server.shutdown() on probe iteration failure
e9529578d fix(mcp): widen shutdown_mcp_servers exception guard to BaseException
64f7f3671 fix(mcp): make non-MCP HTTP endpoint fast-fail robust and non-retryable
c914e4a37 fix(mcp): fail fast on HTML content-type instead of waiting full connect_timeout
```

对 Dovie 的价值：

- Progressive tool disclosure 和 `tool_search` 可减少大工具面带来的上下文成本，也能支撑 Dovie 分身按能力动态发现工具。
- Skills catalog 扩展对技能广场有价值，但不能绕过 Dovie package / binding 生命周期和 profile runtime snapshot。
- MCP probe/shutdown 修复应吸收，避免工具进程和子进程泄漏。

吸收方式：

- 工具发现必须按 Dovie `runtime_scope_key`、profile toolset、父子 agent 精确 allowlist 过滤。
- 技能安装、启停、导入仍走 Dovie `skill package lifecycle`，只吸收 catalog 抓取、安全扫描、自愈逻辑。

### 7. Provider / Model / Vision / Media

代表提交：

```text
f8b8dffcc fix(browser): add native image support to browser_vision and respect supports_vision
f05353397 fix(vision): respect supports_vision in vision_analyze
c77a697fa refactor(vision): consolidate native fast-path gate into one shared helper
6bdbe3076 fix(vision): guard image pixel dimensions, not just bytes
dd4ba4c2c fix(vision): cap pixel dimensions proactively at embed time + declare Pillow
153fe2847 fix(vision): use MiniMax type="video" block (not input_video) + tests
0b46c4163 fix(vision): convert video_url blocks to Anthropic input_video format for MiniMax providers
e3b3d4d75 feat(models): add MiniMax-M3 to native minimax providers + 1M context
fd87c6107 feat(models): add qwen/qwen3.7-plus to nous + openrouter catalogs
3a9bc9d88 fix(model picker): unify /model and `hermes model` lists, add disk cache
```

对 Dovie 的价值：

- Dovie AIGC 产品需要强 vision/media 支持，这部分应作为 P1 吸收。
- 模型 catalog 变化必须经过 Dovie 云端模型代理和价格/能力元数据，不应直接暴露上游 provider key 或用户本地 provider config。

吸收方式：

- `supports_vision`、pixel cap、native image routing 可以进入 Hermes runtime。
- Dovie 前端模型选择仍以云端 model inventory 为准，Hermes runtime 只接收短期代理 token 和 Dovie 注入的模型能力描述。

### 8. Remote messaging / Channels

代表提交：

```text
f3bbfda6d feat(gateway): handle Feishu meeting invitations
731475787 refactor(feishu): slim meeting-invite parser
74e845c00 fix(slack): pass thread_ts in standalone send_message tool path
454d6cbe5 fix(telegram): finalize sealed overflow chunk so split streamed replies render formatting
5407d2559 Fix Telegram DM topic text batch keying
100536134 refactor(gateway): generalize topic recovery via adapter hook
db96fc60d fix(gateway): keep Telegram topic bindings aligned with compression children
566669013 fix(weixin): replace aiohttp ClientTimeout with asyncio.wait_for in _api_post/_api_get
```

对 Dovie 的价值：

- Feishu / WeCom / Slack / Telegram 的 gateway 修复与 Dovie 远程连接页相关。
- 但 Dovie Channels 产品形态不同，不能直接套上上游 Dashboard Channels 页面。

吸收方式：

- 优先吸收 adapter 层协议修复和重连/路由逻辑。
- Dovie UI 数据源仍由 `services/hermes` 适配层统一包装，平台配置、登录态和状态展示按 Dovie 产品语义呈现。

### 9. Docker / Installer / Packaging

代表提交：

```text
0927fb558 feat(docker): auto-redirect `gateway run` to supervised mode inside s6 image
b34532319 fix(docker): tee supervised gateway stdout to docker logs
66489f38c fix(docker): bake build-time git SHA into the image
48083211e fix(docker): accept PUID/PGID as aliases for HERMES_UID/HERMES_GID
475ecea3d fix(install): cap requires-python at <3.14 and pin UV_PYTHON to the venv
c349eca82 fix(packaging): ship locales/ i18n catalogs in wheel, sdist, and Nix
2765b0202 fix(packaging): ship bundled plugin.yaml manifests in wheel and sdist
```

对 Dovie 的价值：

- `requires-python <3.14`、wheel/sdist/Nix 包含文件修复、plugin manifest 打包修复值得关注。
- Docker/s6 主要服务上游部署形态，不是 Dovie 桌面客户端当前主线。

吸收方式：

- Dovie 打包 Hermes runtime 时优先审计 `pyproject.toml`、`uv.lock`、MANIFEST/package data、native deps。
- Docker 修复除非 Dovie 后续支持 Docker runtime，否则暂缓。

## 面向 Dovie 的吸收优先级

### P0：必须优先吸收

这些直接影响 Dovie 客户端稳定性、安全性或用户可见 running 状态。

| 主题 | 代表提交 | 建议方式 | 风险点 |
|---|---|---|---|
| TUI/Gateway 并发锁和恢复 | `5bcb63e40`, `98903d031`, `bd6d09876`, `8077e7d2f` | 手工迁移 | 不能覆盖模块化 gateway |
| Zombie agent / session reset race | `693f4c7e9` | 手工迁移 | run terminal event 必须保留 |
| 审批安全 | `25742372e`, `4e9d886d9`, `8f2931e3e`, `a6a4e6f9d`, `b04c6e95f` | 手工迁移 | Dovie profile home 也要覆盖 |
| Cron 非阻塞和清理 | `eb9cde734`, `9fbfeb31b`, `30412a977` | 手工迁移 | 不恢复原生 cronjob 主入口 |
| State/WAL 可靠性 | `46b2afc56` | 手工迁移 | 需兼容本地 schema version |
| MCP shutdown/probe 稳定 | `751b91446`, `e9529578d`, `64f7f3671`, `c914e4a37` | 手工迁移 | 不能影响 Dovie managed tool bridge |
| Vision pixel cap | `6bdbe3076`, `dd4ba4c2c` | 手工迁移 | 保留 Dovie media/artifact index |

### P1：建议吸收

这些增强产品体验、能力发现或未来扩展，但需要先按 Dovie contract 设计。

| 主题 | 代表提交 | 建议方式 | 说明 |
|---|---|---|---|
| `session.branch` 可见性和 search by id | `a3fb48b2c`, `580d92409`, `9ecc331be` | 吸收语义 | 保留 Dovie lineage/idempotency |
| Progressive tool disclosure / tool search | `369075dc9`, `7427b9d58` | 设计后吸收 | 必须按 profile/runtime toolset 过滤 |
| Skills full catalog / relevance gate | `fb9f3a4ef`, `7050c052e`, `38d3c49aa` | 设计后吸收 | 接入 Dovie skill market lifecycle |
| Desktop 交互经验 | `ac9de2e80`, `35a750eed`, `f66a929a6`, `58eb473ba` | 借鉴设计 | 不吸收 React 代码 |
| Provider/model/vision 增强 | `f8b8dffcc`, `f05353397`, `3a9bc9d88`, `fd87c6107` | 选择性迁移 | 仍以 Dovie 云端模型 inventory 为准 |
| Remote messaging adapter 修复 | Feishu/Slack/Telegram/Weixin commits | 选择性迁移 | 不吸收上游 Dashboard Channels UI |
| Dashboard auth 经验 | `ed9e8ba09`, `f57ce341d`, `bd12b3c23`, `928f1ac0e` | 另设设计文档 | 先统一 Dovie token / WS auth 模型 |

### P2：暂缓

| 主题 | 原因 |
|---|---|
| 上游 React Desktop 全量代码 | 与 Dovie Vue/Electron 产品形态冲突 |
| Bootstrap installer / Windows installer | 当前不是 Dovie Hermes runtime 打包主线 |
| Docker/s6 完整监督模式 | 主要面向容器部署，不是桌面默认路径 |
| Kanban/Honcho/Nous Portal 大功能 | 不属于 Dovie 当前客户端主链路 |
| Website / release docs / contributor metadata | 对产品无直接价值 |

## 对本地已开发能力的影响判断

| 本地 Dovie 能力 | 上游是否有等价更新 | 吸收判断 |
|---|---:|---|
| Dovie gateway contract / capability manifest | 否 | 必须保留本地 |
| profile-scoped runtime worker / `runtime.ensure` | 否 | 必须保留本地 |
| run control / run events / event replay | 部分相关 | 吸收上游 race fix，但保留本地事实源 |
| session branch / lineage / idempotency | 上游有简化 branch | 吸收可见性经验，不替换本地设计 |
| workspace / artifact API | 否 | 必须保留本地 |
| prompt attachments / document parse | 部分媒体相关 | 保留 Dovie extension，吸收安全和 media 限制 |
| desktop visible browser bridge | 上游 desktop/browser 有修复 | 只吸收底层 tool 修复，不替换 Dovie visible bridge |
| Dovie automation | 上游 cron 有修复 | 吸收 scheduler 稳定性，不恢复原生 cron 主入口 |
| skill package/binding lifecycle | 上游 skills catalog 增强 | 吸收 catalog/safety，不替换 binding 模型 |
| 子 agent 精确工具继承 | 否 | 必须保留本地 |
| subagent run snapshot/event API | 否 | 必须保留本地 |

## 建议落地顺序

### 第一批：P0 稳定性和安全

目标：不改变 Dovie 产品面，只修正 runtime 卡死、安全绕过和 scheduler 阻塞。

候选范围：

```text
5bcb63e40
98903d031
bd6d09876
8077e7d2f
693f4c7e9
25742372e
4e9d886d9
8f2931e3e
a6a4e6f9d
b04c6e95f
eb9cde734
9fbfeb31b
30412a977
46b2afc56
751b91446
e9529578d
6bdbe3076
dd4ba4c2c
```

验证重点：

```sh
scripts/run_tests.sh \
  tests/test_tui_gateway_server.py \
  tests/tui_gateway/test_ws_dispatch.py \
  tests/test_hermes_state.py \
  tests/cron/test_scheduler.py \
  tests/tools/test_interrupt.py \
  tests/tools/test_mcp_tool.py \
  tests/tools/test_write_deny.py \
  tests/tools/test_vision_tools.py
```

需要额外手工验证：

- Dovie desktop 发送消息后中断，UI 收到 terminal event。
- 切换历史会话后，live stream 不串会话。
- `session.branch` 后父会话和子会话都可见，历史不重复 replay。
- 自动化任务执行不会让 raw cron session 出现在 Dovie 侧栏。
- approval 拒绝后工具不会继续写 Hermes/Dovie config。

### 第二批：Branch / Session / Search

目标：把上游 branch 可见性和 search 性能吸收到 Dovie branch 架构。

候选范围：

```text
a3fb48b2c
580d92409
9ecc331be
3e59be0c4
3f7d1c801
243e836dc
```

注意：

- 只吸收 session list / search / soft-delete 语义。
- 不采用上游把 user-created branch 简单放入 `parent_session_id` ancestor replay 的方式。
- Dovie compression continuation 和 user-created branch 必须保持两套 lineage 语义。
- 实施结果：已完成上述语义吸收，并增加 Dovie `session_lineage` branch 在父会话 reopen/re-end 后仍默认可见的测试；`/undo [N]` 和 `/rewind` 均通过 Gateway `command.dispatch` 返回 `prefill`，前端只填充 composer，不自动提交。

### 第三批：Tool / Skills / Vision 能力扩展

目标：增强 Dovie 技能广场、工具发现和多模态能力。

候选范围：

```text
369075dc9
7427b9d58
fb9f3a4ef
7050c052e
38d3c49aa
f8b8dffcc
f05353397
c77a697fa
3a9bc9d88
```

注意：

- 工具发现必须绑定 `runtime_scope_key` 和当前 Dovie profile toolset。
- Skills catalog 不能绕过 Dovie package / binding / runtime snapshot。
- Vision routing 要尊重 Dovie 云端模型能力声明。

### 第四批：Dashboard / Remote Gateway / Channels

目标：为未来远程连接和公网 dashboard 统一认证模型做准备。

候选范围：

```text
ed9e8ba09
f57ce341d
bd12b3c23
928f1ac0e
f3bbfda6d
74e845c00
454d6cbe5
566669013
```

注意：

- 先写 Dovie auth / WS token 设计，不直接接上游 OAuth / username-password UI。
- Channels UI 继续由 Dovie 产品层定义。

## 同步红线

后续实现或合并时，以下边界不能回退：

1. `dovie_extension` 是 Dovie 产品能力边界，不能把 Dovie 方法散回 Hermes core。
2. `tui_gateway/server.py` 只做协调，新增逻辑优先落到 `methods/services`。
3. `run_control` 和 `run_events` 是 Dovie UI running 状态事实源。
4. `turn_id`、`run_id`、`client_message_id`、`runtime_scope_key`、`stored_session_id` 必须贯穿 prompt / tool / assistant / interrupt / recall。
5. 文档附件、document parse、display transcript sanitization 继续在 Dovie extension 层。
6. Tool 调用必须保留 `parent_agent`、session cwd、model descriptor、Dovie runtime credentials。
7. 子 agent 工具面继承必须是父级已解析工具 allowlist，而不是上游宽泛 toolset。
8. profile-scoped approval / cron / skills / tools 控制 RPC 必须代理到目标 runtime worker。
9. 只读 session/workspace/artifact 查询不能为了空 profile 创建 SQLite state。
10. User-created branch 和 compression continuation 不应共用同一 ancestor replay 语义。

## 建议下一步

1. 先冻结当前 Dovie `session.branch` 未提交改动，补齐测试并确认本地设计。
2. 按第一批 P0 清单逐项手工迁移，不进行全量 merge。
3. 每个主题迁移后更新 `UPSTREAM_SYNC_CHECKLIST.md` 的对应检查项。
4. 第一批完成后，再单独开文档设计 `Dovie branch / undo / rewind / search` 吸收方案。
5. Dashboard / remote gateway auth 另开设计文档，不夹在 runtime 稳定性同步里。
