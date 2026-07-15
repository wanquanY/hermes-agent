# Hermes Zero Debt P1 人工签收

签收状态：`passed`

签收日期：2026-07-08

签收人：yangwanquan

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

P1 真机验收通过，可以进入 P2。

用户已确认开发真机环境通过 P1 验收：

1. Hermes gateway 启动无异常。
2. 至少一个真实平台会话可以完整 loop 通。
3. 平台 dispatch 无异常。
4. slash command ownership 迁移后未观察到 P1 引入的真机回归。

如果真机失败，必须记录失败平台、复现步骤、关键日志和回滚/修复结论；P1 不得关闭。

## Phase 边界

本文件签收状态已为 `passed`，P1 可按计划关闭并进入 P2。
