# 阶段 5 验收：Run 身份与会话持久化生命周期

## 结论

阶段 5 自动验收完成，进入阶段 6。R2、U6、RT5、RT6、RT7 已按当前 Hermes
架构手工吸收；RT8 的上游递归 visited-set 方案被当前“删除父会话时脱钩并保留
子会话”架构 supersede。最终用户实机验收与 Phase 1-12 统一进行；在此之前不 push、
不合并回主开发分支。

## 条目决策

| ID | 决策 | 当前架构落点 | 验收结果 |
|---|---|---|---|
| R2 | absorb | `RunIdentity`、`RunRepoImpl.claim_identity`、`WorkerPool`、`RunOrchestrator` | 同身份幂等；跨 session/scope/worker/profile 结构化拒绝且不污染 row、pool、event、seq |
| U6 | absorb | `TurnMessageBuffer`、per-agent `RLock`、CLI close snapshot、统一 flush seam | 五类 close/flush 竞态只持久化一次 clean user turn，多模态和 API-local note 不污染 canonical history |
| RT5 | absorb | `close_interrupted_tool_sequence` 与所有 interrupt finalization 路径 | 中断 tool tail 被关闭，普通失败与非 tool tail 保持不变 |
| RT6 | absorb | provider-bound `sanitize_api_messages` 与 sequence repair | 空 tool_calls、重复 call/result id 被规范化，canonical transcript 不变 |
| RT7 | absorb | persisted resume marker 与空 auto-resume fail-safe | 重启后 freshness 可判定，stale resume 不再向模型发送空 turn |
| RT8 | superseded | session repository/application service 的 child-reference orphaning | 自环、双向环、多父引用和重复删除只删除显式目标，子 session/transcript 保留 |

## 身份与兼容边界

`runs` aggregate 的 immutable launch identity 只有 `RunRepoImpl` 能写；orchestrator 不再
旁路修改 row。旧事件投影可能先创建没有 worker 的 placeholder，因此 worker 首次 claim
前允许从 canonical `RunContext` 校准 session/scope；worker claim 后四个维度全部封存。
这既阻止 recycled run id 串线，又保留普通、leader、`@member` 和旧数据恢复的统一路径。

## 自动验收证据

### Hermes

- 身份、pool、orchestrator、repository、gateway、migration、会话竞态、tool tail、
  sanitization、resume 与 session deletion 专项全部通过。
- Gateway/TUI/HermesState/Team Mission 聚合：6,711 passed，53 skipped。
- 全量：1,562 test files；29,127 discovered；28,962 passed；0 failed；耗时 511.7s；
  worker DB 尾部门禁 13 passed。
- `ruff check .`：通过。
- tracked production Python compile：通过。
- import architecture：158 files、321 dependencies、1 kept contract、0 broken。
- zero-debt/架构聚合：41 passed、1 xfailed。
- `git diff --check`：通过。

### Doxie 联调

- Hermes Gateway ABI：205 methods in sync；98 active methods 被 135 required methods 覆盖。
- Electron Node：391 passed；source Node：19 passed。
- TypeScript build、desktop test suite、ESLint（0 errors）和 frontend boundary：通过。
- `corepack pnpm --filter @dovie/desktop test:gates`：退出码 0。

## 阶段收益

- recycled run id 无法把审批、事件或状态串到错误 worker/profile/session/scope。
- CLI close、后台 flush、interrupt、resume 和多模态输入共享一个可重入持久化边界，
  不再依赖调用先后或可变列表别名。
- provider 兼容清洗不再反向污染 canonical transcript，重试和跨模型切换更稳定。
- 会话删除语义从容易出现环和重复删除的递归 cascade，明确为显式目标删除与子会话保留。

## 最终实机验收重点

1. 普通、leader、`@member` 与非活跃会话各执行 submit → tool → interrupt → resume → close，
   检查可见 transcript、run status、approval 与事件归属一致。
2. 在工具执行中关闭并重启桌面，确认 user turn、多模态输入和 tool tail 不丢失、不重复。
3. 对已有 session 继续对话，确认恢复提示不以 system 文本泄漏到页面，工具卡片仍展示
   真实参数和执行状态。
4. 删除含 child session 的父会话，确认 child session/transcript 保留且父引用被清理。
