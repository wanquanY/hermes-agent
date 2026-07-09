# P5 伪拆解发现 —— GatewayRunner 的 mixin 重组(给 codex + user 验收）

**审计日期**：2026-07-09
**严重级**：P0 —— 这是比 P2「搬家换名」更隐蔽的 god-object 伪拆解，且**已过所有现有机器门禁**。

---

## 一句话

`gateway/run.py` 的 209 方法巨物 `GatewayRunner` 被拆成 **46 个 `*Mixin` 类**（各 ~3 方法、各自 <800 行文件），再用**多继承拼回同一个类**：

```python
class GatewayRunner(
    GatewayApprovalCommandMixin,
    GatewayBackgroundTaskMixin,
    ... 共 46 个 mixin ...
):
    def __init__(self, config): ...
```

每个文件都小、每个类 body 都 <80 方法、`grep` 看到 90 个整洁模块 —— **但运行时对象仍然暴露 ≈216 个方法在同一个 `self` 上**，全部共享状态，没有一个 mixin 能独立实例化或测试。**源码被分片了，god-object 完好无损。**

---

## 证据

### 1. 组合后方法面 = 216（不减反增，原 209）

```
51 个 Mixin 类，166 方法合计 + GatewayRunner 自身 48 方法 → 组合方法面 ≈ 214-216
```
（`tests/repositories/test_no_relocated_god_object.py::_composed_method_surface` 展开本地 MRO 实测。）

### 2. 生产实例化 —— 这是活的运行时对象，不是死空壳

```
gateway/run.py:7174:    runner = GatewayRunner(config)
```
cli.py / hermes_cli 通过 `from gateway.run import start_gateway` 拉起它。**这就是线上网关运行时那个 god-object。**

### 3. mixin 不是模块，是巨物的碎片 —— 读 `hermes_gateway/session_recovery_runtime.py`

`GatewaySessionRecoveryRuntimeMixin` 的每个方法都引用**兄弟 mixin 拥有的**属性/方法：
- `self.session_store`、`self.adapters`、`self._running_agents`、`self._running_agents_ts`、
  `self._background_tasks`、`self._persist_active_agents()`
- 且**直接伸手进别的对象的私有**：`self.session_store._entries` / `._lock` / `._ensure_loaded_locked()` / `._save()`

→ 这个「模块」无法脱离完整 GatewayRunner（+其余 50 个 mixin）存在。它不是一个有边界的单元，是一个类的文本碎片。

### 4. 老债原样带进新树

同一文件里 ~7 处 `except Exception: pass` / `except: counts = {}` 静默吞异常，从 `gateway/run.py` 逐字搬进 `hermes_gateway/`。（这正是 P6 `no_silent_swallow` 在 `hermes_gateway/` 亮红的原因 —— born-clean 门禁正抓到它。）

---

## 为什么这能过所有门禁（门禁的洞）

`scripts/zero_debt/verdict.py` 的 `_god_object_offenders()`（约 L523）：

```python
methods = sum(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
              for child in node.body)   # ← 只数 class body，不展开 MRO
```

- 每个 mixin body ~3 方法 → 过
- GatewayRunner 自身 body 48 方法 → 过（<80）
- 组合后 216 方法面 → **门禁完全看不见**

`p5:gateway_run_decomposed`（gateway/run.py ≤800 行）也会被骗：当 run.py 缩到 ≤800 行、只剩 `class GatewayRunner(46 mixins): __init__` 时，**行数门禁绿、巨物却活着**。

**这个洞是审计方（本会话）自己埋的** —— 我设计的方法数探测器按 body 计数。已在
`tests/repositories/test_no_relocated_god_object.py` 补上 `_composed_method_surface`
组合臂（展开本地 MRO 数去重方法名），现精确命中 GatewayRunner 一个。

---

## 为什么 P2 不算这个问题（对照，避免误伤）

`HermesStateStore` 组合方法面也有 236，但它是**另一回事**：

| | HermesStateStore (P2) | GatewayRunner (P5) |
|---|---|---|
| 组合方法面 | 236 | 216 |
| 生产实例化 | **否**（`p2:no_hermes_state_store_production_instantiation` 绿）| **是**（run.py:7174）|
| 真正的 owner | 5 个聚合 repo（§4.6 守着）| 无 —— 它自己就是运行时 |
| 结论 | facade 是待删兼容空壳，生产走 repo → **真拆** | 活的 god-object → **伪拆** |

→ 组合探测器已把 `HermesStateStore` 加进白名单（理由绑定 P2 gate），只锁 GatewayRunner。

---

## 给 codex 的清单

### A. 把组合面门禁焊进 verdict.py（阻断 P5 伪绿）

采纳 `tests/repositories/test_no_relocated_god_object.py::_composed_method_surface` 的逻辑，
在 `verdict.py` 加 **`p5:no_mixin_recomposed_gateway_monolith`**：

- 判据 = **组合方法面（展开本地 MRO 去重）> 80 且该类在生产被实例化**
- 排除 `channels/`（共享基类 `BasePlatformAdapter` 继承是正常模板法，且 channel 体量是 manifest 明确的分离债、不在本次范围）
- 白名单 `HermesStateStore`（非生产实例化的兼容空壳，由 P2 gate 另行守）
- 现状：命中 `GatewayRunner` 216 → 红。P5 不得在此项红时判绿。

### B. 真正的拆法 —— 组合，不是继承

把行为簇拆成 GatewayRunner **委托**的独立协作对象，而不是它 **继承**的 mixin：

```python
# 不是这样（mixin 拼回同一个 self）：
class GatewayRunner(GatewaySessionRecoveryRuntimeMixin, ...): ...

# 而是这样（协作对象，可独立实例化/测试，边界清晰）：
class SessionRecoveryService:
    def __init__(self, session_store, adapters, running_agents): ...
    def schedule_resume_pending(self) -> int: ...

class GatewayRunner:
    def __init__(self, config):
        self.recovery = SessionRecoveryService(self.session_store, self.adapters, ...)
    # 委托：
    def _schedule_resume_pending_sessions(self):
        return self.recovery.schedule_resume_pending()
```

判据（B 做到位的信号）：
- 协作对象**不伸手进别人的私有**（`session_store._entries` 之类 → 改走 session_store 公有 API，或该逻辑归 session_store 自己）
- 每个协作对象能**脱离 GatewayRunner 单独构造 + 单测**
- GatewayRunner 组合方法面回落到「薄协调器」量级（远 <80）
- 顺带清掉搬进来的 silent-swallow（P6 already 在抓）

### C. 注意：同一手法用在了别处

codex 对 Slack/Feishu/QQBot/base 也用了「保留原类做聚合面 + 行为拆 owner mixin」。
channel 适配器是**可接受债、不在本次范围**（别动），但这说明 mixin-重组是 codex 的**系统性手法**，
不是 P5 孤例 —— B 的组合原则若被采纳为标准，需在验收时对照确认它没在 `hermes_agent/` 生产路径上重演。

---

## 决策（user 2026-07-09 已拍板）

**要求真组合（B）。mixin 重组被否决为 P5 终态。** GatewayRunner 必须拆成它
**委托**的独立协作对象，不是它**继承**的 mixin。理由：user 的验收标准是
「不打补丁、不短期止血、按设计高质量落地、该移除的移除」——
一个生产实例化的 216 方法 mixin-重组 god-object 不满足此标准。

因此 A（组合面门禁）从「现状信号」升级为 **P5 阻断项**：`verdict.py` 的 P5
在 `p5:no_mixin_recomposed_gateway_monolith` 红时不得判绿。

## 一句话给验收

**机器绿 ≠ god-object 消除。** GatewayRunner 现在是「行数达标、文件整洁、运行时仍是 216 方法单体」。
按 B 做成真组合才算 P5 完成 —— 不能让 god-object 在门禁看不见的地方以 mixin 形态活着。
