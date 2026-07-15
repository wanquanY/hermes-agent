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
