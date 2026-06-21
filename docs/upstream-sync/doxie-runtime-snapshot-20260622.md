# Doxie Runtime 同步快照 - 2026-06-22

## 结论

当前本地分支在 2026-06-16 快照之后继续收敛 Doxie runtime 的生产边界。本轮暂存代码本身不吸收上游大功能，重点是把已经本地化的 Hermes-as-agent-engine contract 做成可恢复、可索引、可诊断、可长期运行的状态。

本次同步检查应按四组能力核对：

1. `session_index` 是 Doxie 会话侧栏的索引化事实源。
2. Team Mission conversation 删除、列表和运行态投影不能再依赖逐会话重算。
3. `state.db` 必须能控制删除后的文件膨胀。
4. image attachments 必须从 Doxie attachment contract 进入 prompt/runtime，而不是依赖旧的零散 image path 字段。

## 06-16 之后的本地运行时状态

### 1. Session Index 成为侧栏查询边界

06-16 之后的本地提交引入并持续修正 `session_index`：

- `session.index.list` 提供控制面索引查询，侧栏不再必须扫描和富化完整 `sessions/messages/run_events`。
- prompt/run terminal event 会把普通会话和 Team Mission conversation 的运行态投影到 `session_index`。
- Team Mission member-node 内部 sessions、delegate child sessions、terminal mission 的 stale active run 会从侧栏运行态中排除。
- terminal/cancelled/interrupted 状态会清掉 stale `active_run_id`，避免 UI 长期显示 running。

本轮暂存代码继续保护这条边界：

- `tui_gateway/services/runtime_proxy.py` 把 `session.index.list` 注册为 control-plane method，带 scope 的侧栏查询不能被代理到 runtime worker。否则本该毫秒级的主库读取会触发 worker cold start。
- `hermes_team_mission_conversation_state.py` 删除 Team Mission conversation 时同步清理 `session_index`，并能处理正式数据已删、只剩索引行的幽灵会话。

同步检查重点：

- `session.index.list` 必须留在控制面本地读主库。
- `session_index` 是去规范化索引，删除 canonical session/conversation 时必须一起清。
- 上游如果改动 `session.list` 或 runtime proxy dispatch，不能绕过 Doxie 的索引化侧栏路径。

### 2. Team Mission conversation 列表轻量化

本轮暂存代码让 `team_mission.conversation.list` 支持 `lightweight` / `lite`：

- lightweight 列表只返回列表字段和索引化运行态，不逐个 conversation 做 recover active run、runtime projection、mission/node/run binding 富化。
- 打开详情页或画布时再走 `team_mission.conversation.resolve` / `team_mission.conversation.render` / graph/history 相关方法。

同步检查重点：

- 侧栏列表不是画布详情 API，不能重新引入 O(N) graph/runtime 富化。
- 详情页需要的 mission graph、node history、runtime projection 应继续由 dedicated Team Mission RPC 提供。
- 删除 conversation 必须同时清 canonical tables、stable session、run sessions 和 `session_index`。

### 3. State DB 空间回收

本轮暂存代码在 `hermes_state.py` 增加 SQLite 空闲页控制：

- 新建库启用 `PRAGMA auto_vacuum=INCREMENTAL`。
- 启动时按阈值执行 bounded `incremental_vacuum`，把删除 stream delta、Team Mission events 后进入 freelist 的页逐步还给系统。
- 单次回收有上限，避免积压很多时阻塞启动。

背景：Team Mission 流式 delta 和事件裁剪后，`state.db` 可能出现大量 freelist。如果只删除不回收，文件尺寸会继续膨胀，并拖慢会话列表等小查询。

同步检查重点：

- 新库必须在建表前设置 incremental auto-vacuum。
- 既有 `auto_vacuum=NONE` 的库仍需要一次外部 `VACUUM` 切换；代码里的 `incremental_vacuum` 对这类库是 no-op。
- 上游如果改动 `SessionDB.__init__`、WAL 初始化或 schema 初始化顺序，不能把 auto-vacuum 设置挪到建表之后。

### 4. Image Attachments 进入 prompt 合同

本轮暂存代码修正 Doxie image attachments 的进入路径：

- `tui_gateway/methods/prompt.py` 先归一化 submitted attachments，再从 attachments 中提取 image paths。
- `tui_gateway/services/media.py` 使用 `tools.vision_tools.vision_analyze_tool` 做 image prompt enrichment。
- vision 失败时保留可重试 path 和错误摘要，不吞掉用户原始 prompt。
- Team Mission leader conversation submit 保留 image attachment metadata，保证 Doxie 上传截图能进入 leader runtime。

同步检查重点：

- Doxie 的附件事实源是 `attachments`，不是旧的平铺 `image_paths`。
- 图片分析应通过 Hermes vision tool 路由，继承当前 runtime/provider 能力。
- document parse、image enrichment、display transcript sanitization 仍属于 Doxie extension / gateway service 边界，不能散回 core。

## 本轮不吸收的上游方向

- 不用上游完整 session controls 替代 `session_index`、Doxie `run_control` 和 Team Mission conversation projection。
- 不把 Team Mission 运行态回退成每次列表查询扫描 graph/run events。
- 不用客户端本地删除逻辑掩盖 Hermes state 中的 ghost `session_index` 行；必须在 Hermes 删除路径里清理。
- 不用旧 image path 参数绕过 Doxie attachment contract。

## 最小验证包

提交前至少执行：

```sh
git diff --cached --check
python -m py_compile \
  hermes_state.py \
  hermes_team_mission_conversation_state.py \
  tui_gateway/methods/prompt.py \
  tui_gateway/methods/team_mission.py \
  tui_gateway/services/media.py \
  tui_gateway/services/runtime_proxy.py
```

建议的 pytest 目标：

```sh
python -m pytest -q \
  tests/test_hermes_team_mission_gateway.py \
  tests/tui_gateway/test_media.py \
  tests/tui_gateway/test_protocol.py
```
