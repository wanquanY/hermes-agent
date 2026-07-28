# 阶段 4 验收记录：本地执行、路径与插件权限安全

## 状态

**自动验收完成；等待 Phase 1-12 全部完成后的统一用户验收。**

本阶段建立本地 checkpoint commit 后直接进入阶段 5。最终统一验收前不推送、不合并
回主开发分支；上游提交只作为行为和失败场景参考，没有 merge、rebase 或 cherry-pick。

## 吸收结论

| 账本项 | 决策 | 当前 Hermes 实现 |
|---|---|---|
| SEC-CMD-01 至 09 | absorb | `tools.command_safety` 统一规范化、command-position 解析与 hardline 判定，`tools.approval` 只消费 canonical variants |
| SEC-CMD-10 | equivalent | 阶段 0 已有 smart approval prompt-injection guard，本阶段组合回归复核 |
| SEC-PATH-01/02/03/05 至 10 | absorb | `hermes_agent.domain.safe_identifiers`、`agent.file_safety` 与各 I/O adapter 在读写前执行 canonical deny floor 和 containment |
| SEC-PATH-04 | skip-with-reason | Dovie 当前不启用 Teams 录音 surface，不夹带无关 adapter |
| SEC-PROMPT-01 至 03 | absorb | LSP diagnostic、NFKC/invisible Unicode 和 cron 共用规范化的非可信文本策略 |
| SEC-PLUGIN-01 至 05 | absorb | manifest capability、operator opt-in、handler module ownership 与 durable registry policy 共同裁决 override/deregister；Dashboard/MCP/API key 0day 边界复核通过 |
| SEC-PLUGIN-06/07 | superseded/equivalent | 不吸收上游已撤销的 bundled-only 限制，保留当前 plugin discovery 语义 |
| SEC-SURFACE-01/05 | absorb | debug share 先 consent 后采集；Tirith 使用并发安全的连续失败 breaker |
| SEC-SURFACE-02/03/04/06 | skip-with-reason/equivalent | 未启用的 messaging surface 不纳入；既有 Kanban redaction 保持 |

## 关键架构结果

1. shell 安全策略只有一个规范化 owner。NFKC、ANSI/NUL、HOME、IFS、续行、引号、
   heredoc、subshell、brace、反引号和命令分隔符先形成统一 variants，再由审批与
   hardline 消费；yolo 和 cron approve 都不能越过 hardline。
2. domain session ID 与 filesystem artifact name 分离。API identity 严格验证，文件名
   使用稳定无碰撞编码；`/proc`、根 credential store、media、patch Move 和 snapshot
   restore 在任何 I/O 前 fail-closed。
3. plugin override 是持久 capability policy，不是加载线程中的瞬时布尔值。延迟线程、
   direct import、跨插件 deregister 和失败后误记成功均有回归。
4. debug share 的同意发生在采集前；Tirith breaker 只控制重复拉起，不偷偷改变 operator
   配置的 fail-open/fail-closed 结果。
5. 普通 turn 不再隐式访问远端模型目录。显式 catalog refresh 仍可联网，Agent 构造、
   fallback、压缩和 usage 计价只消费本地事实与安全保守值。

## 自动验收证据

### 阶段聚合安全回归

同一 pytest 进程聚合运行 26 个相关模块，覆盖 approval、command mutation、真实 handler、
path/identifier、session API、media、LSP、prompt、plugin、MCP、Dashboard、debug、Tirith、
model metadata、compression feasibility 和 Agent runtime：

- 1,642 passed、3 skipped、0 failed，163.78s；
- 3 条 warning 是 aiohttp 既有 `NotAppKeyWarning`，没有放宽门禁；
- 组合执行发现并修正 Dashboard ASGI binding state 在测试间泄漏，随后把
  Dashboard/MCP/API key/plugin 安全测试放在同一进程重跑：132 passed、1 skipped。

### 运行期元数据根因与性能证据

修复前，`test_run_agent.py` 和 Codex Responses 回归分别因每次构造重复探测远端目录耗时
634.3s 与 686.5s。运行期改为 cache-only 后：

| 回归 | 修复后结果 |
|---|---|
| `tests/run_agent/test_run_agent.py` | 352 passed，16.51s；剩余时间来自 3 个有意的 2s backoff |
| `tests/run_agent/test_run_agent_codex_responses.py` | 70 passed，3.55s |
| metadata/models.dev/usage 定向 | 152 passed |
| metadata/compressor/Copilot 聚合 | 254 passed |

无缓存 Codex、Copilot、OpenRouter、custom endpoint、非 Ollama provider 和 subscription
billing 路径都有“网络调用次数为 0”的行为断言；显式 setup/refresh 的公开调用形状保持兼容。

### 全量、静态与 Doxie 门禁

| 门禁 | 最终结果 |
|---|---|
| Hermes 第二轮全量 | 1,559 files、28,931 passed、0 failed；479.8s，28 workers；worker DB serial tail 13 passed |
| `ruff check .` | All checks passed |
| production Python compile | 全部 tracked production `.py` 通过 `py_compile` |
| `lint-imports` | 156 files、314 dependencies；1 contract kept、0 broken |
| zero-debt / silent fallback / silent swallow / async SQLite / ledger | 31 passed、0 failed、1 expected xfail |
| `git diff --check` | 通过 |
| Doxie Gateway ABI | 205 methods in sync；98 active methods covered by 135 required methods |
| Doxie `test:gates` | 退出码 0；Electron Node 391 passed；Source Node 19 passed；type/lint/frontend boundaries 通过 |

Doxie lint 的 70 条 warning 是当前工作树既存警告；本阶段没有新增忽略或降低门禁。
阶段 4 机器门禁完成时间：2026-07-16。

## 收益与最终实机验收重点

1. 复杂 shell 混淆不再能绕过 hardline；安全命令、引号内说明文字和 commit message
   不会因为扩大安全覆盖而被误杀。
2. 文件、媒体、session artifact、patch 和 snapshot 共用 canonical 边界，避免同一种
   traversal 在不同入口反复出现。
3. 第三方插件必须同时得到 manifest 和 operator 授权，并且实际 handler 必须属于该插件；
   core 或其他插件工具不能被静默替换或删除。
4. 调试上传不会在用户同意前采集信息，Tirith 连续崩溃不会无限拉起。
5. 断网或目录服务异常不再让新对话、压缩或已完成响应额外卡住十秒级超时，普通 turn
   也不会产生非必要的 models.dev/OpenRouter/Ollama 出站请求。

统一实机验收时重点测试：在 yolo 与普通模式分别请求危险 `rm`/`git reset` 混淆变体和
包含这些字样的安全说明文本；读取合法 `@file` 与 `/proc`/凭据路径；安装一个普通插件并
尝试未授权覆盖；执行 `hermes debug share` 的拒绝与同意分支；断网后新建普通、Codex、
leader、`@member` 和非活跃会话，观察首响应、压缩与 usage 完成是否无额外卡顿。
