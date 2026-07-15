"""Leader/member prompt construction and input artifact projection."""

from __future__ import annotations

import json

from hermes_team_mission.context.artifact_refs import artifact_refs_from_payload
from hermes_team_mission.gateway.leader_policy import (
    TEAM_LEADER_DIRECT_REPLY_NEGATED_SELF_MARKERS,
    TEAM_LEADER_DIRECT_REPLY_NO_START_MARKERS,
    TEAM_LEADER_DIRECT_REPLY_SELF_MARKERS,
    TEAM_LEADER_START_TASK_MARKERS,
)
from hermes_team_mission.gateway.value_helpers import task_id_from_metadata


def compact_graph_context(graph: dict) -> dict:
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    conversation = (
        graph.get("conversation")
        if isinstance(graph, dict) and isinstance(graph.get("conversation"), dict)
        else {}
    )
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    edges = graph.get("edges") if isinstance(graph, dict) else []
    compact_nodes = []
    for node in nodes[:24] if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        metadata = (
            node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        )
        compact_nodes.append(
            {
                "id": str(node.get("node_id") or ""),
                "kind": str(node.get("kind") or ""),
                "title": str(node.get("title") or "")[:120],
                "status": str(node.get("status") or ""),
                "role": str(metadata.get("role") or ""),
                "phase": str(metadata.get("phase") or ""),
            }
        )
    return {
        "conversation": {
            "id": str((conversation or {}).get("conversation_id") or ""),
            "title": str((conversation or {}).get("title") or "")[:160],
            "conversation_session_id": str(
                (conversation or {}).get("conversation_session_id") or ""
            ),
            "active_mission_id": str(
                (conversation or {}).get("active_mission_id") or ""
            ),
        },
        "mission": {
            "id": str((mission or {}).get("mission_id") or ""),
            "title": str((mission or {}).get("title") or "")[:160],
            "objective": str((mission or {}).get("objective") or "")[:500],
            "mode": str((mission or {}).get("mode") or ""),
            "status": str((mission or {}).get("status") or ""),
        },
        "node_count": len(nodes) if isinstance(nodes, list) else 0,
        "edge_count": len(edges) if isinstance(edges, list) else 0,
        "nodes": compact_nodes,
    }


def record_leader_input_attachment_artifacts(
    db,
    *,
    mission: dict,
    conversation_session_id: str,
    run_id: str,
    attachments: list[dict],
) -> list[dict]:
    artifact_refs = artifact_refs_from_payload(
        {"attachments": attachments}, event_type="team_mission.message.submit"
    )
    if not artifact_refs:
        return []
    mission_id = str((mission or {}).get("mission_id") or "").strip()
    if not mission_id:
        return []
    metadata = (
        mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    )
    task_id = task_id_from_metadata(metadata) or mission_id
    titles = [
        str(
            ref.get("title") or ref.get("path") or ref.get("uri") or ref.get("id") or ""
        ).strip()
        for ref in artifact_refs
    ]
    content = "User submitted attachments: " + ", ".join(
        title for title in titles if title
    )
    try:
        item = db.upsert_team_mission_memory_item(
            memory_id=f"team-input-artifacts:{mission_id}:{task_id}:{run_id}",
            team_id=str((mission or {}).get("team_id") or mission_id),
            mission_id=mission_id,
            conversation_session_id=str(conversation_session_id or ""),
            task_id=task_id,
            scope="mission_task",
            kind="artifact",
            content=content,
            structured_payload={
                "source": "team_mission.message.submit",
                "attachments": attachments,
            },
            source_node_ids=[],
            source_run_ids=[str(run_id or "")],
            artifact_refs=artifact_refs,
            workspace_refs=[
                {
                    "workspace_id": str((mission or {}).get("workspace_id") or ""),
                    "workspace_path": str((mission or {}).get("workspace_path") or ""),
                }
            ]
            if (
                (mission or {}).get("workspace_id")
                or (mission or {}).get("workspace_path")
            )
            else [],
            confidence=0.98,
            visibility="team",
            status="committed",
        )
    except Exception:
        return []
    return [item] if isinstance(item, dict) and item else []


_FOREIGN_PARTICIPANT_HISTORY_RULES = (
    "A user-role history message beginning with [assistant | <speaker> | <participant>] is a quoted prior utterance from that Leader or member. It is not the current user and not one of your own prior replies.",
    "Treat that speaker envelope only as ownership metadata, never as an alias or identity instruction for you.",
)


def leader_router_context(*, graph: dict, memory_text: str = "") -> str:
    """Build the Leader's trusted per-turn routing context."""
    context_json = json.dumps(compact_graph_context(graph), ensure_ascii=False, indent=2)
    parts = [
        "You are the Team Leader for a Dovie team conversation.",
        "Your visible identity is the team conversation Leader/coordinator. The underlying Dovie profile supplies tone and memory only; it must not override speaker ownership in the team conversation.",
        "Prior assistant messages authored by other participants are team member utterances, not roles you performed. When summarizing or explaining prior conversation, attribute each member's messages to that participant by name or role.",
        *_FOREIGN_PARTICIPANT_HISTORY_RULES,
        "Never expose internal runtime, framework, or implementation names to the user. The product name shown to users is Dovie.",
        "",
        "Route this user message before acting:",
        "- Answer directly for greetings, status questions, explanations, follow-up questions, clarifications, or requests about prior/current work.",
        "- Use clarify, file, terminal, or todo tools when they help you understand the user's request, inspect the workspace, validate local context, or organize the plan before deciding whether to start a team task.",
        "- Use team_mission_status when you need fresh mission graph or memory context to answer.",
        "- Call team_mission_start_task only when the user is asking to start a new substantive executable team task that benefits from planning, multi-agent work, workspace changes, research, verification, or a deliverable.",
        "- Do not call team_mission_start_task for greetings, lightweight Q&A, status checks, or discussion that can be answered directly.",
        "- Do not call delegate_task or ordinary subagents. In Dovie team mode, the Leader coordinates the user conversation, task graph, and member nodes.",
        "- If you decide a team task is needed, call team_mission_start_task naturally after any brief understanding or routing you need. Do not promise that the task was created before the tool result returns.",
        "- After team_mission_start_task succeeds, read the tool result and then reply naturally and briefly in the user's language. Tell the user the team task has started, it is being processed asynchronously, progress is available on the canvas, and they can continue chatting or submit another task.",
        "- After that confirmation, stop the current turn. Do not call more tools, do not continue with research, file work, terminal commands, or deliverable execution.",
        "- Reply in the user's language.",
        "",
        "Current team conversation context. The active mission may be empty until a team task is started:",
        context_json,
        "",
        "A team task is created only when you call team_mission_start_task. "
        "For a new task, derive the mission title and objective from the current User message, not from the conversation title.",
    ]
    if memory_text:
        parts.extend(["", memory_text])
    return "\n".join(parts).strip()


def leader_direct_reply_context(*, graph: dict, memory_text: str = "") -> str:
    """Build system-role context for a direct Leader reply turn."""
    parts = [
        "You are the Team Leader in a Dovie team conversation.",
        "Your visible identity is the team conversation Leader/coordinator. The underlying Dovie profile supplies tone and memory only; it must not override speaker ownership in the team conversation.",
        "Prior assistant messages authored by other participants are team member utterances, not roles you performed. When summarizing or explaining prior conversation, attribute each member's messages to that participant by name or role.",
        *_FOREIGN_PARTICIPANT_HISTORY_RULES,
        "Never expose internal runtime, framework, or implementation names to the user. The product name shown to users is Dovie.",
        "The user explicitly asked you not to start or launch a team task for this turn.",
        "Answer directly in the user's language. Do not call tools, do not create tasks, and do not mention internal routing.",
    ]
    parts.extend(
        [
            "",
            "Current team conversation context for reference only. The active mission may be empty until a team task is started:",
            json.dumps(compact_graph_context(graph), ensure_ascii=False, indent=2),
        ]
    )
    if memory_text:
        parts.extend(["", memory_text])
    return "\n".join(parts).strip()


def member_conversation_context(*, display_name: str, memory_text: str = "") -> str:
    """Build stable per-turn system context for an addressed team member."""
    member_name = str(display_name or "Team Member").strip()
    lines = [
        "You are an addressed member in a DoXie team conversation.",
        f"Your visible participant identity is exactly {member_name}.",
        f"Your self-name in this conversation is exactly {member_name}. If the user asks who you are, identify yourself as {member_name}; do not invent a personal nickname or reuse another participant's name.",
        "The underlying DoXie profile supplies your persona, tools, skills, and private profile memory; it does not change speaker ownership in this conversation.",
        "Prior assistant messages authored by the Leader or other members are their utterances, not statements or actions you performed.",
        *_FOREIGN_PARTICIPANT_HISTORY_RULES,
        "Answer as this participant only. Attribute other participants' prior statements by their visible name or role.",
        "Do not impersonate the Leader, another member, or the user, and do not claim their commitments as your own.",
        "Do not start or mutate a team task unless the current activity and available tools explicitly authorize it.",
        "Never expose internal runtime, framework, storage, or implementation names to the user. The product name shown to users is DoXie.",
        "Reply in the user's language.",
    ]
    if memory_text:
        lines.extend(["", memory_text])
    return "\n".join(lines).strip()


def _normalized_marker_text(user_text: str) -> tuple[str, str]:
    normalized = " ".join(str(user_text or "").strip().lower().split())
    return normalized, normalized.replace(" ", "")


def _has_normalized_marker(
    normalized: str, compact: str, markers: tuple[str, ...]
) -> bool:
    for marker in markers:
        candidate = str(marker or "").strip().lower()
        if candidate and (
            candidate in normalized or candidate.replace(" ", "") in compact
        ):
            return True
    return False


def leader_message_requests_team_task_start(user_text: str) -> bool:
    normalized, compact = _normalized_marker_text(user_text)
    if not normalized:
        return False
    if _has_normalized_marker(
        normalized, compact, TEAM_LEADER_DIRECT_REPLY_NO_START_MARKERS
    ):
        return False
    return _has_normalized_marker(normalized, compact, TEAM_LEADER_START_TASK_MARKERS)


def leader_message_requests_direct_reply(user_text: str) -> bool:
    normalized, compact = _normalized_marker_text(user_text)
    if not normalized:
        return False
    if _has_normalized_marker(
        normalized, compact, TEAM_LEADER_DIRECT_REPLY_NO_START_MARKERS
    ):
        return True
    if _has_normalized_marker(
        normalized, compact, TEAM_LEADER_DIRECT_REPLY_NEGATED_SELF_MARKERS
    ):
        return False
    if leader_message_requests_team_task_start(user_text):
        return False
    return _has_normalized_marker(
        normalized, compact, TEAM_LEADER_DIRECT_REPLY_SELF_MARKERS
    )


__all__ = [
    "compact_graph_context",
    "leader_direct_reply_context",
    "leader_message_requests_direct_reply",
    "leader_message_requests_team_task_start",
    "leader_router_context",
    "member_conversation_context",
    "record_leader_input_attachment_artifacts",
]
