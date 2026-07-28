# P3–P6 门禁缺口清单（给 codex）

**背景**：P2 差点用"SessionDB god-object 整体搬进 `hermes_agent/storage/state_store.py` + 改名 `HermesStateStore`"过关，`grep SessionDB/hermes_state` 被改名+搬家绕过。你事后补了 P2 专用 gate（`state_store_decomposed` / `hermes_state_store_no_methods` / `no_hermes_state_store_production_instantiation`，并注明"改名不算"）。

**问题**：P2 的血教训只补在了 P2。**P3/P4/P5 还留着结构相同的漏洞**，会让同一个"搬家换名 ≠ 拆解"的把戏在下游重演。审计日期 2026-07-08/09。

---

## 已由独立守卫覆盖（你可直接采纳进 verdict.py，无需重写）

`tests/repositories/test_no_relocated_god_object.py`（commit `4f25808f7`）
—— 防搬家换名的通用 god-object 探测器：

- 信号 = **单类方法数 > 80**（行为性，与命名/路径无关）
- 阈值校准：合法最大类 55 方法（feishu adapter / SessionRepoImpl 54）；god-object = HermesStateStore 144、GatewayRunner 209
- 扫 `hermes_agent/` + `channels/` + `hermes_gateway/`（预置，防 `gateway/run.py` 落点）
- 现 xfail（抓 HermesStateStore）；P2 拆完 + P4/P5 无巨物搬入 → xpassed
- **请把它并入 verdict.py 的 P4/P5 段**（如同你采纳 §4.6 `aggregate_table_single_owner`）

---

## 必须你补的 gate（按 phase）

### P3 — Gateway Registry：名字 grep 可改名绕过

现有 gate：
- `no_method_modules` = grep `METHOD_MODULES` → 改名 `HANDLER_MODULES` 就过
- `no_dovie_overrides` = grep `DOVIE_GATEWAY_METHOD_OVERRIDES` → 改名就过

**补法**：加一条**结构 gate**，与名字无关：
- `p3:single_dispatch_registry` —— 生产 dispatch 路径上**只有一个** `MethodRegistry` 实例被 wire；任何"次级 registry / override 表"的注册方法计数 = 0
- 判据不是 grep 常量名，而是：`tui_gateway/server.py` 的 dispatch 确实调 `hermes_agent.gateway.pipeline.dispatch`，且**不存在第二套 method→handler 映射**（无论叫什么名字）
- 兜底已有：`tui_gateway/methods/* 删除` 这条 gate 若真删了，双注册活不下来——但名字 grep 仍应升级成结构判据

### P4 — Runtime / Worker：只查"旧路径无 import" = 搬家可绕

现有 gate 只有：
- `Old worker services have no production imports`
- `Old run-control path has no production imports`

→ 把 `tui_gateway/services/worker_*.py`（几千行）整体搬进 `hermes_agent/orchestration/`，旧路径没人 import 了就过，**巨物换目录苟活**。

**补法**（复制 P2 三件套）：
- `p4:worker_services_decomposed` —— 旧 worker 服务的总行数/方法数收敛到目标（类比 `state_store_decomposed`）
- `p4:no_relocated_worker_monolith` —— 采纳上面的 god-object 守卫（worker god-object 若搬进 blessed 树，方法数超阈值即红）
- `p4:worker_single_owner` —— WorkerPool / 运行时状态单一 owner，无第二套 lease/state 写方

### P5 — Legacy Gateway Retirement：最危险，`gateway/run.py` 18277 行可搬家

现有 gate：
- `gateway/ is gone` ✅（这条对，强制清空）
- `no ^from gateway` → grep 路径，`git mv gateway/run.py → hermes_gateway/run.py` 就过
- **无任何"gateway/run.py 落点不许是巨物"的 gate**

`gateway/run.py` = 18277 行 / GatewayRunner **209 方法**，是第二个 SessionDB。P5 现有 gate 挡不住它整体搬进 `hermes_gateway/` 换名续命。

**补法**：
- `p5:no_relocated_gateway_monolith` —— **直接采纳上面的 god-object 守卫**（GatewayRunner 209 方法搬进 blessed 树立即红）
- `p5:gateway_run_decomposed` —— `gateway/run.py` 的 13 类职责按 `docs/audits/gateway_run_responsibility_map.md` 拆分，落点无 >800 行多职责巨物
- 参考 `docs/audits/gateway_run_responsibility_map.md`（已有的 18277 行责任块拆解建议）

### P6 — Debt Closure：安全网，但有覆盖盲区

现有 `no_silent_swallow_in_v3` 只扫 `hermes_agent/`。
- `channels/` 219 处是既有适配器容错，**属可接受债，不在本次范围，不要强制清零**（会误伤 `except (FileNotFoundError, ...): pass` 合法容错）
- **但** `hermes_gateway/`（若 P5 创建）必须 born clean：把 silent-swallow 扫描范围加上 `hermes_gateway/`，防 `gateway/run.py` 的 silent-swallow 随搬迁溜进新树

---

## 还有一件基础的：verdict.py 只实现了 P0–P2

`scripts/zero_debt/verdict.py --phase P3` 现在直接 fail：
> "only P0, P1, and P2 verdicts are implemented; extend this script before later phase sign-off"

**这是诚实的失败（P3 现在过不了），但必须在做 P3/P4/P5 之前把上面这些 gate 写进 verdict.py**——否则做到那一步无门禁可依，又会重演 P2 那种"先搬完再被驳回"。

---

## 一句话

**把 P2 已验证有效的三件套（行数 gate + 方法数 gate + 改名 gate + 单 owner gate）复制到 P4/P5，并在动 P4/P5 之前先焊好门禁。** 补门禁的成本，现在（还没搬）最低；等搬完再补，等于又要驳回重来一次。
