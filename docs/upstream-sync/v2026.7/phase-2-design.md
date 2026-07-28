# 阶段 2 设计：工具执行合同、批次 deadline 与模型调用计量

## 目标

按当前 Hermes 分层手工吸收 U2、R1、M1，建立从模型工具调用到 registry、执行器、
持久化、replay 和 Gateway 投影的一条 canonical contract；同时让并发工具批次有
wall-clock deadline，并让 Mixture-of-Agents 的每一次 reference/aggregator 调用进入
同一模型 usage owner。

本阶段不吸收 Phase 3/4 的完整出站网络、命令与路径安全治理，也不提前吸收 Phase 12
的 mixed-risk batch segmentation。

## 输入与边界

- 阶段起点：`29e745367`。
- 执行分支：`codex/upstream-absorption-v2026.7-all-phases`。
- U2 参考：`f8361d29c8e2`、`5e50f18b3041`、`a0a6cd80f5c7`、`b9b463f3bd65`。
- R1 参考：`c1784e909`。
- M1 参考：`3bdb23de1`、`aa605b66c`。
- 禁止复制上游 monolith 或把 transport 变成执行语义 owner。

## 缺口核验

1. `ToolRegistry.dispatch()` 目前允许任意 handler 返回值穿过 pipeline；普通 dict、
   bytes、None 或数字会在 logging、plugin hook、budgeting、persistence 中产生不同
   行为。
2. sequential/concurrent executor 把 malformed JSON 和非-object 参数修复为 `{}`，
   可能把模型的无效高风险调用变成无参数真实执行。
3. 并发工具轮询只有 heartbeat 和用户 interrupt，没有 batch deadline；任一不响应
   工具会让 turn 永不结束。
4. interrupted/recovered tool result 没有“确定无副作用”与“副作用未知”的内部事实，
   replay 不能区分可以安全丢弃与必须提示用户检查的调用。
5. MoA 工具的四个 reference 和一个 aggregator 直接调用 OpenRouter client，usage
   没有进入 session totals、cost 与 DB；当前只记录外层模型调用。

## 架构决策

### 1. Registry 是 handler result contract owner

`tools.registry` 只允许两类结果离开 registry：

- 普通 `str`；
- 明确的 multimodal envelope：`{"_multimodal": true, "content": [...]}`。

其他类型统一转换为结构化 `tool_result_contract` 错误。plugin hooks、budget、
persistence 和 transport 永远不再接收未定义 Python 值。

### 2. 参数解析必须 fail-closed

`agent.tool_executor` 提供唯一 `_parse_tool_arguments()`：只有 JSON object 合法；
malformed JSON、scalar、list、空字符串和截断对象都生成一个稳定错误 tool result，
handler、guardrail、checkpoint、callback 和 mutation tracker 调用次数必须为 0。

同一批次中的其他合法调用仍可执行，结果顺序按原始 tool-call 顺序保持。

### 3. Effect 与 output risk 是内部 metadata

`agent.tool_result_classification` 维护 conservative allowlist：已知 read-only 工具在
未完成时标记 `effect_disposition=none`；未知、plugin、MCP 和写工具标记
`effect_disposition=unknown`。该字段持久化并参与 replay cleanup，但 transport 发给
模型前必须剥离。

当前 Hermes 的 canonical message repository 已把可扩展内部事实统一存放在
`metadata_json`，因此本阶段不另加一条与现有 schema 重复的专用列：
`hermes_agent.domain.tool_effect` 负责编解码命名空间键，repository 写入时持久化，
read model 读取时恢复成顶层 `effect_disposition`。这样既保持 provider-facing message
合同，又避免让 agent 层反向依赖 SQLite schema。

外部可控工具输出生成不含原文的 `_tool_output_risk` 元数据；Gateway 只投影 finding
ID、risk 和 redacted 状态。完整 prompt-injection enforcement 留在 Phase 4。

### 4. Batch deadline 与 worker 生命周期只有一个 owner

并发 executor 使用 `future -> original index`，统一 deadline 基于 monotonic clock：

- 默认高于内置最长合法工具 timeout；环境变量可显式调整或关闭；
- deadline 到达时保留已完成结果，对未完成项生成稳定 timeout result；
- cancel 未启动 future，向已运行 worker 广播 interrupt；
- executor `shutdown(wait=False, cancel_futures=True)`，不能在 context-manager `__exit__`
  中重新阻塞；
- result list 仍按原始 tool-call 顺序提交。

### 5. 模型 usage 只有一个 recorder

新增 agent application-level model usage recorder，负责：

- raw provider usage -> `CanonicalUsage`；
- session token/api-call/cost totals；
- real model/provider/base URL pricing；
- session DB 增量持久化；
- 可测试的 per-call attribution（purpose、model、provider）。

普通 conversation、Codex runtime 与 MoA 都必须调用该 owner。MoA handler 从 registry
显式接收 `parent_agent`，四个 reference 与 aggregator 每获得一次 API response 就
各记录一次；retry 产生的真实计费 response 也不能被吞掉。

## 验收要求

1. registry 只放行字符串与合法 multimodal envelope。
2. malformed JSON/scalar/list 不执行 handler，合法 sibling 仍执行且顺序稳定。
3. fast + never-return tool 在 deadline 内返回 fast 结果和 timeout 结果，调用方不被
   executor shutdown 阻塞。
4. `effect_disposition` 在 SQLite round-trip 和 replay cleanup 后保持，任何 transport
   都不把内部字段发给 provider。
5. 高风险外部输出只暴露结构化 finding，不复制原始结果到风险事件。
6. 4 reference + 1 aggregator 产生 5 条可归属 usage，session totals、DB call count 与
   cost 使用每次调用的真实 model。
7. Phase 2 专项、Hermes 全量、静态门禁和 Doxie Gateway contract 全绿。

## 阶段收益

- 模型坏参数不再被“修好”后执行。
- 挂死工具从无限等待变为稳定、有界、可观测的失败。
- 工具 result、effect、risk、persistence 与 replay 共享一个事实合同。
- MoA 的真实调用量和成本完整进入会话计量，避免 advisor/aggregator 漏记。

## 阶段状态

实现完成，正在执行最终全量门禁。自动门禁完成后创建本地 checkpoint 并继续
Phase 3；最终统一用户验收前不 push、不 merge。
