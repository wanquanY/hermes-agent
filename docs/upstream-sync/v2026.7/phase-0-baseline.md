# 阶段 0 基线记录

## Git 冻结

| 项目 | 值 |
|---|---|
| 本地 HEAD | `10ccb9801fe773db3f5ccbb4f9a06987c9d04dd7` |
| 上游快照 | `6997dc81cd21dc88c6cb808a1fb3626b6ce71254` |
| 共同基点 | `1264fab15660b7341aee6123e332e58b91fe2bca` |
| 本地独有提交 | 818 |
| 上游独有提交 | 6313 |
| 本地改动文件 | 1728 |
| 上游改动文件 | 4808 |
| 路径交集 | 638 |
| 运行时核心交集 | 136 |

旧计划记录的本地独有提交为 816；新开发分支合并提交及其后继使当前冻结值变为 818。其余 Git 路径统计未漂移。

## 上游刷新

2026-07-15 执行 `git fetch upstream --prune`，25 秒后网络超时。命令未修改工作树，阶段 0 使用当天此前已经成功获取并完成审计的 `6997dc81...` 固定快照。验收证据不以未固定的 `upstream/main` 名称为准。

## 首次 Hermes 全量基线

命令：`scripts/run_tests.sh`

结果：28,120 passed，290 failed；108 个文件有失败，另有 3 个文件未完成，耗时 787.3 秒。失败主要归为：

1. zero-debt 合并后陈旧测试仍引用已删除的 `gateway/*`、旧 store API 和旧静态 manifest 结构。
2. 测试 schema/fixture 未包含新的 `activity_id`、`user_id` 和 component repository 接口。
3. 单元测试误触模型探测、Tirith 安装等真实网络，导致超时并拖慢巨型测试文件。
4. 少量生产缺口，包括 ACP MCP tool-surface 刷新、plugin image normalization、worker RPC allowlist 和文件尺寸边界。
5. `tests/run_agent/test_run_agent.py` 因累计真实网络等待超过 600 秒被 runner 终止，不能视为完整结果。

## 根因关闭结果

首次基线中的红项没有通过删除断言、放宽失败条件或跳过测试关闭，而是按责任边界完成了以下修复：

1. 把应用服务从 `hermes_agent.domain` 迁入 `hermes_agent.application`，把 SQLite 组装、迁移与 repository 实现迁入 `hermes_agent.composition`，并用 import-linter 固化依赖方向。
2. 拆分 Team Mission Gateway 巨型模块，把 leader prompt、memory context、runtime lifecycle 与纯值转换放回独立 owner。
3. 隔离模型目录、Tirith、浏览器、Copilot 等测试中的真实网络和本机安装状态，使测试输入可复现。
4. 修复 LSP 退出、Web server 清理和 TUI disconnect checkpoint 的资源生命周期；`detach_transport` 返回时持久化检查点已经完成，不再把 SQLite 句柄交给后台线程后立即关闭。
5. 更新 schema、fixture、manifest 与契约测试，使它们验证当前 v3/zero-debt 架构，而不是已退役的 gateway/store owner。

这些修改只用于建立可验证的阶段 0 基线和关闭既有架构债务，没有开始阶段 1 的上游能力吸收。

## 最终 Hermes 验收结果

| 门禁 | 命令 | 结果 |
|---|---|---|
| 全量回归 | `scripts/run_tests.sh` | 1544 files；28,766 passed；0 failed；762.6s；28 workers |
| 静态检查 | `.venv/bin/ruff check .` | All checks passed |
| 字节码编译 | `.venv/bin/python -m compileall -q agent hermes_agent hermes_cli hermes_gateway hermes_team_mission tools tui_gateway channels cron` | 通过 |
| 分层依赖 | `.venv/bin/lint-imports --config .importlinter` | 153 files、307 dependencies；1 contract kept、0 broken |
| zero-debt / silent fallback | `scripts/run_tests.sh tests/observability/test_zero_debt_gates.py tests/observability/test_silent_fallback_scan.py tests/observability/test_silent_swallow_whole_repo.py` | 16 passed、0 failed、1 expected xfail；39.8s |
| 吸收账本 | `scripts/run_tests.sh tests/observability/test_upstream_absorption_ledger.py` | 3 passed、0 failed；100 行账本校验通过 |
| 补丁格式 | `git diff --check` | 通过 |

`scripts/zero_debt/status.py --json` 的 P1/P2 机器判定均为 `pass`。P2 的 closure 仍报告 `human_signoff:not_pending`；这是既有 zero-debt 人工签核元数据状态，不是代码或机器门禁失败，也不替代本轮阶段 0 的用户验收。

## 最终 Doxie 集成验收结果

| 门禁 | 结果 |
|---|---|
| `hermes:check-gateway-contract` | 205 methods in sync；98 个 active desktop methods 被 135 个 requiredMethods 覆盖 |
| Vitest | 263 files；1,967 passed；3 skipped；0 failed |
| Electron Node | 391 passed；0 failed |
| Source Node contract | 19 passed；0 failed |
| TypeScript | `vue-tsc --build` 通过 |
| ESLint | 0 errors；70 个仓库既有 warnings，不阻断门禁 |
| Frontend boundaries | 通过 |

统一命令 `corepack pnpm --filter @dovie/desktop run test:gates` 最终退出码为 0。

## 用户验收期间补充修复

用户实机验收发现 worker 模式下的交互响应只解除阻塞、未发布
`interaction.resolved`，同时 Doxie 历史投影把工具生命周期状态
`completed` 误当作工具详情。阶段 0 在验收关闭前完成源头修复：

1. worker 只有在实际找到并解除待处理请求后才发布 resolved 生命周期；
2. clarify/approval 保留非敏感 choice，secret/sudo 答案不进入事件流；
3. Doxie 工具详情只读取 detail/context/arguments 等展示字段，不再读取 status。

补充验证结果：Hermes 相关协议、路由、持久化与响应契约 174 条测试通过；
Doxie 时间线相关 71 条测试及 TypeScript 类型检查通过；用户实机验收通过。

## 阶段结论

当前状态：**阶段 0 已验收**。

- 阶段 1 代码尚未开始。
- 用户于 2026-07-15 明确验收通过。
- 阶段 0 验收提交 SHA 以本文件所在提交为准；合并记录以主开发分支历史为准。
