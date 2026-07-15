# P2 真机验收 Runbook

状态：`pending_user_execution`

## 目的

验证 P2 数据面 repository ownership 在真实 DoXie 桌面运行中成立。机器门禁已经
证明生产代码只通过正式 aggregate owner 读写；本 runbook 验证进程重启、流式事件、
团队与子任务场景没有出现只在真实运行时暴露的持久化或排序回归。

P2 真机验收通过后，才能把
`docs/audits/zero_debt_phase_p2_human_signoff.md` 从 `pending` 改为通过并进入 P3。

## 机器前置条件

```bash
cd /Users/yangwanquan/syngents/code/hermes-agent
.venv/bin/python scripts/zero_debt/verdict.py --phase P2
.venv/bin/python scripts/zero_debt/phase_closure.py --phase P2
.venv/bin/python scripts/zero_debt/status.py
```

期望：

- P2 machine verdict 为 `pass`。
- P2 closure 为 `fail`，且唯一失败是 human sign-off 仍为 pending。
- 不允许通过修改 closure 规则绕过真机签收。

## 启动

```bash
pkill -f "syngents/code/hermes-agent/.venv.*tui_gateway"
cd /Users/yangwanquan/Personal_projects/AIGC_dev/doxie
DOVIE_HERMES_RUNTIME_MODE=source \
DOVIE_HERMES_SOURCE_DIR=/Users/yangwanquan/syngents/code/hermes-agent \
corepack pnpm desktop:dev 2>&1 | tee /tmp/dovie-dev.log
```

启动验收标准：

- source runtime contract 检查通过，control plane 正常启动。
- 不出现 `conversation_session_id required`、stable session NOT NULL、旧
  `SessionDB` method missing 或 gateway handler owner missing。
- migration 47 保持 parked；在 DoXie frontend H3 删除
  `runtimeSourceSeq` fallback 前不得提前启用。

## 场景一：新建单聊与重启恢复

1. 新建单聊，连续发送两轮不同且可识别的文本。
2. 每轮观察 user、reasoning、tool、assistant 的实时输出。
3. 切换到其他会话再返回。
4. 完全退出并重新启动桌面端，再进入该会话。

验收标准：

- 首轮 user 与 assistant 消息在重启后仍存在。
- 页面无需切换会话即可实时流式输出。
- 每轮只出现一条 user 消息和一份 assistant 内容。
- 时间顺序始终从旧到新，轮次保持一问一答。
- 连续 reasoning 片段按同一 turn 正确聚合，不重复内容。
- tool 调用位于所属 turn 的时间位置，重启后仍可见。

## 场景二：团队会话

1. 新建团队会话并发送一条会触发 leader 与 member 协作的任务。
2. 等待至少一个 member 活动和一个工具调用。
3. 切换会话并重启桌面端后重新进入。

验收标准：

- conversation session id 在创建、提交、render 和重启恢复期间保持一致。
- leader/member 发言、活动与工具事件按 canonical sequence 展示。
- 不出现 `No item with that key`、stable session 为空或会话无法 render。
- participant 昵称与头像由 participant/profile 数据解析，不显示内部 runtime id。

## 场景三：Subagent 活动归属

1. 在主会话触发一个包含 subagent 的复杂任务。
2. 打开 subagent 详情，观察其 reasoning 与工具事件。
3. 关闭详情、切换会话并重启桌面端。

验收标准：

- subagent 执行作为父会话 activity 展示，事件顺序与实际执行顺序一致。
- subagent 内部执行会话不出现在左侧顶层会话列表。
- 主 agent 消息不因 activity projection 重复。
- 重启后 subagent reasoning、工具调用与最终状态仍可恢复。

## 日志检查

```bash
rg -n "ERROR|Traceback|handler error|conversation_session_id required|NOT NULL constraint|No item with that key" /tmp/dovie-dev.log
```

允许记录明确标注为非阻断、且与本次操作无关的已知日志；任何数据面、identity、
timeline 或 activity 错误都视为 P2 验收失败。

## 签收

全部场景通过后，将实际测试日期、会话类型与结果写入
`docs/audits/zero_debt_phase_p2_human_signoff.md`，并执行：

```bash
.venv/bin/python scripts/zero_debt/phase_closure.py --phase P2
```

只有输出 `P2 pass` 才允许开始 P3。
