# 阶段 2 验收记录：工具执行合同、批次 deadline 与统一模型计量

## 状态

**自动验收完成；等待 Phase 1-12 全部完成后的统一用户验收。**

本阶段允许建立本地 checkpoint commit，但最终统一验收前不推送、不合并回主开发
分支。上游提交只作为行为与失败场景参考，没有 merge、rebase 或 cherry-pick。

## 吸收结论

| 账本项 | 决策 | 当前 Hermes 实现 |
|---|---|---|
| R1 | absorb | `agent.tool_executor` 以 monotonic wall-clock deadline 管理并发批次，保留已完成结果且不 join 超时 worker |
| M1 | absorb | `agent.model_usage_recorder` 统一普通对话、Codex 与 MoA 的 token、cost、API call 和 DB 增量 |
| U2 | absorb | registry result、fail-closed 参数、effect、risk、persistence、replay 与 transport 共用一条合同 |
| D1 | equivalent | MoA 保持工具形态，通过 registry 的 `parent_agent` 接入统一计量，不复制上游独立 loop |

## 自动验收证据

### 专项行为

| 行为 | 证据 |
|---|---|
| registry 只放行字符串和合法 multimodal envelope | `tests/tools/test_registry.py`、`tests/test_transform_tool_result_hook.py` |
| malformed JSON/scalar/list/空值不执行 handler，合法 sibling 保持顺序 | `tests/run_agent/test_malformed_tool_arguments.py`、`tests/run_agent/test_run_agent.py` |
| fast + wedged tool 在 deadline 内返回且不等待 executor join | `tests/run_agent/test_concurrent_tool_timeout.py`、interrupt/contextvar 回归 |
| effect 在 SQLite 元数据往返与 replay stub 后保持 | `tests/repositories/test_message_repo_impl.py`、`tests/read_models/test_message_history.py`、`tests/run_agent/test_agent_guardrails.py` |
| effect/risk 不泄漏到 provider，Gateway 风险事件不含原文 | `tests/agent/test_tool_result_contract.py`、transport 与 `tests/tui_gateway/test_tool_events.py` |
| 普通对话、Codex、MoA 共用唯一 usage owner | `tests/agent/test_model_usage_recorder.py`、Codex integration、context token tracking |
| 4 reference + 1 aggregator 产生 5 条真实模型归属与 5 次 DB API 增量 | `tests/tools/test_mixture_of_agents_tool.py` |

阶段 2 合并专项矩阵：17 个文件、271 passed、0 failed。

### 静态与架构门禁

| 门禁 | 结果 |
|---|---|
| `ruff check .` | All checks passed |
| `compileall` | 通过 |
| `lint-imports` | 155 files、313 dependencies；1 contract kept、0 broken |
| zero-debt / silent fallback / async SQLite / ledger | 26 passed、0 failed、1 expected xfail |
| `git diff --check` | 通过 |

### 全量与 Doxie 门禁

Hermes 首轮全量发现一条与新安全合同冲突的旧断言：它要求损坏 JSON 自动变成
`{}` 后执行。生产实现未回退，测试已改为验证 handler 零调用和稳定错误结果；该用例
定向复跑通过。

| 门禁 | 最终结果 |
|---|---|
| Hermes 全量复跑 | 1552 files、28,815 passed、0 failed，714.7s，28 workers |
| Doxie Gateway ABI | 205 methods in sync；98 active methods covered by 135 required methods |
| Doxie Vitest | 583 files；1,970 passed、3 skipped |
| Doxie Electron node | 391 passed、0 failed |
| Doxie source node | 19 passed、0 failed |
| Doxie type/lint/boundaries | 退出码 0；0 lint errors；frontend boundary passed |

Doxie lint 报告的 70 条 warning 为当前工作树既存警告，门禁没有 error，也没有为本阶段
新增忽略或降级规则。阶段 2 机器门禁完成时间：2026-07-16。

## 收益与最终实机验收重点

1. 模型损坏参数不会被 Hermes “修好”后误执行高风险工具。
2. 一个挂死工具不再让整个 turn 无限等待；已完成 sibling 不会丢失。
3. timeout/replay 能区分“确定无副作用”与“可能已经产生副作用”，避免盲目重试。
4. 外部工具输出风险可以结构化观测，事件不复制潜在敏感或攻击性原文。
5. MoA 的真实 5 次调用、token 与成本全部进入会话计量，不再只看到外层一次调用。

统一实机验收时重点测试：损坏工具参数、一个快工具加一个超时工具、超时后的会话
继续使用、外部网页 prompt-injection 风险事件，以及一次 MoA 后会话 usage 增量。
