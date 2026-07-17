# 阶段 13 设计：学习图谱、成长时间线与自我改进成本控制

## 目标与账本范围

阶段 13 手工吸收 `LG1`、`LG2`、`LG3`、`LG4` 与 `BR1`。参考上游快照为
`upstream/main @ a9cc17fd8`，主要来源如下：

- `LG1`：`96552c31e`，profile-scoped learned skill + memory graph。
- `LG2`：`e971dc1e9`，Journey 时间线与 CLI 行为。
- `LG3`：`a0576560e`、`08be8e5ef`、`2fc67a3a5`，共享 detail/edit/delete
  后端与 MemoryStore 原子写入。
- `LG4`：`ec319e4e3`、`ce82b0c3c`、`7e037e1a3`，坏元数据和跨平台时间渲染防护。
- `BR1`：`87c4a5ebb`、`e2fa509bf`、`b5267671f`、`437dcacbb`、
  `2e828d4b7`、`17cfa0f0a`、`8ef006933`、`20871c1d9`、`96bc524a7`、
  `525e1e775`、`56abbaeac`，后台复盘 aux-model 路由、摘要输入、持久化隔离、
  精确先读后写、技能所有权和可恢复归档。

本阶段仍禁止 merge、rebase、cherry-pick 和把上游 React/TUI 状态管理复制进当前运行时。
只吸收领域语义、失败场景与可验证合同。

## 阶段前缺口

1. `profile.growth.summary` 独立扫描 Markdown 行、技能目录和会话文件，成长统计与上游
   Journey 图谱并非同一个 read model。
2. Hermes 没有 profile-scoped Learning Graph RPC；桌面端无法取得 memory/skill 节点、
   关系和可回放时间线。
3. 记忆与技能的查看、编辑、删除没有 Journey 领域入口，容易由消费端直接操作文件。
4. 现有后台复盘固定复用主模型和完整历史；无法显式路由便宜 aux model，也无法在冷模型
   路径用有界摘要降低输入成本。
5. 后台复盘 fork 仍有进程级 stdout 重定向、主会话持久化和 memory-disabled profile
   越权写入风险。

## Owner 与最终设计

### LG1：唯一 Learning Graph read model

`agent.learning_graph.LearningGraphService` 是学习成长领域的唯一事实投影 owner：

- 构造时显式绑定 `hermes_home`，不依赖可变进程环境；profile A/B 并发读取不串目录。
- 数据事实仍由 MemoryStore、profile skill 目录和 skill usage sidecar 持有；图谱只做纯派生，
  不建立第二套业务存储。
- 只把 agent-created 或实际使用过的 profile skill 视为“学会的技能”；bundled/base skill
  不计为成长。
- memory、skill 节点使用 typed canonical id；记忆 id 包含内容指纹和同内容序号，插入或删除
  其他记忆不会把所有后续节点错位。旧 `memory:<source>:<index>` 只在 mutation 入口兼容读取。
- 每个节点携带来源、时间戳来源、创建者、状态和类别；边只允许连接现存节点。

### LG2：Journey 是图谱的时间线投影

- `timeline` 由相同 nodes 按 timestamp 稳定排序生成，不再由另一个扫描器重建。
- CLI `hermes journey`、Gateway RPC 和 Doxie 都消费相同 payload。
- CLI 负责文本/JSON 展示，不持有图谱业务状态；Doxie 继续自行渲染，不搬运上游桌面 UI。

### LG3：统一 mutation seam

`agent.learning_mutations.LearningMutationService` 是 Journey 修改唯一入口：

- memory edit/delete 通过 `MemoryStore.replace_exact/remove_exact` 在 store lock 内做精确、
  原子且带预算校验的改写；空 edit 明确拒绝。
- skill edit 复用 skill manager 校验与缓存失效；delete 使用可恢复 archive，pinned skill 拒绝。
- 所有 mutation 显式绑定 profile home，并用 canonical id 的指纹/序号做 stale 检查。
- mutation RPC 不接受客户端文件路径，消费端不能绕过 profile 边界。

### LG4：成长摘要成为图谱派生投影

- `profile.growth.summary` 的 memory/learned-skill 计数和最近成长事件来自 Learning Graph。
- session 活跃度仍由 session repository 派生，但不冒充学习节点。
- 保留旧字段兼容 Doxie；新增 graph/timeline 统计，不保留第二套 memory/skill 计数规则。

### BR1：后台复盘成本与隔离

- 默认 `auto` 继续使用主模型和完整 warm-cache 历史。
- 只有显式配置不同 provider/model 时才标记 routed，并使用有界 digest；路由失败 fail-soft
  回主模型，不影响前台 turn。
- same-model fork 继承缓存和 reasoning config；routed fork 使用目标 provider 默认 reasoning，
  避免跨 provider 传入非法 effort。
- fork 禁止 session persistence、compression 和外部 memory plugin；memory-disabled profile
  不获得 memory tool。
- stdout/stderr 静默只作用于 review worker 线程，不能吞掉 Gateway 其他线程输出。
- tool result 解析异常只降级单条摘要，不能抹掉已成功的学习动作。
- 后台复盘必须先用 `skill_view` 读取即将修改的精确文件；读取 `SKILL.md` 不授予覆盖
  supporting file 的权限。
- pinned、external、bundled、hub-installed 和 load-bearing built-in 技能对后台复盘只读；
  前台用户明确发起的编辑仍保留原能力。
- 后台复盘禁止无 `absorbed_into` 证据的删除；已验证合并只进入 `.archive`，保留 usage
  lifecycle 和 `hermes curator restore` 恢复路径。

## 验收与退出条件

1. 两个 profile home 并发构图、detail/edit/delete 全程隔离。
2. graph edge 全部可解析，timeline 与 nodes 一一对应，坏 frontmatter 不崩溃。
3. canonical memory id 对无关条目插入/删除稳定，旧 index id 仅兼容 mutation。
4. MemoryStore 格式完全一致、skill archive 可恢复、pinned skill fail closed。
5. `profile.growth.summary` 与 `profile.learning.graph` 的 memory/skill 计数一致。
6. same-model/full-history 与 routed/digest 两条后台复盘路径有行为测试；配置缺失、解析失败、
   memory disabled 和 list-shaped tool result 有故障注入。
7. 后台技能写入覆盖先读后写、pinned/external/内置所有权、无证据删除拒绝和可恢复归档。
8. CLI、Gateway contract、静态/分层和相关全量回归通过。

只有以上门禁均有证据，阶段 13 才能标记“代码与自动验收完成”；Doxie 星图/时间线视觉
验收需消费新 RPC 后单独签核。
