# Hermes 上游同步文档索引

本文档是后续同步 `upstream/main` 时的入口。不要再把所有历史、设计、落地快照和核对表继续追加到单个长文档里。

## 文档分层

| 文档 | 职责 | 更新时机 |
|---|---|---|
| `UPSTREAM_SYNC_CHECKLIST.md` | 同步后的快速分诊、红线和最小验证包 | 每次发现新的高风险同步边界时更新 |
| `docs/selective-upstream-client-sync-20260527.md` | 2026-05-27 起 P0/P1 选择性吸收的历史审计 | 只在需要修正历史结论或 P0/P1 核对证据时更新 |
| `docs/team-mission-runtime-architecture.zh-CN.md` | Team Mission 原生运行时的长期架构设计 | 只有架构原则、数据模型或责任边界变化时更新 |
| `docs/upstream-sync/doxie-runtime-snapshot-YYYYMMDD.md` | 某次 staged/发布节点的本地 Doxie runtime 落地快照 | 每次大批量本地变更提交前新增一篇 |

当前最新快照：

- `docs/upstream-sync/doxie-runtime-snapshot-20260622.md`：`session_index` 侧栏索引、Team Mission 轻量列表/幽灵索引清理、state.db incremental vacuum、image attachments。
- `docs/upstream-sync/doxie-runtime-snapshot-20260616.md`：Doxie Gateway contract、profile/team registry、conversation render snapshot、storage stats、Team Mission workspace/recovery。

## 后续同步检查顺序

1. 先读 `UPSTREAM_SYNC_CHECKLIST.md`，按文件变更范围定位风险模块。
2. 再读最新的 `docs/upstream-sync/doxie-runtime-snapshot-*.md`，确认当前本地 contract 和事实源边界。
3. 如果涉及 Team Mission graph/history/stream，再读 `docs/team-mission-runtime-architecture.zh-CN.md`。
4. 如果涉及 2026-05-27 上游 P0/P1 选择性吸收范围，再回查 `docs/selective-upstream-client-sync-20260527.md`。

## 新快照写法

每篇快照只回答四个问题：

1. 本次 staged 代码新增或改变了哪些 Doxie-facing contract。
2. 哪些本地边界必须在后续上游同步时保留。
3. 哪些上游能力仍不应直接吸收，因为会破坏 Doxie 作为客户端、Hermes 作为 agent engine 的分层。
4. 本轮最小验证包是什么。

禁止在快照里复制完整设计文档、完整提交清单或长表格。长设计进入架构文档，逐项上游核对进入历史审计文档，快照只保留当前同步判断所需的事实。
