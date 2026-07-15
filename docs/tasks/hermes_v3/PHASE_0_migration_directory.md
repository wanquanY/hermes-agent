# Phase 0 — Migration 目录独立化 + import-linter 骨架

**上游文档**：`/Users/yangwanquan/Personal_projects/AIGC_dev/doxie/docs/hermes_agent_architecture_v3.md` §12 Phase 0（v3.0.1 修订）

**分支**：`feat/team-timeline-rearchitecture`（继续沿用；工作树干净时开工）

**目标**：把 `hermes_state.py::_init_schema` 里的 inline migration 全部搬到 `hermes_agent/storage/migrations/`；single declarative source。同时把 `import-linter` 骨架接入 `pyproject.toml`（初期只声明分层名，不启严；给 Phase D 拆解留护栏）。

---

## 严格边界

### 只允许改动的文件（白名单）

**新增**：
- `hermes_agent/__init__.py`（若不存在）
- `hermes_agent/storage/__init__.py`
- `hermes_agent/storage/migrations/__init__.py`
- `hermes_agent/storage/migrations/base.py`（`Migration` 抽象类 + 目录扫描 loader）
- `hermes_agent/storage/migrations/0001_declarative_baseline.py`（对应 `_reconcile_columns` + SCHEMA_SQL 首次建表）
- 每个 `if current_version < N` 块拆一个文件，命名 `00NN_<slug>.py`（N ∈ {10, 11, 14, 18, 20, 28, 29, 30, 31, 33, 34, 36, 37, 38, 39}，共 15 个数据迁移文件）
- `hermes_agent/storage/migrations/0040_reserved_for_phase_a.md`（占位说明，不含代码，保留 0040-0046 号段给 Phase A/B/E/M）
- `hermes_agent/storage/migrations/README.md`（说明 loader 契约 + 版本号规则）
- `.importlinter`（import-linter 配置骨架，只声明 L0-L5 层名，允许所有跨层）
- `tests/storage/test_migrations_loader.py`（loader 单测）
- `tests/storage/test_migrations_smoke.py`（端到端：空 DB 应用所有迁移 = 现有 SCHEMA_VERSION）

**修改**：
- `hermes_state.py`（**仅** `_init_schema` 方法体：改为 `MigrationRunner(cursor).run_all()`；inline 版本块删除；`_reconcile_columns` 保留私有方法但 baseline 迁移调用它）
- `pyproject.toml` / `pyproject.toml`（补 `[tool.importlinter]` + `import-linter` 到 optional dev deps）
- `hermes_team_mission/state/maintenance.py`（**仅**把独立 `run_team_mission_startup_maintenance` 相关 schema-level migration 内容搬进 `storage/migrations/` 对应版本文件；业务级 maintenance 保留在原文件）

### 禁止改动

- 任何 `_reconcile_columns` 内 SCHEMA_SQL 定义（这是 declarative baseline，v3.0.1 保留）
- 任何业务代码（Repository / gateway / dispatch / worker）
- 任何 `hermes_team_mission/runtime/` 目录
- 任何 UI / 前端仓
- `SCHEMA_VERSION` 常量值（保持现值，只搬运不升级）
- `hermes_state.py:6839-6881` `telegram_dm_topic_schema_version` 独立 schema chain —— 本 Phase 不动，属另一 subsystem，Phase E 再处理
- **不要在 pyproject.toml 里加严格 import-linter contract**——只放骨架，具体分层约束 Phase D1 才启严

---

## 具体动作

1. 建 `hermes_agent/storage/migrations/base.py`：
   ```python
   class Migration(Protocol):
       version: int
       description: str
       def apply(self, cursor: sqlite3.Cursor) -> None: ...

   class MigrationRunner:
       def __init__(self, cursor: sqlite3.Cursor): ...
       def run_all(self) -> None:
           """Read schema_version, load all Migration modules from this dir,
           apply those with version > current in ascending order, bump
           schema_version at the end."""
   ```
   要求：
   - 目录扫描按文件名前缀 `NNNN_` 排序
   - baseline (0001) 特殊处理：无 `schema_version` 记录时才跑（首次建表）；之后跳过
   - 每个 migration 显式声明 `version: int`；loader 校验和文件名一致
   - Loader 找不到 migration 或版本号冲突 → 抛异常，不 fallback

2. 拆 `hermes_state.py:1946-2107` 的 16 个版本块，每块搬到 `00NN_<slug>.py`：
   - `0010_fts_trigram_backfill.py`（对应 `if current_version < 10`）
   - `0011_fts_reindex.py`（`< 11`）
   - `0014_...py`（`< 14`）
   - `0018_...py`（`< 18`）
   - `0020_...py`（`< 20`）
   - `0028_...py`（`< 28`）
   - `0029_...py`（`< 29`）
   - `0030_...py`（`< 30`）
   - `0031_...py`（`< 31`）
   - `0033_...py`（`< 33`）
   - `0034_...py`（`< 34`）
   - `0036_...py`（`< 36`）
   - `0037_...py`（`< 37`）
   - `0038_...py`（`< 38`）
   - `0039_...py`（`< 39` / `< SCHEMA_VERSION` 之一，视代码判定；请精确对应）
   - slug 词从原代码注释里取（如 `< 10` 的注释是 "trigram FTS5 table" → slug `fts_trigram_backfill`）

3. `_init_schema` 简化为：
   ```python
   def _init_schema(self):
       cursor = self._conn.cursor()
       cursor.executescript(SCHEMA_SQL)
       self._reconcile_columns(cursor)
       # data migrations 从目录扫描
       from hermes_agent.storage.migrations import MigrationRunner
       MigrationRunner(cursor, self).run_all()   # runner 内部处理 baseline / version bump
       # 保留 deferred 索引 + backfill 相关调用（那些不是 data migration 而是 startup routine）
       ...
   ```
   注意：原代码里 `_backfill_session_list_summaries / _backfill_session_runtime_state / _backfill_tool_events / _drop_deprecated_member_chat_runs / _backfill_session_index_conversation_kind / reconcile_team_mission_node_primary_key / migrate_active_mission_id_to_conversation_missions / _migrate_activities_kind_mission_check` 里，部分是**首次建表 backfill**（保留在 `_init_schema` startup 路径），部分是**版本升级 backfill**（搬到 migration 文件）。判定标准：
   - 只在 `row is None` 分支（首次建 DB）跑的 → startup routine 保留
   - 只在 `if current_version < N` 分支跑的 → 搬去 migration 文件
   - 两处都跑的 → 拆两处调用（idempotent 前提下）

4. team_mission migration 内容整合：
   - 检查 `hermes_team_mission/state/maintenance.py` 内是否有 schema-level DDL（`ALTER TABLE / CREATE TABLE IF NOT EXISTS`）
   - 有的话搬进对应版本 migration 文件（如 `0039_team_mission_events_index.py`），保留 py 里的业务函数
   - 没有则本步跳过

5. `import-linter` 骨架：`.importlinter` 内容：
   ```ini
   [importlinter]
   root_packages =
       hermes_agent

   [importlinter:contract:layered-skeleton]
   name = Hermes v3 layered skeleton (骨架, 未启严)
   type = layered
   layers =
       hermes_agent.transport
       hermes_agent.gateway
       hermes_agent.orchestration
       hermes_agent.domain
       hermes_agent.repositories
       hermes_agent.storage
   ignore_imports =
       hermes_agent.** -> hermes_agent.**
   ```
   pyproject.toml 加：
   ```toml
   [project.optional-dependencies]
   dev = [..., "import-linter>=2.0"]
   ```
   （注意：`ignore_imports = hermes_agent.** -> hermes_agent.**` 是全放行，只求 loader 能起来，Phase D1 才收紧）

6. `tests/storage/test_migrations_loader.py`：
   - loader 目录扫描顺序单测
   - 版本号冲突（两个文件同版本）抛异常
   - migration.apply 抛错时 runner 中止不误报成功

7. `tests/storage/test_migrations_smoke.py`：
   - 空 DB → 跑 `_init_schema` → 查 `PRAGMA table_info(...)` 断言核心表存在 + `schema_version = SCHEMA_VERSION`
   - 半升级 DB (`schema_version = 15`) → 跑 → 断言最终 `= SCHEMA_VERSION` 且中间迁移都执行过

---

## 门禁（必须全绿才算 Phase 0 完成）

```bash
cd /Users/yangwanquan/syngents/code/hermes-agent

# 1) 全量单测通过
pytest -x

# 2) 新目录存在且有 loader
ls hermes_agent/storage/migrations/ | grep -E "^00[0-4][0-9]_" | wc -l   # 期望 >= 15
test -f hermes_agent/storage/migrations/base.py
test -f .importlinter

# 3) inline chain 消除
grep -c "if current_version <" hermes_state.py                            # 期望 0
grep -n "MigrationRunner" hermes_state.py                                 # 期望 >= 1

# 4) telegram_dm_topic 独立 schema 未被误改
grep -c "telegram_dm_topic_schema_version" hermes_state.py                # 期望 == 2 (不变)

# 5) import-linter 骨架能起来
lint-imports --config .importlinter                                       # 期望 exit 0
```

---

## 交付格式

**分两次 commit**（便于复验分层）：

- Commit 1：`refactor(storage): PHASE 0.1 migrations 目录 + Migration 抽象 + loader + baseline`
  - 只含 `hermes_agent/storage/migrations/{base.py,__init__.py,0001_declarative_baseline.py}` + `tests/storage/test_migrations_loader.py`
  - `hermes_state.py` 不变
- Commit 2：`refactor(storage): PHASE 0.2 inline chain 拆 15 个版本 migration 文件 + team_mission 整合 + importlinter 骨架`
  - 剩下所有内容
  - `_init_schema` 改为调 `MigrationRunner`

commit message 中文正文风格与项目历史一致（参考 `git log --oneline` 前 20 条）。

---

## 反 scope 蔓延约束

- **禁止顺手改** `_reconcile_columns` 里的表定义（那是 Phase A 的活）
- **禁止顺手补** projected_message_id / projected_tool_event_id 相关任何逻辑（那是 Phase A 消费方审计的活）
- **禁止顺手改** SeqAllocator / RunStateMachine / WorkerPool 相关代码（各自 Phase）
- **禁止顺手加** 新 method / 新 gateway / 新业务逻辑
- 如发现需要改上面任何一项才能完成 Phase 0，**停下来在 commit message 里写清楚阻塞点，不要越界**

---

## 交付后（我会做的复验）

1. `git status/git diff` 独立看 diff 与白名单一致
2. `pytest` 独立跑一遍
3. 门禁 5 条 grep 复核
4. 抽样看 `0010_fts_trigram_backfill.py` 内容是否忠实搬运原 `if current_version < 10` 块
5. 复验通过 → 派 Phase A 任务卡；不通过 → 回归对应 commit 让 codex 修
