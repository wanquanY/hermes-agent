# 阶段 14：上游能力面与三层资产架构验收

## 固定基线

- 上游事实源：`upstream/main @ fb0ed8396c1c598e3c116f41eea476ce18aa2dd3`
- 快照日期：2026-07-20
- 本阶段不再刷新远端，不以分支名作为验收输入。
- 吸收方式仍为行为和失败场景的手工重构，不 merge、rebase 或 cherry-pick 上游。

## 资产覆盖

| 资产层 | 固定上游 | 本地 | 结论 |
|---|---:|---:|---|
| Skill (`SKILL.md`) | 178 | 189 | 上游新增和更新批次已进入本地发现合同；本地保留额外能力 |
| Plugin (`plugin.yaml`) | 93 | 97 | 上游 manifest 无缺口；本地额外提供 Figma 等能力包 |
| MCP catalog | 4 | 5 | 上游 catalog 无缺口；额外项为 Plugin 所有的 Figma Desktop MCP |

数量不是唯一验收标准。Skill 还要通过 discovery metadata、本地链接、支持目录和
2,000 行上限；Plugin 还要验证声明资产不能逃逸包目录；MCP 还要通过共享 catalog、
安全安装、OAuth/HTTP/stdio 生命周期和工具 allowlist 合同。

## 本地重构后的 owner

| 责任 | 唯一 owner | 约束 |
|---|---|---|
| Plugin 声明的 Skill/MCP 资产 | `hermes_cli/plugin_assets.py` | manifest 驱动、路径 confinement、统一注册与注销 |
| MCP 可安装目录 | `hermes_cli/mcp_catalog.py` | CLI、Dashboard、TUI 共用同一 catalog |
| Figma 外部能力 | `plugins/productivity/figma` | 一个 Plugin 同时拥有 Skill 和 MCP manifest；不复制到全局目录 |
| Custom endpoint | `hermes_cli/custom_endpoints.py` | 领域服务负责校验、持久化、更新与删除 |
| Auxiliary task | `hermes_cli/auxiliary_tasks.py` | 默认配置、CLI、Dashboard、Plugin reserved key 共用一份目录 |
| Interim assistant 事件 | `message.interim` | Gateway、共享 JSON-RPC、TUI 和 Doxie projection 使用同一语义 |
| Desktop 多窗口 | Doxie Electron 主进程模块 | window manager、event dedupe、power-save 和 menu 分责，不回填巨型 main |

阶段 14 新增的主要 owner 分别为 49、281、254 和 394 行，均低于工程的 2,000 行
限制；大文件只保留 composition wiring，没有继续把新责任堆入既有单体。

## 自动验收证据

- Skill / Plugin / MCP 聚合：328 passed，0 failed。
- Figma capability market、Plugin asset、MCP catalog、Dashboard/TUI 与打包：55 passed，0 failed。
- Auxiliary task 配置、注册、CLI/Dashboard 一致性：75 passed，0 failed。
- checkpoint、消息平台、provider、custom endpoint、interim event、LSP 等阶段聚合：
  30 passed，0 failed。
- Doxie desktop 完整 `test:gates`：退出码 0；Vitest 342 个文件，1965 passed、
  3 skipped；Design System 46 passed；Electron 424 passed；Node source 17 passed；
  VNext quality 58 passed。
- Doxie desktop source policy：1,706 个文件、506 个 strict files，9 项债务规则均为 0；
  legacy debt、accessibility blocker、i18n overflow、route deviation 均为 0。
- Doxie 与 Hermes 的生成契约同步：253 个 Gateway 方法、112 个原生工具；TypeScript
  type-check、ESLint、Frontend boundary 和 10 项 VNext boundary rules 全部通过。
- Doxie Figma fidelity：4 个正式场景全部通过；新建对话、普通对话、团队审批和团队运行
  的 RMSE 分别为 0.102425、0.140075、0.123002、0.104037，全部几何锚点均在合同阈值内。

台账对应 `UP1` 至 `UP24`。其中 `UP19`、`UP22`、`UP23` 采用 `superseded`，含义是
能力已经吸收，但上游分散的 UI/目录/列表实现被本地领域服务和统一资产图替代，不是跳过。

## 外部运行条件

Figma Desktop MCP 的本地地址是 `http://127.0.0.1:3845/mcp`。离线安装、默认只读工具
allowlist、Dashboard/TUI 展示和配置写入均已自动验收；当前实机探测返回连接失败，因为
Figma Desktop 没有启动 MCP server。该项是外部进程 readiness，不是代码或三层架构缺口。

## 尚未替代的发布验收

- 真实 Figma Desktop 启动后的 initialize / tools/list / 只读调用 smoke test。
- 多窗口真实 macOS 前后台切换、系统休眠和 keep-awake 长稳。
- 真实用户配置目录的升级与回滚演练。

这些实机项不阻塞代码吸收完成，但不得被自动测试结果冒充为已执行。
