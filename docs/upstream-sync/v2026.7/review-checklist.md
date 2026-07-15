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
- [ ] 文档状态为“实施完成，待验收”，且下一阶段没有偷跑。
- [ ] 用户明确批准本阶段。
