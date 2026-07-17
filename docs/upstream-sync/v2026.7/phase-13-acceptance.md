# 阶段 13 验收：学习图谱、成长时间线与自我改进

## 当前状态

- 代码实现：完成，位于 `codex/absorb-upstream-learning-journey`，尚未提交或推送。
- 专项测试：434 passed、0 failed。
- Hermes 回归与静态门禁：1,596 files、29,228 passed、0 failed；Ruff、
  compileall、import-linter 和 Web production build 通过。
- Hermes Dovie extension 合同：通过；Doxie desktop 生成合同和成长页视觉消费不在本分支
  修改范围内，保留为用户联调验收项。

## 自动验收清单

- [x] Learning Graph 节点、边、cluster、timeline 不变量。
- [x] profile home 并发隔离与无跨 profile mutation。
- [x] canonical/legacy memory id、stale id、原子 edit/delete。
- [x] skill detail/edit/archive/pinned 防护与缓存失效。
- [x] `profile.growth.summary` 与图谱计数同源。
- [x] Gateway graph/detail/edit/delete RPC 合同和权限分类。
- [x] `hermes journey --json/list/edit/delete` CLI 合同。
- [x] background review same-model warm-cache 与 routed digest 分流。
- [x] background review session、memory、tool、stdout 隔离与异常容错。
- [x] background skill 精确先读后写、所有权保护和可恢复归档。
- [x] `ruff`、`compileall`、分层检查、相关全量回归。

## 自动验收证据

### 阶段专项

```bash
.venv/bin/pytest -q \
  tests/agent/test_learning_graph.py \
  tests/agent/test_learning_mutations.py \
  tests/agent/test_background_review_routing.py \
  tests/agent/test_thread_scoped_output.py \
  tests/hermes_cli/test_journey.py \
  tests/run_agent/test_background_review.py \
  tests/tools/test_memory_tool.py \
  tests/tools/test_skill_manager_tool.py \
  tests/tools/test_skills_tool.py \
  tests/test_hermes_agent_profile_registry.py \
  tests/test_dovie_gateway_contract.py \
  tests/test_manifest_method_modules_coverage.py \
  tests/tui_gateway/test_ws_dispatch.py \
  tests/tui_gateway/test_review_summary_callback.py
```

结果：434 passed、0 failed。

### 标准全量

```bash
scripts/run_tests.sh -j 8
```

结果：1,596 files、29,228 passed、0 failed，耗时 707.6s。首次全量暴露并关闭了
delegate 单文件超限、吸收账本枚举、manifest 新 RPC 漏报、worker DB 代理死白名单以及旧
background-review 测试补丁 owner 错位；最终结果来自修复后的第二次干净全量，不以首次红项
或局部复跑替代。

### 静态、分层与构建

| 门禁 | 结果 |
|---|---|
| `.venv/bin/ruff check .` | 通过 |
| production package `compileall` | 通过 |
| `.venv/bin/lint-imports` | 169 files、336 dependencies；1 contract kept、0 broken |
| `tests/observability/test_zero_debt_gates.py`（包含于标准全量） | 14 passed |
| `npm run build`（`web/`） | TypeScript + Vite production build 通过 |
| `git diff --check` | 通过 |

## 用户验收重点

1. 分别打开两个智能员工的成长页，确认记忆、技能和时间线完全隔离。
2. 新增一条记忆或让后台复盘形成技能后刷新，确认图谱和成长摘要同时增加且只增加一次。
3. 编辑记忆后确认内容刷新、节点身份更新且没有重复节点；删除后不再出现。
4. 删除普通 learned skill 应进入可恢复 archive；pinned skill 必须明确拒绝。
5. 配置单独的 background-review aux model 后长会话复盘仍能形成同等学习结果，且输入成本
   相比完整历史明显下降；不配置时保持主模型 warm-cache 路径。

自动门禁已经完成。以上 Doxie 页面消费、双 profile 实机数据和真实 provider 成本对比仍需
用户验收，未被单元测试结论替代。
