# Hermes upstream v2026.7 手动吸收

本目录是 `v2026.7` 吸收周期在 Hermes 仓库内的唯一执行事实源。Doxie 仓库中的审计和分阶段计划负责说明“为什么吸收”，本目录负责记录“按什么基线实施、逐项如何决策、用什么证据验收”。

## 当前状态

- 当前阶段：阶段 1-12 实现与自动门禁全部完成，等待统一用户实机验收。
- 后续阶段：建立本地 Phase 12 checkpoint；验收前不 push、不合并回主开发分支。
- 吸收方式：只参考上游行为、失败场景和测试，禁止 merge、rebase 或 cherry-pick `upstream/main`。
- 上游快照：`upstream/main @ 6997dc81cd21dc88c6cb808a1fb3626b6ce71254`。2026-07-15 刷新请求因网络超时未更新引用，阶段 0 明确冻结当前已缓存快照。

## 文件职责

| 文件 | 职责 |
|---|---|
| `phase-0-design.md` | 阶段 0 边界、退出条件和禁止项 |
| `phase-0-baseline.md` | Git、测试、静态与 Doxie 合同基线 |
| `phase-1-design.md` | 阶段 1 owner、异步 I/O 边界、durability policy 与退出条件 |
| `phase-1-acceptance.md` | 阶段 1 自动门禁、实机验收入口、收益与限制 |
| `phase-2-design.md` | 阶段 2 工具合同、deadline、effect/risk 与计量 owner 设计 |
| `phase-2-acceptance.md` | 阶段 2 专项、全量、静态与最终实机验收重点 |
| `phase-3-design.md` | 阶段 3 child env、脱敏、redirect credential 与 SSRF 安全设计 |
| `phase-3-acceptance.md` | 阶段 3 专项、全量、静态、Doxie 门禁与最终实机验收重点 |
| `phase-4-design.md` | 阶段 4 命令、路径、提示、插件权限、调试与熔断安全设计 |
| `phase-4-acceptance.md` | 阶段 4 安全专项、运行期元数据性能、全量与 Doxie 验收证据 |
| `phase-5-design.md` | 阶段 5 RunIdentity、turn persistence、tool protocol 与 resume 生命周期设计 |
| `phase-5-acceptance.md` | 阶段 5 身份、竞态、tool protocol、全量与 Doxie 联调证据 |
| `phase-6-design.md` | 阶段 6 compression verdict、stream-stale breaker 与 interrupt queue 设计 |
| `phase-6-acceptance.md` | 阶段 6 稳定状态机、worker IPC、全量与 Doxie 联调证据 |
| `phase-7-design.md` | 阶段 7 单一 approval gate、硬 deny、rule key、reason 与脱敏设计 |
| `phase-7-acceptance.md` | 阶段 7 Approval 跨 surface、全量与 Doxie 联调证据 |
| `phase-8-design.md` | 阶段 8 Subagent 统一 Activity/Run 生命周期、取消与有界上下文设计 |
| `phase-8-acceptance.md` | 阶段 8 同步/异步、fan-out、spill、全量与 Doxie 联调证据 |
| `phase-9-design.md` | 阶段 9 Responses route、Codex compaction、reasoning 与 verification 设计 |
| `phase-9-acceptance.md` | 阶段 9 Codex/Responses/verification 专项证据与阶段 10 已关闭红项来源 |
| `phase-10-design.md` | 阶段 10 MCP canonical identity、富内容、initialize 与恢复状态机设计 |
| `phase-10-acceptance.md` | 阶段 10 MCP 专项、全量、静态、Doxie 门禁与统一实机验收重点 |
| `phase-11-design.md` | 阶段 11 Automation claim、Kanban single writer 与 active-work drain 设计 |
| `phase-11-acceptance.md` | 阶段 11 多进程所有权、进程排空、全量与 Doxie 验收证据 |
| `phase-12-design.md` | 阶段 12 cwd、mixed batch、provider pool、vision、silence、fallback 与 caching 设计 |
| `phase-12-acceptance.md` | 阶段 12 专项、全量、静态、Doxie 证据与统一实机验收入口 |
| `absorption-ledger.csv` | 全量稳定 ID、阶段归属、决策和验收口径 |
| `review-checklist.md` | 用户逐阶段验收清单 |
| `evidence/baseline.json` | 可机读 Git 基线 |
| `evidence/overlap-files.txt` | 双方路径交集 |
| `evidence/runtime-core-overlap-files.txt` | 运行时核心路径交集 |

## 重新生成 Git 证据

```bash
python scripts/upstream_absorption_baseline.py \
  --output-dir docs/upstream-sync/v2026.7/evidence \
  --local-ref 10ccb9801fe773db3f5ccbb4f9a06987c9d04dd7 \
  --upstream-ref 6997dc81cd21dc88c6cb808a1fb3626b6ce71254
```

执行证据必须使用固定 SHA，不能使用会漂移的分支名覆盖已验收证据。

## 当前待验收摘要

- 阶段 0：2026-07-15 已验收并合入阶段 1 起点。
- 阶段 1：U1、R4 已按当前架构手工吸收；U4、B1 由 R4 supersede。
- Hermes 全量回归：28,789 passed，0 failed。
- Hermes 静态、分层、zero-debt、silent fallback 与账本门禁：全部通过。
- Doxie Gateway contract 与 `test:gates`：退出码 0。
- 阶段 2：R1、M1、U2 已实现；专项 271 passed、0 failed；Hermes 全量
  28,815 passed、0 failed；Doxie contract/gates 退出码 0。
- 阶段 3：U3、SEC-CRED-01 至 06、SEC-NET-01 至 04 已实现；Hermes 全量
  28,855 passed、0 failed；Doxie contract/gates 退出码 0。
- 阶段 4：SEC-CMD、SEC-PATH、SEC-PROMPT、SEC-PLUGIN 与 SEC-SURFACE 的阶段 4
  条目已实现；安全聚合 1,642 passed、3 skipped；Hermes 全量 28,931 passed、0 failed；
  Doxie contract/gates 退出码 0。
- 阶段 5：R2、U6、RT5、RT6、RT7 已按当前架构吸收；RT8 由子会话脱钩保留
  supersede；Hermes 全量 28,962 passed、0 failed；Doxie contract/gates 退出码 0。
- 阶段 6：U5、M4、C1 已按当前架构吸收；C2 保留在阶段 8，B2/B3 保留在阶段 12；
  Hermes 全量 28,994 passed、0 failed；Doxie contract/gates 退出码 0。
- 阶段 7：A1-A5 已按当前架构吸收；审批编排归一为单一 gate，用户 deny 成为
  backend-independent hard boundary，rule key、拒绝原因和展示脱敏跨 surface 一致；
  Hermes 全量 29,016 passed、0 failed；Doxie contract/gates 退出码 0。
- 阶段 8：C2、CB3、D2 已按当前架构吸收；同步/异步 delegation 共用唯一
  `SubagentExecutionService`、Activity/Run 与 typed event，移除 synthetic completion message；
  hook 与 fan-in summary 具备安全有界 spill；Hermes 阶段聚合 626 passed、0 failed，
  全量 29,044 passed、0 failed；Doxie contract/gates 退出码 0。
- 阶段 9：U7、CX1、CX2、CX4 已按当前架构吸收；route policy、native compaction、
  reasoning projector 与 verification aggregate 专项聚合 766 passed、0 failed；运行期
  catalog cache-only 修正 112 passed。标准全量发现的 MCP OAuth discovery 阻塞已由阶段 10
  的有界 lifecycle 与 cache-first OAuth 启动边界从根因关闭。
- 阶段 10：R3、U8、RT1、RT2 已按当前架构吸收；MCP 使用无歧义 canonical identity，
  rich/error content 统一 materialize，四类 transport initialize 有界收敛，parked server
  单 owner/backoff 自恢复；Hermes 全量 29,104 passed、0 failed，静态/分层/观测门禁与
  Doxie desktop `test:gates` 全绿。
- 阶段 11：U10、U11、RT3 已按当前架构吸收；cron execution owner/heartbeat、Kanban
  board-scoped single writer 与 transport-neutral active-work drain 共用明确生命周期边界；
  Chronos JWKS 因本地无 provider 按 `skip-with-reason` 关闭。Hermes 全量 29,129 passed、
  0 failed；Doxie desktop `test:gates` 退出码 0。
- 阶段 12：U9、U12、RT4、B2、B3、B4、B6 已按当前架构吸收；session-scoped cwd、
  collision/inert fail-closed、effect-safe mixed batch、统一 HTTP pool、silence projection、
  fallback cooldown、vision budget 与 prompt caching toggle 均有行为证据。阶段聚合
  464 passed、10 skipped；Hermes 标准全量 1,592 files、29,188 passed、0 failed；
  observability 86 passed、13 skipped、1 xfailed；Doxie desktop `test:gates` 退出码 0。
- 发布级长稳证据：真实反向代理、真实 macOS reopen 与 24 小时 soak 尚待统一实机验收，
  未被自动门禁结果替代。
- 用户验收：改为全部阶段完成后统一进行；阶段 checkpoint 仅保留在本地，尚未推送或合并回主开发分支。

各阶段完整命令、结果、收益与实机验收入口见对应 `phase-*-acceptance.md`；逐项签核
见 `review-checklist.md`。
