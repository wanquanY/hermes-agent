# 阶段 12 设计：正确性尾项、受控并发与本轮关闭

## 目标与账本范围

阶段 12 手工吸收 `U9`、`U12`、`RT4`、`B2`、`B3`、`B4`、`B6`。参考的上游
commit/branch 族为：

- `U9`：`7e84d2b5a43d`、`370ebf2d3509`、`dfeedf613dcd`。
- `U12`：`271a9d8ec6ad`。
- `RT4`：`8324dd19c`、`51c1ba697`。
- `B2`：`b561815eb`。
- `B3`：`fae920642`、`5f91da2d1`、branch tip `0b50fe957`。审计不能只看 tip；
  `5f91da2d1` 虽只修测试时序，其祖先 `fae920642` 包含本地尚缺的 fallback exhaustion
  cooldown 产品行为。
- `B4`：`c21cbe2fd`。
- `B6`：`e6184c1cc`、`400891d54`。

吸收继续遵守本轮总原则：只提取最终语义和失败场景，按当前 Hermes owner 手工实现；不
merge/rebase/cherry-pick 上游，不引入上游 UI、旧 adapter 或第二套 runtime。

## 阶段前缺口

1. 共享 terminal environment 只有一个可变 `env.cwd`。两个 Gateway conversation 复用同一环境时，
   后完成的命令会把另一个会话的 cwd 当成默认目录；file tools 也会读取同一个陈旧事实。
2. skill slash command 只按规范化 slug 注册，可能覆盖 core command、core alias 或另一个 skill。
3. patch parser 接受只有 context line 的 hunk，并可能把 search/replace 完全相同的 hunk 当成功。
4. tool batch 只有“整批并发/整批串行”二选一；一个副作用 barrier 会让其前后独立 read-only call
   全部失去并发机会，直接整批并发又会破坏顺序和外部副作用安全。
5. provider client 使用自定义 socket options transport。它既替换 httpx/OS 默认 socket 行为，也在
   main/auxiliary 构造点之间产生策略漂移。
6. vision fan-out 没有 process-wide active/queue budget，图片下载、base64 与模型调用可随请求数同时
   放大。
7. Gateway streaming 会在 whole-response filter 生效前把 `NO_REPLY`/`[SILENT]` 直接投影到聊天页。
8. fallback chain 在非 rate-limit 失败后跨 turn 立即重放整个链，对大上下文重复编码和发送；现有
   rate-limit 恢复虽已用 monotonic clock，却缺少非 rate-limit exhaustion gate。
9. prompt caching 没有全局 kill switch；fallback/model restore 还可能从陈旧 snapshot 恢复旧策略。

## Owner 与最终设计

### U9-A：session-scoped terminal cwd

`tools.terminal_cwd_registry` 是 `(environment_key, session_key) -> cwd` 的唯一逻辑事实源：

- terminal 每次执行按 `explicit workdir > session cwd > configured cwd` 解析。
- 同一共享 environment 的执行与 cwd capture 由 per-environment guard 线性化，避免 A/B 命令完成顺序
  把 cwd 写到错误 session。
- foreground 成功后记录 environment 实际 cwd；background spawn 使用该 session 的解析结果。
- file tools 先读相同 registry。若存在明确 Gateway session、但尚无本 session 记录，必须回退配置的
  workspace root，不能读取另一个 session 留在共享 `env.cwd` 上的状态。
- environment 真正销毁时整体清理其 cwd facts；一次 session 命令结束不删除会话 cwd。

### U9-B：skill/core command namespace

`agent.skill_commands` 在发现阶段执行 namespace 裁决：

- core command 名和 core alias 永远优先，skill 不得静默覆盖。
- skill 名经同一 slug normalization 后 first-wins；后续碰撞项不注册。
- 裁决只影响 slash-command registry，不改变 skill 目录加载和显式 `skill_view` 能力。

### U9-C：patch inert hunk fail-closed

`tools.patch_parser` 保留当前 parser/application owner，但增加三个不变量：

- context-only hunk 不计作变更；整个 patch 无真实变更时明确拒绝。
- search 与 replacement 完全相同时跳过，最终仍由“至少一个真实变更”门禁裁决。
- 报错使用原始 hunk index，跳过 inert hunk 后诊断仍能准确指向输入。

### U12：按 effect 分段的 mixed tool batch

`agent.tool_dispatch_helpers._plan_tool_batch_segments` 只负责纯规划，executor 只负责执行：

- read-only/effect-free 连续区间可并发；已知 path-scoped file operations 只有在路径互不重叠时并发。
- interactive、terminal、未知 plugin/MCP effect、参数无法解析和路径无法证明的 call 是顺序 barrier。
- MCP transport 声明“能并发接收请求”不等价于“业务调用无副作用”，因此未知 MCP fail-closed 串行。
- call 不越过 barrier；每段输出仍按原 tool-call 顺序写回。
- aggregate tool-output budget 与 pending steer 只在全 turn 完成后执行一次，不能逐段重复截断/注入。

### RT4：统一 provider HTTP pool 生命周期

`agent.process_bootstrap.build_provider_http_client` 是 main 与 auxiliary OpenAI-compatible client 的共同
factory：

- 使用 httpx `Limits(max_keepalive_connections=20, max_connections=100,
  keepalive_expiry=20s)`；允许用明确 Hermes env 变量调整。
- `read=None` 支持长 SSE，connect/write/pool 保持有界。
- 不提供自定义 `socket_options` transport，保留 httpx/OS 默认的 TCP_NODELAY、TLS 和 chunked/SSE 行为。
- proxy 由现有 `_get_proxy_for_base_url` 一次解析并尊重 `NO_PROXY`；client 使用 `trust_env=False`，避免
  transport 再次从环境选择不同路径。
- sync/async auxiliary client 与 main AIAgent 都注入独立 client；生命周期随其 SDK client 关闭，不把
  已关闭 transport 写回长期复用的 kwargs。

### B4：vision active/queue capacity

vision handler 外层拥有 process-wide budget：

- active 默认 `min(host CPU, 4)`（默认配置固定为 4），queue 默认 256；配置和环境可显式覆盖。
- 只有获得 active slot 后才进入 native/aux handler，图片下载、读取、base64 和模型请求不会由等待队列
  提前分配。
- queue 满在 payload allocation 前返回结构化 tool error。
- acquisition 使用 event-loop 友好的非阻塞轮询；等待 task 取消不会遗留 executor waiter 或吞掉 semaphore
  permit。active/waiting/peak 由 snapshot 暴露给测试和诊断。

### B2：持久化与投影分离的 silence protocol

`hermes_gateway.response_filters` 是 exact/partial marker 的共同事实源：

- 只对成功 turn 的完整 `NO_REPLY`、`NO REPLY`、`SILENT`、`[SILENT]` 生效；空输出、失败 turn、正文中
  提及 marker 均不抑制。
- streaming 在 buffer 仍可能成为 marker 时暂缓 preview；stream end exact match 时 best-effort 删除旧
  preview，并清空所有 delivery flags。
- non-streaming 与 queued-followup delivery 共用 exact predicate。
- control marker 先进入 transcript persistence，再只把 outbound response 变为空；模型保留自己的决策，
  用户页面不显示内部控制 token。

### B3：monotonic fallback exhaustion gate

- rate-limit/billing 继续使用既有 60 秒 cooldown。
- 非 rate-limit failure 走过非空 fallback chain 并耗尽时，在同一 `_rate_limited_until` 上以 `max` 建立
  5 秒 gate，绝不缩短已有窗口。
- invalid/deduplicated/unconfigured entry 的递归跳过必须携带原 `FailoverReason`，不能在递归中把 60 秒
  原因退化成 5 秒。
- deadline 的建立和 restore 比较只使用 `time.monotonic()`；系统 wall clock 前跳/回拨均不影响 gate。

### B6：prompt caching 全路径 kill switch

- `prompt_caching.enabled` 默认 true，false 时 native Anthropic、OpenRouter、第三方 Anthropic-compatible
  全部返回 `(use_cache=False, native_layout=False)`。
- model switch/fallback 每次按目标 provider/model 重新计算；restore primary 也读取 live config，而非恢复
  snapshot 中可能过期的 cache flag。
- 非布尔配置 fail-safe 为“保持当前兼容默认 enabled”并告警，避免拼写错误静默改变计费行为。

## 可验收退出条件

1. U9 三项分别有独立 regression；两个 Gateway session 的 terminal/file relative path 不交叉。
2. mixed batch 的 barrier 顺序、路径重叠、unknown/MCP fail-closed、whole-turn budget/steer exactly-once
   有行为测试。
3. main/aux sync/async provider clients 共用 pool policy；proxy/NO_PROXY、pool limits 和无 socket override
   有测试。
4. 100 项 vision fan-out 的 `peak_active` 不超过预算，完成后 active/waiting 为零；取消和 queue-full 不
   泄漏 permit，等待项不进入 payload handler。
5. silence marker 的 single-shot、token-by-token、preview retract、无 delete support、正文 passthrough、
   queued followup 与 persistence 分离全部通过。
6. fallback exhaustion、60 秒窗口保留、递归 reason、wall-clock jump 与 prompt-cache live restore 通过。
7. 阶段专项、Hermes 标准全量、ruff/compileall/import-linter/observability/账本、Doxie desktop
   `test:gates` 全绿。
8. 自动门禁完成后建立本地 checkpoint；真实 macOS reopen、真实 provider/reverse-proxy 长稳以及 24 小时
   session/stream/cron soak 保留给用户统一实机验收，未实际执行前不得写成“已通过”。

## 收益

- conversation 共享 runtime 不再共享 cwd 事实；相对文件操作不会偶发落到另一个 worktree。
- skill/patch 输入在 registry/parser owner 处 fail-closed，消除静默覆盖与“成功但无变更”。
- mixed batch 只在 effect 可证明时恢复并发，性能收益不以重排副作用为代价。
- provider 连接恢复到标准 HTTP pool 生命周期，避免自定义 socket transport 对 TLS/SSE/反向代理的破坏。
- vision fan-out 的内存与外部请求放大受到 active+queue 双界限约束。
- Gateway 内部控制 token 不再泄漏到 Doxie/聊天平台，fallback storm 与 prompt cache 计费策略都有明确
  的跨 turn 控制边界。

## 回滚与限制

- terminal registry 与 terminal/file consumer 必须整体回滚；只撤一侧会重新制造双事实源。
- mixed planner、segmented executor 和 whole-turn finalization 是一个原子协议，不能只保留分段执行。
- HTTP factory 回滚不能恢复 provider-specific socket workaround；若发现特定 provider 问题，应先用
  request/response 证据修正统一 factory。
- semaphore 是单进程边界；多进程部署的全局 vision quota 仍需外部 durable coordinator。
- 自动化测试证明 pool 结构、取消和故障边界，不等价于真实网络 24 小时 soak；长稳结果必须单独记录。
