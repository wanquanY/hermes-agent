# 阶段 3 验收记录：凭据边界与出站网络安全

## 状态

**自动验收完成；等待 Phase 1-12 全部完成后的统一用户验收。**

本阶段建立本地 checkpoint commit 后直接进入阶段 4。最终统一验收前不推送、不合并
回主开发分支；上游提交只作为行为和失败场景参考，没有 merge、rebase 或 cherry-pick。

## 吸收结论

| 账本项 | 决策 | 当前 Hermes 实现 |
|---|---|---|
| U3 | absorb | `hermes_cli.urllib_security` 统一 credential-bearing urllib redirect policy |
| SEC-CRED-01/02 | absorb | `tools.environments.local.hermes_subprocess_env` 统一非可信、依赖型和 model-driving child env 权限 |
| SEC-CRED-03/04/05/06 | absorb | `agent.redact` 统一 env dump、Fireworks、URL userinfo 与 terminal/process 输出脱敏；browser CDP 在日志边界强制 URL 脱敏 |
| SEC-NET-01/02/03 | absorb | `tools.url_safety` 统一 httpx redirect target、Yuanbao preflight、IPv6 scope 与 DNS 多地址 fail-closed |
| SEC-NET-04 | absorb | `hermes_cli.web_server._ws_client_is_allowed` 在无认证 loopback 模式拒绝缺失或空 peer |
| SEC-CRED-07 | equivalent | 阶段 0 已有 `shlex` 与 child env quoting 证据，本阶段不复制实现 |

## 权限与信任边界

`hermes_subprocess_env()` 明确区分两层凭据：

- Tier 1 是 Gateway、GitHub、infra、dashboard 和动态 Hermes 内部 secret，任何经该
  policy 启动的 child 都不能继承，显式 overlay 也不能绕过；
- Tier 2 是 provider/tool credential，只有明确声明 `inherit_credentials=True` 的
  model-driving child 可以继承；browser、CUA、LSP 和 dependency installer 默认没有
  provider authority，browser 只按窄 allowlist 加回自身 backend key。

该合同覆盖 Hermes 会执行模型、工具、依赖或外部程序的 child boundary。可信的
Gateway/platform control-plane 启动仍由其专属 owner 注入运行所需 messaging credential，
不经过普通 child policy；因此这里不宣称“全局所有 subprocess 都剥离所有凭据”。

## 自动验收证据

### 凭据、脱敏与真实 redirect

| 行为 | 证据 |
|---|---|
| 真实 child process 环境不含 Tier 1、动态 Hermes secret 和未授权 provider key | `tests/tools/test_hermes_subprocess_env.py` |
| Codex、Copilot、worker、browser、CUA、LSP、installer 等 spawn 使用统一 policy | 对应 transport/worker/browser/LSP/CUA 定向回归 |
| foreground、background poll/log/wait 和 completion watcher 不返回 env dump 原始 secret | `tests/agent/test_redact.py`、`tests/tools/test_process_registry.py`、terminal 回归 |
| Fireworks key、bare URL token 和 CDP query/userinfo 不进入日志 | redactor 与 browser CDP 回归 |
| 两个真实本地 HTTP server 证明 same-origin 保留 custom credential、跨端口 origin 只保留 Accept/User-Agent | `tests/hermes_cli/test_urllib_security.py` |
| scheme downgrade 被视为跨 origin；redirect loop fail-closed；installed late auth hook 不能重新注入 secret | `tests/hermes_cli/test_urllib_security.py` |

最近一次 urllib 与 URL safety 合并专项：129 passed、0 failed。阶段 3 扩展安全矩阵
累计覆盖 601 项定向测试；新增的 scheme downgrade、redirect loop 和 DNS 混合答案断言
在全量后再次定向通过。

### SSRF 与 WebSocket peer

| 行为 | 证据 |
|---|---|
| httpx event hook 在 `next_request=None` 时从 `Location` + `urljoin` 得到真实目标 | `tests/tools/test_url_safety.py` |
| Yuanbao 初始 metadata/private URL 在创建 client 前拒绝，每个 redirect hop 再检查 | `tests/channels/test_yuanbao_media_ssrf.py` |
| IPv6 scope ID 在分类前剥离；不可解析地址或 public/private 混合 DNS 答案 fail-closed | `tests/tools/test_url_safety.py` |
| 无认证 loopback dashboard 对缺失/空 WebSocket peer 拒绝，已认证模式保持原语义 | `tests/hermes_cli/test_dashboard_auth_gate.py` |

当前 SSRF 合同检查解析时返回的全部地址，并对每个 redirect 重新执行策略；它不宣称
提供 socket-level DNS pinning。后续如果引入连接级 pinning，必须由 HTTP transport
owner 实现，不能在调用点伪装成 hostname 二次比较。

### 全量门禁发现并关闭的真实竞态

第二轮全量在 28-way I/O 压力下暴露了两件不能用放宽断言掩盖的问题：

1. direct transient stream delta 与 subscription checkpoint 竞态会把 `你`、`好` 后再
   投影一次 `你好`。当前 Gateway 记录 direct delivery 的 UTF-16 coverage cursor，完整
   前缀 checkpoint 抑制、部分前缀只投影未见 suffix，snapshot 仍保持权威。
2. durability benchmark 与 27 个并行测试争用磁盘时不能反映其 1.0 秒产品阈值。测试
   runner 增加显式 serial-tail sentinel，阈值未放宽，串行尾部实测 0.8 秒。

同时修正 macOS `/private/var/folders` 用户临时目录的逐候选 canonical 检查，以及损坏
显式 `VIRTUAL_ENV` 不得误选 ambient Conda 的 Python 解析优先级。相关 131 项 runner、
stream、Team Mission 与 durability 定向回归通过。

### 静态、全量与 Doxie 门禁

| 门禁 | 最终结果 |
|---|---|
| Hermes 全量 | 1556 files、28,855 passed、0 failed；661.6s，28 workers；serial durability 0.8s |
| `ruff check .` | All checks passed |
| production Python compile | 全部 tracked production `.py` 通过 `py_compile` |
| `lint-imports` | 155 files、313 dependencies；1 contract kept、0 broken |
| zero-debt / silent fallback / silent swallow / async SQLite / ledger | 29 passed、0 failed、1 expected xfail |
| `git diff --check` | 通过 |
| Doxie Gateway ABI | 205 methods in sync；98 active methods covered by 135 required methods |
| Doxie `test:gates` | 退出码 0；Electron Node 391 passed；Source Node 19 passed；type/lint/frontend boundaries 通过 |

Doxie lint 的 70 条 warning 是当前工作树既存警告；本阶段没有新增忽略或降低门禁。
阶段 3 机器门禁完成时间：2026-07-16。

## 收益与最终实机验收重点

1. Codex、browser、LSP、CUA 和依赖安装进程只拿到职责所需权限，不能继承 Hermes
   control-plane、Gateway 或操作者的完整 credential keyring。
2. `printenv`、background log、completion watcher、Fireworks error、Git URL 与 CDP
   endpoint 经过同一脱敏事实源，避免“前台安全、后台泄漏”。
3. provider/catalog 的认证 header 只在同 origin redirect 保留，换 scheme、host 或
   effective port 后即使 installed hook 尝试补回也会在最终边界被移除。
4. media 下载在初始地址和每个 redirect hop 都拒绝 private/metadata/不可解析目标，
   IPv6 scope 与空 WebSocket peer 不再成为 fail-open 缺口。
5. 高并发下流式文本不再因 direct/subscription 双路径重复显示。

统一实机验收时重点测试：启动 worktree runtime 后让 agent 执行 `printenv` 和后台
`printenv`、启动一次 Codex/browser/subagent、访问一个会跳转的公开 URL、观察浏览器
CDP 错误日志，以及连续中文/emoji 流式输出是否有重复片段。
