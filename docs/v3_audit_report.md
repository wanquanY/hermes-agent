# Hermes Agent Backend v3.0.3 架构重构验收报告

> **审查日期**：2026-07-07
> **审查依据**：`docs/hermes_agent_architecture_v3.md` v3.0.3（Phase 0→M 落地路径 + J1-J11 不变量）
> **审查方法**：3 路并行子代理深度代码审查 + 独立复验关键"伪绿"指控
> **审查仓库**：`/Users/yangwanquan/syngents/code/hermes-agent`

---

## 总判定：未通过验收

**新架构是一套"并行影子系统"——`hermes_agent/` 包代码质量高、测试覆盖好、不变量结构完整，但它没有替换生产路径。** 真正的运行时仍 100% 走老代码（`hermes_state.py` + `tui_gateway/` + `gateway/`）。新老代码并存度约 **91%**，老代码仍是运行时主干。

`hermes_agent/` 45 个文件中仅 3 个域服务（SeqAllocator / RunStateMachine 词汇表 / run_terminator）被生产代码调用，其余 42 个文件（93%）**零生产调用**，仅测试可达。

---

## 一、新老共存耦合度（最严重问题）

| 层 | 老（生产主干） | 新（hermes_agent/） | 新生产接线率 |
|---|---|---|---|
| **SessionDB / Repository** | `hermes_state.py` 7446行/197方法 + 9 mixin（`hermes_state_runs.py` 3472行等共 ~16k 行） | 5 RepoImpl 1722行，真实 SQL 实现 | **0%**（零生产调用） |
| **Gateway dispatch** | `tui_gateway/` 116文件/43k行，38 方法模块，双注册热覆盖 | `hermes_agent/gateway/` 13文件，registry/pipeline/auth 完整 | **0%**（server.py 不引用） |
| **老 gateway/** | `gateway/` 63文件/92k行，62 生产文件 import | — | **Phase J 未退役，100% 活** |
| **run 事件写入** | `append_run_event` 直接 INSERT（2处）+ `next_run_event_seq` MAX+1 | EventLedger 321行 | **0%**（EventLedger 零生产调用） |
| **seq 分配** | `next_run_event_seq` 仍活（含 MAX+1 TOCTOU fallback） | SeqAllocator 127行 | **混合**（嵌入老 append_run_event，但读路径仍走老） |
| **terminal 转换** | `run_control.py` legacy frame publish | run_terminator 313行 | **混合**（被调用但 try/except 静默降级回老路径） |
| **Migration** | `_reconcile_columns` + inline version-gated chain 仍在 | MigrationRunner + 21 文件 | **混合**（runner 从 _init_schema 调用，但老 inline 同时运行） |

### 关键证据

**老单体仍是运行时唯一主干：**
- `hermes_state.py` 7446 行 / 197 方法，仍 import 全部 9 个 `hermes_state_*` mixin（`hermes_state.py:29-49`）
- `_reconcile_columns`（`hermes_state.py:1354`）+ `_init_schema`（`:1967`）+ `if current_version <`（`:6785`）inline migration 仍在
- `hermes_state.py` grep 不到任何 `self._session_repo` / `self._run_repo` delegate 模式——新 Repository 未被老 SessionDB 引用
- 30+ 生产文件仍直接 `import hermes_state`（`tui_gateway/server.py`、`run_agent.py`、`hermes_team_mission/*` 等）
- `tui_gateway/` 仍 116 个 .py，85 个生产文件 import 它，`server.py` 只用 `tui_gateway` 不用 `hermes_agent.gateway`

**新 Repository 是真实实现但零生产调用：**
- 5 个 `*Impl` 类有真实 SQL（非 NotImplementedError 空壳）
- 例：`SessionRepoImpl.create()`（`session_repo.py:132-146`）真实 `INSERT OR REPLACE` + 参数化绑定
- 例：`RunRepoImpl.append_event()`（`run_repo.py:166-182`）委托 `EventLedger.append()`
- **但** `grep -rn "SessionRepoImpl|RunRepoImpl|MessageRepoImpl|TeamMissionRepoImpl|AgentProfileRepoImpl" --exclude-dir=hermes_agent --exclude-dir=tests` → **0 生产引用**
- 唯一实例化点是 `stdio_daemon.py:81-85`，而 stdio_daemon 自身无生产调用

**新 Gateway 方法未接线：**
- `tui_gateway/server.py:960-969` 的 `method()` 装饰器只接受 `tui_gateway.methods.*` 和 `hermes_team_mission.gateway.*` 两个前缀
- `hermes_agent.gateway.methods.*` 不在白名单内，无法注册到生产 dispatch
- 生产 dispatch 方法表来源：`METHOD_MODULES`（`method_registration.py:11-50`，38 个模块，全部老路径）
- 新 `hermes_agent/gateway/methods/` 6 个 .py → **生产接线 0/6 = 0%**

---

## 二、逐 Phase 落地判定

### ❌ 未落地的 Phase（5 个）

#### Phase D — 拆 SessionDB → 5 Repository

| 门禁要求 | 实测 | 状态 |
|---|---|---|
| `wc -l hermes_state.py < 500` | 7446 行 | ❌ |
| `grep -rn "SessionDBRunMixin" hermes_agent/ = 0` | `hermes_state.py:45` 仍 import，`:837` 仍是基类 | ❌ |
| `grep -rn "class Session.*Mixin" = 0` | 9 个 mixin 全在 | ❌ |

5 个 RepoImpl 真实实现但零生产调用，SessionDB 197 方法仍是唯一数据门。

#### Phase E — 统一 EventLedger

| 门禁要求 | 实测 | 状态 |
|---|---|---|
| `grep -rn "team_mission_events" hermes_agent/ = 0` | team_mission_events 独立 seq 域仍活 | ❌ |
| `list_run_events/list_tool_events` 收敛到 EventLedger | 老方法独立 SQL 查询，不委托 EventLedger | ❌ |
| `0045_team_mission_events_retire` + `0046_tool_events_readonly` 迁移 | 0045 实际是 `runs_terminal_columns`，无 0046 | ❌ |

- `hermes_team_mission/state/event_log.py:737` 仍 `SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM team_mission_events`——独立 seq 域
- `hermes_state_runs.py:1676` `append_run_event` 直接 `INSERT OR IGNORE INTO run_events`，不经 EventLedger
- `hermes_state_runs.py:2704` recovery 路径直接 `INSERT INTO run_events`
- EventLedger 零生产调用（`grep -rn "EventLedger" --exclude-dir=hermes_agent --exclude-dir=tests` → 0）

#### Phase G — 单一 gateway registry + 归一 + 鉴权

| 门禁要求 | 实测 | 状态 |
|---|---|---|
| `grep -rn "DOVIE_GATEWAY_METHOD_OVERRIDES" hermes_agent/ = 0` | 4 处（注释引用 + pyc） | ⚠️ |
| `grep -rn "sessionId|runId" hermes_agent/gateway/methods/ = 0` | 0（v3 内达标） | ✅ |
| 生产 dispatch 走新 registry | server.py 100% 走 tui_gateway | ❌ |

- `dovie_extension/gateway_methods.py:30` `DOVIE_GATEWAY_METHOD_OVERRIDES` frozenset 约 104 项仍存在
- `tui_gateway/server.py:337` `_EXTRACTED_METHOD_OVERRIDES = _DOVIE_EXTENSION.gateway_method_overrides()` 仍加载
- `tui_gateway/server.py:1672` `_DOVIE_EXTENSION.register_gateway_methods(_methods)` 热覆盖路径仍活跃
- 新 `hermes_agent/gateway/` 完整但生产不引用——是"正确但未上线的影子"

#### Phase J — 退役老 gateway/

| 门禁要求 | 实测 | 状态 |
|---|---|---|
| `rm -rf gateway/` + pytest 全绿 | gateway/ 63文件全在 | ❌ |

- `gateway/` 根目录 63 个 .py 文件全在（92489 行）
- `tui_gateway/services/platform_connections.py:516/540/593/612/642/680` 仍 import `gateway.platforms.feishu/qqbot/weixin`
- `tui_gateway/server.py:946` import `gateway.run`
- `tui_gateway/methods/system.py:108` import `gateway.status`
- `tui_gateway/methods/handoff.py:38` import `hermes_gateway.config`
- `pyproject.toml [tool.setuptools.packages.find]` 仍含 `"gateway", "gateway.*"`

#### Phase M — drop runtime_source_seq

| 门禁要求 | 实测 | 状态 |
|---|---|---|
| `grep -rn "runtime_source_seq" hermes_agent/ = 0` | 19 处（主要在 migrations） | ❌ |

- `0047_drop_runtime_source_seq.py` 存在但 `PENDING_FRONTEND_H3 = True`，`apply()` 抛 `FrozenMigrationError`（设计性 parked）
- `hermes_state_run_event_reference.py:374-406` 仍活跃写入 `runtime_source_seq`（`:385 runtime_source_seq = ?`）
- parked 符合 spec（前端 H3 未行），但 Phase M 本身未达成

### ⚠️ 部分落地的 Phase（4 个）

#### Phase 0 — Migration 目录独立化

| 门禁要求 | 实测 | 状态 |
|---|---|---|
| `grep -c "if current_version <" hermes_state.py = 0` | 1 处（`hermes_state.py:6785`） | ❌ |
| inline chain 消除 | `_reconcile_columns`（`:1354`）+ inline migrate + runner 三层混合 | ❌ |
| `@migration(version=` ≥ 39 | 21 个迁移文件 | 部分 |

- `hermes_agent/storage/migrations/` 目录 + `MigrationRunner` + 21 文件存在且从 `_init_schema` 调用（`hermes_state.py:2021-2023`）
- **但** `_init_schema` 仍是混合模式：`executescript(SCHEMA_SQL)` → `_reconcile_columns()` → 5 个 inline migrate → 3 个 backfill → MigrationRunner
- spec 要求简化为"扫描 migrations 目录 + 按 version 应用"，未达成

#### Phase A — Identity 统一 + FK

| 门禁要求 | 实测 | 状态 |
|---|---|---|
| `grep stored_session_id|stable_session_id 应用层 = 0` | 1194 处 | ❌ |
| `PRAGMA foreign_key_check = 0` | 未独立验证 | ? |
| interaction 落库 | `_internal.interaction.*` 已落 run_events | ✅ |

- 0040 迁移存在且活跃
- **但** `stored_session_id` 应用层仍有 1194 处，包括新代码 `tui_gateway/services/interaction_registry.py:66` 也在用
- interaction 持久化通道（`_internal.interaction.requested/resolved/expired` → run_events）已落地

#### Phase B — SeqAllocator 集中

| 门禁要求 | 实测 | 状态 |
|---|---|---|
| `grep INSERT INTO run_events（非 seq_allocator）= 0` | `hermes_state_runs.py:2704` 仍直接 INSERT | ❌ |
| `grep SELECT MAX(seq) = 0` | `hermes_state_runs.py:833` 仍 MAX+1 fallback；15 处 | ❌ |
| `PRAGMA busy_timeout = 5000` | 未独立验证 | ? |

- SeqAllocator 核心 `_allocate_once`（`seq_allocator.py:108-127`）正确实现 `UPDATE...RETURNING` + 3 次退避 + `SeqAllocatorBusy`
- **但** `next_run_event_seq`（`hermes_state_runs.py:821-837`）仍活，row is None 时 fallback 到 `SELECT MAX(seq)+1`（TOCTOU 面）
- bootstrap MAX+1 逻辑三处重复：`seq_allocator.py:71` / `event_ledger.py:233` / `run_terminator.py:286`
- `append_run_event`（`hermes_state_runs.py:1447`）用新 SeqAllocator 分配 seq，但同一函数 `:1676` 仍直接 INSERT（不经 EventLedger）

#### Phase I — 无 silent swallow

| 门禁要求 | 实测 | 状态 |
|---|---|---|
| `ruff check --select silent-swallow = 0` | 工具存在但未接入 CI | ❌ |
| CI 阻断 | `.github/workflows/` 无 silent swallow 门禁 | ❌ |

- `hermes_agent/observability/silent_swallow_lint.py` AST 扫描工具存在
- `tests/observability/test_silent_swallow_lint.py` 单测存在
- **但** CI workflow 未接入；覆盖范围只 `hermes_agent/`，`tui_gateway/` + `hermes_state*.py` + `gateway/` 全部豁免
- `pyproject.toml [tool.ruff.lint] select = ["PLW1514"]`——无 silent-swallow 自定义规则

### ✅ 仅在 v3 子系统内达成（生产不受益）

- **J6/J7/J8/J9**（gateway registry/归一/鉴权/ErrorCode）：新 gateway 代码完整且测试覆盖好，但生产网关 tui_gateway 完全不用它
- **J10**（handshake）：`handshake.py` + `transport/router.py:146` 真调用，但 tui_gateway 生产路径不发 handshake

---

## 三、不变量达成度

| 不变量 | 达成度 | 核心问题 |
|---|---|---|
| **J1** 单一 identity + FK | ❌ 未达成 | `stored_session_id` 1194 处；新代码 `interaction_registry.py:66` 也在用 |
| **J2** seq 单调+原子 | ⚠️ 部分 | 核心正确+并发测试；`hermes_state_runs.py:833` MAX+1 fallback 残留 |
| **J3** 单一 EventLedger | ❌ 未达成 | EventLedger 存在但 2 处直接 INSERT 旁路；**J3 测试假绿** |
| **J4** RunStateMachine 单入口 | ❌ 未达成 | run_terminator 本体正确，但 6 处直接 UPDATE 旁路 + 静默降级 |
| **J6** 单一 registry | ⚠️ 部分（v3 内达成） | 生产仍双注册热覆盖 |
| **J7** snake_case 归一 | ⚠️ 部分（v3 内达成） | 生产仍 camelCase 双写 |
| **J8** 鉴权强制 | ⚠️ 部分（v3 内达成） | 生产仍 `_READ_ONLY_DB_METHODS` frozenset 硬编码 |
| **J9** ErrorCode enum | ⚠️ 部分（v3 内达成） | 生产仍 magic number |
| **J10** handshake | ⚠️ 部分（v3 内达成） | 生产不发首帧 |
| **J11** 无 silent swallow | ❌ 未达成 | CI 未接入；覆盖范围 excludes 老代码 |

---

## 四、三个必须优先修复的"伪绿"问题

### 伪绿 1：J3 测试假绿（最危险）

**位置**：`tests/domain/test_j3_event_ledger_single_writer.py:20,80`

```python
V3_PKG = REPO_ROOT / "hermes_agent"       # 第 20 行：只扫这个目录
for path in _iter_python_files(V3_PKG):    # 第 80 行：仓库根的 hermes_state_runs.py 不在范围内
```

**问题**：`hermes_state_runs.py` 在仓库根目录（`/Users/yangwanquan/syngents/code/hermes-agent/hermes_state_runs.py`），不在 `hermes_agent/` 包内。它的 2 处 `INSERT INTO run_events`（`hermes_state_runs.py:1676` 和 `:2704`）是生产环境主写入路径，但 J3 测试完全检测不到。

**后果**：J3 测试给出假绿，让团队误以为"单一 EventLedger"不变量已达成。实际生产环境中 EventLedger 零调用，所有写入走老 `append_run_event` 直接 INSERT。

**修复方向**：
1. 扫描范围扩大到整个仓库（排除 tests/ 和 migrations/）
2. 或将 J3 守卫改为 grep-based：`grep -rn "INSERT INTO run_events" --include="*.py" . | grep -v "event_ledger\|seq_allocator\|storage/migrations\|tests/"` 期望 0

---

### 伪绿 2：J4 被 6 处直接 UPDATE runs 旁路

**位置**：`hermes_state_runs.py`

```
line 790:  UPDATE runs           # _repair_control_only_active_runs_locked()
line 923:  UPDATE runs           # upsert_run()
line 1939: UPDATE runs           # append_run_event 内
line 2744: UPDATE runs           # recovery 循环
line 3036: UPDATE runs           # compact_run_events
line 3399: UPDATE runs           # 另一处
```

**额外问题**：`tui_gateway/services/run_control.py:2190-2224` 的 terminate_run 包装器有静默降级：

```python
# run_control.py:2190
atomic_result = None
try:
    from hermes_agent.domain.run_terminator import terminate_run as _domain_terminate_run
    atomic_result = _domain_terminate_run(conn, ...)       # :2204
except ValueError:
    atomic_result = None                                    # :2216 回退老路径
except Exception:
    _log.exception("run_state_machine.terminate_run failed ...")
    atomic_result = None                                    # :2224 回退老路径
```

任何异常都会跳过 domain terminate_run，退化为只发 frame 不更新 DB status——run 会卡在 running。

**后果**：J4"单入口"不变量在实际运行中被击穿。`upsert_run()` 可写任意 terminal 状态，完全绕过 `terminate_run()` 的 idempotency + transition 校验。

**修复方向**：
1. J4 守卫 grep 扩大到仓库根：`grep -rn "UPDATE runs" --include="*.py" . | grep -v "run_terminator\|run_state_machine\|storage/migrations\|tests/"` 期望 0
2. `run_control.py` 的 fallback 改为 raise 或明确降级标记（`terminal_degraded=1`），不静默回退

---

### 伪绿 3：anchor_seq 语义错误

**位置**：`tui_gateway/services/interaction_registry.py:76-78, 85-118`

**spec §7.4.4 要求**：`anchor_seq` 是"触发它的 run_event（通常是 tool.complete）的 seq"，由 caller 显式传入。

**实现实际**：

```python
# interaction_registry.py:70-78
saved = db.append_run_event(stored_session_id, frame)   # 写 _internal.interaction.requested 事件
if internal_type == "_internal.interaction.requested" ...:
    _set_requested_anchor_seq(db, stored_session_id, request_id, int(saved["seq"]))  # :76
    saved["anchor_seq"] = int(saved["seq"])   # :78 ← 把 anchor_seq 设为 interaction 自身的 seq
```

```python
# interaction_registry.py:85-118
def find_interaction_anchor_seq(db, session_id, request_id) -> int:
    """Return the request event seq for a persisted interaction, if known."""
    row = conn.execute("""
        SELECT seq FROM run_events
         WHERE session_id = ? AND interaction_request_id = ?
           AND event_type = '_internal.interaction.requested'   # ← 查的是 interaction 自己的 seq
    """)
```

**后果**：前端 R4 排位拿到的 `anchor_seq` 不是"触发 approval 的 event seq"（如 tool.complete 的 seq），而是"approval 请求自身的 seq"。语义完全错位，排位锚点失去业务含义。

**修复方向**：
1. `InteractionRegistry.request()` 的 `anchor_seq` 改为 caller 必传参数（spec §7.4.4 明确"必填位置参数"）
2. 业务侧（tool 执行内触发 approval 处）显式传入触发 event 的 seq
3. `find_interaction_anchor_seq` 改为查触发 event，而非 interaction request 自身

---

## 五、未落地的 spec 契约

### 1. §6.2 14 arm payload dataclass 完全未建

spec §6.2.1 要求 14 个 payload dataclass 逐字段定义：
- `MessageStartPayload` / `MessageDeltaPayload` / `MessageCompletePayload`
- `ReasoningDeltaPayload` / `ReasoningAvailablePayload` / `ThinkingDeltaPayload`
- `ToolStartPayload` / `ToolGeneratingPayload` / `ToolProgressPayload` / `ToolDeltaPayload` / `ToolCompletePayload`
- `ErrorPayload` / `SessionInterruptedPayload` / `SessionRecalledPayload`

组成 `TypedPayload` union，且明确"**无 Dict[str, Any]**"。

**实测**：整个 `hermes_agent/` 包零个 payload dataclass 存在。唯一接近的是 `run_repo.py:47`：

```python
@dataclass(frozen=True)
class CanonicalEventSpec:
    event_type: str
    payload: dict[str, Any]   # ← 直接违反 spec "无 Dict[str, Any]"
    run_id: str
    turn_id: str = ""
    preassigned_seq: int | None = None
```

也没有 `CanonicalEventType` enum——事件类型在整个代码库中是裸字符串。

**风险**：spec 明确"Phase A 前 freeze...Phase A 后 payload 字段只能 additive，不能 rename/remove/改类型"。前端 v3.1 Phase A5 `exhaustive switch + never` 依赖此 freeze。现在零个 dataclass 是高风险。

### 2. §7.4 Interaction 类型契约缺失

spec 要求的以下类型全部未建：
- `InteractionFrame` dataclass（§7.4.3）—— 实际是裸 dict
- `InteractionFrameType` enum（§7.4.3）—— 实际是字符串拼接 `f"interaction.{status}"`
- `InteractionRegistry` 类（§7.4.4）—— 功能分散在 `interaction_registry.py`（helper 函数）和 `worker_frame_router.py`（frame 下发）
- `InternalRunEventType` enum（§7.4.2）—— `_internal.interaction.*` 是字符串常量在 dict 映射中

### 3. Phase E 迁移编号偏移

| spec 要求 | 实测 |
|---|---|
| `0045_team_mission_events_retire.py` | `0045_runs_terminal_columns.py`（Phase C 用） |
| `0046_tool_events_readonly.py` | 不存在 |

Phase E 的两个迁移文件根本不存在，Phase E 未落地。

---

## 六、唯一真实落地的部分

以下 3.5 个域服务本身实现质量高、测试覆盖好，是**真落地而非空壳**，但它们被嵌入老代码内部作为子步骤替换，而非老代码被替换：

### 1. SeqAllocator（`hermes_agent/domain/seq_allocator.py`，127 行）

- `_allocate_once`（`:108-127`）正确实现 `UPDATE seq_counter SET next_seq = next_seq + 1 ... RETURNING next_seq - 1`
- `allocate_only`（`:89-105`）3 次指数退避（0.1s/0.3s/0.9s）+ `SeqAllocatorBusy` 抛出
- 被 `hermes_state_runs.py:20` import，`:1447`（append_run_event 主写入）和 `:2673`（orphan recovery）真实调用
- `test_seq_allocator.py:125` 有真并发测试（12 workers × 15 events = 180 events，断言无 collision/gap）

### 2. RunStateMachine 词汇表（`hermes_agent/domain/run_state_machine.py`，151 行）

- 状态常量 `ACTIVE_RUN_STATUSES` / `TERMINAL_RUN_STATUSES` / `RUN_OPENING_EVENT_TYPES`
- `terminal_status_from_event()` / `prefer_terminal_run_status()` / `resolve_run_status_transition()` 等 7 个转移函数
- 被 `hermes_state_runs.py:11-19` import 10 个符号，15+ 处使用
- **注意**：这是纯状态词汇表，不含 `terminate_run()`。spec §7.2 说的 `RunStateMachine.terminate_run()` 实际在 `run_terminator.py`

### 3. run_terminator（`hermes_agent/domain/run_terminator.py`，313 行）

- `terminate_run()`（`:79-266`）本体实现质量高：
  - ✅ BEGIN IMMEDIATE（`:126`）
  - ✅ Idempotency（`:137` 已 terminal → IDEMPOTENT_SKIP）
  - ✅ SeqAllocator 同事务分配 terminal_seq（`:159`）
  - ✅ EventLedger.append with preassigned_seq（`:207`）
  - ✅ UPDATE runs SET status, terminal_seq, terminal_cause, completed_at（`:217-240`）
  - ✅ DEGRADED 路径（`:160-199`）：SeqAllocatorBusy → 更新 runs.status + terminal_degraded=1 + log `event=degrade cause=seq_busy`
  - ✅ 3 路径收敛通过 `TerminateCause` enum
- 被 `tui_gateway/services/run_control.py:2195-2212` 调用（**但有 try/except 静默降级**）
- `test_run_terminator.py` 覆盖 APPLIED / IDEMPOTENT_SKIP / DEGRADED / error 映射

### 3.5. MigrationRunner（`hermes_agent/storage/migrations/base.py`，295 行）

- `MigrationRunner.run_all()` 按目录扫描 + 文件名排序 + 版本去重
- 从 `hermes_state.py:2021-2023` `_init_schema` 调用
- 0047 frozen 机制（`FrozenMigrationError` 被 runner 捕获 skip）设计合理
- **但**老 inline migration 同时运行，非唯一路径

---

## 七、建议下一步

这个架构不能按"已落地"交付。核心问题是**新老切换没做**——新代码是正确但未上线的影子系统。

### 优先级 1：修伪绿（阻断误判）

1. **J3 测试扫描范围**：`V3_PKG` 改为 `REPO_ROOT`（或排除 tests/migrations 的全仓扫描），让 `hermes_state_runs.py:1676/2704` 的直接 INSERT 能被检测到
2. **J4 旁路守卫**：加 `grep -rn "UPDATE runs" --include="*.py" . | grep -v "run_terminator\|storage/migrations\|tests/"` 期望 0 的 CI 门禁
3. **run_control.py 静默降级**：`except Exception: atomic_result = None` 改为 raise 或标 `terminal_degraded=1`，不静默回退

### 优先级 2：建类型契约（阻断前端）

4. **14 arm payload dataclass**：在 `hermes_agent/domain/canonical_event.py` 建 14 个 frozen dataclass + `CanonicalEventType` enum + `TypedPayload` union，spec 明确要求 Phase A 前 freeze
5. **Interaction 类型**：建 `InteractionFrame` / `InteractionFrameType` / `InternalRunEventType` / `InteractionRegistry` 类
6. **anchor_seq 语义修正**：改为 caller 必传触发 event 的 seq

### 优先级 3：定切换计划（核心硬骨头）

7. **Phase D 切换**：`hermes_state.py` 的方法改为 delegate 到新 RepoImpl，而非继续叠加。这是阻断 J3 旁路的前提
8. **Phase G 切换**：`tui_gateway/server.py` 的 dispatch 改走 `hermes_agent.gateway.pipeline`，METHOD_MODULES 替换为新 registry
9. **Phase E 收敛**：`append_run_event` → `EventLedger.append` 切换，收敛写入路径
10. **Phase J 退役**：确认 `gateway/` 的活依赖后删除

### 优先级 4：收敛外围

11. **Phase 0 收尾**：删 `_reconcile_columns` + inline `if current_version <` 残留
12. **Phase A 收尾**：`stored_session_id` 1194 处全量替换
13. **Phase I CI 接入**：silent_swallow_lint 接入 `.github/workflows/`，覆盖范围扩大到全仓

---

## 附录：关键文件清单

| 文件 | 行数 | 角色 |
|---|---|---|
| `hermes_state.py` | 7446 | 老单体 SessionDB，仍是运行时唯一主干 |
| `hermes_state_runs.py` | 3472 | 老 RunMixin，含 6 处 UPDATE runs 旁路 + 2 处直接 INSERT run_events |
| `hermes_team_mission/state/event_log.py` | — | team_mission_events 独立 seq 域（MAX+1） |
| `tui_gateway/server.py` | — | 生产网关 dispatch，走 METHOD_MODULES + DOVIE_GATEWAY_METHOD_OVERRIDES |
| `tui_gateway/services/run_control.py` | — | terminate_run 包装器，有静默降级 fallback |
| `tui_gateway/services/interaction_registry.py` | 191 | interaction 持久化 + anchor_seq 语义错误 |
| `gateway/`（根目录） | 92489 | 老 gateway/，Phase J 未退役 |
| `hermes_agent/domain/seq_allocator.py` | 127 | ✅ SeqAllocator 真实落地 |
| `hermes_agent/domain/run_state_machine.py` | 151 | ✅ 状态词汇表真实落地 |
| `hermes_agent/domain/run_terminator.py` | 313 | ⚠️ terminate_run 本体正确但有 fallback |
| `hermes_agent/domain/event_ledger.py` | 321 | ❌ 真实实现但零生产调用 |
| `hermes_agent/repositories/*.py` | 1722 | ❌ 5 RepoImpl 真实但零生产调用 |
| `hermes_agent/gateway/*.py` | ~1500 | ❌ registry/pipeline/auth 完整但生产不引用 |
| `hermes_agent/storage/migrations/` | 21 文件 | ⚠️ MigrationRunner 被调用但老 inline 同时运行 |
| `tests/domain/test_j3_event_ledger_single_writer.py` | — | ❌ J3 假绿（扫描范围盲区） |
