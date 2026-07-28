# 阶段 12 验收：正确性尾项、受控并发与本轮关闭

## 结论

阶段 12 的 `U9`、`U12`、`RT4`、`B2`、`B3`、`B4`、`B6` 已按当前 Hermes
架构手工吸收。阶段专项、Hermes 标准全量、静态与分层门禁、observability 以及
Doxie desktop `test:gates` 全绿。

当前状态严格区分为：

- 代码实现：完成。
- 自动化与本地故障注入：完成。
- 用户统一实机验收：未执行，等待 Phase 1-12 一次性验收。
- 真实 OpenResty/Cloudflare 长连接、真实 macOS reopen、24 小时
  session/stream/cron soak：未执行，不宣称通过。
- Git：只允许建立本地 checkpoint；统一验收前不 push、不合并回主开发分支。

## 吸收范围、当前 owner 与收益

| 条目 | 当前 owner | 已证明的行为 | 直接收益 |
|---|---|---|---|
| U9 terminal cwd | `tools.terminal_cwd_registry`、terminal/file tool seam | cwd 按 environment + session 隔离；文件工具不读取其他会话的共享 cwd | 多会话、多 worktree 不再互相串目录 |
| U9 skill collision | `agent.skill_commands` | core command/alias 优先；skill slug 规范化后 first-wins | skill 不再静默覆盖核心命令或彼此覆盖 |
| U9 inert patch | `tools.patch_parser` | context-only、search==replace 与全 inert patch fail closed | “执行成功但没有改动”的假成功被消除 |
| U12 mixed batch | tool effect classifier、batch segment planner、single finalizer | 只读段可并行；副作用、未知、交互、terminal、plugin/MCP 是串行 barrier | 在可证明安全时提升吞吐，副作用顺序不被并发破坏 |
| RT4 provider pool | `agent.process_bootstrap.build_provider_http_client` | sync/async client 共用 pool limits、idle expiry、proxy/no-proxy 语义且不覆盖 socket 选项 | 降低长会话连接抖动和过度建连风险 |
| B2 silence | `hermes_gateway.response_filters` 与 turn/stream finalizer | `NO_REPLY`、`[SILENT]` 只作为持久化语义，不投影成页面系统消息或气泡 | 静默协议不再污染用户时间线 |
| B3 fallback cooldown | fallback chain state | 非限流耗尽使用 monotonic 5 秒 gate；限流/计费仍保留 60 秒 | 连续失败不再形成跨 turn 请求风暴 |
| B4 vision budget | `tools.vision_tools` | 进程级 active/queue 上限、排队取消与 permit 回收 | 大批量图片不再无界挤占内存和 provider 连接 |
| B6 prompt caching | `agent.prompt_caching` 与 runtime restore policy | 全局 toggle 在主调用、fallback、model switch/restore 后都重新生效 | 可预测地启停 Anthropic prompt caching，不留恢复路径旁路 |

## 自动验收证据

### 阶段专项

执行 Phase 12 新增和相邻合同测试聚合：

```text
464 passed, 10 skipped in 13.72s
```

覆盖 terminal/file cwd、skill collision、patch parser、mixed batch、provider pool、
prompt caching、vision、silence、fallback cooldown、MCP effect barrier，以及 GMI 模型目录
测试隔离。

其中 provider pool 使用本地 HTTP/1.1 server 加速执行 30 轮 idle-reap/reconnect；vision
执行 100 项 fanout，验证并发峰值、queue full、取消和 permit 无泄漏。这些是可重复的本地
故障注入，不等价于真实反向代理或 24 小时 soak。

### 标准全量回归及红项收敛

`scripts/run_tests.sh` 的最终结果：

```text
1592 files, 29188 tests passed, 0 failed in 511.8s (28 workers)
```

关闭过程保留了两次红项证据，没有把失败当作噪声跳过：

1. 首次全量为 29,186 passed、2 failed。失败是旧 MCP 测试把 transport parallel opt-in
   误当成业务 effect-safe；测试被改为符合 Phase 12 的安全契约，未知副作用仍串行。
2. 第二次全量为 29,187 passed、1 failed。GMI 静态回退用例只 stub 旧 fetch path，
   全量并发时偶发访问真实 profile `/models`；补吸收上游 `f34cf7e3a` 的测试隔离后，
   GMI 文件 25 passed，最终全量全绿。

### 静态、分层与观测门禁

| 门禁 | 结果 |
|---|---|
| `ruff check .` | 通过 |
| `compileall` | 通过 |
| import-linter | 169 files、336 dependencies；1 contract kept、0 broken |
| `pytest -q tests/observability` | 86 passed、13 skipped、1 xfailed |
| `git diff --check` | 通过 |

### Doxie desktop 集成门禁

`corepack pnpm --filter @dovie/desktop test:gates` 退出码 0：

| 子门禁 | 结果 |
|---|---|
| Vitest | 266 files；1,978 passed、3 skipped |
| Electron Node | 391 passed、0 failed |
| src-node-spec | 19 passed、0 failed |
| Gateway contract | 205 methods in sync；98 active methods covered by 135 required methods |
| TypeScript | 通过 |
| ESLint | 0 errors、70 既有 warnings |
| frontend boundaries | 通过 |

## 统一实机验收清单

启动 Doxie 时必须指向本 worktree：

```bash
DOVIE_HERMES_RUNTIME_MODE=source \
DOVIE_HERMES_SOURCE_DIR=/Users/yangwanquan/syngents/code/hermes-agent-upstream-absorption-v2026.7 \
DOVIE_STREAM_TRACE=1 \
DOVIE_HERMES_GATEWAY_WS_FRAME_TRACE=1 \
corepack pnpm desktop:dev 2>&1 | tee /tmp/dovie-dev.log
```

统一验收重点：

1. 同时打开两个 Doxie 会话，分别进入不同 worktree；执行 `cd` 后用相对路径读写，确认
   cwd 不串会话，重开会话后仍落在正确 workspace root。
2. 触发只读 + terminal/写工具 + 只读的 mixed batch，确认只读段可并发、barrier 前后顺序
   稳定，取消后没有重复副作用或重复结果。
3. 验证合法 patch 正常修改；context-only、原文等于替换文和全 inert patch 明确报错。
4. 配置与核心命令同 slug 的 skill，确认核心命令不被覆盖，碰撞 skill 的解析结果稳定。
5. 让模型输出严格的 `NO_REPLY` 或 `[SILENT]`，确认页面没有系统提示消息、空气泡或
   pending follow-up 重发；历史持久化仍能区分真正静默完成。
6. 连续制造 provider/fallback 失败，确认非限流耗尽有短 cooldown，限流/计费仍执行长
   cooldown，系统不会形成请求风暴。
7. 切换 `prompt_caching.enabled`，再做 model switch、fallback 和 primary restore，确认开关
   始终生效。
8. 使用大量图片验证排队、取消和后续任务恢复；不应出现永久占用 permit 或无界并发。
9. 经真实 OpenResty/Cloudflare 类反向代理执行长流和空闲后复用，观察连接重建与错误率。
10. 完成真实 macOS 强杀/reopen，以及 24 小时 session、stream、cron soak；检查无重复执行、
    stale stream、连接风暴、跨会话 cwd 污染和未回收后台工作。

第 1-8 项是次日功能验收重点；第 9-10 项是发布级长稳证据。未完成第 9-10 项前，可以判定
“本轮代码与自动门禁完成”，不能据此单独给出 GA Go 决策。
