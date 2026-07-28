# 阶段 10 设计：MCP canonical identity、富内容与可恢复生命周期

## 目标与账本范围

阶段 10 手工吸收 `R3`、`U8`、`RT1`、`RT2`。上游提交只作为协议事实与失败模式输入：

- `e01f58ff1`、`b70a4e753`：MCP wire name 使用无歧义的
  `mcp__server__tool`；Anthropic OAuth 路径不能再次拼接旧前缀。
- `1d98f8dd9`、`284a3cd47`：CallToolResult 的 ResourceLink、EmbeddedResource、
  Audio、Image、structuredContent 和 error content 都是有效协议内容，不能静默丢弃。
- `1f6836cd8`、`4638f3b43`：initialize 是启动生命周期的一部分，必须有内部 deadline；
  外层 discovery timeout 取消后不能留下 transport task、subprocess 或 FD。
- `2ea03d8c6`、`e412316b8`：启动失败的 server 应进入低成本 parked 状态，单 owner
  自探测恢复，禁止每次 discovery 都创建新连接风暴。

禁止 merge、rebase 或 cherry-pick 上游实现。Hermes 当前 registry、MCP background loop、
OAuth manager、dynamic list_changed、delegation toolset inheritance、Gateway/TUI 和 Codex runtime
migration 是本地约束。

## 阶段前事实与风险

1. 当前写入名为 `mcp_server_tool`。server 和 tool 都允许下划线，旧格式无法无上下文解析，
   parallel safety 因此还需要额外 provenance side map。
2. registry dispatch、schema lookup、toolset lookup 与 exact-tool filter 都可能读取持久化旧名；
   若在多个调用点分别兼容，会永久形成双写和多重迁移规则。
3. `mcp_tool.py` 同时拥有 transport、lifecycle、schema、result materialization、registry 与 OAuth
   恢复，文件已经超过 4000 行；新增逻辑必须拆到独立 owner。
4. stdio、SSE、新旧 HTTP 四条路径都直接 `await session.initialize()`。外层
   `wait_for(_connect_server)` 取消 `start()` 时，内部 `run()` 是 detached task，可能继续占用子进程。
5. 初始连接重试耗尽后 task 结束且 server 未进入 `_servers`；后续任意 discovery 又会从零重试。
6. result success 只保留 text/image/structuredContent，error 只拼 text，resource blob 只显示字节数。
7. OAuth provider 对有效 token 也抢先做真实 metadata discovery，导致普通 initialize 被多个 10 秒
   HTTP 请求阻塞；401 驱动的标准 discovery 尚未获得执行机会。

## 唯一 owner 设计

### Canonical MCP identity

新增 `tools.mcp_identity`，唯一负责 component sanitization、canonical wire name、MCP 判定和旧名读取。

- 所有新 schema、utility、dynamic refresh、Codex projector 与 Anthropic adapter 只写
  `mcp__server__tool`。
- canonical 名可以无歧义拆分；server/tool component 内的下划线不会改变边界。
- 旧 `mcp_server_tool` 只在 registry 的 name-resolution read seam 迁移。resolver 读取已注册 MCP
  provenance，并按最长 server component 匹配；不注册 legacy alias、不返回 legacy schema、不持久化旧名。
- 无已知 server 或存在同长歧义时 fail closed，不猜测。一次 read 命中会产生可观察 warning，调用与
  后续 transcript 只使用 canonical name。
- delegation inheritance 按 toolset provenance 工作；parallel safety 按 canonical name 的 server
  component 与注册 provenance 交叉确认；OAuth、reload 与 Codex migration 不再自行拼前缀。

### MCP content normalizer

新增 `tools.mcp_content`，把 SDK block 归一为 Hermes 可安全消费的文本/媒体投影。

- Text 原样保留；Image/Audio/EmbeddedResource blob 先做严格 base64 与大小上限校验，再写共享 cache；
  MIME 决定安全扩展名，文件名不使用远端 path。
- EmbeddedResource text 保留 URI、MIME 与正文；ResourceLink 保留 URI/name/description/MIME/size，
  并提示 canonical `mcp__server__read_resource`，不自动访问远端 URI。
- structuredContent 与模型向 content 同时存在时都保留；error path 复用相同 normalization，只把顶层
  envelope 标为 error，避免诊断证据丢失。
- `resources/read` 复用同一 materializer，不再把 blob 降级成字节数占位符。
- 单 block 和单 result 均有上限；坏 block 产生可见 marker 与日志，但不使其他合法 block 丢失。

### 有界 initialize 与 cancellation ownership

新增 `tools.mcp_lifecycle`，唯一提供 initialize deadline、task cancellation/drain 与 parked backoff 值对象。

- stdio、SSE、新 HTTP、legacy HTTP 四条路径都调用同一 `bounded_initialize`；timeout 使用
  `initialize_timeout`，缺省继承 `connect_timeout`，必须大于零且有全局上限。
- `MCPServerTask.start` 自己拥有并 await `run` task；caller cancellation 会显式 cancel-and-drain，
  不允许 detached task。
- transport context、ClientSession、httpx client 和 stdio PID tracking 都仍在同一 run task 进入/退出；
  timeout/cancel 必须穿过 async-with finally 后才向 discovery 返回。
- registry 仅发布 connected session 的完整工具快照；任何 disconnect/park/shutdown 都先原子撤销该
  server 的 canonical names，杜绝 stale handler。

### Parked server 恢复状态机

`MCPServerTask` 是单 server lifecycle owner，显式状态为
`created -> starting -> connected -> parked -> connected|stopping -> stopped`。

- server 在启动前即进入 `_servers`，因此重复 discovery 只读取同一 owner，不创建第二个 task。
- 初始/重连预算耗尽后撤销工具、记录 last error 与 next probe，进入 parked；task 保持低成本等待。
- parked timer 每个 backoff 窗口只允许一次 probe；失败指数 backoff 到配置上限，成功后先 discover，
  再用 registry lock 内的 replace 操作发布完整工具集并切换 connected。
- list_changed、manual reconnect 和 parked revive 都调用同一 publish seam；新旧工具 diff 在一次同步
  mutation 中完成，观察者不会看到半注册快照。
- shutdown 唤醒 parked waiter、取消 refresh tasks、等待 transport teardown、撤销 registry，最终 stopped。
- `get_mcp_status` 暴露 state、last_error、next_probe_at 与 tools；banner 的 connected 兼容字段由 state 派生。

### OAuth 启动边界

- 有效缓存 token 直接尝试 MCP initialize；不为“可能发生的 401”预先访问 metadata endpoint。
- token 已失效且具备 refresh token 时，才执行 bounded metadata prefetch；总 deadline 内失败后交给标准
  OAuth 401/discovery 流，不阻塞其他 server。
- manager 仍是 provider/cache/watch/recovery 唯一 owner，MCPServerTask 不复制 OAuth 状态。

## 可验收退出条件

1. 新注册、dynamic refresh、utility、Anthropic OAuth、Codex projector 只产生双下划线 name；registry
   可在已知 server 下读取旧单下划线持久调用，schema/dispatch 后不再写回旧名；歧义 fail closed。
2. fake CallToolResult 覆盖 ResourceLink、EmbeddedResource text/blob、Audio、Image、structuredContent、
   error mixed blocks、坏 base64 与超限；结果字段、媒体 tag 和错误证据不丢失且不自动 fetch URL。
3. 四种 transport 的 initialize 正常、hung、exception、caller cancel 均有 fault injection；task、session、
   client、stdio child、FD/provenance/registry 在 deadline 后收敛。
4. parked server 单 owner、自探测 backoff、恢复、恢复时 tools changed、并发 discovery、manual reload 与
   shutdown 均有状态机测试；恢复只发布一次，无 discovery storm。
5. 有效 OAuth token 启动不访问 discovery；401 正常转入 SDK flow；失效 refresh token 的 prefetch 有总界。
6. MCP tool/OAuth/dynamic/content/lifecycle、delegation inheritance、Codex migration、Gateway capability、
   静态/分层/账本、Hermes 全量与 Doxie 门禁全部通过。

## 阶段收益

- Hermes 与 Codex/Claude MCP 生态使用同一无歧义 wire identity，历史会话可读但系统不再制造旧债。
- MCP 的资源、音频、图片、结构化结果和错误诊断完整到达 agent 与各 surface。
- 坏或挂死的 MCP server 不再拖死启动或泄漏进程；恢复无需重启 Hermes，且不会形成重连风暴。
- registry、OAuth、dynamic discovery、delegation 与 transport 围绕同一身份和生命周期事实源工作。
