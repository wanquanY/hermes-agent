# Doxie Runtime 同步快照 - 2026-06-16

## 结论

当前暂存代码继续强化 Hermes 作为 Doxie agent engine 的本地运行时边界，不是对上游 Desktop、Dashboard OAuth、API server session controls 或 React chat surface 的全量吸收。

后续同步上游时，应把本轮变更按以下六组能力核对，而不是按单个文件逐项猜测：

1. Doxie Gateway contract 和 extension method ownership。
2. Agent profile / team registry 的原生状态。
3. Team Mission conversation、workspace、graph、runtime history 和 recovery。
4. Run/event/message 的结构化 payload 和终态归并。
5. Conversation render snapshot、activity、storage stats 等只读客户端查询。
6. Profile-scoped runtime proxy 和 prompt attachment 入口。

## 本轮 staged 代码快照

### 1. Doxie Gateway contract 升级

- `doxie_extension/manifest.py` 的 contract/version 进入 `2026-06-15`。
- required methods 扩展到 `conversation.activity.list`、`conversation.render_snapshot`、`team_mission.conversation.render`、`profile.*`、`team_registry.*`、`storage.stats` 等客户端主路径。
- `doxie_extension/gateway_methods.py` 明确 Doxie method modules 和 overrides，确保 extracted gateway methods 被 Doxie extension 边界接管，而不是散落在 upstream core 默认注册里。

同步检查重点：

- `tui_gateway/core/method_registration.py` 仍必须加载 extension overrides。
- `gateway.capabilities` 必须能反映 manifest 中的 Doxie required methods。
- `prompt.submit`、`session.*`、`run.*`、`team_mission.*`、`profile.*`、`team_registry.*` 不能回退到 upstream 默认行为。

### 2. 原生 profile / team registry 状态

- `hermes_state.py` schema version 提升到 `20`，并新增 agent profile 与 team registry 相关表、索引和迁移。
- `hermes_state_agent_profiles.py` 承载 profile/draft 的创建、查询、归档和列表逻辑。
- `hermes_state_team_registry.py` 承载 team 与 member registry，作为 Team Mission 成员和 leader profile 的事实源。
- `hermes_agent_profile_growth.py` 提供 profile growth summary，用于 Doxie profile 侧的成长统计。
- `tui_gateway/methods/profile_registry.py` 和 `tui_gateway/methods/team_registry.py` 暴露 Doxie-facing RPC。

同步检查重点：

- 不要把 profile/team registry 降级成 Doxie 客户端本地状态。
- Team Mission 负责人解析必须继续从 Hermes state 的 team registry/profile registry 读取。
- profile draft 与 active profile 的生命周期要通过 Hermes RPC 管理。

### 3. Team Mission conversation / workspace / recovery

- `hermes_team_leader_runtime_context.py` 从 team/mission/conversation state 推导 leader runtime context，避免依赖 Doxie 每次传完整 profile runtime details。
- `hermes_team_mission_conversation_projection.py` 和 `tests/test_team_mission_conversation_mirror.py` 覆盖 Team Mission conversation 与普通会话投影一致性。
- `tui_gateway/services/team_mission_workspace.py` 统一解析 Team Mission workspace context。
- `tui_gateway/services/team_mission_conversation_recovery.py` 支持 conversation active run 恢复。
- `hermes_team_mission_artifact_refs.py` 与 `workspace_artifacts` 路径把 artifacts 归属回 mission/node。

同步检查重点：

- DoXie 不应通过客户端自建 store 维护 Team Mission graph/history/workspace 的平行事实源。
- conversation route、stable session、mission id、node run binding 必须能在 Hermes state 中互相解析。
- workspace/artifact enrichment 要从 Hermes Gateway 服务读取，不从客户端猜路径或扫描文件。

### 4. Run/event/message payload 归并

- `hermes_runtime_event_payloads.py` 抽出 terminal text 和 hash metadata，避免 run terminal event 的可展示文本被错误覆盖。
- `hermes_state_runs.py` 继续承担 run registry、run events、terminal status、message projection 的事实源。
- `agent/chat_completion_helpers.py`、`agent/doxie_diagnostics.py` 和 streaming 测试覆盖 Doxie 诊断与流式边界。
- `tui_gateway/services/run_control.py`、`runtime_proxy.py`、`ws.py` 继续维护 run id / turn id / runtime scope / stored session id 的一致性。

同步检查重点：

- terminal event 不能被 control-only event 或无 deliverable 的失败事件覆盖。
- `runtime_scope_key`、`run_id`、`turn_id`、`stored_session_id`、`runtime_session_id` 必须贯穿 run events、messages 和 WS event。
- Doxie diagnostics 必须保持无锁 stderr 输出，不能引入 logging lock 死锁风险。

### 5. 只读客户端查询

- `tui_gateway/methods/conversation_activity.py` 提供会话活动列表。
- `tui_gateway/methods/conversation_render_snapshot.py` 提供 `2026-06-16` schema 的 conversation render snapshot，并复用于 `team_mission.conversation.render`。
- `tui_gateway/services/storage_stats.py` 和 `storage.stats` 提供 profile/workspace/storage 统计。
- `tui_gateway/services/session_store.py`、`gateway_store.py`、workspace/artifact services 继续避免只读查询创建空 DB 或假状态。

同步检查重点：

- 只读查询不得为了展示而创建空 profile state。
- render snapshot 是客户端渲染合同，不应回退成让 DoXie 重放原始 messages/run_events 自行拼装。
- storage stats 不应越过 profile-aware Hermes home。

### 6. Runtime proxy 与 prompt attachments

- `runtime.ensure` 仍由 Doxie extension、system methods、runtime proxy 共同维护。
- 暂存代码收敛 worker 环境变量边界：worker 启动环境保留 `HERMES_HOME`、`DOXIE_HERMES_RUNTIME_SCOPE_KEY`、`DOXIE_AGENT_PROFILE_ID`，不再把 profile version id 当作必需环境变量。
- `tui_gateway/services/prompt_attachments.py` 统一 prompt submit 附件字段解析，Doxie document parse enrichment 继续在 extension 层完成。
- `agent/auxiliary_client.py`、`agent/title_generator.py` 等辅助模型路径继续接受 Doxie runtime credentials/profile context。

同步检查重点：

- scoped control RPC 需要代理到 runtime worker 的必须继续代理，不能误跑在 control plane。
- prompt attachment 只做归一化和 enrichment，不应把 Doxie 文档解析逻辑扩散回 Hermes core。
- 子进程 runtime 的 profile identity 应以 scope/profile id 为主，不依赖易漂移的 profile version env。

## 本轮不吸收的上游方向

- 不用上游完整 Dashboard OAuth / WS ticket 替换 Doxie sidecar token 与 runtime proxy。
- 不用上游 API server session controls 替换 Doxie `run_control` / `prompt.submit` / `runtime.ensure` 主链路。
- 不把 Team Mission 回退成 Kanban SQLite 投影或客户端自建 graph store。
- 不把 Doxie document parse、display transcript sanitization、desktop browser bridge 从 extension 边界拆回 core。

## 最小验证包

提交前至少执行：

```sh
git diff --cached --check
python -m py_compile \
  hermes_state.py \
  hermes_state_agent_profiles.py \
  hermes_state_team_registry.py \
  hermes_team_leader_runtime_context.py \
  hermes_runtime_event_payloads.py \
  tui_gateway/methods/profile_registry.py \
  tui_gateway/methods/team_registry.py \
  tui_gateway/methods/conversation_activity.py \
  tui_gateway/methods/conversation_render_snapshot.py \
  tui_gateway/services/storage_stats.py \
  tui_gateway/services/team_mission_workspace.py \
  tui_gateway/services/team_mission_conversation_recovery.py
```

建议的 pytest 目标：

```sh
python -m pytest -q \
  tests/test_doxie_gateway_contract.py \
  tests/test_hermes_agent_profile_registry.py \
  tests/test_hermes_team_registry.py \
  tests/test_hermes_team_leader_runtime_context.py \
  tests/test_team_mission_conversation_mirror.py \
  tests/tui_gateway/test_storage_stats.py \
  tests/tui_gateway/test_session_store.py \
  tests/tui_gateway/test_ws_dispatch.py
```
