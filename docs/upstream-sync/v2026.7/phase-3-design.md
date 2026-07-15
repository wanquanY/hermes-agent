# 阶段 3 设计：凭据边界与出站网络安全

## 目标

按当前 Hermes 分层手工吸收 U3、SEC-CRED-01 至 SEC-CRED-06、SEC-NET-01 至
SEC-NET-04。阶段完成后，Hermes 的子进程环境、终端输出、凭据型 urllib 请求、
httpx 重定向和 WebSocket loopback peer 都由明确的唯一策略 owner 负责。

本阶段只参考上游缺陷场景和测试，不 merge、rebase 或 cherry-pick。阶段 4 的命令、
路径和工具输出信任治理不在本阶段提前实现。

## 输入与边界

- 阶段起点：`787d59bbd`。
- 执行分支：`codex/upstream-absorption-v2026.7-all-phases`。
- 子进程凭据参考：`9c6229ce2`、`a658f3b28`。
- 输出脱敏参考：`c1c179a23`、`c701c6dad`、`3483424aa`、`576424cc1`。
- urllib redirect 参考：`a061788d4356`、`5415b77658e9`、`6e75ba7fa066`、
  `1f46145e0391`、`d83cd6f7c3ac`。
- httpx/SSRF 参考：`500c2b1e4`、`d65468e7f`、`ed966696e`。
- WebSocket peer 参考：`ed3d12a76`。

## 缺口核验

1. terminal/background 已有静态 provider blocklist，但 browser、Codex、Copilot、
   dependency installer 和 Gateway worker 等非 terminal spawn 仍可复制完整
   `os.environ`；动态 `AUXILIARY_*` 与 `GATEWAY_RELAY_*` secret 也无法由静态名单覆盖。
2. foreground terminal 以 `code_file=True` 处理所有输出，background process 的
   poll/log/wait 没有共用输出脱敏边界；`printenv` 的无前缀 opaque token 会泄漏。
3. Fireworks key、bare-token URL userinfo 与 CDP endpoint 的专用日志场景缺少可验证
   的脱敏合同。
4. provider/model/Azure catalog 的 credential-bearing urllib 请求没有统一 redirect
   policy；任意自定义 header、Cookie 或不同端口 origin 都可能继承凭据。
5. httpx response hook 依赖 `response.next_request`，而 event hook 阶段该字段可为空；
   `Location` 指向 metadata/private IP 时可能绕过检查。Yuanbao 下载没有 preflight。
6. IPv6 scope ID 解析失败后被跳过；全部地址均不可解析时会 fail-open。
7. loopback dashboard 的空 WebSocket peer 当前被允许。

## 架构决策

### 1. `tools.environments.local` 是非可信、依赖型和 model-driving child env 的策略 owner

在既有 terminal policy 上建立 `hermes_subprocess_env()`，明确两层权限：

- Tier 1：Gateway、GitHub、infra、dashboard 和动态 Hermes 内部 secret，所有 child
  无条件剥离；
- Tier 2：provider/tool credential，只有明确标注 `inherit_credentials=True` 的
  model-driving child 才可继承。

动态 secret predicate 同时被 terminal、background、Docker、passthrough guard 和
非 terminal helper 使用。显式 browser backend key 以窄 allowlist 加回，不能恢复完整
operator keyring。真实子进程环境快照是验收依据。

可信 Gateway/platform control-plane 启动需要专属 messaging credential，由对应 owner
显式注入且不属于普通 child policy；本阶段不把“所有 subprocess 一刀切净空”当作
安全目标，避免破坏平台连接后再在下游补凭据。

### 2. `agent.redact` 是 terminal/process/log 输出的唯一脱敏 owner

新增 `redact_terminal_output(output, command)`：仅 env/printenv/set/export/declare
输出启用 KEY=value opaque-secret pass，其他命令保持 source-safe 模式。foreground、
background poll/log/wait 和 watcher 投影都调用同一函数。

Fireworks、bare-token URL userinfo 归入 canonical redactor；CDP 日志在专用边界额外
启用 URL query/userinfo redactor，避免依赖全局配置或 logger formatter 偶然兜底。

### 3. redirect credential 与 SSRF 是两个独立策略

- `hermes_cli.urllib_security`：credential-bearing stdlib 请求的 owner。同 origin
  保留 header；scheme、host 或 effective port 改变时只允许 Accept/User-Agent，且
  sanitizer 在已安装 hook 之后最后执行。
- `tools.url_safety`：目标地址安全与 httpx redirect target 解析的 owner。redirect
  首先从 `Location` + `urljoin` 解析，再退回 `next_request`；所有 media hook 复用。

这两类策略不混合：前者防凭据转发，后者防服务端请求进入 private/metadata 网络。

### 4. 网络边界必须 fail-closed

IPv6 scope ID 在 IP 分类前剥离；仍不可解析的地址使 `is_safe_url()` 返回 false。
Yuanbao media 同时做初始 URL 与每个 redirect hop 检查。loopback dashboard 在
`ws.client` 缺失或 host 为空时拒绝，gated/public 模式原有语义不变。

## 自动验收矩阵

1. 真实 child env：Tier 1/dynamic secret 在 terminal、Codex、Copilot、browser、
   installer 和 worker 中不可见；model-driving child 仍能获得必要 provider key。
2. secret mutation：随机 opaque token、Fireworks key、bare URL token、CDP query/
   userinfo 在 foreground/background/watcher/log 中均不出现原文。
3. urllib：same-origin 保留任意 custom header；cross-origin/port/scheme redirect 只保留
   allowlist；已安装 cookie/auth hook 不能在 sanitizer 后重新注入。
4. httpx：`next_request=None` 且 Location 为 absolute/relative private target 时阻断；
   public target允许；Yuanbao 初始 private URL 在 client 创建/请求前阻断。
5. IPv6 scope 与一次解析返回的 DNS 多地址中任一不安全或不可解析地址均 fail-closed；
   本阶段不把地址检查夸大为 socket-level DNS pinning。
6. 四个 dashboard WebSocket endpoint 共用的 helper 对空 peer 拒绝，gated/public
   模式保持。
7. 阶段专项、静态/分层/zero-debt、Hermes 全量与 Doxie contract/gates 全部通过。

## 完成条件

上述账本项全部有当前 owner、实现和行为测试证据；建立本地 Phase 3 checkpoint 后
直接进入 Phase 4。最终统一用户验收前不 push、不 merge。
