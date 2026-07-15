# Hermes upstream v2026.7 手动吸收

本目录是 `v2026.7` 吸收周期在 Hermes 仓库内的唯一执行事实源。Doxie 仓库中的审计和分阶段计划负责说明“为什么吸收”，本目录负责记录“按什么基线实施、逐项如何决策、用什么证据验收”。

## 当前状态

- 当前阶段：阶段 0 已验收。
- 后续阶段：阶段 1 已解除验收门禁，但尚未开始实施。
- 吸收方式：只参考上游行为、失败场景和测试，禁止 merge、rebase 或 cherry-pick `upstream/main`。
- 上游快照：`upstream/main @ 6997dc81cd21dc88c6cb808a1fb3626b6ce71254`。2026-07-15 刷新请求因网络超时未更新引用，阶段 0 明确冻结当前已缓存快照。

## 文件职责

| 文件 | 职责 |
|---|---|
| `phase-0-design.md` | 阶段 0 边界、退出条件和禁止项 |
| `phase-0-baseline.md` | Git、测试、静态与 Doxie 合同基线 |
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

## 阶段 0 验收摘要

- Hermes 全量回归：28,766 passed，0 failed。
- Hermes 静态、分层、zero-debt、silent fallback 与账本门禁：全部通过。
- Doxie Gateway contract 与 `test:gates`：退出码 0。
- 验收期间补充修复：worker 交互响应闭环与 Doxie 工具历史详情投影均已通过专项回归和用户实机验收。
- 阶段 1：未开始。
- 用户验收：2026-07-15 已通过；阶段 0 实现允许提交并合入主开发分支。

完整命令、结果和已知非阻断项见 `phase-0-baseline.md`；用户验收入口见 `review-checklist.md`。
