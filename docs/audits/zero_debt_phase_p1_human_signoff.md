# Hermes Zero Debt P1 人工签收

签收状态：`pending`

签收日期：待填写

签收人：待填写

## Phase

P1 - Channels And Slash Commands Independence

## 真机验收 Runbook

- `docs/audits/zero_debt_phase_p1_real_device_runbook.md`

## 机器侧证据

当前机器侧 P1 门禁已经通过：

```bash
.venv/bin/python scripts/zero_debt/verdict.py --phase P1 --json > docs/audits/zero_debt_phase_p1_verdict.json
# 86 checks, 0 failed

.venv/bin/pytest tests/channels tests/observability/test_channels_p1_boundary.py tests/observability/test_module_liveness_audit.py -q
# 72 passed, 1 warning

git diff --check
# passed
```

完整机器 verdict：

- `docs/audits/zero_debt_phase_p1_verdict.json`

相关结构债审计：

- `docs/audits/zero_debt_file_size_audit.md`

## 真机验收项

签收前必须在开发真机环境确认以下 P1 相关行为：

1. 启动 Hermes gateway 不再依赖 `gateway.platforms.*` 旧平台 owner。
2. 至少一个真实平台通道可以完成会话启动、消息接收、消息发送。
3. 平台连接状态、session context、runtime status、sticker cache、WhatsApp identity 等 channel support owner 均由 `channels.*` 路径承担。
4. 未观察到由 P1 迁移引入的启动异常、通道导入异常、平台 dispatch 异常。

## 用户签收结论

待用户真机确认后，将本节改为：

```text
P1 真机验收通过，可以进入 P2。
```

如果真机失败，必须记录失败平台、复现步骤、关键日志和回滚/修复结论；P1 不得关闭。

## Phase 边界

在本文件签收状态仍为 `pending` 时，P1 不算正式关闭，不得按计划进入 P2。
