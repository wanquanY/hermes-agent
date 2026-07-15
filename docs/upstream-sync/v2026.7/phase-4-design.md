# 阶段 4 设计：本地执行、路径与插件权限安全

## 目标

按当前 Hermes 架构手工吸收账本中归属阶段 4 的 SEC-CMD、SEC-PATH、SEC-PROMPT、
SEC-PLUGIN 与 SEC-SURFACE 条目，并复核阶段 0 已判定为 equivalent、superseded 或
skip-with-reason 的相邻条目。阶段完成后，复杂 shell
命令、文件与会话标识符、非可信提示内容、插件工具覆盖和调试上传均先经过各自唯一的
安全策略 owner，再进入执行或持久化边界。

本阶段只参考上游失败场景和测试，不 merge、rebase 或 cherry-pick。当前 Hermes 已有
审批、媒体缓存、插件加载与 session runtime 分层，因此不复制上游零散正则或旧路径；
所有吸收都落在当前职责边界，并保留拒绝原因和可审计证据。

## 输入与边界

- 阶段起点：`89e6c2d0d`。
- 执行分支：`codex/upstream-absorption-v2026.7-all-phases`。
- 命令安全参考：`74e59b8b6`、`6a6fd4211`、`51feecc2b`、`7534b5be2`、
  `17f07aebd`、`7bfdc0bca`、`a81b519d4`、`0c0b4b698`、`3b2bb30c5`。
- 路径与标识符参考：`868fa9566`、`64e6b98ba`、`10a54ccc2`、`42d017469`、
  `100e7be20`、`e6f66bc0f`、`ea1372d2a`、`1debd5e8f`、`ae4669990`。
- 提示、插件与表面安全参考：`ea9f8bd16`、`099df3cd8`、`fbfccbb3e`、
  `12f5624a7`、`310122231`、`179eb8c2a`、`db0fd8f29`、`7726ce304`、
  `54f32af4a`、`07cc567df`。

## 架构决策

### 1. `tools.command_safety` 是 shell 规范化与 hardline 判定的唯一 owner

从过大的 `tools.approval` 中抽出纯命令安全内核。它先做 NFKC、HOME 归一化、反斜杠
续行和 IFS 折叠，再用 quote-aware command-start tokenizer 标记真正的 shell 命令位置。
hardline 和 dangerous patterns 都只消费规范化 variants，而不是各自修补输入。

- `rm` 的根目录折叠、引号、`${HOME}`、brace/subshell 与 command position 共用一套解析；
- `git reset/branch` 与 `sudo` 支持 shell 实际接受的长参数缩写；
- heredoc、env/config 尾随参数与注释使用明确 token boundary；
- 命令字符串中的说明文字或引号内容不能误触发 command-position 规则；
- hardline 永远在 yolo、cron approve 和普通审批之前执行。

### 2. canonical path 与 identifier 策略在进入 I/O 前执行

建立可复用的路径/标识符 owner，而不是让每个文件名调用点自行 `replace()`：

- `/proc` 凭据/内存视图与 `@file` 共用 canonical read deny floor；
- media delivery 先执行绝对 deny floor，再检查 Hermes cache 或 operator allow root；
- session 外部 API ID 严格验证，持久化 artifact 使用无碰撞、跨平台安全 filename component；
- patch header extraction 与 parser 接受同一语法，覆盖 Move 两端、无空格 header 和 traversal；
- quick snapshot ID、manifest source/destination 都验证 resolved containment，任一非法项使恢复
  在写入前 fail-closed。

数据库中的 domain session ID 不被文件名规则改写；只有 filesystem artifact 使用稳定编码，
从而保持会话语义和现有 API 合同。

### 3. `tools.threat_patterns` 是非可信文本规范化的唯一事实源

LSP diagnostic 在注入模型上下文前限制长度、清理控制字符并 HTML escape；threat scanner
以原文检查 invisible Unicode，以 NFKC 文本匹配威胁模式。cron 不再维护第二份 invisible
字符表。常见单词 `Praxis` 从 C2 域名启发式中移除，避免正常工程内容被误报。

### 4. 插件覆盖是 durable capability policy，不是瞬时调用布尔值

第三方插件覆盖既有工具必须同时满足：

1. manifest 声明 `tool_override` capability；
2. operator 在 `plugins.entries.<plugin-id>.allow_tool_override` 显式启用；
3. 实际 handler 的定义 module 属于获授权插件 namespace。

授权由 registry 持久记录，延迟线程或直接 import 不能借加载时上下文逃逸。插件 deregister
默认只能删除自己拥有的工具；core、其他插件和 MCP 动态 refresh 各有明确且可测试的边界。
拒绝必须稳定、可记录，并且不得把失败注册误记为成功。

### 5. 调试上传与 Tirith 故障必须显式可控

CLI `debug share` 在任何采集和网络调用前要求交互确认或 `--yes`；非 TTY 且无确认时
fail-closed。本地生成不需要上传同意，Gateway `/debug` 由用户显式动作提供 consent。

Tirith 采用 lock-protected consecutive-failure breaker：成功执行重置计数，连续运行故障
达到阈值后停止重复拉起。breaker open 时仍遵守 `tirith_fail_open`，不能用熔断偷偷改变
operator 的 fail-open/fail-closed 决策。

### 6. 运行期模型元数据只消费本地事实，不隐式访问远端目录

全量门禁暴露出 Agent 构造、上下文压缩和 usage 计价会在普通 turn 中隐式访问
models.dev、OpenRouter、Codex、Copilot 或 Ollama 元数据端点。失败结果未形成统一缓存，
会让每次 Agent 创建和已经完成的响应额外等待网络超时。

当前设计把“显式 catalog setup/refresh”与“运行期读取”分开：显式刷新仍可联网；运行期
构造、fallback、压缩可行性、`@` 引用预处理和 usage 记录只读显式配置、持久缓存、允许
过期的离线 models.dev 缓存、Codex 静态值或 provider-safe floor。非 Ollama provider
绝不试探 `/api/show`，subscription billing provider 绝不为计价访问远端 catalog。
这既关闭了非必要出站面，也避免网络状态改变 turn 延迟和结果。

## 自动验收矩阵

1. command mutation：引号、HOME、IFS、续行、subshell、brace、注释和缩写变体命中正确；
   quoted prose、commit message 与非 command-position 文本不误报。
2. handler zero-call：hardline 输入在 terminal/backend handler 调用前拒绝，yolo 不能绕过。
3. path mutation：proc、@file、media、patch、snapshot 与 session artifact 的 traversal、symlink、
   根 credential store 和碰撞输入 fail-closed；安全输入保持原行为。
4. prompt mutation：LSP 控制字符/HTML/超长内容被清理，Unicode compatibility/invisible
   输入共用 canonical scanner，Praxis 不误报。
5. plugin matrix：默认拒绝；仅 capability 或仅 operator opt-in 均拒绝；二者同时满足才允许；
   direct import、延迟线程、跨插件 deregister 均不能越权；MCP own refresh 保持。
6. debug/Tirith：非 TTY 无 consent 时零采集、零上传；`--yes` 和交互 yes 正常；breaker
   并发安全、成功重置、阈值后不再 spawn，且两种 fail policy 都有证据。
7. runtime metadata：无缓存和断网下构造 Codex、Copilot、OpenRouter 与 custom endpoint
   不访问网络；显式刷新仍保留联网能力；usage 完成不能被 pricing probe 延迟。
8. 阶段专项、静态/分层/zero-debt、Hermes 全量与 Doxie contract/gates 全部通过。

## 完成条件

账本中的 absorb/equivalent/skip/superseded 决策均有当前 owner 和行为证据；建立本地
Phase 4 checkpoint 后直接进入 Phase 5。最终统一用户验收前不 push、不 merge。
