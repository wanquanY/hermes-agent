# 阶段 9 验收：Codex / Responses 路由、压缩、推理与验证事实源

## 阶段结论

阶段 9 已完成 `U7`、`CX1`、`CX2`、`CX4` 的手工吸收。Responses route、Codex
app-server compaction、reasoning/content 投影与 verification aggregate 分别由独立 owner
负责；普通 provider、Codex、subagent 与 Team Mission 共享同一持久证据和完成判断，不在
transport 内复制业务状态。

## 已锁定的行为合同

- `ResponsesRoutePolicy` 是 replay 与最终 preflight 的唯一策略：Copilot 永不 replay
  connection-scoped assistant message id；跨 issuer encrypted reasoning 不回放；middleware
  或 override 重新注入的危险字段仍在 dispatch 前剥离。
- Codex app-server 的真实 thread 只由 `thread/compact/start` 压缩；`native|hermes|off`
  明确区分自动 owner，usage、event、lease 与 verdict 各提交一次，本地 transcript 不被
  provider summary 重写。
- Responses commentary/analysis 与 app-server reasoning delta 只进入 reasoning channel；
  `final_answer` 才进入 assistant content，实时流与最终 transcript 不重复。
- `VerificationService` 以 conversation/workspace/edit-generation 持久化 changed paths 与
  terminal evidence。未验证代码回答在流式边界被暂存，只允许一次 typed 内部补救；纯文档、
  无 verify contract 和 `auto` 下的人类消息 surface 不触发完成守卫。
- `project.facts` 与 `verification.status` 是 Gateway 只读投影；Doxie 不需要读取 provider
  私有 hook 或推断工具文本。
- provider/model picker 的 models.dev 查询改为运行期 cache-only，显式 setup/refresh 才允许
  访问网络，避免 model switch/fallback 被 15 秒 catalog 请求卡住。

## 自动验收证据

- Phase 9 Codex、Responses、compression、verification、Gateway、worker 聚合：
  766 passed、0 failed。
- 核心 stream/verification 定向与静态复核：231 passed、0 failed；`ruff check` 与
  `git diff --check` 通过。
- 运行期 models.dev cache-only 修正：112 passed、0 failed；覆盖 Codex picker、
  OpenCode、provider fallback 与 catalog capability。
- 第一次标准全量 `scripts/run_tests.sh` 共发现 1581 files / 29,245 tests，在并行机器负载与
  外部网络不可用时暴露 12 个失败：provider catalog 的隐式网络读取已在本阶段从根因修复；
  async delegation 隔离复跑通过；剩余 MCP OAuth discovery 阻塞属于阶段 10 `RT1` 的明确
  生命周期范围，将在阶段 10 修复后重新运行完整门禁。该红项不被记为通过。

## 最终统一实机验收重点

1. 在 OpenAI Codex、Copilot Responses 和 fallback/model switch 后继续长会话，确认不出现
   旧 item id 导致的持续 401，且 commentary 只显示为思考过程。
2. 让 Codex app-server 长会话触发自动压缩并手动压缩一次，确认只走 native thread、
   transcript 可继续、usage/compaction 事件各一次。
3. 修改代码后先让模型尝试直接结束，确认未验证正文不会闪现；随后运行 test/lint 后只显示
   最终已验证回答，页面不出现伪造系统消息。
4. 查询 `project.facts` 与 `verification.status`，确认普通、Leader、`@member`、subagent
   对同一可见 conversation/workspace 使用同一代次与证据。

## 阶段收益

- Codex/Copilot 长会话不再因 route/连接轮换或跨 issuer reasoning 回放随机永久失败。
- app-server compaction 真正缩小 provider thread，减少额外 LLM 调用和双份摘要漂移。
- reasoning、final content、usage 与 verification 在实时 UI、持久 transcript 和所有执行
  surface 上具有同一语义。
- “完成”由新鲜验证证据支撑；未验证草稿不会先流给用户，失败、中断和预算耗尽仍保留真实
  terminal 语义。

阶段 9 checkpoint 只保留在本地，不 push、不合并。标准全量的 MCP OAuth 红项必须由
阶段 10 关闭，最终 Phase 12 总门禁全绿后再交用户统一实机验收。
