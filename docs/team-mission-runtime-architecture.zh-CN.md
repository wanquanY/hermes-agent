# Hermes Team Mission 原生运行时详细设计与实现方案

## 1. 结论

Team Mission 必须成为 Hermes 原生运行模型。Hermes 是团队任务的执行事实源，DoXie Desktop 是客户端，Kanban 是 Hermes 内部调度器。

最终链路必须是：

```text
DoXie UI -> Hermes Team Mission API -> Hermes Team Mission Runtime
DoXie UI <- Hermes Mission Event Stream <- Leader/Worker Runtime Events
```

不能继续使用：

```text
Hermes Kanban DB -> DoXie 轮询/投影 -> DoXie 自建任务图/历史
```

## 2. 当前问题根因

### 2.0 现状边界

当前 Hermes 并没有原生四种 Team Mission 模式。Hermes 现有的团队/多 agent 基础能力是 Kanban 多 worker 任务图机制：

1. board。
2. task。
3. parent/child dependency。
4. assignee profile。
5. dispatcher。
6. worker lifecycle。
7. `kanban_show/create/comment/block/complete` 工具。

当前 DoXie UI 中的四种模式已经存在于产品层，但它们不是 Hermes runtime 的原生 strategy。它们目前主要通过 DoXie `hermes-kanban-adapter` 转换为 root task 初始状态、approval gate task 和 Leader prompt contract。

因此本文中的四种模式策略是目标机制，不是现状描述。实现时必须把这些策略落到 Hermes runtime 代码中，不能继续只靠 DoXie prompt 包装。

### 2.1 黑盒执行

当前 DoXie Team Mission 主要依赖 Kanban task projection。Leader 规划和 worker 执行期间，DoXie 没有订阅 mission 级运行事件，只能等待 Kanban 状态变化后刷新任务图。因此用户看到的是页面长时间无反应，然后突然出现节点。

### 2.2 节点缺少过程

节点过程没有作为 Hermes mission/node 维度的一等历史保存。worker runtime events、工具调用、artifact 归属没有统一挂到 mission node 上，所以前端只能展示任务结束摘要或 Kanban result。

### 2.3 存储职责混乱

Kanban task events 被迫承担任务生命周期、调度状态、执行过程、UI 投影触发等多种职责。高频 runtime stream 和低频任务状态混在一起，会导致性能、可恢复性和数据库可靠性问题。

### 2.4 错误修复方向

在 DoXie 桌面端复制 `runs/run_events/messages/tool_calls/artifacts` 是错误方向。这样会形成两个事实源，后续必然产生双写、同步、迁移、冲突和历史不一致。

正确方向是：Hermes 原生实现 Team Mission runtime/history/stream，DoXie 只消费 Hermes API。

## 3. 设计目标

1. Leader 规划过程实时流式输出到主会话。
2. Leader 创建节点和边时，任务图实时更新。
3. worker 执行过程实时输出到对应节点。
4. 工具调用参数、过程、结果、错误和 artifact 全部可查询。
5. 历史记录不按 token chunk 存储，必须复用普通模式的 delta coalescing。
6. Kanban 数据库损坏不能导致 mission 历史丢失。
7. DoXie 不维护 Team Mission 的平行运行事实源。
8. Hermes 提供统一 mission API 和 mission event stream。

## 4. 非目标

1. 不在 DoXie 新建 Team Mission runtime history store。
2. 不让 DoXie 读取 Kanban SQLite 文件。
3. 不从聊天文本反推任务图。
4. 不把 Kanban task events 当规范执行历史。
5. 不允许 worker 直接写 Kanban SQLite。
6. 不为了兼容当前 projection 路径牺牲长期架构。

## 5. 总体架构

```text
Hermes Team Mission Runtime
├── Mission Store
│   ├── team_mission_conversations
│   ├── team_missions
│   ├── team_mission_nodes
│   ├── team_mission_edges
│   ├── team_mission_run_bindings
│   └── team_mission_artifacts
├── Ordinary Runtime Store
│   ├── sessions
│   ├── messages
│   ├── runs
│   └── run_events
├── Mission Event Publisher
├── Leader Planner
├── Worker Binder
└── Kanban Scheduler Adapter
```

关键原则：

1. `team_mission_conversations` 保存长期团队会话和稳定 leader session，是团队会话路由的事实源。
2. `team_missions` 保存一次团队任务的 graph 和归属绑定，并通过 `conversation_id` 挂到长期 conversation。
3. runtime events 继续写 Hermes 普通 `run_events`。
4. canonical messages 继续写 Hermes 普通 `messages`。
5. `team_mission_run_bindings` 是 mission/node 和普通 run/session 的桥梁。
6. mission event stream 通过 binding 聚合普通 run events。
7. `team_mission_nodes` 不复制 run/session runtime 身份；所有 graph/read-model 输出必须从 `team_mission_run_bindings` join 最新绑定，并把 `run_id`、`stored_session_id`、`runtime_session_id`、`runtime_scope_key`、`runtime_binding` 投影到对应 node。

## 6. 数据模型

### 6.1 `team_mission_conversations`

保存长期团队会话。它是 DoXie `/assistant/hermes/conversations/:conversationId` 路由、左侧主对话、右侧多任务画布的 canonical identity。

字段：

1. `conversation_id TEXT PRIMARY KEY`
2. `team_id TEXT`
3. `stable_session_id TEXT NOT NULL UNIQUE`
4. `title TEXT NOT NULL`
5. `objective TEXT`
6. `workspace_id TEXT`
7. `workspace_path TEXT`
8. `status TEXT NOT NULL`
9. `active_mission_id TEXT`
10. `created_by_user_id TEXT`
11. `metadata_json TEXT`
12. `created_at REAL NOT NULL`
13. `updated_at REAL NOT NULL`

约束：

1. 同一个 `stable_session_id` 只能对应一个 conversation。
2. 同一个 conversation 可以连续承载多次 mission/task，`active_mission_id` 指向当前打开或最近更新的 mission。
3. DoXie 本地不能维护 `team_conversations` 作为会话表；只能把本表返回的 canonical conversation 用于路由、侧边栏和任务图状态 enrichment。

Gateway：

1. `team_mission.conversation.ensure`：创建/更新 canonical conversation，并保证 stable session 存在；它只建立团队会话容器，不创建 `team_missions` 记录，不启动 root node。
2. `team_mission.conversation.resolve`：按 conversation id、mission id 或 stable session 解析 conversation + active mission + graph；当 conversation 还没有 active mission 时，返回空 mission/graph。
3. `team_mission.conversation.list`：按 team/workspace/status 列出 conversation。
4. `team_mission.conversation.rename`：重命名 canonical conversation，并同步 stable session 标题；不得改写 active mission 的任务标题。
5. `team_mission.conversation.delete`：删除 canonical conversation、关联 mission graph 和 stable session。

Control-plane 边界：

1. `team_mission.conversation.ensure` 和 `team_mission.message.submit` 是 canonical conversation 写入口，必须在 Hermes control-plane 执行，不能按 Leader `runtime_scope_key` 整个代理到 scoped runtime worker。
2. Leader 对话的实际模型执行隔离发生在下层 `run.submit`/`session.resume` runtime lease：`message.submit` 先在 control-plane 建立 conversation、workspace binding、run registry，再把 Leader 执行容器按 `team:*:leader-conversation` scope 恢复或复用。
3. 所有以 team stable session 写入的 transcript、run event、terminal state 和 conversation status projection 必须按 stable session 选择 control-plane DB；当前线程处于 Leader profile context 时也不能写入 profile DB。
4. Leader owner runtime 校验只用于拒绝已经处在冲突 scoped worker 内的请求；control-plane 入口没有 `DOVIE_HERMES_RUNTIME_SCOPE_KEY` 是合法状态，不能被解释为未进入 owner scope。owner scope 必须作为下层 `run.submit.runtime_scope_key` 传递并由 runtime lease 执行。

未发布阶段不做团队会话历史数据兼容。`team_mission.conversation.ensure` 只负责 canonical conversation 的创建/更新，不扫描旧 mission run events、不回填旧最终交付消息，也不返回 backfill 结果。缺失的历史团队会话数据应删除后重新测试。

### 6.2 `team_missions`

保存一次团队任务的核心信息。一个团队会话可以包含多条 mission；普通寒暄、澄清、状态查询只写入 conversation transcript，不应创建 mission。只有 Leader 在 `team_mission.message.submit` 中明确调用 `team_mission_start_task`，或客户端调用显式任务创建入口时，才在当前 conversation 下创建新的 mission。

字段：

1. `mission_id TEXT PRIMARY KEY`
2. `conversation_id TEXT`
3. `team_id TEXT`
4. `title TEXT NOT NULL`
5. `objective TEXT`
6. `workspace_id TEXT`
7. `workspace_path TEXT`
8. `mode TEXT NOT NULL`
9. `status TEXT NOT NULL`
10. `leader_session_id TEXT`
11. `created_at REAL NOT NULL`
12. `updated_at REAL NOT NULL`
13. `completed_at REAL`
14. `metadata_json TEXT`

状态集合：

1. `draft`
2. `planning`
3. `waiting_approval`
4. `running`
5. `partially_blocked`
6. `blocked`
7. `verifying`
8. `completed`
9. `failed`
10. `cancelled`

### 6.3 `team_mission_nodes`

保存任务图节点。

字段：

1. `node_id TEXT PRIMARY KEY`
2. `mission_id TEXT NOT NULL`
3. `kind TEXT NOT NULL`
4. `title TEXT NOT NULL`
5. `objective TEXT`
6. `status TEXT NOT NULL`
7. `assignee_profile_id TEXT`
8. `assignee_profile_version_id TEXT`
9. `runtime_scope_key TEXT`
10. `output_contract_json TEXT`
11. `metadata_json TEXT`
12. `position_x REAL`
13. `position_y REAL`
14. `created_at REAL`
15. `updated_at REAL`

节点类型必须是 Hermes 运行时生命周期语义，不承载专业分工标签。持久化层只允许 canonical kind：

1. `root`
2. `worker`
3. `discussion`
4. `verifier`
5. `synthesis`
6. `approval_gate`

`research`、`analysis`、`implementation`、`testing`、`verification`、`quality` 等专业分工只能作为 `metadata.work_type`、`output_contract.focus` 或 capability 线索保存，写库前必须归一化为 `worker`。`synthesizer`、`summary` 只能作为客户端或旧数据别名，进入 Hermes 运行时后必须归一化为 `synthesis`。`verification` 不等价于 `verifier`：前者是工作类型，后者是 Hermes 控制节点。

负责人规则：

1. `assignee_member_id` 必须引用当前 mission metadata 中存在且未禁用的 team member。
2. `assignee_profile_id` 必须能解析到某个 team member，或明确表示外部 profile ownership。
3. `root`、`approval_gate`、`verifier`、`synthesis` 属于 Leader-owned control nodes；如果调用方没有显式给出有效负责人，Hermes 必须默认分配给 Leader。
4. planning tool 遇到不存在的 `assignee_member_id` 必须拒绝本次 mutation，不能把 run id、node id、profile id 或任意字符串持久化为 member id。
5. 数据库内部 upsert 遇到历史或内部调用带来的无效显式 member id 时，必须丢弃该无效显式值并按默认负责人规则解析，保证 graph 中不出现不可解析负责人。

### 6.4 `team_mission_edges`

保存任务图依赖。

字段：

1. `edge_id TEXT PRIMARY KEY`
2. `mission_id TEXT NOT NULL`
3. `from_node_id TEXT NOT NULL`
4. `to_node_id TEXT NOT NULL`
5. `kind TEXT NOT NULL`
6. `metadata_json TEXT`
7. `created_at REAL`

### 6.5 `team_mission_run_bindings`

绑定 Hermes 普通 run 和 mission node。

字段：

1. `run_id TEXT PRIMARY KEY`
2. `mission_id TEXT NOT NULL`
3. `node_id TEXT`
4. `session_id TEXT NOT NULL`
5. `runtime_session_id TEXT`
6. `runtime_scope_key TEXT`
7. `role TEXT NOT NULL`
8. `metadata_json TEXT`
9. `created_at REAL`
10. `updated_at REAL`

这是最关键的表。它保证 Team Mission 不复制普通运行存储，而是通过绑定关系复用 `runs/run_events/messages`。

读取契约：

1. `team_mission_run_bindings` 是节点 runtime 身份的唯一权威来源。
2. `team_mission_nodes` 只保存任务图结构、负责人和当前业务状态，不新增 `runtime_session_id` / `runtime_stable_session_id` 等重复列。
3. `team_mission.graph`、conversation graph、单节点读取和 conversation summary 必须把同一 node 的最新 binding 投影到 node 顶层字段，前端不得再从 metadata、route shell 或 event fallback 拼接节点 runtime 身份。

### 6.6 `team_mission_artifacts`

保存 artifact 的 mission/node 归属。

字段：

1. `artifact_id TEXT PRIMARY KEY`
2. `mission_id TEXT NOT NULL`
3. `node_id TEXT`
4. `run_id TEXT`
5. `tool_call_id TEXT`
6. `kind TEXT NOT NULL`
7. `title TEXT NOT NULL`
8. `uri TEXT NOT NULL`
9. `mime_type TEXT`
10. `metadata_json TEXT`
11. `created_at REAL`

### 6.7 `team_mission_memory_items`

保存 Team Conversation Memory 的结构化记忆项。它是同一个 Team Mission conversation 中多轮任务连续协作的事实源，不能由 DoXie 从聊天文本临时拼接。

字段：

1. `id TEXT PRIMARY KEY`
2. `team_id TEXT NOT NULL`
3. `mission_id TEXT NOT NULL`
4. `conversation_session_id TEXT NOT NULL`
5. `task_id TEXT`
6. `scope TEXT NOT NULL`
7. `kind TEXT NOT NULL`
8. `content TEXT NOT NULL`
9. `structured_payload_json TEXT`
10. `source_node_ids_json TEXT`
11. `source_run_ids_json TEXT`
12. `artifact_refs_json TEXT`
13. `workspace_refs_json TEXT`
14. `confidence REAL NOT NULL`
15. `visibility TEXT NOT NULL`
16. `status TEXT NOT NULL`
17. `created_at REAL NOT NULL`
18. `updated_at REAL NOT NULL`
19. `invalidated_at REAL`

约束：

1. `scope` 至少支持 `mission_task`、`conversation`、`team`、`member`、`workspace`。
2. `kind` 至少支持 `summary`、`decision`、`artifact`、`risk`、`preference`、`constraint`、`open_question`、`reusable_result`。
3. `visibility` 至少支持 `team`、`leader_only`、`member_private`。
4. `status` 至少支持 `draft`、`committed`、`invalidated`、`deleted`。
5. 每条可注入记忆必须有来源 node/run/artifact 之一；没有来源的自由文本不能进入 Memory Pack。

### 6.7 `team_mission_memory_edges`

保存记忆之间的关系，支持失效、修正、引用和提升为长期团队记忆。

字段：

1. `id TEXT PRIMARY KEY`
2. `from_memory_id TEXT NOT NULL`
3. `to_memory_id TEXT NOT NULL`
4. `relation TEXT NOT NULL`
5. `metadata_json TEXT`
6. `created_at REAL NOT NULL`

关系类型：

1. `supports`
2. `supersedes`
3. `invalidates`
4. `derived_from`
5. `promoted_to_team_memory`
6. `referenced_by_task`

核心索引：

1. `(conversation_session_id, status, updated_at DESC)`
2. `(team_id, scope, kind, updated_at DESC)`
3. `(mission_id, task_id)`
4. artifact refs 反向索引
5. 可替换的 embedding/vector index

## 7. 普通表复用规则

### 7.1 `runs`

Leader 和 worker 都是普通 Hermes run。创建 run 时必须带：

1. `run_id`
2. `session_id`
3. `runtime_scope_key`
4. `metadata_json.mission_id`
5. `metadata_json.node_id`
6. `metadata_json.team_role`

### 7.2 `run_events`

所有 runtime event 继续写普通 `run_events`。Team Mission 通过 `team_mission_run_bindings` 查询。

禁止新增一张 mission 专用 chunk event 表。

### 7.3 `messages`

规范历史继续保存在普通 `messages`：

1. assistant content 保存完整合并后的内容。
2. reasoning 保存到现有 reasoning 字段。
3. tool call 保存到现有 tool 字段和 tool role message。
4. metadata 中记录 mission/node/run 归属。

## 8. 事件协议

### 8.1 Mission 结构化事件

Hermes 必须发布这些 mission 级事件：

```json
{
  "type": "mission.node.created",
  "mission_id": "mission-xxx",
  "node_id": "node-xxx",
  "seq": 12,
  "timestamp": 1780000000.0,
  "payload": {
    "node": {
      "node_id": "node-xxx",
      "kind": "worker",
      "title": "调研最新 AI 趋势",
      "status": "ready"
    }
  }
}
```

事件类型：

1. `mission.created`
2. `mission.plan.started`
3. `mission.node.created`
4. `mission.node.updated`
5. `mission.edge.created`
6. `mission.approval.requested`
7. `mission.plan.completed`
8. `mission.run.bound`
9. `mission.artifact.created`
10. `mission.status.changed`
11. `mission.completed`
12. `mission.failed`

### 8.2 Runtime 标准事件

Leader 和 worker 的输出仍然使用普通 runtime event：

1. `message.start`
2. `message.delta`
3. `reasoning.delta`
4. `thinking.delta`
5. `tool.start`
6. `tool.progress`
7. `tool.complete`
8. `message.complete`
9. `error`

每个事件必须能解析：

1. `mission_id`
2. `node_id`
3. `run_id`
4. `session_id`
5. `runtime_scope_key`

文本流 ABI：

1. `message.delta` 只表示 append-only 增量。当前协议只允许 `mode=append` 或省略 `mode`；`payload.delta` 是本帧新增 suffix，`payload.text` 只能作为同值展示兼容字段，不能表达累计全文。
2. `message.delta mode=snapshot` 不属于当前 Team Conversation / Team Mission live ABI。需要修正已输出文本时，不得通过 snapshot delta 修正。
3. 当底层模型 adapter 发出累计文本但该累计文本不能通过追加变成当前已显示文本时，Gateway 必须丢弃该 unsafe frame，不能把公共前缀后的差异伪装成 append delta。
4. `message.complete` 是同一 `run_id/turn_id/message_id` 的终态边界，必须携带 `payload.text` 作为 authoritative final content；如果 live stream 与 final response 不一致，差异通过 `message.complete.final_text_mismatch=true` 暴露。
5. DoXie 侧展示只应把 `message.delta` 当追加流，把 `message.complete.text` 当最终收敛文本；任务图、左侧会话和历史 hydration 不得各自定义第二套文本合并语义。

### 8.3 delta 合并规则

Team Mission 不实现自己的 chunk 合并。所有 `message.delta`、`reasoning.delta`、`thinking.delta` 继续走 `SessionDBRunMixin.append_run_event`。

这意味着：

1. 连续同 identity delta 更新同一行。
2. 工具事件、状态事件、终止事件保留边界。
3. canonical message 在 `message.complete` 后进入 `messages`。
4. mission events 只是查询视图，不是第二份历史。
5. 合并后的 `message.delta` 仍必须保持 append 语义：`payload.delta` 是从上一条已投递文本到当前文本的真实新增 suffix，不得把累计文本或 complete 文本包装成 delta。

## 9. 四种团队模式

Team Mission 必须原生支持四种团队模式。模式不是 DoXie 前端文案，也不是 prompt 里的临时描述，而是 Hermes mission runtime 的执行策略。

本章描述的是目标产品效果和目标 Hermes strategy。它的实现依据来自：

1. DoXie 已经暴露的四个 mode。
2. 当前 `hermes-kanban-adapter` 中的 mode prompt contract。
3. Hermes 已有 Kanban 编排能力。
4. 普通 Hermes runtime stream/history 存储能力。

它不是说 Hermes 当前已经具备这些 mode strategy。

四种模式共享同一套底层能力：

1. mission graph。
2. leader/worker run binding。
3. mission event stream。
4. 普通 `runs/run_events/messages` 历史。
5. artifact 归属。

差异体现在规划、审批、调度、节点启动和最终收敛策略。

### 9.1 `discussion`

讨论协作模式。

用户效果：

1. 用户提出一个问题或主题后，多个成员以各自身份给出观点。
2. 页面不是展示“执行任务进度”，而是展示多成员发言、分歧、共识和汇总。
3. 默认不改文件、不执行外部副作用动作。
4. 最终输出是讨论纪要、共识、分歧、建议和待确认问题。

模式需求：

1. 多个团队成员围绕问题给观点、判断、风险和建议。
2. 默认不执行写入型任务。
3. 默认不要求形成完整 worker/verifier/synthesizer 执行链。
4. 最终由 Leader 或 synthesizer 汇总观点。

Hermes 行为：

1. 创建 root discussion node。
2. 为参与成员创建 discussion worker node。
3. worker node 可以并行启动。
4. 工具调用默认限制为低风险读取型工具。
5. 不自动执行 destructive/write/external action。
6. 可选 synthesizer node 汇总观点。

事件要求：

1. `mission.node.created` 实时创建成员讨论节点。
2. 每个成员的输出以普通 `message.delta` 流式进入 node。
3. 汇总完成时发布 `mission.completed`。

状态收敛：

1. 所有 discussion worker 完成后进入 synthesis。
2. synthesis 完成后 mission 为 `completed`。

### 9.2 `supervised_mission`

监督执行模式。

用户效果：

1. 用户发起复杂任务后，Leader 先实时说明理解、拆解和计划。
2. 任务图在规划过程中逐步出现，而不是规划结束后突然出现。
3. worker 执行前，用户能看到完整计划并点击批准。
4. 批准前 worker 不应实际执行。
5. 批准后节点开始执行，用户能看到每个节点的过程、工具和产物。

模式需求：

1. Leader 先规划真实任务图。
2. 用户批准后再启动 worker 执行。
3. 高风险动作必须等待用户确认。

Hermes 行为：

1. 创建 root planning node。
2. Leader 流式输出规划过程。
3. Leader 创建 `worker`、`verifier`、`synthesis` nodes 和 edges。
4. Hermes 创建 approval gate node。
5. `mission.plan.completed` 后 mission 进入 `waiting_approval`。
6. 用户调用 `team_mission.approve` 后，Hermes 释放 approval gate 的下游节点。

事件要求：

1. 规划文字必须通过 `message.delta` 进入主会话。
2. 节点和边必须通过 `mission.node.created`、`mission.edge.created` 实时进入任务图。
3. approval gate 必须通过 `mission.approval.requested` 显示。

状态收敛：

1. approval 前 worker 不启动。
2. approval 后按依赖启动 worker。
3. `verifier` 通过后进入 `synthesis`。
4. synthesis 完成后 mission 为 `completed`。

### 9.3 `autonomous_mission`

自主执行模式。

用户效果：

1. 用户发起任务后，Leader 仍然实时规划并展示任务图。
2. 低风险节点不等待用户整图批准，规划完成后自动执行。
3. 用户看到团队持续推进，而不是每一步都等确认。
4. 只有高风险、越权、外部副作用或业务不确定节点才请求用户确认。
5. 局部阻塞不影响其他可执行节点继续推进。

模式需求：

1. Leader 规划后自动推进低风险任务。
2. 不需要用户批准整个任务图。
3. 遇到高风险动作、越权目录、外部副作用或业务不确定性时局部阻塞。

Hermes 行为：

1. 创建 root planning node。
2. Leader 实时创建任务图。
3. `mission.plan.completed` 后，低风险 ready nodes 自动启动。
4. 高风险节点进入 `blocked` 或 `waiting_approval`。
5. 用户只批准局部风险节点，不批准整个 mission。

事件要求：

1. 自动启动节点发布 `mission.run.bound`。
2. 高风险阻塞发布 `mission.approval.requested`，payload 必须包含风险原因、请求动作和影响范围。
3. 每个自动执行节点仍然完整流式输出。

状态收敛：

1. 所有非阻塞节点自动推进。
2. 局部阻塞不应让整个 mission 黑盒停滞。
3. 所有执行链完成后进入 verifier/synthesizer。

### 9.4 `manual_graph`

手动任务图模式。

用户效果：

1. 用户进入后看到可编辑任务图画布。
2. 用户自己添加节点、设置依赖、指定成员、定义交付要求。
3. Hermes 不擅自拆任务，不自动扩展任务图。
4. 用户提交或启动节点后，Hermes 按用户图执行。
5. 每个节点仍然有流式过程、工具详情和产物。

模式需求：

1. 用户或上游系统明确提供任务图。
2. Hermes 不自行扩展任务图。
3. Hermes 只负责按图执行、依赖调度、状态更新和历史保存。

Hermes 行为：

1. 接收 nodes/edges。
2. 校验图结构。
3. 创建 mission graph。
4. 不启动 Leader 自主规划。
5. ready nodes 可按用户动作或策略启动。

事件要求：

1. 接收到的每个 node/edge 仍发布 mission 结构化事件。
2. 用户修改图时发布 `mission.node.updated` 或 `mission.edge.created`。
3. worker 输出仍走普通 runtime events。

状态收敛：

1. Hermes 严格按用户提供依赖执行。
2. 不自动新增节点，除非用户显式调用 graph mutation API。
3. 所有终端节点完成后 mission 为 `completed`。

### 9.5 模式差异表

| 模式 | 是否 Leader 规划 | 是否整图审批 | 是否自动启动 worker | 是否允许 Hermes 改图 | 主要用途 |
| --- | --- | --- | --- | --- | --- |
| `discussion` | 轻量规划 | 否 | 是 | 可创建讨论节点 | 多成员观点讨论 |
| `supervised_mission` | 是 | 是 | 批准后启动 | 是 | 高可控复杂执行 |
| `autonomous_mission` | 是 | 否，局部风险审批 | 是 | 是 | 低风险自主推进 |
| `manual_graph` | 否 | 按节点策略 | 用户或策略控制 | 默认否 | 用户指定任务图 |

### 9.6 模式不是前端分支

DoXie 不应该在前端自行解释四种模式。DoXie 只把 mode 传给 Hermes，并根据 Hermes mission events 渲染结果。

Hermes 必须负责：

1. 根据 mode 决定是否创建 Leader planning run。
2. 根据 mode 决定是否创建 approval gate。
3. 根据 mode 决定 worker 自动启动策略。
4. 根据 mode 决定风险审批粒度。
5. 根据 mode 决定是否允许自动 graph mutation。

### 9.7 模式策略接口

Hermes 需要新增 `TeamMissionModeStrategy` 抽象。每个 mode 都必须通过 strategy 处理 mission 生命周期，而不是散落在 gateway、prompt、DoXie 或 Kanban adapter 中。

建议接口：

```python
class TeamMissionModeStrategy:
    mode: str

    def initialize_graph(self, mission, members) -> GraphPatch:
        ...

    def should_start_leader(self, mission) -> bool:
        ...

    def on_plan_event(self, mission, event) -> StrategyActions:
        ...

    def on_plan_completed(self, mission) -> StrategyActions:
        ...

    def on_user_approval(self, mission, approval) -> StrategyActions:
        ...

    def select_ready_nodes(self, mission, graph) -> list[str]:
        ...

    def can_mutate_graph(self, mission, actor) -> bool:
        ...

    def risk_policy(self, mission, node, action) -> RiskDecision:
        ...

    def reduce_status(self, mission, graph, runs) -> str:
        ...
```

`StrategyActions` 可以包含：

1. 创建节点。
2. 创建边。
3. 更新节点状态。
4. 创建 approval request。
5. 启动 run。
6. 取消 run。
7. 发布 mission event。
8. 标记 mission 完成或失败。

### 9.8 四种模式状态机

`discussion` 状态机：

```text
draft
-> planning
-> running
-> synthesizing
-> completed
```

规则：

1. 没有整图 approval。
2. discussion worker 可以并行。
3. 默认不进入 verifying，除非用户显式要求可执行交付。
4. 所有成员发言完成后进入 synthesis。

`supervised_mission` 状态机：

```text
draft
-> planning
-> waiting_approval
-> running
-> verifying
-> synthesizing
-> completed
```

规则：

1. planning 结束前不启动 worker。
2. planning 完成后必须进入 `waiting_approval`。
3. 用户批准后进入 `running`。
4. worker 完成后进入 verifier。
5. verifier 通过后进入 synthesis。

`autonomous_mission` 状态机：

```text
draft
-> planning
-> running
-> partially_blocked
-> running
-> verifying
-> synthesizing
-> completed
```

规则：

1. planning 完成后自动启动低风险 ready nodes。
2. 高风险节点进入局部 approval，不阻塞全图。
3. 如果全部剩余节点都阻塞，mission 才进入 `blocked`。
4. 用户批准局部风险后恢复对应节点。

`manual_graph` 状态机：

```text
draft
-> ready
-> running
-> verifying
-> completed
```

规则：

1. 初始 graph 来自用户或上游 payload。
2. 不启动自动 Leader planning。
3. 用户可以继续编辑 graph。
4. 启动执行后 Hermes 按依赖调度。

### 9.9 风险和审批策略

风险判断必须在 Hermes strategy 层完成，不能靠 DoXie UI 猜。

风险维度：

1. 是否写文件。
2. 是否跨 workspace。
3. 是否执行 shell 命令。
4. 是否访问外部网络。
5. 是否调用付费或外部系统。
6. 是否删除、覆盖、提交、发布、发送消息。
7. 是否缺少业务输入。

审批差异：

1. `discussion`：默认禁止高风险动作，必要时请求用户把讨论升级为执行任务。
2. `supervised_mission`：整图 approval 是必需，高风险动作仍可二次审批。
3. `autonomous_mission`：无整图 approval，但高风险动作局部审批。
4. `manual_graph`：按节点策略审批，用户可以把节点设置为手动启动或自动启动。

### 9.10 任务图变更权限

不同模式下 graph mutation 权限不同：

1. `discussion`：Hermes 可以创建讨论节点和汇总节点，不应创建执行节点。
2. `supervised_mission`：Leader planning 阶段可以创建完整执行图；approval 后默认不允许大幅改图，除非发出计划变更审批。
3. `autonomous_mission`：Leader 可以根据执行反馈追加节点，但必须发布 `mission.node.created` 并说明原因；高风险追加节点需要 approval。
4. `manual_graph`：只有用户或明确授权的外部调用可以改图，Hermes 不自动改图。

## 10. Hermes API 设计

### 10.1 `team_mission.create`

输入：

```json
{
  "team_id": "team-xxx",
  "title": "最近 AI 的发展方向是什么",
  "objective": "最近 AI 的发展方向是什么",
  "workspace": {
    "id": "workspace-default",
    "path": "/path/to/workspace"
  },
  "mode": "supervised_mission",
  "leader_profile_id": "agent-default",
  "members": []
}
```

输出：

```json
{
  "mission": {},
  "graph": {
    "nodes": [],
    "edges": []
  }
}
```

行为：

1. 创建 `team_missions`。
2. 创建 root planning node。
3. 创建 leader session/run。
4. 写入 `team_mission_run_bindings`。
5. 发布 `mission.created` 和 `mission.run.bound`。

模式要求：

1. `discussion`：创建讨论 root 和成员 discussion nodes。
2. `supervised_mission`：创建 root planning node 和 Leader run。
3. `autonomous_mission`：创建 root planning node 和 Leader run，规划完成后自动推进低风险节点。
4. `manual_graph`：要求 payload 提供 nodes/edges，不启动自主 Leader planning。

### 10.2 `team_mission.graph`

输入：

```json
{ "mission_id": "mission-xxx" }
```

输出：

```json
{
  "mission": {},
  "nodes": [
    {
      "node_id": "node-worker",
      "run_id": "run-worker",
      "stored_session_id": "team:mission-xxx:node:node-worker",
      "runtime_session_id": "runtime-worker",
      "runtime_scope_key": "profile:worker",
      "runtime_binding": {}
    }
  ],
  "edges": [],
  "run_bindings": []
}
```

### 10.3 `team_mission.events`

输入：

```json
{
  "mission_id": "mission-xxx",
  "after_seq": 0,
  "limit": 500
}
```

输出：

```json
{
  "events": [],
  "next_seq": 120,
  "has_more": false
}
```

事件来源：

1. mission 结构化事件。
2. 通过 `team_mission_run_bindings` 查询到的 `run_events`。

### 10.4 `team_mission.subscribe`

订阅 mission event stream。DoXie 只使用这个 stream 渲染 Team Mission。

要求：

1. 支持断线后按 seq 恢复。
2. 支持 mission 范围过滤。
3. 支持 node 范围过滤。
4. 支持运行中事件实时推送。
5. 支持历史事件补发。

### 10.5 `team_mission.approve`

批准监督执行计划。

行为：

1. 更新 approval node 状态。
2. 释放依赖。
3. 创建或启动 worker run。
4. 发布 `mission.node.updated`、`mission.run.bound`。

### 10.6 `team_mission.cancel`

取消 mission。

行为：

1. 标记 mission 为 `cancelled`。
2. 取消 active run。
3. 通知 Kanban scheduler 停止内部任务。
4. 保留所有已产生历史。

### 10.7 Team Conversation Memory API

Hermes 原生提供团队会话记忆 API。DoXie 只能通过这些 API 展示、禁用、删除和解释记忆引用，不能自己构造 Team Mission prompt memory。

#### `team_mission.memory.compile`

为一个 mission/task 编译结构化记忆。

输入：

```json
{
  "mission_id": "mission-1",
  "task_id": "task-1",
  "mode": "final|partial|blocked|canceled"
}
```

输出：

```json
{
  "mission_id": "mission-1",
  "task_id": "task-1",
  "memory_item_ids": ["mem-1", "mem-2"]
}
```

行为：

1. 从 `team_mission_run_bindings` 找到相关 runs。
2. 读取 run events、messages、node metadata、artifact refs。
3. 生成 `summary`、`decision`、`artifact`、`risk`、`constraint`、`open_question`、`reusable_result` 等 memory items。
4. 写入 `team_mission_memory_items`。
5. 发布 `mission.memory.compiled`。

#### `team_mission.memory.pack`

为新任务生成 Leader Memory Pack。

输入：

```json
{
  "mission_id": "mission-1",
  "objective": "new task objective",
  "workspace_id": "workspace-1",
  "limit": 8
}
```

输出：

```json
{
  "mission_id": "mission-1",
  "memory_pack": {
    "items": [],
    "conflicts": [],
    "artifact_refs": []
  }
}
```

行为：

1. 硬过滤同一 `conversation_session_id`、同 team、同 workspace、同 task lineage。
2. 语义召回相关 memory items。
3. 排除 `invalidated`、`deleted` 和权限不可见的 items。
4. 按相关性、时间、置信度、来源完整性和 kind 重排。
5. 压缩为可注入 Leader prompt 的 Memory Pack。
6. 写入 `referenced_by_task` 记忆边，保留审计。

#### `team_mission.memory.slice`

为 worker/verifier/synthesizer 节点生成 Memory Slice。

输入：

```json
{
  "mission_id": "mission-1",
  "node_id": "node-worker",
  "objective": "node objective",
  "limit": 5
}
```

输出：

```json
{
  "mission_id": "mission-1",
  "node_id": "node-worker",
  "memory_slice": {
    "items": [],
    "artifact_refs": []
  }
}
```

约束：

1. Worker 不接收完整团队会话历史。
2. Worker 只接收与节点 objective、依赖输出、artifact refs、风险和验收标准相关的记忆。
3. `leader_only` 和其他成员私有记忆不进入普通 worker slice。

#### `team_mission.memory.list/update/delete/events`

用于 DoXie 展示和用户管理：

1. `team_mission.memory.list`：按 mission、conversation、team、kind、status 查询。
2. `team_mission.memory.update`：固定、编辑摘要、标记过期、修改 visibility。
3. `team_mission.memory.delete`：软删除并保留审计。
4. `team_mission.memory.events`：订阅 memory compile、reference、invalidate、delete 事件。

## 11. Leader Planning 流程

1. DoXie 调用 `team_mission.create`。
2. Hermes 创建 mission 和 root node。
3. Hermes 为当前 objective 构建 Leader Memory Pack。
4. Hermes 启动 Leader run，并把 Memory Pack 作为结构化背景注入 prompt。
5. Leader 文本输出通过 `message.delta` 实时进入 `run_events`。
6. Leader 调用 mission graph API 创建节点和边。
7. 每次创建节点或边都发布 mission 结构化事件。
8. supervised 模式创建 approval gate。
9. Leader 完成规划后发布 `mission.plan.completed`。
10. DoXie 主会话显示 Leader 全部流式文本。
11. DoXie 任务图根据 mission events 实时增长。

Memory Pack 注入边界：

1. 当前用户 objective 优先级最高。
2. Team Conversation Memory 仅作为背景、可复用产物和风险提示。
3. 如果历史记忆与当前 objective 冲突，Leader 必须指出冲突，并以当前 objective 为准。
4. Memory Pack 不包含完整聊天流水。
5. 每次注入都记录 memory item ids 和引用原因。

## 12. Worker 执行流程

1. mission 进入执行状态。
2. Hermes 根据节点依赖启动 ready worker。
3. Hermes 为节点 objective 构建 Memory Slice。
4. 每个 worker run 创建后写入 `team_mission_run_bindings`。
5. worker 输出通过普通 runtime event 写入 `run_events`。
6. 工具调用通过普通 `tool.start/tool.complete` 事件进入 stream。
7. artifact 产生时写 `team_mission_artifacts` 并发布 `mission.artifact.created`。
8. worker `message.complete` 后更新 node 状态。
9. verifier/synthesizer 按依赖继续执行。
10. 当前轮完成后触发 `team_mission.memory.compile`。

Worker Memory Slice 边界：

1. 只包含与节点目标相关的历史记忆。
2. 必须包含依赖输出、相关 artifact refs、约束、验收标准。
3. 不包含其他成员完整对话历史。
4. 不包含权限不可见或已失效记忆。
5. 如果 memory slice 与节点目标冲突，worker 必须上报冲突，不得自行采用历史结论覆盖当前目标。

## 13. Kanban 降级方案

Kanban 保留为 Hermes 内部 scheduler，不再作为 DoXie UI 事实源。

允许 Kanban 做：

1. 依赖调度。
2. ready/blocking 状态管理。
3. worker 分派兼容。
4. retry/cancel 协调。

禁止 Kanban 做：

1. 保存 token stream。
2. 保存规范历史。
3. 作为 DoXie 任务图主数据源。
4. 暴露 SQLite 路径给 worker。
5. 让 DoXie 轮询 Kanban DB 还原过程。

## 14. DoXie 对接方案

DoXie 只需要做客户端：

1. 调 `team_mission.create` 创建任务。
2. 调 `team_mission.subscribe` 订阅事件。
3. 用 mission events 渲染任务图。
4. 用 runtime events 渲染主会话和节点输出。
5. 调 `team_mission.approve/cancel/retry` 发送用户操作。

DoXie 不应该：

1. 自建 mission runtime store。
2. 从 Kanban DB 读任务状态。
3. 从聊天文本解析任务图。
4. 双写 Hermes 历史。

## 15. 测试矩阵

### 15.1 存储测试

1. 创建 mission。
2. 创建 node/edge。
3. 绑定 run。
4. 查询 graph。
5. 查询 mission events。
6. 删除 mission 后级联删除 graph 绑定。

### 15.2 coalescing 测试

1. 连续两个 `message.delta` 合并成一条 run event。
2. `tool.start` 打断 delta 合并。
3. 工具后新的 `message.delta` 开新段。
4. mission event 查询能读到合并后的事件。

### 15.3 Leader 流式规划测试

1. Leader 输出文字实时进入 mission stream。
2. Leader 创建 node 立即发布 `mission.node.created`。
3. Leader 创建 edge 立即发布 `mission.edge.created`。
4. supervised 模式产生 approval gate。

### 15.4 Worker 执行测试

1. worker run 绑定 node。
2. worker 输出进入对应 node stream。
3. tool call 参数和结果可查。
4. artifact 归属 mission/node。
5. node 状态随 run 完成更新。

### 15.5 故障隔离测试

1. Kanban DB malformed 时，mission graph/history 仍可读。
2. active run 取消后历史保留。
3. gateway 断线重连后按 seq 恢复。
4. 重复事件不会重复渲染。

### 15.6 四种模式测试

1. `discussion` 创建讨论节点，不创建整图 approval gate。
2. `supervised_mission` 规划完成后进入 `waiting_approval`，approval 前 worker 不启动。
3. `autonomous_mission` 规划完成后自动启动低风险 ready worker，高风险节点局部阻塞。
4. `manual_graph` 使用输入 nodes/edges，不自动启动 Leader 改图。
5. 四种模式都必须复用普通 `run_events/messages`，不能产生平行历史存储。

### 15.7 Team Conversation Memory 测试

1. 第一轮任务完成后生成 committed memory items。
2. 第二轮任务启动前生成 Leader Memory Pack，且当前 objective 使用第二轮任务目标。
3. Memory Pack 包含相关 artifact refs 和前序决策。
4. invalidated/deleted memory 不进入 pack。
5. Worker 只收到相关 Memory Slice，不收到完整会话历史。
6. 用户禁用团队记忆时，Leader run 不注入 Memory Pack。
7. 每次 memory 注入都能追踪到 memory item ids 和 source node/run/artifact。

## 16. 实现顺序

### 第一阶段：Hermes Mission Store

已实现基础状态层：

1. `team_missions`
2. `team_mission_nodes`
3. `team_mission_edges`
4. `team_mission_run_bindings`
5. `team_mission_artifacts`
6. `SessionDBTeamMissionMixin`
7. mission run event 复用普通 `append_run_event`
8. `initialize_team_mission_from_strategy` 作为 Hermes 原生模式初始化入口

### 第二阶段：四种模式策略层

已实现基础策略层：

1. `discussion` mode strategy。
2. `supervised_mission` mode strategy。
3. `autonomous_mission` mode strategy。
4. `manual_graph` mode strategy。
5. `TeamMissionModeStrategy` 统一表达初始图、整图审批、自动启动、改图权限和风险策略。
6. mode strategy 单元测试。

### 第三阶段：Gateway API

已实现 JSON-RPC API：

1. `team_mission.create`
2. `team_mission.graph`
3. `team_mission.events`
4. `team_mission.subscribe`
5. mission 级实时订阅 fan-out。
6. 订阅层对合并后的 stream delta 做差量投递，保证历史合并存储和实时增量显示不冲突。

### 第四阶段：Leader Planner 工具/API

已实现基础 Gateway API 和 Leader planner tools：

1. `team_mission.node.create`
2. `team_mission.edge.create`
3. `team_mission.node.update`
4. `team_mission.plan.complete`
5. graph mutation 写入 Hermes Team Mission graph。
6. graph mutation 通过绑定的 Leader run 追加 `mission.*` 事件。
7. `tools/team_mission_planning_tools.py` 作为 loader adapter，`hermes_team_mission/tools/planning.py` 暴露内部 `team_mission_planning` toolset：
   - `team_mission_node_create`
   - `team_mission_edge_create`
   - `team_mission_plan_complete`
8. `team_mission_leader` 是 Team Mission Leader 的基础工具集，不随 phase 收窄；稳定 Leader conversation 和任意绑定到 Leader/root node 的运行都应具备 Leader 工具能力。
9. Leader 基础工具包含 `team_mission_status` 和 `team_mission_team_profile`；规划/改图阶段在此基础上额外叠加 `team_mission_planning`。
10. Leader 规划启动 prompt 不能内联团队成员画像、能力标签、profile id 或 member id 清单；成员能力必须通过 `team_mission_team_profile` 工具读取。
11. planner tools 通过当前 run binding 校验 actor、node kind、phase 和 mode strategy；只有绑定到 mission root/Leader planning run 的上下文可以改图。
12. planner tools 创建的 worker node 会继承当前 task context，避免跨任务污染。

待实现：

1. plan change approval。
2. planner prompt 与 tool contract 继续收敛为更严格的 schema 和验收文案。

### 第五阶段：Worker Binder

已实现基础 Gateway API：

1. `team_mission.node.start`
2. `team_mission.node.bind_run`
3. worker 节点使用独立 stored session。
4. `team_mission.node.start` 复用现有 `run.submit` 和 runtime lease，不直接创建 agent。
5. run binding 写入 `team_mission_run_bindings`。
6. worker runtime event 通过 run binding 归属到 mission/node。
7. 节点启动成功后写入 `mission.node.started`。
8. bound worker 的 `message.complete` / `error` 经过普通 `run_control.record_event` 后自动归约 node status。
9. dependency reducer：上游完成后下游节点从 `blocked_waiting_dependency` 推进到 `ready`。
10. `team_mission.graph.reduce`
11. `team_mission.schedule.ready`
12. `team_mission.schedule.ready` 复用 `team_mission.node.start`，不直接创建 runtime。
13. 执行类模式工作节点全部完成后自动创建 verifier。
14. verifier 完成后自动创建 synthesis。
15. `team_mission.schedule.ready` 可启动自动创建的 verifier/synthesis 节点。

待实现：

1. 更完整的并发调度策略和重试策略。

### 第五阶段后置：Team Conversation Memory

已实现：

1. `team_mission_memory_items`
2. `team_mission_memory_edges`
3. `team_mission.memory.compile`
4. `team_mission.memory.pack`
5. `team_mission.memory.slice`
6. Leader planning prompt 注入 Memory Pack。
7. Worker prompt 注入 Memory Slice。
8. memory reference/invalidation events。
9. `team_mission.message.submit` 为稳定 Leader conversation 构建产品上下文，并在 Leader 显式调用 `team_mission_start_task` 时创建新 mission。
10. `tools/team_mission_leader_tools.py` 作为 loader adapter，`hermes_team_mission/tools/leader.py` 暴露内部 `team_mission_leader` toolset：
    - `team_mission_status`
    - `team_mission_team_profile`
    - `team_mission_start_task`
11. `team_mission_start_task` 只在稳定 Leader conversation 中用于把用户消息提升为新 mission；`team_mission_status` 和 `team_mission_team_profile` 同时支持稳定 Leader conversation 和绑定到 mission node 的 Leader run。
12. capability snapshot 可通过 `team_capability.snapshot.*` API 绑定到 conversation/mission，Leader 和 worker 可读取经过快照固定的团队能力。

要求：

1. 必须先修正连续任务启动时的 objective 优先级，确保新任务目标不会被上一轮 mission objective 覆盖。
2. Memory Pack 必须可禁用、可审计、可恢复。
3. DoXie 不实现平行 memory fact store。

### 第六阶段：DoXie 切换

实现：

1. DoXie 创建 mission 改调 Hermes API。
2. DoXie Team Mission 页面订阅 Hermes mission stream。
3. DoXie Kanban projection 降为 legacy fallback。

### 2026-06-10 当前落地快照

本批 staged 代码已经把 Team Mission 从“Kanban projection + DoXie prompt contract”推进为 Hermes 原生 runtime 的第一版：

1. State 层：
   - `hermes_team_mission/state/session_mixin.py`
   - `hermes_team_mission/state/session_*.py`
   - `hermes_state_team_capabilities.py`
   - `hermes_state.py` schema version `16`
   - mission graph、conversation、run binding、artifact、memory、capability snapshot 均进入 Hermes state.db。
2. Gateway 层：
   - `tui_gateway/methods/team_mission.py` 兼容加载入口
   - `hermes_team_mission/gateway/*.py`
   - `team_mission.conversation.*`
   - `team_mission.message.submit`
   - `team_mission.node.*`
   - `team_mission.edge.create`
   - `team_mission.plan.complete`
   - `team_mission.schedule.ready`
   - `team_mission.memory.*`
   - `team_capability.snapshot.*`
3. Runtime/event 层：
   - `run_control` 支持 mission subscription。
   - mission subscription 会对已合并存储的 stream delta 做实时差量投递。
   - `TeamMissionReadyScheduler` 通过 `run_control.register_team_mission_ready_scheduler()` 接入 terminal event 后的 ready-node 调度。
   - `kanban_runtime_events.py` 将 Kanban worker lifecycle 投影为 runtime event，而不是让 DoXie 读取 Kanban SQLite。
4. Tool 层：
   - `team_mission_leader` toolset 是 Leader 基础工具集，给稳定 Team Mission Leader conversation 和任意绑定到 Leader/root node 的 Team Mission run 使用。
   - `team_mission_planning` toolset 是 internal toolset，只在允许规划/改图的 Leader run 上叠加使用。
   - 普通 worker 和 DoXie 对话不会获得 planner graph mutation tools。
5. DoXie contract：
   - `dovie_extension/manifest.py` contract/version 更新到 `2026-06-10`。
   - Dovie gateway manifest 暴露 Team Mission、Team Capability、message metadata merge 等 required methods。
   - `team_mission` methods 作为 Dovie extension override 注册，仍保持 Dovie client -> Hermes Gateway 的单一事实源。

### 2026-06-11 prodv0.9.3 当前落地快照

本批 staged 代码继续补齐 Team Mission 生产运行边界，重点是节点历史、profile tool、节点类型规范、负责人校验和控制事件不会污染普通运行状态：

1. Node runtime history：
   - 新增 `tui_gateway/methods/team_mission_history.py`。
   - 新增 `tui_gateway/services/team_mission_runtime_history.py`。
   - `team_mission.node.history` 按 mission/node/session 解析 run binding，返回节点关联的 transcript messages、runtime events、tool events、artifacts 和 run binding 摘要。
   - 该方法加入 Dovie extension required methods 和 runtime proxy control allowlist，Dovie 右侧节点详情不需要扫描普通 session 或 Kanban SQLite。
2. Node kind 和 assignee 规范：
   - 新增 `hermes_team_mission_node_kinds.py`，将 `research`、`analysis`、`implementation`、`testing`、`verification` 等专业分工统一归一化为 `worker`，并把原始语义保存到 metadata。
   - `synthesizer` / `summary` 等旧别名统一归一化为 `synthesis`。
   - `root`、`approval_gate`、`verifier`、`synthesis` 是 Leader-owned control nodes；无有效负责人时默认归属 Leader。
   - 无效 `assignee_member_id` 不允许进入 graph。planning tools 遇到不存在 member 必须拒绝，内部 upsert 遇到历史无效值必须丢弃并按默认负责人规则解析。
3. Team profile tools：
   - 新增 `hermes_team_mission/tools/profile.py`，`tools/team_mission_profile_tools.py` 只保留 loader adapter。
   - `team_mission_team_profile` 读取并压缩 capability snapshot，Leader 不再通过 prompt 内联团队成员画像、profile id 或 member id 清单。
   - `team_mission_leader` 是 Leader 基础 toolset；`team_mission_planning` 只在 planning / change-request 阶段叠加。
4. Active run 修复：
   - `hermes_state_runs.py` 区分 runtime lifecycle events 和 mission control events。
   - `mission.*` 控制事件仍写入 `run_events` 供 Team Mission replay，但不会单独打开或保持 ordinary run active，避免 Leader conversation 因 control-only event 显示 busy。
   - 新增 `repair_control_only_active_runs()` 兜底修复历史脏 active rows。
5. Toolset scope 持久化：
   - `gateway_session_toolsets` 记录 session 级 enabled / disabled toolset overrides。
   - `resolve_session_toolsets()` 在 agent rebuild 时恢复非 exact 的 toolset scope，避免 runtime 重建后丢失 Leader conversation 的 Team Mission 工具面。
   - `exact` scope 仍只作为本次控制平面的权限边界，不持久化为会话默认工具面。
6. Scheduler 稳定性：
   - `TeamMissionReadyScheduler` 启动节点失败时会把节点标记为 `blocked` 并记录 `start_error`，不会让 scheduler 异常吞掉 ready graph 状态。
   - scheduler 统一使用 canonical node kind 判断 approval gate / verifier / synthesis。

### 2026-06-13 当前落地快照

本批 staged 代码继续补齐 Dovie 侧真实运行问题的可诊断性、终止一致性和 Team Mission Leader 会话归一化：

1. Dovie runtime diagnostics：
   - 新增 `agent/dovie_diagnostics.py`，使用 `os.write(2, ...)` 输出诊断，避免依赖 Python logging 锁。
   - conversation loop、system prompt restore/build、pre-LLM hook、memory prefetch、API kwargs build、streaming API call、chat-completions client/create 等关键阶段会输出 `[dovie-turn-stage]` / `[dovie-stream-stage]`。
   - TUI gateway agent build 会输出 `[dovie-agent-build-stage]`，便于定位 Dovie runtime 卡在 profile context、system prompt、client create 还是 provider request。
2. Active run terminalization：
   - gateway `_emit()` 在 terminal event 写入 run event 前释放对应 runtime session active run。
   - sidecar parent watchdog 在父进程消失前调用 `_shutdown_sessions()`，尽力给 active run 写入 interrupted terminal event。
   - `_finalize_session()` 会进入对应 profile context，terminalize active run，再提交 memory / end session，避免 Dovie 侧刷新后看到长期 running。
3. Team Mission Leader conversation context：
   - `_agent_context_options_for_session()` 为 `team_leader` runtime 禁用 context files、memory 和 soul identity，避免普通 profile 规则污染 Leader 路由判断。
   - `team_mission.message.submit` / Leader run 创建路径写入稳定 Team Mission product context，工具读取上下文不再依赖前端临时 prompt 拼接。
   - stable Leader conversation 的 `team_mission_leader` 工具面由 session/toolset scope 恢复，不因 runtime rebuild 丢失。
4. Team Mission conversation normalization：
   - `normalize_team_mission_conversation_session()` 会从 session metadata / Dovie product context 识别 Leader conversation，自动确保 `team_mission_conversations` 记录并把 session source 归一化为 `team_mission`。
   - `team_mission_conversation_history_sql()` 只把有 active mission、历史消息或 active run 的 conversation 视为可路由历史，避免空 Leader 容器污染 session list。
   - `is_team_mission_run_session()` 和 `team_mission_run_session_ids()` 用 run bindings 标记内部 mission node runtime sessions，session list 默认隐藏这些内部执行 session。
5. Tool handoff：
   - 新增 `agent/tool_handoff.py`，支持工具执行路径把 structured handoff 转为最终 assistant 响应。
   - Team Mission Leader tools 可以把任务启动、状态查询等确定性结果以 handoff 形式返回，减少二次模型 follow-up 造成的延迟和卡 running 风险。
6. Runtime proxy / pool 稳定性：
   - runtime proxy 只透传真正属于 runtime worker 的 Team Mission live control 方法；canonical conversation 写入口必须留在 control-plane。
   - runtime pool 相关测试覆盖 Dovie session / runtime lifecycle，防止 worker 进程和 active run 残留。

## 17. 验收标准

1. 用户发送 supervised mission 后，主会话立即出现 Leader 流式输出。
2. 任务图在规划过程中逐步出现节点和边。
3. approval gate 实时出现并可批准。
4. worker 节点展示完整过程、工具、结果和 artifact。
5. 页面刷新后从 Hermes 恢复完整 mission。
6. 连续 5000 个 delta 不会产生 5000 条规范历史。
7. Kanban DB 损坏不影响 mission graph/history 查询。
8. DoXie 没有 Team Mission 平行 runtime fact store。
9. 所有 Team Mission 历史都能通过 Hermes API 查询。
10. 四种团队模式都由 Hermes mode strategy 驱动，而不是 DoXie 前端分支驱动。
11. 同一团队会话第二轮任务能引用第一轮结构化记忆，但当前 objective 仍以第二轮任务为准。
12. Worker 只能获得相关 Memory Slice，不能被完整历史上下文污染。
13. 用户删除或标记过期的团队记忆不会进入后续 Memory Pack。
