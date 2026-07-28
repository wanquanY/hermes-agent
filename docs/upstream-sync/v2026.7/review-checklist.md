# v2026.7 分阶段验收清单

## 阶段 0

- [x] Git 基线 SHA、提交差异和路径交集可重复生成。
- [x] 100 项账本无重复 ID、无空决策、无无理由跳过。
- [x] Hermes 全量测试通过，且没有真实网络和本机服务偶合。
- [x] zero-debt/v3、silent fallback、import-linter、ruff 通过。
- [x] Doxie Gateway contract 与前端 gates 通过。
- [x] 阶段 1 代码尚未开始。
- [x] 用户明确批准阶段 0。

机器门禁完成时间：2026-07-15。用户实机验收于 2026-07-15 通过，阶段状态为
“已验收”；阶段 0 验收当时，阶段 1 尚未开始。

## 阶段 1

- [x] U1、R4 的本地 owner、上游来源和测试证据已经写入阶段设计与验收记录。
- [x] SQLite durability policy 只有一个 owner，真实连接工厂不再散落覆盖策略。
- [x] Gateway、worker、Team Mission 与 sidecar 的同步 SQLite 工作统一经过 async boundary。
- [x] 锁内只更新内存；文件写、数据库 I/O、agent cleanup 与 subscription poll drain 在锁外执行。
- [x] cancellation、timeout、并发 shutdown、慢 I/O 和 macOS `SIGKILL` reopen 均有真实行为测试。
- [x] Hermes 全量、静态、分层与观测门禁全部通过。
- [x] Doxie Gateway contract 与 desktop `test:gates` 全部通过。
- [x] 阶段 1 自动门禁完成后才允许开始阶段 2。
- [ ] 用户在 Phase 1-12 全部完成后统一执行实机场景并明确批准。

机器门禁完成时间：2026-07-16。用户已授权连续实施，Phase 1 等待最终统一验收。

## 后续阶段通用验收

- [ ] 本阶段账本条目全部有本地设计、实现和可复现测试证据。
- [ ] 当前 owner 符合 Hermes 架构，没有把责任写回已删除 monolith。
- [ ] 本阶段收益由行为测试证明，不以代码存在或 grep 命中代替。
- [ ] 全量回归与 Doxie 合同通过。
- [ ] 文档状态为“实施完成，待统一验收”；只有本阶段机器门禁通过后才进入下一阶段。
- [ ] 用户明确批准本阶段。

## 阶段 2

- [x] R1、M1、U2、D1 的当前 owner、上游参考与架构决策已经记录。
- [x] registry result 和 model-emitted arguments 都有 fail-closed producer contract。
- [x] 并发工具批次有 wall-clock deadline，已完成结果保持原始顺序。
- [x] effect disposition 可持久化、可 replay，且不会进入 provider wire format。
- [x] 风险事件只投影 finding ID 和状态，不复制原始工具输出。
- [x] 普通对话、Codex 与 MoA 共用一个 model usage recorder。
- [x] 4 个 reference 和 1 个 aggregator 产生 5 条真实归属 usage。
- [x] Hermes 最终全量复跑与 Doxie 门禁完成。
- [ ] 用户在 Phase 1-12 全部完成后统一执行实机场景并明确批准。

机器门禁完成时间：2026-07-16。Hermes 28,815 passed、0 failed；Doxie
Gateway contract 与 desktop `test:gates` 退出码 0。阶段 2 等待最终统一验收。

## 阶段 3

- [x] U3、SEC-CRED-01 至 06、SEC-NET-01 至 04 的当前 owner 和上游参考已记录。
- [x] 非可信、依赖型与 model-driving child env 共用一个显式权限 policy。
- [x] 动态 Hermes secret 和 Tier 1 credential 不能通过 inherited env 或 overlay 绕过。
- [x] foreground、background、watcher 和 browser CDP 日志有统一脱敏证据。
- [x] 真实本地 server 证明同 origin 保留凭据、跨 origin/scheme/port 剥离凭据。
- [x] installed hook 不能在最终 redirect boundary 重新注入 secret。
- [x] initial media URL、redirect、IPv6 scope、混合 DNS 地址和空 WS peer 均 fail-closed。
- [x] direct stream/subscription checkpoint 竞态与 durability 并发基准污染已从根因关闭。
- [x] Hermes 最终全量复跑与 Doxie 门禁完成。
- [ ] 用户在 Phase 1-12 全部完成后统一执行实机场景并明确批准。

机器门禁完成时间：2026-07-16。Hermes 28,855 passed、0 failed；Doxie
Gateway contract 与 desktop `test:gates` 退出码 0。阶段 3 等待最终统一验收。

## 阶段 4

- [x] 阶段 4 的 command/path/prompt/plugin/surface 条目及阶段 0 相邻决策均已复核。
- [x] shell mutation 共用 canonical variants 与 command-position owner，hardline 不能被 yolo 绕过。
- [x] session/API identity 与 filesystem artifact name 分离，所有路径在 I/O 前执行 deny floor。
- [x] patch Move、snapshot restore、media、`@file` 与 `/proc` 路径有 traversal/symlink 回归。
- [x] LSP、cron 与 threat scanner 共用 NFKC/invisible Unicode 事实源且不误报 Praxis。
- [x] plugin override 同时要求 manifest capability、operator opt-in 和 handler module ownership。
- [x] Dashboard/MCP/API key 0day 路径在同一进程组合执行通过，测试不依赖全局状态泄漏。
- [x] debug share 在采集前 consent，Tirith 连续故障 breaker 保持原 fail policy。
- [x] Agent runtime、compression 与 usage 只读本地 metadata，普通 turn 无隐式 catalog 网络。
- [x] Hermes 最终全量复跑、静态/分层门禁与 Doxie `test:gates` 完成。
- [ ] 用户在 Phase 1-12 全部完成后统一执行实机场景并明确批准。

机器门禁完成时间：2026-07-16。阶段聚合 1,642 passed、3 skipped；Hermes
28,931 passed、0 failed；Doxie Gateway contract 与 desktop `test:gates` 退出码 0。
阶段 4 等待最终统一验收。

## 阶段 5

- [x] R2、U6、RT5、RT6、RT7 的 identity、turn persistence、tool protocol 与 resume owner 已记录。
- [x] cross-wired run、tool id/argument sanitization、orphan trimming 和 resume freshness 有行为证据。
- [x] RT8 由当前子会话脱钩保留语义 supersede，理由与回归已记录。
- [x] Hermes 全量、静态/分层与 Doxie `test:gates` 完成。
- [ ] 用户在 Phase 1-12 全部完成后统一执行实机场景并明确批准。

机器门禁完成时间：2026-07-16。Hermes 28,962 passed、0 failed；Doxie
Gateway contract 与 desktop `test:gates` 退出码 0。阶段 5 等待最终统一验收。

## 阶段 6

- [x] U5、M4、C1 的 compression、stream-stale 与 interrupt queue owner 已记录。
- [x] compression verdict/lease 与 stream breaker 可跨 turn、restart、rotation 和 worker IPC 恢复。
- [x] legacy/Codex compaction 共享完成 seam，压缩期间普通输入 FIFO 且 control path 可中断。
- [x] C2 保留 Phase 8，B2/B3 保留 Phase 12，没有重复计入阶段收益。
- [x] Hermes 全量、静态/分层与 Doxie `test:gates` 完成。
- [ ] 用户在 Phase 1-12 全部完成后统一执行实机场景并明确批准。

机器门禁完成时间：2026-07-16。Hermes 28,994 passed、0 failed；Doxie
Gateway contract 与 desktop `test:gates` 退出码 0。阶段 6 等待最终统一验收。

## 阶段 7

- [x] A1-A5 的单一 gate、rule key、硬 deny、reason 与脱敏 owner 已记录。
- [x] model/registry/sequential/concurrent/runtime helper 只经统一 pre-tool policy seam。
- [x] plugin approve 只升级人审；无 responder、notify/hook/gate 异常 fail closed。
- [x] session 隔离、permanent persistence、explicit/reason-hash key 有行为测试。
- [x] hard deny 先于 yolo/off/allowlist/backend skip，且零 pending/handler。
- [x] CLI、Gateway、TUI、worker、MCP、Team Mission 与 Doxie 合同门禁完成。
- [ ] 用户在 Phase 1-12 全部完成后统一执行实机场景并明确批准。

机器门禁完成时间：2026-07-16。Hermes 29,016 passed、0 failed；静态、分层、
账本/单一 owner 与 Doxie desktop `test:gates` 全绿。阶段 7 等待最终统一验收。

## 阶段 8

- [x] C2、CB3、D2 的 hook spill、delegation headroom 与执行模式 owner 已记录。
- [x] sync/async 共用唯一 `SubagentExecutionService`、Activity/Run 和 terminal contract。
- [x] async 单任务、fan-out、root/child cancel、backpressure、shutdown 与 orphan 恢复有行为证据。
- [x] parent 先 terminal 或新回合已开始时，迟到 child terminal 仍持久化并归属原 Run/Activity；
  断线回放可收敛 Doxie subagent 面板。
- [x] Doxie 以生成合同覆盖 101 个 Hermes 内建工具，并为当前 7 个动态集成工具建立明确展示；
  `get_activity` 等新增工具不再回退为“工具调用 + 结构化数据”。
- [x] synthetic completion message、进程内业务 registry 与 completion queue 已移除。
- [x] hook/summary spill 安全、有界、可清理，预算幂等且 fan-out 顺序稳定。
- [x] Hermes 全量、静态/分层/zero-debt 与 Doxie `test:gates` 完成。
- [ ] 用户在 Phase 1-12 全部完成后统一执行实机场景并明确批准。

机器门禁完成时间：2026-07-16。阶段聚合 626 passed；Hermes 29,044 passed、
0 failed；Doxie Gateway contract 与 desktop `test:gates` 退出码 0。阶段 8 等待最终统一验收。

## 阶段 9

- [x] U7、CX1、CX2、CX4 的 route、compaction、reasoning、usage 与 verification owner 已记录。
- [x] OpenAI/Copilot 主调用、auxiliary 与 fallback 共用 Responses route policy。
- [x] Codex native compaction 不回落 legacy compressor，lease/transcript/usage 仍归统一事实源。
- [x] app-server reasoning delta 经统一 event projector，实时与最终 transcript 无重复。
- [x] verification aggregate、provider attribution、prompt caching 与 delegation headroom 有行为证据。
- [x] 阶段 9 专项与相邻协议门禁完成；MCP OAuth 阻塞明确转入且已由阶段 10 关闭。
- [ ] 用户在 Phase 1-12 全部完成后统一执行实机场景并明确批准。

机器门禁完成时间：2026-07-16。阶段聚合 766 passed、0 failed；阶段 10 已关闭标准全量中
发现的 MCP OAuth discovery 阻塞。阶段 9 等待最终统一验收。

## 阶段 10

- [x] R3、U8、RT1、RT2 的 canonical identity、rich content 与 lifecycle owner 已记录。
- [x] 所有新写入只使用 `mcp__server__tool`；legacy 只在唯一 registry read seam 迁移且歧义 fail closed。
- [x] Text/Image/Audio/ResourceLink/EmbeddedResource/structured/error content 共用 materializer。
- [x] stdio、SSE、streamable/legacy HTTP initialize 有 deadline，cancel/timeout 后资源收敛。
- [x] parked server 单 owner、指数 backoff、自恢复和原子 registry replace 有故障注入证据。
- [x] 有效 OAuth token 不做推测性 discovery，失效 token 的 refresh/discovery 有总 deadline。
- [x] 严格异步委派 `<1s` 合同保留，并由标准 runner 串行尾测避免 CPU 竞争污染。
- [x] Hermes 全量、静态/分层/observability 与 Doxie `test:gates` 完成。
- [ ] 用户在 Phase 1-12 全部完成后统一执行实机场景并明确批准。

机器门禁完成时间：2026-07-16。Hermes 1585 files、29,104 passed、0 failed；
observability 86 passed、13 skipped、1 xfailed；Doxie desktop `test:gates` 退出码 0。
阶段 10 等待最终统一验收。

## 阶段 11

- [x] U10、U11、RT3 的 execution claim、single writer 与 active-work owner 已记录。
- [x] Cron claim/heartbeat/completion 全部 compare owner，recurring next-run 与 claim 原子推进。
- [x] drain timeout 保留执行 owner，不把仍可能产生副作用的 Python 线程伪装成 terminal。
- [x] 同 board Kanban dispatcher 单 writer、不同 board 并行，锁异常 fail closed。
- [x] Gateway、API、cron 与 TUI worker 共用 active-work registry，drain 后结构化拒绝新 work。
- [x] `deadline_expired` 与 cancel 后 remaining 分离，clean marker 不再覆盖非优雅 shutdown。
- [x] API terminal run 与 SSE retention 解耦，迟到客户端不影响进程 liveness。
- [x] Chronos JWKS 因当前无 provider 按 `skip-with-reason` 关闭，未引入 dead code。
- [x] Hermes 全量、静态/分层/observability 与 Doxie `test:gates` 完成。
- [ ] 用户在 Phase 1-12 全部完成后统一执行实机场景并明确批准。

机器门禁完成时间：2026-07-16。Hermes 1,589 files、29,129 passed、0 failed；
observability 86 passed、13 skipped、1 xfailed；Doxie desktop `test:gates` 退出码 0。
阶段 11 等待最终统一验收。

## 阶段 12

- [x] U9、U12、RT4、B2、B3、B4、B6 的当前 owner、上游来源与收益已记录。
- [x] terminal/file cwd 按 environment + session 隔离，显式 gateway session 不读取共享 cwd。
- [x] core/skill slug collision 与 inert patch 按明确规则 fail closed。
- [x] mixed batch 只并行 effect-free segment，未知/MCP/plugin/terminal/交互调用保持 barrier。
- [x] sync/async provider client 共用 pool limit、idle expiry、proxy/no-proxy 且无 socket override。
- [x] vision active/queue budget、100 fanout、queue full、取消与 permit 回收有行为证据。
- [x] `NO_REPLY`/`[SILENT]` 持久化但不投影为页面系统消息或回复气泡。
- [x] fallback exhaustion cooldown 使用 monotonic，prompt caching toggle 覆盖 restore/fallback。
- [x] GMI 静态回退测试隔离真实网络，标准全量不再依赖外部 catalog 状态。
- [x] 阶段聚合、Hermes 全量、静态/分层/observability 与 Doxie `test:gates` 完成。
- [ ] 用户按 `phase-12-acceptance.md` 完成 Phase 1-12 统一实机场景并明确批准。
- [ ] 真实 OpenResty/Cloudflare 类反向代理长连接验证完成。
- [ ] 真实 macOS 强杀/reopen 与 24 小时 session/stream/cron soak 完成。

机器门禁完成时间：2026-07-16。阶段聚合 464 passed、10 skipped；Hermes 1,592 files、
29,188 passed、0 failed；observability 86 passed、13 skipped、1 xfailed；Doxie desktop
`test:gates` 退出码 0。阶段 1-12 代码和自动门禁完成，已合入 `dev`，等待最终统一实机
验收。

## 阶段 13

- [x] LG1-LG4、BR1 的当前 owner、上游来源、禁止项和收益已记录。
- [x] Learning Graph、Journey、mutation 与成长摘要同源实现完成。
- [x] 后台复盘 aux 路由、digest、技能所有权防护与隔离实现完成。
- [x] 阶段专项、Hermes 回归、静态/分层与 Hermes Dovie extension contract 完成。
- [ ] 用户按 `phase-13-acceptance.md` 完成双 profile、成长变更与 aux-review 实机验收。

机器门禁完成时间：2026-07-17。阶段专项 434 passed；Hermes 1,596 files、29,228
passed、0 failed；Ruff、compileall、import-linter、zero-debt 与 Web production build
全绿。Doxie desktop 的生成合同、成长页消费和视觉验收留待跨仓联调。
