# 阶段 10 验收：MCP canonical identity、富内容与可恢复生命周期

## 结论

阶段 10 的 `R3`、`U8`、`RT1`、`RT2` 已按 Hermes 当前架构手工吸收，专项、标准全量、
静态/分层/可观测性与 Doxie 桌面门禁全部通过。阶段状态为“实施完成，待 Phase 1-12
统一用户实机验收”。最终统一验收前不 push、不合并回主开发分支。

## 已关闭的协议与生命周期缺口

### R3：canonical MCP identity

- `tools.mcp_identity` 是 component 清洗、`mcp__server__tool` 生成、解析与 legacy 判定的唯一 owner。
- schema、utility、dynamic refresh、Codex event projector、Anthropic adapter、toolset 和 guardrail
  只制造双下划线 canonical name。
- registry 保留唯一 legacy read seam：只对已注册 MCP provenance 做最长 server-name 匹配，歧义或
  未知 server fail closed；不注册 alias、不双写旧名。
- 历史 tool call 经 `repair_tool_call` 解析后立即 canonicalize，后续 dispatch、transcript 和 schema
  不再传播旧格式。

### U8：MCP rich content

- `tools.mcp_content` 统一处理 Text、Image、Audio、ResourceLink、EmbeddedResource text/blob、
  structured content 与 error mixed content。
- blob 使用严格 base64、MIME、安全扩展名和尺寸上限写入共享 cache；坏 block 产生可观察 marker，
  不吞掉同一结果中的合法 block。
- ResourceLink 只保留安全元数据并指向 canonical resource tool，不自动访问远端 URL。
- `tools/call` 与 `resources/read` 复用同一 materializer，错误路径不再只保留 text。

### RT1：有界 initialize 与资源回收

- stdio、SSE、streamable HTTP 和 legacy HTTP 四条 transport 共用 `bounded_initialize`。
- initialize deadline、caller cancellation 和 connect failure 都穿过同一 cancel-and-drain/transport
  teardown 路径；run task 不再 detached。
- registry 只发布 connected session 的完整快照，disconnect、park 和 shutdown 都撤销 stale tools。

### RT2：parked server 自恢复

- 每个 server 只有一个 `MCPServerTask` lifecycle owner；启动失败后进入 parked，而不是从
  discovery 入口重复创建连接。
- probe 使用指数 backoff 和单窗口单次执行；成功后原子 replace 工具快照并 unpark，失败不阻塞
  其他 server。
- manual reconnect、list_changed、parked revive 与 shutdown 共用 publish/revoke seam；状态接口暴露
  state、last error 和 next probe time。
- 有效缓存 OAuth token 直接 initialize，不再因推测未来 401 而抢先访问 metadata endpoint；失效
  token 的 refresh/discovery 仍受总 deadline 约束。

## 自动化验收证据

### MCP 与相邻协议专项

| 范围 | 结果 | 证明内容 |
|---|---:|---|
| `tests/tools/test_mcp_tool.py` | 196 passed | transport、initialize、result、OAuth、shutdown 主路径 |
| 文件名包含 MCP 的聚合测试 | 648 passed | identity、content、dynamic discovery、OAuth、park/recover、稳定性 |
| ACP / MCP / transport / agent 聚合 | 303 passed | Gateway/ACP、provider adapter 与 runtime 投影一致 |
| protocol / ACP / Codex 聚合 | 266 passed | canonical name、repair、Codex/Anthropic 相邻路径 |
| model mock 修正聚合 | 66 passed | cache-only catalog mock 合同保持一致 |
| Doxie approval presenter 专项 | 27 passed | 阶段 7/10 跨仓用户输入与工具展示没有回归 |

### 标准全量与架构门禁

| 门禁 | 结果 |
|---|---|
| `scripts/run_tests.sh`（默认 28 workers + serial tail） | 1585 files，29,104 passed，0 failed，643.9s |
| 16-worker 全量复核 | 1585 files，29,101 passed，0 failed，656.7s（增加 runner 回归前） |
| `.venv/bin/ruff check .` | passed |
| production package `compileall` | passed |
| `.venv/bin/lint-imports --config .importlinter` | 1 contract kept，0 broken |
| `pytest -q tests/observability` | 86 passed，13 skipped，1 xfailed |
| `git diff --check` | passed |

标准全量首次暴露 38 个 P9 测试 mock 仍使用无参数 lambda，以及一个高并发下的 TUI 内部等待窗口；
均从测试基础设施根因修复。随后默认 28-worker 全量仅剩异步委派 `<1s` 墙钟合同受其他 pytest
进程 CPU 竞争污染。该严格阈值没有放宽：测试文件声明 `hermes-test-runner: serial`，runner 回归证明
它进入串行尾测，最终标准命令完整通过。TUI 的 10 秒只是不把测试线程调度延迟误判为产品 SLA，
被测的“释放 live session 后才向慢客户端写入”顺序断言保持不变。

### Doxie 集成门禁

在显式移除本地 source-runtime 环境变量后执行：

```bash
env -u DOVIE_HERMES_RUNTIME_MODE -u DOVIE_HERMES_SOURCE_DIR \
  corepack pnpm --filter @dovie/desktop test:gates
```

结果：退出码 0。关键证据包括 Vitest 266 files / 1,978 passed / 3 skipped、Electron Node
391 passed、source Node 19 passed、Gateway ABI 205 methods in sync、98 个 active desktop methods
被 135 个 required methods 覆盖、type-check、ESLint（0 errors，70 个既有 warnings）和 frontend
boundary 全部通过。

## 收益确认

- MCP 工具身份从有歧义的单下划线命名升级为生态一致、可逆解析的 canonical wire identity；旧会话
  可读，但系统不再制造新旧双债。
- 音频、图片、资源、结构化内容和错误诊断完整进入统一结果合同，不再被降级成残缺文本。
- 挂死或异常 MCP server 有明确 deadline、资源回收和低成本自恢复，不拖死 runtime、不泄漏孤儿
  task/进程，也不制造 discovery storm。
- OAuth、registry、dynamic discovery、delegation、Codex 与 Anthropic 围绕同一身份和生命周期事实源
  工作，减少跨 surface 漂移。

## 统一实机验收重点

1. 使用包含下划线的 server/tool 名连接 MCP，确认 UI 与模型看到 `mcp__server__tool`；打开旧会话，
   确认旧调用仍可 replay 且新事件只写 canonical name。
2. 调用返回 ResourceLink、EmbeddedResource、Audio、Image、structured/error mixed content 的 MCP，
   确认文本、媒体与诊断都可见且没有自动访问远端资源链接。
3. 启动一个 initialize 挂死/退出的 fake server，确认 Hermes 其余能力可用、无孤儿进程；恢复 server
   后等待自动 unpark，工具只出现一次。
4. 使用有效缓存 OAuth token 启动 MCP，确认无多余 metadata discovery；让 token 失效后确认受控刷新/
   重新授权，不阻塞其他 server。

## 回滚与限制

- 回滚必须整体撤销 canonical identity、content normalizer 和 lifecycle owner，不能只恢复单下划线写入，
  否则会形成双身份 registry。
- legacy name 兼容依赖已知 server provenance；无法无歧义解析的历史名字会明确失败，不做猜测性执行。
- 本阶段证明自动恢复和资源收敛，不替代阶段 11 的全进程 active-work drain，也不替代阶段 12 的
  24 小时真实长稳验收。
