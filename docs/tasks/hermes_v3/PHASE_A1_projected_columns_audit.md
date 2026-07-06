# Phase A1 — projected_message_id / projected_tool_event_id 消费方审计

**上游文档**：`/Users/yangwanquan/Personal_projects/AIGC_dev/doxie/docs/hermes_agent_architecture_v3.md` §12 Phase A（v3.0.1）

**背景**：v3.0 原稿把 `run_events.projected_message_id / projected_tool_event_id / projection_state` 列定性为"grep 未见赋值 = 死代码"，Phase A 计划迁移 0043 直接 drop。评审复查发现**这是错的**：至少 5 处活跃写入路径（`hermes_state_run_event_codec.py:110` / `hermes_state_run_event_reference.py:382-383` / `hermes_state_runs.py:1705,1763` / `team_transcript_writer.py:1558` / `run_control.py:1955-1961`）。**盲目 drop 会破线**。

Phase A1 是**纯审计工作**，产物是一份 markdown 报告，为 Phase A2 迁移决策提供事实基础。**本 Phase 不动任何代码**。

**分支**：`feat/team-timeline-rearchitecture`（沿用；工作树干净）
**前置**：Phase 0 已合入到 `d6357891c`

---

## 严格边界

### 只允许改动的文件（白名单）

**新增**：
- `docs/audits/projected_columns_consumers.md`（唯一产出）

### 禁止改动

任何 `.py` / `.toml` / `.ini` / `.sql` 文件。**只读代码，只写报告**。

---

## 报告结构（必须包含全部 section）

```markdown
# projected_message_id / projected_tool_event_id 消费方全量审计

**审计日期**：2026-07-06
**目标列**：
- `run_events.projected_message_id TEXT` (hermes_state.py:463)
- `run_events.projected_tool_event_id TEXT` (hermes_state.py:464)
- `run_events.projection_state TEXT` (hermes_state.py:465, 已知与前二 sibling)

**目的**：为 Phase A2 决策"保留 / 迁移 / 条件性 drop"提供事实清单。

---

## 1. Schema 定义与索引

<列出：
 - 列定义（含默认值、约束）
 - 相关 index/unique 约束
 - `_reconcile_columns` 是否 track 这些列
 - 现有迁移文件是否有 default backfill
>

## 2. 写入方（SET/UPDATE/INSERT）

针对每个写入点，给出：

| 文件:行 | 语句类型 | 触发场景 | 写入值来源 | 是否条件性 |
|---|---|---|---|---|
| hermes_state_run_event_codec.py:110 | UPDATE COALESCE | ... | ... | ... |
| ... | ... | ... | ... | ... |

**写入方分类**（每个写入点归为一类）：
- (W1) **首次投影落库**：run_event 落 messages 表时同步写 projected_message_id
- (W2) **补写空值**：老数据的 projection_state 从 pending → done 时 backfill
- (W3) **测试 fixture**：仅在测试代码里 SET
- (W4) **导入路径**：从 legacy state 迁移时填

## 3. 读取方（SELECT/COALESCE 过滤）

针对每个读取点：

| 文件:行 | 查询用途 | 依赖字段 | 若列消失的后果 |
|---|---|---|---|
| hermes_state_run_event_reference.py:144 | 反查 tool_event_id | projected_tool_event_id | 反查失败, tool events 关联断链 |
| ... | ... | ... | ... |

**读取方分类**：
- (R1) **反查桥接**：run_event → message / tool_event 的桥
- (R2) **业务过滤**：`WHERE COALESCE(projected_message_id, '') != ''` 过滤 out 未投影
- (R3) **测试断言**

## 4. 迁移目标可行性评估

针对每一类写入/读取，评估是否可以迁移到 v3.0 架构：

| 用途 | v3.0 替代方案 | 迁移复杂度 | 依赖 Phase | 建议 |
|---|---|---|---|---|
| run_event → message 反查 (R1/W1) | `run_events.reference_json` 加字段 or EventLedger 派生查询 | 中 | Phase E EventLedger 完成后 | 迁移目标: reference_json 加 `projected_message_id` 字段 |
| ... | ... | ... | ... | ... |

## 5. 结论 & Phase A2 建议

- 【总建议】保留列 / 部分迁移 / 全迁移
- 【Phase A2 具体动作】列出必须做的迁移步骤
- 【延期到 Phase E】列出等 EventLedger 就位后再做的迁移步骤
- 【永久保留】若某些用途没有更好替代，明确说明保留原因

**明确禁止**：Phase A2 迁移 0043 无条件 drop 这两列。若要 drop, 必须先完成本报告结论 §5 里列出的全部消费方迁移。

## 6. Grep 复现命令

供审计员独立复核：

```bash
grep -rn "projected_message_id\|projected_tool_event_id\|projection_state" . --include="*.py"
grep -rn "reference.get(\"conversation_message_id\"" . --include="*.py"
```
```

---

## 具体动作

1. 用 `grep -rn "projected_message_id\|projected_tool_event_id\|projection_state" . --include="*.py"` 全仓列匹配
2. 每个匹配点**打开源文件读上下文**（至少前后 15 行），判定属于写/读、条件、业务语义
3. 按上面 6 个 section 结构组织报告，事实清单严格锚定 `文件:行`
4. 结论 §5 必须给出：**Phase A2 是否可以 drop（是/否）+ 若否，列 A2 必须迁移的消费方 + 若某些延到 Phase E 明确列出**
5. **对每一处写入方**，说明写入值的 upstream 来源（谁生成这个 id）
6. **对每一处读取方**，说明若列消失后系统会退化到什么行为（silent failure? crash? functional regression?）

---

## 门禁

```bash
test -s docs/audits/projected_columns_consumers.md
wc -l docs/audits/projected_columns_consumers.md   # 期望 >= 200 行
grep -c "^## " docs/audits/projected_columns_consumers.md   # 期望 == 6 (六个 section)
grep -c "hermes_state_run_event_codec.py:110" docs/audits/projected_columns_consumers.md  # 期望 >= 1
grep -c "hermes_state_runs.py:170[0-9]" docs/audits/projected_columns_consumers.md  # 期望 >= 1
grep -c "team_transcript_writer.py:15[0-9]" docs/audits/projected_columns_consumers.md  # 期望 >= 1
```

---

## 反 scope 蔓延

- **禁止顺手改代码**——本 Phase 只读代码写 markdown
- **禁止顺手改 hermes_state.py:463-465 列定义**（那是 Phase A2 的活）
- **禁止跳过某个引用点**——全部写入/读取点必须清点，宁可 over-inclusive
- **禁止基于任何"是否是死代码"的直觉判断**——只列事实，语义分类基于代码上下文

---

## 交付格式

- 只写 `docs/audits/projected_columns_consumers.md` 一个文件
- **不要碰 git**，commit 由主控代做
- 完成后打印总结 + 门禁自检结果

## 最终产出

```
## Phase A1 交付总结

文件：docs/audits/projected_columns_consumers.md
行数：<n>
Section 数：<n>

### 写入方总数：<n>
### 读取方总数：<n>
### 唯一涉及文件数：<n>

### 结论
- Phase A2 是否可以 drop projected_*: 是/否
- 若否, Phase A2 必须迁移的消费方：<列表>
- 延期到 Phase E: <列表>
- 永久保留原因：<列表>

### 门禁自检
- 行数 >= 200: <是/否>
- 6 section: <是/否>
- 各关键锚点覆盖: <是/否>
```
