# Zero Debt File Size Audit

Audit date: 2026-07-08

## Scope

This audit records file-size debt discovered while closing the P1 channel-owner
machine gates. It is not a P1 rollback condition because P1's ownership target
is `channels/platforms/**`, but it is a hard input for subsequent zero-debt
vertical slices.

## P1 Channel Platform Result

All Python files under `channels/platforms/**` are at or below 2000 lines.

Near-limit channel files:

| Lines | File |
|---:|---|
| 1996 | `channels/platforms/base.py` |
| 1972 | `channels/platforms/feishu.py` |
| 1937 | `channels/platforms/telegram_inbound.py` |
| 1932 | `channels/platforms/discord.py` |

These files remain valid for P1, but future edits to these owners should prefer
responsibility extraction over adding more behavior.

## Production Files Above 2000 Lines

These files violate the repository-wide quality target and must be addressed by
future vertical slices rather than hidden behind compatibility paths.

| Lines | File | Planned Owner/Slice |
|---:|---|---|
| 18574 | `gateway/run.py` | P3 gateway registry and gateway-run vertical decomposition |
| 14821 | `cli.py` | CLI decomposition slice |
| 13741 | `hermes_cli/main.py` | CLI decomposition slice |
| 7740 | `hermes_cli/auth.py` | CLI auth owner split |
| 7477 | `hermes_state.py` | P2 data-plane repository ownership |
| 7006 | `hermes_cli/web_server.py` | CLI web-server owner split |
| 6391 | `hermes_cli/kanban_db.py` | Kanban repository slice |
| 5945 | `hermes_cli/config.py` | CLI config owner split |
| 5477 | `agent/auxiliary_client.py` | Agent support service split |
| 5467 | `hermes_cli/gateway.py` | CLI gateway owner split |
| 5099 | `run_agent.py` | Runtime entrypoint decomposition |
| 4870 | `agent/conversation_loop.py` | Agent runtime loop decomposition |
| 4426 | `tools/browser_tool.py` | Tool implementation split |
| 4275 | `tools/mcp_tool.py` | Tool implementation split |
| 3994 | `hermes_cli/models.py` | CLI model owner split |
| 3759 | `tools/skills_hub.py` | Tool implementation split |
| 3558 | `hermes_cli/setup.py` | CLI setup owner split |
| 3469 | `tools/delegate_tool.py` | Tool implementation split |
| 3363 | `hermes_cli/tools_config.py` | CLI tools config owner split |
| 3342 | `plugins/platforms/google_chat/adapter.py` | Plugin adapter split |
| 3140 | `hermes_state_runs.py` | P2 data-plane repository ownership |
| 2881 | `tui_gateway/services/run_control.py` | P3/P4 gateway service migration |
| 2749 | `hermes_cli/model_setup_flows.py` | CLI model setup split |
| 2677 | `hermes_cli/kanban.py` | Kanban service split |
| 2571 | `agent/chat_completion_helpers.py` | Agent model helper split |
| 2545 | `hermes_team_mission/gateway/runtime_methods.py` | Team mission gateway slice |
| 2531 | `agent/context_compressor.py` | Agent compression split |
| 2462 | `tools/terminal_tool.py` | Tool implementation split |
| 2415 | `cron/scheduler.py` | Scheduler owner split |
| 2410 | `tui_gateway/methods/session.py` | Gateway session vertical slice |
| 2369 | `tools/tts_tool.py` | Tool implementation split |
| 2255 | `agent/agent_runtime_helpers.py` | Agent runtime helper split |
| 2240 | `agent/anthropic_adapter.py` | Model adapter split |
| 2220 | `tools/approval.py` | Approval tool split |
| 2217 | `plugins/kanban/dashboard/plugin_api.py` | Plugin API split |
| 2025 | `scripts/release.py` | Release script split |
| 2024 | `tools/send_message_tool.py` | Tool implementation split |
| 2024 | `hermes_team_mission/gateway/common.py` | Team mission gateway common split |
| 2012 | `hermes_cli/doctor.py` | CLI doctor split |
| 2002 | `hermes_team_mission/state/session_conversations.py` | Team mission state split |

## Gate Policy

- Phase-specific gates remain scoped to the active vertical slice.
- Repository-wide file-size debt is tracked here and must trend down as slices
  touch the listed owners.
- No future slice should add behavior to an already over-limit production file
  without extracting ownership in the same phase.
