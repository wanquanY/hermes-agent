# 上游 main 合并方案 - 2026-05-22 14:19:35

## 范围

当前评估分支：

```text
codex/merge-upstream-main-20260522-141935
```

计划合并目标：

```text
upstream/main @ 1264fab15 fix(tui): surface verbose tool details (#30225)
```

当前本地基线：

```text
dev / origin/dev @ 6f1e6271f feat: add Doxie sidecar web tools
共同基点 @ d36413211 chore(release): bump ACP Registry assets in lockstep with pyproject
```

本文档只做合并评估和方案设计。当前还没有执行 `git merge upstream/main`。

## 总体差异

本地 `dev` 相比 `upstream/main` 有 4 个独有提交：

```text
55148290e feat: add Doxie gateway integrations
05c1bb313 feat: add gateway run control
7bcec8501 feat: add skill package lifecycle
6f1e6271f feat: add Doxie sidecar web tools
```

`upstream/main` 相比本地 `dev` 有 827 个新增提交。

如果合并，预计总体变更规模：

```text
1129 files changed
121079 insertions
71317 deletions
```

dry-run 合并发现直接内容冲突文件：

```text
agent/image_routing.py
gateway/session_context.py
hermes_cli/runtime_provider.py
hermes_constants.py
hermes_state.py
model_tools.py
run_agent.py
tests/test_hermes_state.py
tests/test_tui_gateway_server.py
tests/tools/test_computer_use.py
tools/computer_use/cua_backend.py
tools/computer_use/schema.py
tools/computer_use/tool.py
tui_gateway/server.py
tui_gateway/ws.py
uv.lock
```

这次合并最关键的不是普通行级冲突，而是架构层面的冲突：

- 本地 Doxie 集成把 `tui_gateway` 拆成了 `methods/` 和 `services/` 模块；
- 上游继续以 `tui_gateway/server.py` 为主实现，并在这个文件上累积了大量新功能和修复；
- 本地把 `hermes_state.py` 拆成了多个 mixin 文件；
- 上游保留单文件 `hermes_state.py`，并加入了大量 SQLite/WAL/并发可靠性修复；
- 本地新增 Doxie 专属工具和 toolset；
- 上游新增了大量通用工具、Provider、安全、TUI、Gateway 能力，如果简单接受上游，会删除若干本地专属文件。

## 有被覆盖风险的本地实现

### 1. Doxie 产品工具

本地相关文件：

```text
tools/doxie_agent_profile_tool.py
tools/doxie_web_tools.py
toolsets.py
tests/tools/test_doxie_agent_profile_tool.py
tests/tools/test_doxie_web_tools.py
```

本地实现内容：

- `design_agent_profile` 和 `test_agent_profile` 把 Hermes 接到 Doxie 产品级“分身 / persona / profile 设计”流程。
- 工具从 `gateway.session_context` 读取 Doxie 会话上下文。
- 草稿持久化、预览、沙盒测试、验证、发布等产品行为交给 Doxie backend bridge。
- `serper_search_tool` 和 `jina_web_parser_tool` 通过 Doxie 代理托管付费 SERPER/Jina 类凭证，使用 `DOXIE_LLM_RUNTIME_TOKEN`。
- `toolsets.py` 增加了内部 toolset `doxie` 和普通 toolset `doxie_web`。

上游状态：

- 上游没有等价的 Doxie 分身设计工具。
- 上游新增了通用的 `x_search`、web provider plugin、browser provider plugin 和很多 web/tool 改进，但这些不能替代 Doxie 产品级工作流。
- 如果冲突解决时完全接受上游删除，这些 Doxie 工具会消失。

取舍结论：

```text
保留本地 Doxie 产品工具。
```

理由：

这不是上游通用能力的重复实现，而是 Doxie 产品域专属集成。上游能力更新、更广，但没有实现 Doxie 分身设计桥接，也没有实现 Doxie 托管 SERPER/Jina 凭证代理。因此应该保留这些文件，并把它们重新挂到上游最新的 `toolsets.py`、`model_tools.py` 机制上。

合并做法：

- 保留 `tools/doxie_agent_profile_tool.py`。
- 保留 `tools/doxie_web_tools.py`。
- 先接受上游新版 `toolsets.py`，再补回 `doxie`、`doxie_web` toolset。
- 保留 `INTERNAL_TOOLSETS = {"doxie"}` 和相关 helper，除非上游已有更好的内部 toolset 机制。
- 冲突解决后恢复并更新 Doxie 工具测试。

### 2. Skill Package Lifecycle

本地相关文件：

```text
tools/skill_package_lifecycle.py
tests/tools/test_skill_package_lifecycle.py
```

本地实现内容：

- 把本地 ZIP skill 包导入当前 Hermes home。
- 把已安装 skill 复制到另一个 Hermes home。
- 做安全的压缩包路径校验、拒绝 symlink、运行本地 skill 安全扫描、原子替换目录。
- 安装/复制后清理并重载 skill 缓存。

上游状态：

- 上游 `tools/skills_hub.py` 已经大幅升级：
  - 默认可信 `huggingface/skills` tap；
  - skills.sh 和 browse.sh 来源；
  - bundle 路径校验；
  - quarantine / scan / install 流程；
  - 更新检查和来源 provenance。
- 上游看起来没有本地 ZIP 导入 / 跨 home 复制这个独立能力。

取舍结论：

```text
保留这个能力，但不要原样保留成一套平行的生命周期实现。
```

理由：

本地实现有价值，也做了安全处理；但上游 Skills Hub 现在是更完整、更 canonical 的生命周期入口。原样保留一个独立模块，会造成安装语义、审计日志、来源记录、扫描流程重复。更好的设计是保留“ZIP 导入 / 跨 home 复制”能力，但尽量接入上游现有 `SkillBundle`、quarantine、scan、install 机制。

合并做法：

- `tools/skills_hub.py` 以上游为基础。
- 把本地 ZIP 导入、跨 home 复制改造成上游生命周期的小 wrapper。
- 保留 `tests/tools/test_skill_package_lifecycle.py`，但按上游当前 Skills Hub 结构更新预期。
- 避免重新引入绕过上游 provenance/quarantine 的直接 `shutil.move` 安装路径。

### 3. TUI Gateway 模块化、Doxie Sidecar、Run Control、Workspace、Artifact

本地高风险文件：

```text
tui_gateway/doxie_sidecar.py
tui_gateway/methods/*
tui_gateway/services/*
tui_gateway/server.py
tui_gateway/ws.py
tests/tui_gateway/*
tests/test_tui_gateway_server.py
tests/test_tui_gateway_ws.py
docs/Hermes/workspace-artifacts-architecture.md
```

本地实现内容：

- 把 `tui_gateway/server.py` 拆分成 method 模块和 service 模块。
- `doxie_sidecar.py` 用 token auth 通过 WebSocket 暴露 TUI gateway。
- `services/run_control.py` 提供持久 run registry、事件日志、订阅过滤、active-run 跟踪、事件 replay。
- workspace/artifact service 把工作区和产物作为 Doxie 客户端的一等概念。
- `services/doxie_cron_jobs.py` 提供比上游简单 `cron.manage` 更丰富的 Doxie cron 管理接口。
- `ws.py` 支持 Doxie/WebSocket 方法白名单，并在断连时解绑 run-control 订阅。

上游状态：

- 上游 `tui_gateway/server.py` 现在是继续演进后的大型单文件实现。
- 上游 `tui_gateway/ws.py` 比共同基点更完善，使用 `agent.async_utils.safe_schedule_threadsafe`，线程到事件循环的发送更安全。
- 上游没有本地 Doxie sidecar、workspace/artifact 持久化、run-control service 模块，也没有 Doxie 版 cron 管理层。
- 上游有大量不能丢的 TUI 修复：verbose tool details、Termux、scrollback、mouse tracking、session branch、clipboard、RPC/transport 修复等。

取舍结论：

```text
以上游 TUI gateway 为基础，把本地 Doxie gateway 能力移植进去。
不要整套保留本地拆分版本。
```

理由：

本地拆分更符合职责分离，也更接近长期最佳实践；但上游在单文件结构上继续演进，包含大量用户可见修复。直接保留本地拆分会丢上游 TUI 修复，并扩大长期分叉。根本解法是先保证行为合并正确：以上游当前实现为底座，把 Doxie 必需能力移植进去。后续如果要重新模块化，应作为单独重构，在测试稳定后进行。

合并做法：

- `tui_gateway/server.py` 先以上游为主解决。
- 再移植这些本地概念：
  - Doxie sidecar 入口；
  - `session.create` / resume 路径中的 Doxie auth/product context 字段；
  - workspace 规范化与 session 绑定；
  - `workspace.current`、`workspace.list`、`artifacts.list`；
  - 从文件变更类 tool completion 中提取 artifact；
  - 如果 Doxie UI 仍依赖，则补回持久 run/event API；
  - 如果桌面端仍依赖，则补回更丰富的 `cron.manage` 行为。
- `tui_gateway/ws.py` 以上游为基础，只补必要的 Doxie detach / allow-list 行为。
- 保留 `docs/Hermes/workspace-artifacts-architecture.md` 作为设计依据。
- 移植完成后逐步恢复测试。

### 4. `hermes_state.py` 拆分

本地相关文件：

```text
hermes_state.py
hermes_state_messages.py
hermes_state_platform.py
hermes_state_runs.py
hermes_state_search.py
tests/test_hermes_state_*.py
tests/hermes_state_fixtures.py
```

本地实现内容：

- 把 `SessionDB` 拆成 message、platform、run/event、search mixin。
- 增加 run/event 持久化，供本地 TUI gateway run control 使用。
- 降低单文件职责集中度。

上游状态：

- 上游保留单文件 `hermes_state.py`，从合并视角看会删除本地拆分文件。
- 上游加入了大量数据库可靠性修复：WAL 写锁竞争处理、随机 jitter 重试、checkpoint、schema 更新、搜索/session 处理等。

取舍结论：

```text
接受上游 `hermes_state.py` 为基础。只在 Doxie run-control 仍需要时补回缺失的 run/event schema 和 API。
不要在这次大合并里强行保留本地 mixin 拆分。
```

理由：

本地拆分方向是好的，也符合项目“单文件不要过大、职责分离”的原则；但上游单文件里有最新的数据库正确性修复。大合并时强推拆分会显著增加风险，容易丢掉 DB 可靠性修复。更稳的路线是先合行为：保留上游 DB 正确性，补 Doxie 缺口；等合并稳定后，再单独做 `hermes_state.py` 模块化重构。

合并做法：

- `hermes_state.py` 以上游为基础。
- 对照本地 `SessionDBRunMixin`，只补最终 gateway 会调用的 API：
  - `upsert_run`
  - `update_run`
  - `list_runs`
  - `append_run_event`
  - `list_run_events`
  - `next_run_event_seq`
  - stale active-run cleanup 相关 helper
- 测试不要原样恢复拆分测试，而是改成适配上游单文件结构的行为测试。

### 5. Computer Use 冲突

冲突文件：

```text
tools/computer_use/cua_backend.py
tools/computer_use/schema.py
tools/computer_use/tool.py
tests/tools/test_computer_use.py
```

本地状态：

- 本地改动包含和 Doxie/runtime 相关的 computer-use 接入变化。

上游状态：

- 上游近期对 computer-use 做了大量修复：
  - 新增 `tools/computer_use/vision_routing.py`；
  - capture vision 走 auxiliary vision；
  - 限制 elements 数量，避免 context 爆炸；
  - 修复 `capture_after` app context；
  - 增加 drag action；
  - 修正 MCP `type_text`；
  - app filter 无匹配时明确报错。

取舍结论：

```text
优先采用上游 computer-use 实现，只在确认缺失时补 Doxie 专属 hook。
```

理由：

上游实现更新、更完整，并解决了明确的正确性和性能问题。本地在这块的改动不像 Doxie tools 那样是完整产品域能力。直接保留本地版本大概率会回退上游修复。

合并做法：

- `tools/computer_use/*` 以上游为主。
- 审查本地 diff，只寻找 Doxie runtime/auth/context 的必要 hook。
- 只有测试或 Doxie 入口证明需要时，才补最小集成代码。

### 6. Runtime Provider / Hermes Home / Session Context

冲突文件：

```text
gateway/session_context.py
hermes_cli/runtime_provider.py
hermes_constants.py
run_agent.py
model_tools.py
```

本地实现内容：

- Doxie runtime auth 处理。
- Doxie 产品上下文传播。
- Doxie 工具进入 tool discovery / agent runtime。

上游状态：

- 上游加入了大量通用 runtime/provider 改进：
  - xAI OAuth/proxy；
  - provider metadata 和 custom endpoint 修复；
  - Codex app-server runtime；
  - credential pool 修复；
  - 安全加固；
  - model/tool routing 变化。

取舍结论：

```text
通用 runtime/provider 逻辑以上游为基础。Doxie auth/context/tool 注册作为窄补丁补回。
```

理由：

Provider/runtime 是高影响面代码。上游这里包含很多正确性和安全修复，不能因为 Doxie 集成直接覆盖掉。Doxie 的改动应该保持小而明确，集中在 session context、auth failure 处理和 tool registration 边界。

合并做法：

- provider 解析和 runtime 选择以上游为主。
- 补回 Doxie runtime auth failure 检测和用户提示。
- 只通过 gateway/session context 边界传播 Doxie 产品上下文。
- 通过正常 registry/toolset 机制注册 Doxie 工具。

### 7. Document Parse Tool 和 RL Tool

从合并视角看，上游会删除这些本地文件：

```text
tools/document_parse_tool.py
tools/rl_training_tool.py
```

上游状态：

- 上游明显在做 core debloating，尤其是 RL/Atropos 相关能力被移出或重构。
- 上游也新增/调整了 optional skills 和 docs 来承载更重的功能。

取舍结论：

```text
不要默认恢复到 core。
```

理由：

这两块不是明显的 Doxie 产品专属能力。上游删除它们大概率是设计方向：core 更轻，重型/小众能力迁到 plugin 或 optional skill。合并时不应逆向把它们塞回 core。

合并做法：

- 默认接受上游删除。
- 如果后续 Doxie 测试或产品需求证明必须依赖，再以 plugin/optional skill 形式恢复，而不是直接恢复 core tool。

### 8. `uv.lock`

冲突文件：

```text
uv.lock
```

取舍结论：

```text
代码依赖决策完成后重新生成 lockfile，不手工解冲突。
```

理由：

lockfile 手工解冲突风险高。上游这次有大量依赖、lazy install、分发方式变化。应该先完成代码层合并，再按仓库当前流程刷新 lockfile 并验证。

## 推荐合并策略

### 阶段 1：机械合并时偏向上游基础设施

以下范围先以上游为底座：

```text
run_agent.py
model_tools.py
hermes_cli/runtime_provider.py
hermes_constants.py
tools/computer_use/*
tools/browser_*
gateway/run.py
hermes_cli/config.py
pyproject.toml
uv.lock
```

原因：这些区域上游改动多、修复多、影响面大，本地 Doxie 改动应该作为窄补丁重新接入，而不是覆盖上游。

### 阶段 2：明确保留并移植 Doxie 产品能力

需要保留/移植：

```text
tools/doxie_agent_profile_tool.py
tools/doxie_web_tools.py
Doxie toolsets
Doxie runtime auth failure handling
Doxie product context propagation
Doxie sidecar WebSocket entry
workspace/artifact APIs
run-control/event replay APIs（如 Doxie UI 仍依赖）
增强版 Doxie cron 管理（如桌面端仍依赖）
```

原则：保留能力，不保留过时挂载方式。所有 Doxie 能力都要重新贴合上游最新结构。

### 阶段 3：避免恢复非产品必要分叉

不要只因为本地存在就恢复：

```text
旧 document_parse_tool.py
旧 rl_training_tool.py
旧 tools/browser_providers/*
完整 hermes_state mixin 拆分
完整 tui_gateway methods/services 拆分
```

这些应该只有在产品需求明确或单独重构计划批准后再恢复。

### 阶段 4：测试验证

优先跑 Doxie 和冲突区域测试：

```bash
scripts/run_tests.sh tests/tools/test_doxie_agent_profile_tool.py -q
scripts/run_tests.sh tests/tools/test_doxie_web_tools.py -q
scripts/run_tests.sh tests/tools/test_skill_package_lifecycle.py -q
scripts/run_tests.sh tests/test_tui_gateway_server.py -q
scripts/run_tests.sh tests/test_tui_gateway_ws.py -q
scripts/run_tests.sh tests/tui_gateway -q
scripts/run_tests.sh tests/test_hermes_state.py tests/test_hermes_state_runs.py -q
scripts/run_tests.sh tests/tools/test_computer_use.py -q
```

然后跑更广的关联测试：

```bash
scripts/run_tests.sh tests/tools/test_cronjob_tools.py tests/cron -q
scripts/run_tests.sh tests/hermes_cli/test_runtime_provider_resolution.py -q
scripts/run_tests.sh tests/test_model_tools.py -q
scripts/run_tests.sh tests/run_agent/test_run_agent.py -q
```

## 冲突处理决策表

| 区域 | 本地更优点 | 上游更优点 | 建议 |
| --- | --- | --- | --- |
| Doxie profile tools | 产品专属分身设计工作流 | 无等价实现 | 保留本地 |
| Doxie managed web tools | Doxie 托管凭证代理 | 通用 web/x_search provider | 本地和上游并存 |
| Skill package lifecycle | ZIP 导入、跨 home 复制 | Skills Hub 来源、quarantine、审计更完整 | 把本地能力接入上游生命周期 |
| TUI gateway | 模块化、workspace/artifact、run-control | 新 TUI 修复和持续演进 | 上游为底座，移植 Doxie 能力 |
| `hermes_state` | 职责拆分更好 | DB 并发和可靠性修复更多 | 上游为底座，只补必要 run/event API |
| Computer use | 可能有 Doxie hook | 最新 bug/perf 修复更多 | 优先上游 |
| Browser providers | 本地路径较旧 | 上游已 plugin 化 | 优先上游 |
| Document/RL core tools | 本地仍有实现 | 上游 core debloating | 默认不恢复 |
| Runtime/provider | Doxie auth/context | OAuth/proxy/security/provider 修复 | 上游为底座，窄补 Doxie |

## 执行门禁

在该方案确认前，不执行：

```bash
git merge upstream/main
```

方案确认后，下一步才执行：

```bash
git merge upstream/main
```

执行后按本文档顺序处理冲突：先用上游基础设施稳定主干，再逐项移植 Doxie 产品能力，最后补测试和 lockfile。
