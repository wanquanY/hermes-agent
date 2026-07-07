# Hermes v3.0.2 Backend Landing — Codex 审核 Brief

**目标 commit**: `564dd18d8` on branch `feat/team-timeline-rearchitecture`
**仓库**: `/Users/yangwanquan/syngents/code/hermes-agent`
**Spec**: `docs/hermes_agent_architecture_v3.md`（v3.0.3 契约冻结版）
**Landing 时间跨度**: 25 loop 迭代（Phase 0 → Phase M）
**已知已提交前置**: `4ca9ebaed` (Phase 0.1) + `d6357891c` (Phase 0.2) + `0c4c05094` (Phase A1)

---

## 审核目标

**判断本 commit 是否高质量落地 spec v3.0.2/v3.0.3**：
1. 结构性 —— 是否符合 spec §3 6 层 + §4 五聚合根 + §J11 不变量
2. 正确性 —— 关键路径（identity fold、SeqAllocator、RunStateMachine、EventLedger、handshake）是否有实现漏洞
3. 覆盖 —— 声称的测试指标是否真实、是否有隐藏的 flaky/占位
4. 遗留 —— 已知 gap 判断是否合理，是否有未识别的隐患

---

## 审核范围

### 只审这个 commit 里的内容
```
git show --stat 564dd18d8 | head -5
120 files changed, 19781 insertions(+), 19 deletions(-)
```
文件树限定：
- `hermes_agent/**`
- `tests/{domain,gateway_v3,observability,orchestration,repositories,transport}/**`
- `tests/storage/test_{identity_fk_migration,phase_m_frozen_migration,run_state_machine,seq_allocator}.py`
- `tests/gateway/test_{contract_capabilities,interaction_persistence}.py`
- `docs/audits/{phase_d5_identity_alias_usage,phase_j_gateway_liveness}.md`
- `docs/reviews/hermes_v3_landing_status.md`
- `.importlinter`
- `tui_gateway/services/{contract_capabilities,interaction_registry,runtime_scope}.py`

### 不在本次审核范围
- `hermes_state*.py` / `hermes_team_mission/**` / `tui_gateway/methods/**` / `tui_gateway/services/{worker_*,run_control,profile_context,worker_frame_router,worker_pool,worker_runtime,worker_supervisor}.py` —— 这些是**另一个 session 的团队时间线 v1 落地**，在工作树里但**不在本 commit**
- `docs/hermes_agent_architecture_v3.md`（spec 本身）—— 只作参照，不审内容

---

## 审核方法与四板斧

### 1. Spec 对齐性（结构）
对照 spec 各章检查：

| Spec 章节 | 主要要求 | 需验证的落地文件 |
|---|---|---|
| §3 六层 | Transport→Gateway→Orchestration→Repository→Domain→Storage 单向 | `hermes_agent/{transport,gateway,orchestration,repositories,domain,storage}/**`, `.importlinter` |
| §4.1 SessionRepo | sessions + session_index + session_branches；`session_id` 唯一 | `hermes_agent/repositories/session_repo.py` |
| §4.2 RunRepo | runs + run_events；SeqAllocator 消费；terminate 走 domain | `hermes_agent/repositories/run_repo.py` |
| §4.3 MessageRepo | messages（含 FTS）；一次 append 一 seq | `hermes_agent/repositories/message_repo.py` |
| §4.4 TeamMissionRepo | missions + nodes + **activities + activity_commands** 两表；`allocate_only` 共享 seq 域 | `hermes_agent/repositories/team_mission_repo.py` |
| §4.5 AgentProfileRepo | profiles + versions + growth_summary | `hermes_agent/repositories/agent_profile_repo.py` |
| §5.1 identity | `session_id` 唯一语义；wire 上别名 fold 而不删列 | `hermes_agent/gateway/pipeline.py` `_fold_session_id_aliases`，`docs/audits/phase_d5_identity_alias_usage.md` |
| §6.1 EventLedger | 单一 append 入口；`_internal.*` 过滤；in_transaction 兼容 | `hermes_agent/domain/event_ledger.py` |
| §6.3 SeqAllocator | `seq_counter` 表 + `BEGIN IMMEDIATE` + `busy_timeout=5000` + `allocate_only` + `SeqAllocatorBusy` 降级 | `hermes_agent/domain/seq_allocator.py`, migration `0044` |
| §7 RunStateMachine | 单入口 terminate_run；三 terminal 路径改调；race 只产 1 event；竞争优先级 `WORKER_CRASHED > MANUAL_KILL > WORKER_EMITTED` | `hermes_agent/domain/{run_state_machine.py,run_terminator.py}` |
| §7.4 Interaction 双通道 | `_internal.interaction.*` 落 run_events + `InteractionFrame` 独立 top-level；`request` 带 `anchor_seq` | `hermes_agent/gateway/methods/*` 及相关 test |
| §8 WorkerPool | 单副本；ShardedRpcLock 含 busy_timeout + lease TTL + backpressure | `hermes_agent/orchestration/worker_pool.py`, `sharded_rpc_lock.py` |
| §J1-J11 不变量 | 见后 | 各处 |
| §10.1 Migration | declarative；号连号；base.py 一次执行 | `hermes_agent/storage/migrations/**` |
| §10.2 观测契约 | 无 silent swallow；RecoverableError/FatalError 分类 | `hermes_agent/observability/{silent_swallow_lint,error_taxonomy}.py` |
| §11 Handshake | contractVersion 3.1 首帧；nested cursor/toolEvents；`deprecations: string[]` | `hermes_agent/gateway/handshake.py`, `methods/handshake_method.py`, `tui_gateway/services/contract_capabilities.py` |

**重点排查**：
- 是否有 spec §4.6 "跨聚合根禁止" 违反（RepoImpl 之间直接调用）
- 是否有 seq 双列/双域 残留（应该只有 run_events.seq，`runtime_source_seq` 应被 0047 drop 掉）
- session_id 别名 fold 是否覆盖所有 in-bound + out-bound（`fold_response_aliases`）
- `_internal.*` 事件是否严格不进入 CanonicalEventType 枚举

### 2. 关键路径 diff 独立复验
用 `git show 564dd18d8 -- <path>` 逐个跑：

```
git show 564dd18d8 -- hermes_agent/domain/run_terminator.py
git show 564dd18d8 -- hermes_agent/domain/seq_allocator.py
git show 564dd18d8 -- hermes_agent/domain/event_ledger.py
git show 564dd18d8 -- hermes_agent/gateway/pipeline.py
git show 564dd18d8 -- hermes_agent/gateway/methods/handshake_method.py
git show 564dd18d8 -- hermes_agent/repositories/session_repo.py
git show 564dd18d8 -- hermes_agent/repositories/team_mission_repo.py
```

### 3. 独立门禁复跑（不要信 dashboard 数字）
在 `/Users/yangwanquan/syngents/code/hermes-agent` 目录用 venv 跑：

```bash
# 主 v3 suite
.venv/bin/python -m pytest tests/transport tests/gateway_v3 tests/orchestration \
                              tests/repositories tests/observability tests/domain -q
# 期望：357 passed, 13 skipped

# 变异 suite
RUN_MUTATION_TESTS=1 .venv/bin/python -m pytest tests/observability/test_mutation_verification.py -q
# 期望：13/13 passed

# import-linter
.venv/bin/lint-imports
# 期望：KEPT

# mypy
MYPYPATH= .venv/bin/mypy --no-incremental --ignore-missing-imports \
                           --follow-imports=silent --explicit-package-bases \
                           hermes_agent/domain/ hermes_agent/repositories/ \
                           hermes_agent/gateway/ hermes_agent/orchestration/ \
                           hermes_agent/transport/ hermes_agent/observability/
# 期望：Success: no issues found in 43 source files

# silent-swallow lint
.venv/bin/python -c "from hermes_agent.observability.silent_swallow_lint import scan_paths; from pathlib import Path; print(len(scan_paths([Path('hermes_agent')])))"
# 期望：0
```

### 4. 变异验证 spot-check
从 `tests/observability/test_mutation_verification.py` 里选 J1/J2/J4/J8/J11 五条，
手工制造反向 mutation（比如把 `BEGIN IMMEDIATE` 改成 `BEGIN`、`WORKER_CRASHED` 优先级降到最低），
确认对应 mutation test 会红——不能只信 harness 说"pass"，要看它是否真的能失败。

---

## 关键实现决策与理由（供 codex 判断合理性）

1. **stdio_daemon 用 `conn_provider = lambda _: conn`**（单一共享 conn）—— 这是 v3.0.2 简化，per-session 分片留到后续。理由：spec §5.1 承诺"single conn per session_id"没在本轮做，daemon 是 dev/test 通道。审 codex 判断：这够 v3.0.2 达标吗？

2. **run_terminator + event_ledger 都加了 `owns_tx = not conn.in_transaction`** —— 为了兼容 RepoImpl 调用链会自己开事务的情况。审 codex 判断：nested tx 检测在 SQLite 上是否可靠？

3. **`session.update_index` 白名单参数**（fields bag 不透传）—— 主动窄化 gateway 写路径。审 codex 判断：是否有合法场景被砍掉？

4. **`_internal.interaction.*` 不在 CanonicalEventType 枚举** —— 双通道设计。审 codex 判断：wire 上（gateway/pipeline）过滤是否严格？

5. **删除 3 个 `hermes_agent/storage/{run_state_machine,exceptions,seq_allocator}.py` 兼容 shim** —— 无消费方；符合 spec §12 "该废弃的废弃"。审 codex 判断：全仓 grep 是否真无消费？

6. **import-linter 层序 = `repositories 在 domain 上`** —— repo 依赖 domain services，spec §3.1 语义。审 codex 判断：这个反直觉但正确的层序解释是否清晰？

---

## 已知已识别 gap（不希望 codex 再报）

1. **Phase J 老 gateway/ 目录 63 文件 92k 行** —— user 明确 "channel 相关的不动"，主动豁免；不需要审此项
2. **Phase D5 wire 协议 18 处消费方替换** —— audit 已在 `docs/audits/phase_d5_identity_alias_usage.md`，判定不适合 loop 机械替换，专项决策
3. **附录 C 完成度自查清单 26 项 `[ ]`** —— 需 user 真机三症状回归后打勾
4. **跨仓前后端联调三症状复现验收** —— 未做，属 user 明早验收范围
5. **WebSocket / HTTP transport adapter** —— L5 Protocol 已抽象但只有 InMemory + stdio 两实现
6. **stdio_daemon 无 per-session conn 分片**（见上文 #1）
7. **全 pytest 主 suite** —— 只跑了 v3 六目录 357 pass；`tests/` 根目录有 telegram/e2e 依赖挂起（**非 v3 引入**），无法声称"零回归"

---

## 期望审核输出

Codex 请按如下格式返回：

```
## 结构性判断（对齐 spec §3-§11）
- [PASS/FAIL/PARTIAL] 每一层的评述
## 正确性隐患（rank by severity）
- HIGH: ...
- MED: ...
- LOW: ...
## 声称指标核对（357/13/13-mutation/import-linter/mypy）
- 独立复验结果
## 未识别 gap（我漏了什么）
- ...
## 判决
- READY_FOR_ACCEPTANCE / NEEDS_REWORK / BLOCKER
```

---

## 审核者请注意

- **不要修代码**，只审 + 报告
- 若发现 blocker，指明具体文件:行 + spec 条款 + 复现步骤
- 若发现 P1，给出 patch 建议但不落
- 期望 xhigh 反思强度；这是 v3.0.2 landing 的最终验收前审核
- 优先跑独立门禁再看 diff —— 若 pytest 数字对不上，先停下报告
