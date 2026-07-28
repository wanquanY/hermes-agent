# projected_message_id / projected_tool_event_id 消费方全量审计

**审计日期**：2026-07-06  
**目标列**：
- `run_events.projected_message_id TEXT` (`hermes_state.py:463`)
- `run_events.projected_tool_event_id TEXT` (`hermes_state.py:464`)
- `run_events.projection_state TEXT` (`hermes_state.py:465`, 与前二者同属 projection 状态组)

**目的**：为 Phase A2 决策“保留 / 迁移 / 条件性 drop”提供事实清单。  
**审计口径**：本报告只依据代码上下文和全仓 grep 结果分类，不按“是否像死代码”的直觉排除引用点。写入/读取按“引用簇”统计；测试 fixture 与断言单列为测试契约。

---

## 1. Schema 定义与索引

### 1.1 列定义

| 文件:行 | 定义 | 默认值 / 约束 | 事实 |
|---|---|---|---|
| `hermes_state.py:463` | `projected_message_id TEXT` | nullable，无 default，无 FK，无 UNIQUE | 保存 `run_events` 行投影到 `messages.conversation_message_id` 后的桥接 id。 |
| `hermes_state.py:464` | `projected_tool_event_id TEXT` | nullable，无 default，无 FK，无 UNIQUE | 保存 `run_events` 行投影到 `tool_events.id` 后的桥接 id。 |
| `hermes_state.py:465` | `projection_state TEXT` | nullable，无 default，无 CHECK | 标记投影/引用状态，当前代码写入 `raw` / `projected` / `referenced`。 |
| `hermes_state.py:467` | `UNIQUE(session_id, seq)` | 只约束 event seq | 不保护 `projected_*` 的引用完整性。 |

### 1.2 索引与约束

| 文件:行 | 对象 | 事实 |
|---|---|---|
| `hermes_state.py:722-723` | `idx_run_events_projection_state ON run_events(projection_state, session_id, seq)` | 仅索引 `projection_state`，用于状态过滤；未索引 `projected_message_id` / `projected_tool_event_id`。 |
| `hermes_state.py:712-725` | 其他 `run_events` index | 覆盖 session、scope、run、participant、retention、runtime_source_seq；无 projected id index。 |
| `hermes_state.py:445-468` | `run_events` table | 无 FK 到 `messages` 或 `tool_events`；`projected_tool_event_id` 存 TEXT，但来源是 `tool_events.id` 的整数值字符串。 |

### 1.3 `_reconcile_columns` 覆盖情况

| 文件:行 | 机制 | 事实 |
|---|---|---|
| `hermes_state.py:1300-1327` | 从 `SCHEMA_SQL` 的内存库解析 declared columns | projected 三列在 `SCHEMA_SQL`，因此属于 reconciler 的声明式目标。 |
| `hermes_state.py:1333-1367` | `_reconcile_columns()` 对缺列执行 `ALTER TABLE ... ADD COLUMN` | 三列会被自动补到 legacy DB；列类型按 `TEXT` 添加，无 default。 |
| `hermes_state.py:1961-1968` | `_init_schema()` 先 executescript，再 `_reconcile_columns(cursor)` | 每次启动都会确保 live table 有声明列。 |

### 1.4 现有迁移与 default backfill

| 文件:行 | 迁移 / backfill | 对 projected 三列的影响 |
|---|---|---|
| `hermes_agent/storage/migrations/0036_run_event_frame_indexes_backfill.py:22-23` | 调用 `owner._backfill_run_event_frame_indexes(cursor)` | 间接调用 frame helper，为缺 frame/search index 的 legacy rows 写 `projection_state='raw'`。 |
| `hermes_state.py:1642-1676` | `_backfill_run_event_frame_indexes()` | 对每个待补 row 调 `update_run_event_frame_columns(..., projection_state="raw")`；不生成 `projected_message_id` 或 `projected_tool_event_id`。 |
| `hermes_state_runs.py:2228-2272` | `backfill_run_event_frame_blobs()` | 手动/服务调用的同类 backfill，也传 `projection_state="raw"`。 |
| `hermes_agent/storage/migrations/0034_tool_events_backfill.py:22-23` | tool events read model backfill | 只补 `tool_events`；未写 `run_events.projected_tool_event_id`。 |
| `hermes_agent/storage/migrations/` | 现有文件到 `0039_team_mission_event_json_storage.py` | 当前仓库未见 `0043_projected_columns_audit.py`；无迁移文件 drop 或默认回填 projected id。 |

### 1.5 Schema 小结

- 三列当前是 `run_events` 的柔性桥接列：nullable、无 FK、无 UNIQUE。
- `projection_state` 是唯一有索引支持的目标列，且生产代码用它过滤 “是否已 referenced / projected”。
- `_reconcile_columns()` 会持续把三列补回来；若 Phase A2 只写 drop migration、不改 `SCHEMA_SQL`，启动 reconciler 会重新添加列。
- 当前没有“默认 backfill projected id”的迁移；`projected_message_id` 和 `projected_tool_event_id` 由运行时投影路径产生。

## 2. 写入方（SET/UPDATE/INSERT）

### 2.1 写入方总表

| 文件:行 | 语句类型 | 触发场景 | 写入值来源 | 是否条件性 |
|---|---|---|---|---|
| `hermes_state_run_event_codec.py:110-112` | UPDATE COALESCE helper | `update_run_event_frame_columns()` 被 frame/index backfill 调用时 | 调用方传入的 `projected_message_id` / `projected_tool_event_id` / `projection_state`；空字符串不覆盖既有值 | 是，`NULLIF(?, '')` 非空才覆盖 projected id/state。 |
| `hermes_state.py:1675` | helper 参数写入 | `_backfill_run_event_frame_indexes()` 修复旧 row 的 frame/search index | 固定传 `projection_state="raw"`；projected id 留空 | 是，仅处理缺 frame/search index 的 rows。 |
| `hermes_state_runs.py:1615` | UPDATE | append 时与已有 stream row coalesce | 保留已有 `projection_state`，为空时填 `raw` | 是，只在 coalesced stream update 发生。 |
| `hermes_state_runs.py:1647-1669` | INSERT | `append_run_event()` 新增普通 run_event | 固定插入 `projection_state='raw'` | 是，非 coalesced 且 `INSERT OR IGNORE` 成功时。 |
| `hermes_state_runs.py:1700` | 内存返回字段写入 | tool event 投影成功后返回给调用者 | `project_tool_event(conn, inserted_event).get("id")` | 是，仅 `event_type in TOOL_EVENT_TYPES` 且 tool projection 返回 id。 |
| `hermes_state_runs.py:1705-1706` | UPDATE | tool event 写入/更新 `tool_events` 后回写 run_event | `project_tool_event()` 返回的 `tool_events.id`，转字符串写 `projected_tool_event_id`；`projection_state` 空时填 `raw` | 是，需 inserted row 存在且 tool projection 成功。 |
| `hermes_state_runs.py:1741` | 内存返回字段写入 | message complete 投影成功后返回给调用者 | `RuntimeTranscriptWriter.project_message_complete_event_locked()` 返回的 `conversation_message_id` | 是，仅 `message.complete` 且 transcript projection 成功。 |
| `hermes_state_runs.py:1763-1764` | UPDATE | message complete 投影到 `messages` 后回写 run_event | `assistant_conversation_message_id_for(AssistantMessageIdentity(session_id, run_id, message_seq_in_run))` 生成并由 writer 返回 | 是，需 `conversation_message_id` 非空。 |
| `hermes_state_runs.py:2271` | helper 参数写入 | `backfill_run_event_frame_blobs()` 手动补 frame/index | 固定传 `projection_state="raw"`；projected id 留空 | 是，仅处理缺 frame/search index 的 rows。 |
| `hermes_state_runs.py:2664` | INSERT | terminal maintenance 合成 `message.complete` row | INSERT 列包含 `projection_state`；值在后续参数区固定随 terminal frame 写入 | 是，仅 terminal row 补写路径。 |
| `hermes_state_runs.py:3177` | UPDATE | `compact_run_events()` 去重 terminal groups | 保留既有 `projection_state`，为空时填 `raw` | 是，只对保留 row 更新。 |
| `hermes_state_runs.py:3285` | UPDATE | `compact_run_events()` 合并 stream segments | 保留既有 `projection_state`，为空时填 `raw` | 是，只对保留 row 更新。 |
| `hermes_state_run_event_reference.py:382-384` | UPDATE | `reference_run_event_payloads()` 将大 payload 改成 referenced payload | `message.complete` 来源于 `messages.conversation_message_id`；`tool.complete` 来源于 `tool_events.id`；状态固定 `referenced` | 是，仅 eligible rows 且 reference target 可找到。 |
| `hermes_team_mission/runtime/team_transcript_writer.py:1558-1559` | UPDATE | team conversation read model/backfill 修复未投影 `message.complete` rows | `RuntimeTranscriptWriter.project_message_complete_event_locked()` 返回的 `conversation_message_id` | 是，仅未投影且 writer 成功。 |
| `tests/test_team_mission_conversation_mirror.py:1470-1492` | 测试 INSERT fixture | seed legacy raw `message.complete` row | 固定 `projection_state='raw'` | 是，测试 fixture。 |
| `tests/gateway/test_team_transcript_writer_raw_segments.py:102-124` | 测试 INSERT fixture | seed legacy raw run_events | 固定 `projection_state='raw'` | 是，测试 fixture。 |
| `tests/gateway/test_team_transcript_writer_raw_segments.py:598-600` | 测试 UPDATE fixture | 强制清空 projection，触发 backfill rerun | `projected_message_id=''`，`projection_state='raw'` | 是，测试 fixture。 |

### 2.2 写入方分类

| 类别 | 写入点 | 业务语义 |
|---|---|---|
| (W1) 首次投影落库 | `hermes_state_runs.py:1705-1706`, `hermes_state_runs.py:1763-1764` | `append_run_event()` 同步维护 `tool_events` / `messages` read model，并把桥接 id 写回 `run_events`。 |
| (W2) 补写空值 / backfill | `hermes_state.py:1675`, `hermes_state_runs.py:2271`, `team_transcript_writer.py:1558-1559` | 修复旧 row 的 frame/search index 或 team transcript projection。 |
| (W3) 状态保持 / compaction | `hermes_state_runs.py:1615`, `hermes_state_runs.py:3177`, `hermes_state_runs.py:3285` | 合并/压缩 run_events 时不丢已有状态，空状态补 `raw`。 |
| (W4) 引用化压缩 | `hermes_state_run_event_reference.py:382-384` | 将大 message/tool payload 改成 referenced payload，并保留反查桥接 id。 |
| (W5) 导入/合成路径 | `hermes_state_runs.py:1647-1669`, `hermes_state_runs.py:2664` | 新增或合成 run_event 时初始化 `projection_state`。 |
| (W6) 测试契约 | `tests/test_team_mission_conversation_mirror.py:1473`, `tests/gateway/test_team_transcript_writer_raw_segments.py:105`, `:599-600` | regression tests 用 raw/cleared state 触发或验证生产 backfill。 |

### 2.3 上游 id 来源

- `projected_message_id` 的主要来源是 `RuntimeTranscriptWriter.project_message_complete_event_locked()`：它在 `team_transcript_writer.py:1268-1274` 使用 `assistant_conversation_message_id_for(AssistantMessageIdentity(session_id, run_id, message_seq_in_run))` 生成确定性 conversation message id，再在 `team_transcript_writer.py:1384-1396` upsert 到 `messages`。
- `projected_tool_event_id` 的来源是 `project_tool_event()`：它在 `hermes_state_tool_events.py:435-471` upsert `tool_events`，再在 `hermes_state_tool_events.py:493-502` SELECT 并返回 row dict；`append_run_event()` 将返回的 `id` 写回 `run_events`。
- `projection_state='referenced'` 的来源是 `reference_projected_run_event_payloads()`：它在 `hermes_state_run_event_reference.py:195-245` 构造 slim referenced payload，并在 `:382-384` 标记 referenced。
- `projection_state='raw'` 不是 schema default，而是 append/backfill/compaction 路径显式写入或补齐。

## 3. 读取方（SELECT/COALESCE 过滤）

### 3.1 读取方总表

| 文件:行 | 查询用途 | 依赖字段 | 若列消失的后果 |
|---|---|---|---|
| `hermes_state_run_event_reference.py:144-157` | `_tool_row_for_event()` 优先用 `projected_tool_event_id` 精确反查 `tool_events` | `projected_tool_event_id` | 退化到 `tool_call_id` 或 `(run_id, turn_id, seq)` fallback；旧数据/合并数据可能反查失败，导致 tool referenced payload 无法生成。 |
| `hermes_state_run_event_reference.py:281` | rehydrate message referenced payload 时 fallback 到 row column | `projected_message_id` | 若 referenced payload 缺 `conversation_message_id`，无法从 `messages` 补回完整 content/status，表现为 silent 内容缺失。 |
| `hermes_state_run_event_reference.py:304` | rehydrate tool referenced payload 时 fallback 到 row column | `projected_tool_event_id` | 若 referenced payload 缺 `tool_event_id`，无法从 `tool_events` 补回 result/result_text/status，表现为 tool result 缺失。 |
| `hermes_state_run_event_reference.py:353` | `reference_projected_run_event_payloads()` 候选过滤 | `projection_state` | 若无替代状态，已 referenced rows 会被重复处理或查询报错，payload 压缩幂等性失效。 |
| `hermes_state_run_event_reference.py:417` | referenced 剩余数量统计 | `projection_state` | 统计查询报错或 remaining count 不可信。 |
| `hermes_state_runs.py:1987` | append 后 leader report-ready context lookup | 内存字段 `_projected_message_id`，由 `projected_message_id` 写入路径同源 | leader report ready event 可能拿不到刚生成的 message id，mission report completion signal 延迟或缺失。 |
| `hermes_state_runs.py:2122` | `list_run_events()` 调用 rehydrate | 间接依赖 `projected_message_id` / `projected_tool_event_id` | 读取 referenced rows 时无法恢复完整 message/tool payload。 |
| `hermes_state_runs.py:2159` | `list_run_events_by_activity()` 调用 rehydrate | 间接依赖 `projected_message_id` / `projected_tool_event_id` | activity 视图中的 referenced payload 恢复不完整。 |
| `hermes_state_runs.py:2491` | `list_run_events_filtered()` 调用 rehydrate | 间接依赖 `projected_message_id` / `projected_tool_event_id` | 过滤视图返回 slim payload，调用方看到内容/result 缺失。 |
| `tui_gateway/services/run_control.py:1955-1961` | worker event fanout 后 leader report-ready lookup | 内存字段 `_projected_message_id`，由 run_event projection 产生 | sidecar report-ready 事件缺少刚投影 message id；当前代码 catch 并记录错误，主事件仍可能已落库。 |
| `hermes_team_mission/runtime/team_transcript_writer.py:1118-1146` | `leader_report_ready_context_for_run()` 接收 projected id 参数 | `projected_message_id` 的内存传递值 | 若调用方无法提供 projected id，只能 fallback 到 mission result；刚完成的 leader report 可能尚未记录 result id，ready event 缺失。 |
| `hermes_team_mission/runtime/team_transcript_writer.py:1471-1485` | artifact.created 合并到最近 projected message | `projected_message_id` | artifact card 无法合入对应 assistant message，用户可见消息缺 artifact metadata。 |
| `hermes_team_mission/runtime/team_transcript_writer.py:1529-1530` | backfill 未投影 message.complete 候选 | `projected_message_id`, `projection_state` | 历史 raw rows 无法被精准识别；可能重复投影或完全跳过。 |
| `hermes_team_mission/runtime/team_transcript_writer.py:1590-1619` | backfill artifact cards 到已有 projected messages | `projected_message_id` | 历史 artifact 修复找不到目标 message。 |
| `tui_gateway/services/storage_stats.py:55-57` | storage diagnostics payload column size统计 | 三列名 | 若 drop 不同步更新 diagnostics，统计 SQL 构造可能引用不存在列，runtime state/storage stats 报错或跳过该表。 |
| `tests/test_team_mission_conversation_mirror.py:1204-1211` | 断言 append 投影回写 | `projected_message_id`, `projection_state` | 回归测试失败；说明 team leader/member chat 投影契约断裂。 |
| `tests/test_team_mission_conversation_mirror.py:1508-1515` | 断言 read model 触发 backfill 后回写 | `projected_message_id`, `projection_state` | 回归测试失败；历史 raw row 修复契约断裂。 |
| `tests/test_hermes_state.py:967` | 断言 append 返回 `_projected_message_id` | 内存字段 `_projected_message_id` | 回归测试失败；调用方无法拿到投影 id。 |
| `tests/test_hermes_state.py:1061` | 断言 late/early worker flush 仍 claim projection | 内存字段 `_projected_message_id` | 回归测试失败；duplicate/claim 行为改变。 |
| `tests/test_hermes_state.py:1092` | 断言 mission node message.complete 不投影 | 内存字段 `_projected_message_id` absence | 回归测试失败；禁止投影的边界可能失守。 |
| `tests/test_run_event_frame_codec.py:126-127` | 断言 message referenced payload 保存桥接 id | `projection_state`, `projected_message_id` | reference/re-hydrate 契约测试失败。 |
| `tests/test_run_event_frame_codec.py:169-170` | 断言 tool referenced payload 保存桥接 id | `projection_state`, `projected_tool_event_id` | tool referenced payload 契约测试失败。 |
| `tests/gateway/test_team_transcript_writer_raw_segments.py:704` | 断言 message_seq=0 仍生成显式 projected id | 内存字段 `_projected_message_id` | raw segment identity regression 测试失败。 |

### 3.2 读取方分类

| 类别 | 读取点 | 业务语义 |
|---|---|---|
| (R1) 反查桥接 | `hermes_state_run_event_reference.py:144`, `:281`, `:304`; `hermes_state_runs.py:2122`, `:2159`, `:2491` | 在 slim referenced payload 与 `messages` / `tool_events` read model 之间恢复完整 payload。 |
| (R2) 业务过滤 | `hermes_state_run_event_reference.py:353`, `:417`; `team_transcript_writer.py:1476`, `:1529-1530`, `:1594` | 按投影状态区分 raw/projected/referenced，避免重复处理或找到正确目标 message。 |
| (R3) report-ready sidecar | `hermes_state_runs.py:1987`, `run_control.py:1955-1961`, `team_transcript_writer.py:1118-1146` | leader report 完成后把刚投影的 conversation message id 传给 mission ready/status 事件。 |
| (R4) 诊断统计 | `storage_stats.py:55-57` | storage stats 把三列当作 run_events payload/size columns。 |
| (R5) 测试断言 | 上表所有 `tests/...` 行 | 多个 regression tests 明确把这些列/内存字段作为行为契约。 |

## 4. 迁移目标可行性评估

| 用途 | v3.0 替代方案 | 迁移复杂度 | 依赖 Phase | 建议 |
|---|---|---|---|---|
| run_event -> message 反查 (R1/W1) | 在 `run_events.reference_json` 或 canonical event payload 内保存 `{kind:"message", conversation_message_id}`；EventLedger list 统一 rehydrate | 中 | Phase E EventLedger 或 A' 引入 reference_json | A2 不 drop；若要迁移，先双写 reference_json + `projected_message_id`，再改 rehydrate 读取 reference_json。 |
| run_event -> tool_event 反查 (R1/W1) | 在 `reference_json` 保存 `{kind:"tool", tool_event_id}`；长期由 EventLedger 派生 tool read model | 中-高 | Phase E tool_events read-only/materialized view | A2 不 drop；需先保证 `tool_events.id` 或新 canonical id 可稳定引用。 |
| `projection_state` 过滤 raw/projected/referenced (R2/W2/W4) | 明确状态字段迁到 `reference_json.state` 或 EventLedger projection metadata | 中 | Phase A' 或 Phase E | 不能仅删除；所有 `COALESCE(projection_state,'')` 查询必须先替换。 |
| team transcript artifact 合并 (R2) | 通过 MessageRepo 查询 `messages.metadata.run_id/turn_id` 或 reference_json 的 message id | 中 | MessageRepo 边界清晰后，或 Phase E | 可迁移，但要保持“最近 projected message + turn_id 可选匹配”语义。 |
| leader report-ready sidecar (R3) | Run append 返回 typed projection result；或 EventLedger append result 带 `message_ref` | 中 | Phase C/E 更合适 | 当前依赖内存 `_projected_message_id`，A2 若改列不应顺手破坏返回字段。 |
| frame/index backfill 默认 raw (W2/W3/W5) | 使用 `reference_json.state='raw'` 或不再需要 state | 低-中 | A' 可做 | 可较早迁移，但必须同步 compaction、append、backfill 三类写点。 |
| storage diagnostics (R4) | 删除列名或改为动态 PRAGMA column existence 过滤 | 低 | A2 同步即可 | 若最终 drop，diagnostics 必须同 PR 改，否则查询不存在列。 |
| 测试契约 (R5/W6) | 测试改为断言新 reference_json / EventLedger projection result | 中 | 跟随生产迁移 | 不能先删生产列再留旧断言；测试红即说明消费方未迁完。 |

### 4.1 可迁移但不能无条件 drop 的原因

- `projected_message_id` 不只是“历史缓存”：append 写时投影、read model backfill、artifact merge、leader report-ready 都在消费。
- `projected_tool_event_id` 不只是 “tool_events 冗余”：reference 压缩优先用它精确反查 tool row，避免 fallback 误配。
- `projection_state` 是当前唯一显式区分 raw/projected/referenced 的列；删除前需要一个同等状态来源。
- `_reconcile_columns()` 仍以 `SCHEMA_SQL` 为准，drop 迁移若不改 schema 会被重新补列；改 schema 又会立刻破坏所有未迁移 SQL。

## 5. 结论 & Phase A2 建议

### 5.1 总建议

- **Phase A2 是否可以 drop `projected_message_id / projected_tool_event_id / projection_state`：否。**
- 当前最稳妥策略是 **Phase A2 保留列**，并把迁移拆成独立 A' 或延后到 Phase E EventLedger 就位后执行。
- 若产品/架构要求最终 drop，必须先完成下列消费方迁移，并保持一段双写/双读窗口。

### 5.2 Phase A2 必须迁移的消费方

1. `hermes_state_run_event_reference.py:144`, `:281`, `:304`, `:353`, `:382-384`, `:417`  
   必须先有替代的 reference metadata 与 projection state，否则 referenced payload 压缩/rehydrate 会 silent degrade 或 SQL 失败。
2. `hermes_state_runs.py:1705-1706`, `:1763-1764`, `:1987`, `:2122`, `:2159`, `:2491`  
   append 投影回写、leader report-ready 传参、list rehydrate 都要切到新结构。
3. `hermes_team_mission/runtime/team_transcript_writer.py:1118-1146`, `:1471-1485`, `:1529-1559`, `:1590-1619`  
   team transcript backfill/artifact merge/report-ready 需要新 message ref 查询来源。
4. `tui_gateway/services/run_control.py:1955-1961`  
   sidecar report-ready 不能继续依赖旧 `_projected_message_id` 语义，或需保留 typed projection result。
5. `tui_gateway/services/storage_stats.py:55-57`  
   若 drop，需要同步移除/动态过滤统计列。
6. 所有测试契约：`tests/test_team_mission_conversation_mirror.py:1204-1215`, `tests/test_hermes_state.py:967`, `:1061`, `:1092`, `tests/test_run_event_frame_codec.py:126-170`, `tests/gateway/test_team_transcript_writer_raw_segments.py:599-704`, `tests/test_pr12_e2e_symptom_regression.py:131-133`。

### 5.3 延期到 Phase E

- `tool_events` 与 `run_events` 的 canonical/derived 边界应等 Phase E EventLedger / tool_events materialized view 方案确定后再迁移。
- `reference_projected_run_event_payloads()` 的 referenced payload rehydrate 最适合并入 EventLedger list/replay 层，避免继续在多个 list 方法里散落调用。
- `projection_state='referenced'` 的长期归属建议进入 EventLedger projection metadata，而不是继续作为 ad hoc run_events 列。

### 5.4 永久保留或长期保留理由

- 若 Phase E 之后仍保留 `messages` 和 `tool_events` 作为独立 read model，某种稳定桥接 id 仍然必要；可以不叫 `projected_*`，但语义不能消失。
- 若不引入 `reference_json` 或等价 projection metadata，则 `projection_state` 仍需保留，否则 raw/projected/referenced 的幂等过滤无来源。
- 若保持当前 `list_run_events*` 直接返回可 rehydrated payload 的接口契约，删除桥接列前必须证明 referenced payload 自身总是携带完整反查 id。

## 6. Grep 复现命令

### 6.1 任务卡要求命令

```bash
grep -rn "projected_message_id\|projected_tool_event_id\|projection_state" . --include="*.py"
grep -rn "reference.get(\"conversation_message_id\"" . --include="*.py"
```

### 6.2 本次审计辅助命令

```bash
rg -n "projected_message_id|projected_tool_event_id|projection_state" . --glob "*.py"
rg -n "reference_projected_run_event_payloads|rehydrate_referenced_run_event|update_run_event_frame_columns\\(|backfill_unprojected_message_complete_events_locked|backfill_projected_message_artifacts_locked|merge_artifact_event_into_projected_message_locked|leader_report_ready_context_for_run" . --glob "*.py"
rg -n "def project_message_complete_event_locked|assistant_conversation_message_id_for|def project_tool_event|project_tool_event\\(" hermes_team_mission/runtime/team_transcript_writer.py hermes_state_tool_events.py hermes_state_runs.py
rg -n "def _reconcile_columns|ALTER TABLE|ADD COLUMN|schema_version|0036|0043|projected" hermes_state.py hermes_agent/storage/migrations/*.py
find hermes_agent/storage/migrations -maxdepth 1 -type f -name "*.py" -print | sort
```

### 6.3 本次 grep 结果覆盖的唯一文件

| 类别 | 文件 |
|---|---|
| Schema / storage | `hermes_state.py`, `hermes_state_runs.py`, `hermes_state_run_event_codec.py`, `hermes_state_run_event_reference.py`, `hermes_state_tool_events.py` |
| Team transcript / gateway | `hermes_team_mission/runtime/team_transcript_writer.py`, `tui_gateway/services/run_control.py`, `tui_gateway/services/storage_stats.py` |
| Tests | `tests/test_team_mission_conversation_mirror.py`, `tests/test_hermes_state.py`, `tests/test_run_event_frame_codec.py`, `tests/test_pr12_e2e_symptom_regression.py`, `tests/gateway/test_team_transcript_writer_raw_segments.py` |

### 6.4 统计口径

- 写入方总数：15 个引用簇，其中生产 12 个，测试 fixture 3 个。
- 读取方总数：23 个引用簇，其中生产/诊断 15 个，测试断言 8 个。
- 唯一涉及文件数：12 个直接 grep 文件；若把 `project_tool_event()` 的值来源文件 `hermes_state_tool_events.py` 计入上游来源，则为 13 个。
- 关键锚点已覆盖：`hermes_state_run_event_codec.py:110`、`hermes_state_runs.py:1705`、`team_transcript_writer.py:1558`。
