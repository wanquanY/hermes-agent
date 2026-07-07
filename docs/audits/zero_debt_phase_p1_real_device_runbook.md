# P1 真机验收 Runbook

状态：`pending_user_execution`

## 目的

本 runbook 用于完成 P1 的用户真机签收。P1 机器侧已经验证 channel owner
迁移和边界门禁；真机侧只验证开发环境中真实启动和至少一个真实通道的行为。

P1 真机签收通过后，才能把
`docs/audits/zero_debt_phase_p1_human_signoff.md` 从 `pending` 改为通过，并进入
P2。

## 前置确认

在真机验收前先确认机器侧状态：

```bash
cd /Users/yangwanquan/syngents/code/hermes-agent
.venv/bin/python scripts/zero_debt/status.py
.venv/bin/python scripts/zero_debt/verdict.py --phase P1 --json
.venv/bin/pytest tests/channels tests/observability/test_channels_p1_boundary.py tests/observability/test_module_liveness_audit.py -q
git diff --check
```

期望：

- P1 verdict 为 `pass`
- P1 closure 为 `fail`，且失败原因是 `human_signoff:not_pending`
- P1 channel tests 通过
- `git diff --check` 通过

## 真机验收项

### 1. Gateway 启动

启动开发环境 Hermes gateway / DoXie sidecar。

验收标准：

- 启动过程不出现 `gateway.platforms.*` import error。
- 启动过程不出现 missing channel owner error。
- 启动过程不出现 slash command owner import error。

失败记录格式：

```text
步骤：Gateway 启动
结果：失败
命令：
关键日志：
结论：
```

### 2. 平台通道导入与连接状态

选择至少一个当前开发环境可用的真实通道。

验收标准：

- 通道 adapter 从 `channels.platforms.*` 导入。
- 平台 connected-check / status 能正常返回。
- 不依赖 `gateway.platforms.*` fallback。

失败记录格式：

```text
步骤：平台通道连接
平台：
结果：失败
关键日志：
结论：
```

### 3. 消息收发

在真实通道或 DoXie 开发 sidecar 中完成一轮消息收发。

验收标准：

- 入站消息能进入 Hermes。
- 出站回复能通过同一通道返回。
- 无 channel adapter method missing / import missing / permission-tag missing 错误。

失败记录格式：

```text
步骤：消息收发
平台：
输入：
结果：失败
关键日志：
结论：
```

### 4. Channel Support Owner

验证 channel support owner 不再来自 legacy gateway 文件。

验收标准：

- `channels/platform_registry.py` owns platform registry。
- `channels/session_context.py` owns session context。
- `channels/runtime_status.py` owns runtime status。
- `channels/sticker_cache.py` owns sticker cache。
- `channels/whatsapp_identity.py` owns WhatsApp identity。

### 5. 回归观察

观察至少一个常用开发会话。

验收标准：

- 没有 P1 引入的启动异常。
- 没有 P1 引入的通道 dispatch 异常。
- 没有 P1 引入的 platform connected-check 异常。

## 通过记录模板

如果全部通过，将以下内容写入
`docs/audits/zero_debt_phase_p1_human_signoff.md`：

```text
签收状态：`passed`
签收日期：YYYY-MM-DD
签收人：yangwanquan

P1 真机验收通过，可以进入 P2。

真机结果：
- Gateway 启动：通过
- 平台通道连接：通过，平台=<填写>
- 消息收发：通过，平台=<填写>
- Channel support owner：通过
- 回归观察：通过
```

写入后必须确认：

```bash
.venv/bin/python scripts/zero_debt/phase_closure.py --phase P1
```

期望：`P1 pass`

## 失败处理

任何一项失败时：

- 不得进入 P2。
- 不得把 human sign-off 改为 passed。
- 必须在 sign-off 文件中记录失败平台、复现步骤、关键日志和下一步修复 owner。
