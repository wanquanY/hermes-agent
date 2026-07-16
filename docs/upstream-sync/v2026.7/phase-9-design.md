# 阶段 9 设计：Codex / Responses 路由、压缩、推理与验证事实源

## 目标与账本范围

阶段 9 手工吸收 `U7`、`CX1`、`CX2`、`CX4`。上游提交只提供协议事实和失败场景：

- `b9146a47bc86`、`bce17bf6a2e2`：Copilot Responses 的 message item id 绑定连接，
  credential rotation、重启或负载均衡后不可 replay，且 dispatch 前必须再次执行策略。
- `d1c8c0341`、`87b65e24a`、`8121dbb1660a`、`8c62a922965a`、
  `ec6982fbcf11`：只有 Codex app-server 拥有真实 thread，原生 compaction 必须由它执行；
  interrupted、usage gap 与 compaction verdict 必须进入 Hermes 统一生命周期。
- `b3b1e58ad`、`538173f67`：commentary/analysis 是 reasoning，不是最终 assistant content。
- `verification-hook` 及其后续修正：编辑、验证命令和完成判断之间需要可查询证据；该能力
  必须进入 domain/application seam，不能成为 Codex transport 私有 hook。

禁止 merge、rebase 或 cherry-pick 上游代码；本地 RunContext、compression lease/verdict、
usage recorder、event callback、Gateway/TUI projector 与 Doxie 合同是最终约束。

## 阶段前事实与风险

1. Responses adapter 已按 issuer 过滤 encrypted reasoning，并能保留 message item 的
   id/status/phase；但调用链以多个布尔参数表达 route，主调用、override 后 preflight、辅助
   调用和 fallback 没有同一不可绕过策略。
2. Copilot 的 message item id 是 connection scoped。当前 replay 与 preflight 都可能保留
   短 id，某次连接轮换后会让整个持久会话持续 401。
3. Phase 6 已统一 app-server compaction notification、usage 与 verdict 的观察 seam；但
   `conversation_compression` 仍可能调用 legacy auxiliary summarizer，无法缩小 Codex 真 thread。
4. app-server raw event 与 Responses stream 都有 reasoning/content 两条投影路径。commentary
   仍可能进入 content delta 或最终正文，造成 UI 重复或把工具前导语当最终回复。
5. 文件 mutation 只有失败 footer。系统没有“改了什么、跑了什么、范围多大、是否新鲜”的
   verification aggregate，任何 provider 都可能在未验证时声称完成。

## 唯一 owner 设计

### Responses route policy

新增纯领域 `ResponsesRoutePolicy`，以显式 route kind 统一决定：issuer identity、encrypted
reasoning replay、assistant message id replay、phase/status replay 与最终 preflight sanitization。

- resolver 只根据已解析 provider/base URL/api mode 建立 policy；adapter 不再自己猜 route。
- `ResponsesApiTransport.build_kwargs`、`convert_messages`、request override 后 preflight、retry、
  auxiliary 与 fallback 都传递同一 policy。
- Copilot 永远移除 message item id，但保留 content/status/phase；OpenAI Codex 可保留合法短
  id；跨 issuer reasoning 丢弃；重复 item id 按首次出现稳定去重。
- preflight 是最后不可绕过边界：middleware 或 request override 重新注入 unsafe id 仍被剥离。

### Codex app-server compaction coordinator

只在 `api_mode=codex_app_server` 启用原生 thread compaction。Responses OAuth 路径仍由 Hermes
拥有 transcript 并使用 provider-portable compressor，禁止持久化 provider-locked compaction item。

- protocol session 只负责 `thread/compact/start`、notification、interrupt、timeout 和 typed result。
- application coordinator 负责模式 `native|hermes|off`、compression lease、调用、terminal 判定、
  usage/verdict/event/persistence 的一次性提交；`conversation_compression` 只调用 coordinator。
- `native` 表示由 Codex 自动压缩，Hermes preflight 不重复触发；`hermes` 表示达到 Hermes 阈值或
  手动命令时触发 app-server 原生压缩；`off` 表示不自动触发，但显式手动压缩仍走原生协议。
- interrupted、timeout、error 不算完成，不递增成功边界；usage 缺失仍消费 pending verdict，后续
  普通 turn 不得错误承接一次旧 compaction。
- 本地 transcript 不被 summary 重写；session event、usage、lease 和 compression verdict 仍进入
  Hermes 事实源，成功边界只记录一次。

### reasoning projector

`CodexEventProjector` 是 app-server event 分类唯一 owner，Responses normalization 是 Responses
item 分类唯一 owner；二者输出相同的 `reasoning delta / final reasoning / assistant content` 语义。

- commentary/analysis delta 只进入 reasoning callback，不进入 assistant stream accumulator。
- completed commentary/analysis item 保留用于 replay，但只进入 reasoning 字段。
- final_answer 才进入 assistant content；实时累计与最终 transcript 去重且顺序一致。
- transport 只传递 raw protocol event，Gateway/TUI/Doxie 继续消费统一 Hermes event 名称。

### verification aggregate

新增 domain verification evidence/value objects 与 application `VerificationService`：

- file mutation 成功后记录 changed path 与 edit generation；terminal/code tool 完成后按 workspace
  已知 verify commands 分类 test/lint/type/build/check、targeted/full、passed/failed。
- repository 只负责持久 evidence/state；application service 负责 freshness、aggregate status、
  retention 和 pre-completion decision；provider/transport 不得写 verification 状态。
- verify-before-complete 是有界、可配置的 application policy：有代码修改且没有新鲜 passing
  evidence 时给 agent 一次 typed verification requirement；不伪装 user/system 消息，不覆盖真实
  provider failure，不无限续跑。消息型 surface、纯文档变更和没有 verify contract 的 workspace
  可得到明确 not_applicable。
- Gateway 查询只读 application service 投影；普通、Codex、subagent 和 Team Mission 共享相同
  session/workspace aggregate。

## 可验收退出条件

1. fake OpenAI/Copilot/other Responses route 回放旧、缺失、重复、超长和 middleware 重注入 id；
   主调用、preflight、auxiliary、fallback 结果一致，Copilot 无 connection-scoped id。
2. model switch/fallback 后跨 issuer reasoning 不 replay，合法 unstamped legacy item 保持兼容；
   delegation summary headroom 不受 route 变化影响。
3. app-server manual/threshold compaction 调用真实 `thread/compact/start`；成功 transcript 可恢复、
   usage/verdict/event 各一次，绝不调用 legacy auxiliary compressor。
4. auto native 不重复触发；interrupted、timeout、错误、空 usage 和进程退休均有故障注入测试。
5. Responses 与 app-server commentary delta 只出现在 reasoning；最终 transcript 顺序一致无重复。
6. edit→targeted/full pass/fail、stale evidence、多 workspace、纯文档、无 contract、并发写入、retention
   与 restart 均有 verification aggregate 行为测试；pre-complete requirement 有界且跨 provider 一致。
7. 阶段专项、Hermes 全量、静态/分层/账本门禁与 Doxie `@codex`/Gateway `test:gates` 全绿。

## 阶段收益

- Codex/Copilot 长会话不会因 route 或连接轮换随机永久 401，fallback/model switch 可恢复。
- app-server 压缩真正缩小 Codex thread，减少额外 LLM 调用和本地 summary 与真实上下文漂移。
- reasoning、final content 与 usage 在实时 UI、持久 transcript 和所有 surface 上语义一致。
- “已完成”可以由新鲜验证证据支撑，而不是依赖模型自述；该能力对 Codex 和普通 provider 等价。
