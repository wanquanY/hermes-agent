# Hermes upstream v2026.7 手动吸收

本目录是 `v2026.7` 吸收周期在 Hermes 仓库内的唯一执行事实源。Doxie 仓库中的审计和分阶段计划负责说明“为什么吸收”，本目录负责记录“按什么基线实施、逐项如何决策、用什么证据验收”。

## 当前状态

- 当前阶段：阶段 5 自动验收完成，正在进入阶段 6 Compression 与流稳定状态机。
- 后续阶段：阶段 6-12 按计划连续实施，全部完成后统一用户验收。
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
- 用户验收：改为全部阶段完成后统一进行；阶段 checkpoint 仅保留在本地，尚未推送或合并回主开发分支。

各阶段完整命令、结果、收益与实机验收入口见对应 `phase-*-acceptance.md`；逐项签核
见 `review-checklist.md`。
