# 阶段 0 设计：冻结基线与建立吸收护栏

## 目标

在任何上游能力进入本地实现前，先建立可重复、可审计、可中止的工程基线。阶段 0 不吸收业务能力，也不以“已有红项”为理由降低后续门禁。

## 输入与边界

- 本地冻结点：`10ccb9801fe773db3f5ccbb4f9a06987c9d04dd7`。
- 上游冻结点：`6997dc81cd21dc88c6cb808a1fb3626b6ce71254`。
- 共同基点：`1264fab15660b7341aee6123e332e58b91fe2bca`。
- 执行分支：`codex/upstream-absorption-v2026.7`。
- 执行 worktree：`/Users/yangwanquan/syngents/code/hermes-agent-upstream-absorption-v2026.7`。

## 护栏

1. 禁止 merge、rebase、cherry-pick `upstream/main` 或其提交。
2. 每项能力必须先在 `absorption-ledger.csv` 有稳定 ID、阶段、决策和验收口径。
3. `equivalent` 必须有本地行为证明；`skip-with-reason` 必须说明产品或架构边界；`superseded` 必须指向替代项。
4. 每阶段只修改该阶段 owner；发现跨阶段前置依赖时退回设计，不偷跑后续阶段。
5. 当前全量门禁红项必须在阶段 0 关闭；不允许把不稳定测试环境或重构遗留红项带入阶段 1。
6. 每阶段完成后停止，由用户验收后再更新阶段状态并进入下一阶段。

## 阶段 0 退出条件

- 固定 SHA 的 Git 差异证据可重复生成。
- 账本覆盖审计中的 49 个能力 ID、4 个架构决策和 47 个安全 ID，共 100 行且机器校验通过。
- `scripts/run_tests.sh` 全量通过，不访问未声明的真实网络服务，不依赖本机偶然安装的系统命令。
- v3/zero-debt 机器门禁、silent fallback scan、import-linter、ruff 全绿。
- Doxie Gateway contract 与前端 gates 全绿。
- `phase-0-baseline.md` 记录命令、结果、修复簇和最终提交。

退出条件全部满足后，阶段状态只能写“实施完成，待验收”。用户明确验收后才可写“已验收”。

## 实施结果

2026-07-15，以上机器退出条件全部满足；用户实机验收及验收期间补充回归随后通过，阶段状态更新为 **已验收**。验收证据见 `phase-0-baseline.md`，逐项确认见 `review-checklist.md`。

阶段 1 尚未开始；阶段 0 提交和合并只关闭本阶段，不夹带阶段 1 实现。
