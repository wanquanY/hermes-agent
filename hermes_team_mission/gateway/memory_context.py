"""Participant-aware Team Mission memory and context snapshot construction."""

from __future__ import annotations

import json

from hermes_agent.domain.conversation_memory import MemoryAccessContext
from hermes_agent.domain.participants import leader_participant_id, member_participant_id
from hermes_team_mission.gateway.value_helpers import falsey, node_role, truthy


def team_memory_disabled(params: dict, mission: dict) -> bool:
    if truthy(params.get("disable_team_memory") or params.get("disableTeamMemory")):
        return True
    if falsey(
        params.get("use_team_memory")
        if "use_team_memory" in params
        else params.get("useTeamMemory")
    ):
        return True
    metadata = (
        mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    )
    policy = metadata.get("memory") if isinstance(metadata.get("memory"), dict) else {}
    return truthy(policy.get("disabled"))


def team_memory_include_team_scope(params: dict, mission: dict) -> bool:
    metadata = (
        mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    )
    policy = metadata.get("memory") if isinstance(metadata.get("memory"), dict) else {}
    explicit_scope = str(
        params.get("memory_scope")
        or params.get("memoryScope")
        or policy.get("scope")
        or ""
    ).strip().lower()
    if explicit_scope in {"team", "team_wide", "cross_conversation", "workspace"}:
        return True
    if explicit_scope in {"conversation", "session", "mission"}:
        return False
    return truthy(
        params.get("include_team_memory")
        or params.get("includeTeamMemory")
        or params.get("cross_conversation_memory")
        or params.get("crossConversationMemory")
        or policy.get("include_team_memory")
        or policy.get("includeTeamMemory")
        or policy.get("cross_conversation")
        or policy.get("crossConversation")
    )


def memory_items_from_payload(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    memory = payload.get("memory_pack") or payload.get("memory_slice") or {}
    items = memory.get("items") if isinstance(memory, dict) else []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def memory_context_text(*, label: str, payload: dict) -> str:
    items = memory_items_from_payload(payload)
    if not items:
        return ""
    lines = [
        f"{label} (Hermes structured background; not new user input)",
        "Current user objective has highest priority. Use memory only as background, reusable artifacts, risks, and constraints. If memory conflicts with the current objective, surface the conflict and follow the current objective.",
        "",
        "Relevant memory:",
    ]
    for item in items[:12]:
        item_id = str(item.get("id") or "").strip()
        kind = str(item.get("kind") or "summary").strip()
        content = str(item.get("content") or "").strip()
        if len(content) > 700:
            content = content[:697].rstrip() + "..."
        sources: list[str] = []
        source_nodes = item.get("source_node_ids") if isinstance(item.get("source_node_ids"), list) else []
        source_runs = item.get("source_run_ids") if isinstance(item.get("source_run_ids"), list) else []
        artifacts = item.get("artifact_refs") if isinstance(item.get("artifact_refs"), list) else []
        if source_nodes:
            sources.append("nodes=" + ",".join(str(node_id) for node_id in source_nodes[:4]))
        if source_runs:
            sources.append("runs=" + ",".join(str(run_id) for run_id in source_runs[:4]))
        artifact_uris = [
            str(artifact.get("uri") or artifact.get("path") or artifact.get("id") or "")
            for artifact in artifacts[:3]
            if isinstance(artifact, dict)
        ]
        artifact_uris = [uri for uri in artifact_uris if uri]
        if artifact_uris:
            sources.append("artifacts=" + ",".join(artifact_uris))
        source_text = f" Sources: {'; '.join(sources)}." if sources else ""
        id_text = f"{item_id} " if item_id else ""
        lines.append(f"- [{id_text}{kind}] {content}{source_text}")
    return "\n".join(lines).strip()


def actor_context_snapshot_fields(
    db,
    *,
    conversation_session_id: str,
    participant_id: str,
    execution_scope_key: str,
    activity_id: str,
    activity_kind: str,
    profile_id: str = "",
    profile_version_id: str = "",
    node_id: str = "",
    attempt_id: str = "",
    selected_memory_ids: list[str] | None = None,
) -> dict:
    """Create an immutable per-run actor context snapshot when supported."""
    participant = (
        db.participants.get_participant(conversation_session_id, participant_id) or {}
    )
    memory_namespace = str(participant.get("memory_namespace") or "").strip()
    memory_revision = int(participant.get("memory_revision") or 0)
    transcript_cursor = int(participant.get("transcript_cursor") or 0)
    conversation_revision = 0
    snapshot_id = ""
    memory_service = getattr(db, "conversation_memory", None)
    if memory_service is not None:
        conversation_revision = memory_service.current_conversation_revision(
            conversation_session_id
        )
        snapshot = memory_service.create_snapshot(
            conversation_session_id=conversation_session_id,
            actor_participant_id=participant_id,
            execution_scope_key=execution_scope_key,
            activity_id=activity_id,
            activity_kind=activity_kind,
            node_id=node_id,
            attempt_id=attempt_id,
            conversation_revision=conversation_revision,
            participant_memory_revision=memory_revision,
            transcript_cursor=transcript_cursor,
            selected_memory_ids=list(selected_memory_ids or []),
        )
        snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
    return {
        "profile_id": str(profile_id or participant.get("agent_profile_id") or "").strip(),
        "profile_version_id": str(
            profile_version_id or participant.get("agent_profile_version_id") or ""
        ).strip(),
        "memory_namespace": memory_namespace,
        "conversation_revision": conversation_revision,
        "transcript_cursor": transcript_cursor,
        "participant_memory_revision": memory_revision,
        "visibility_policy_id": "team-conversation-v1",
        "context_snapshot_id": snapshot_id,
    }


def actor_conversation_memory_text(
    db,
    *,
    conversation_session_id: str,
    actor_participant_id: str,
    actor_role: str,
    profile_id: str = "",
    activity_id: str = "",
    node_id: str = "",
    limit: int = 12,
) -> tuple[list[str], str]:
    memory_service = getattr(db, "conversation_memory", None)
    if memory_service is None:
        return [], ""
    resolved_memory = memory_service.resolve_visible(
        MemoryAccessContext(
            conversation_session_id=conversation_session_id,
            actor_participant_id=actor_participant_id,
            actor_role=actor_role,
            activity_id=activity_id,
            node_id=node_id,
            profile_id=profile_id,
        ),
        statuses=("committed",),
        limit=max(1, min(int(limit or 12), 50)),
    )
    items = list(resolved_memory.get("items") or [])
    conflicts = list(resolved_memory.get("conflicts") or [])
    actor_summary = memory_service.latest_actor_summary(
        conversation_session_id, actor_participant_id
    )
    if not items and not actor_summary:
        return [], ""
    lines: list[str] = []
    if actor_summary:
        lines.extend(
            [
                "Actor context summary (trusted compressed projection; not a user message or shared memory):",
                "This summary belongs only to the current participant. Preserve every embedded participant_id; another participant's statement is never your own action or commitment.",
                json.dumps(actor_summary.get("summary") or {}, ensure_ascii=False, sort_keys=True)[:6000],
                "",
            ]
        )
    if items:
        lines.extend(
            [
                "Participant-aware conversation memory (trusted runtime context; not new user input):",
                "Use only the items visible to this actor. Preserve owner and speaker attribution; never claim another participant's memory as your own.",
            ]
        )
    item_ids: list[str] = []
    for item in items:
        memory_id = str(item.get("memory_id") or "").strip()
        if memory_id:
            item_ids.append(memory_id)
        content = str(item.get("content") or "").strip()
        if len(content) > 700:
            content = content[:697].rstrip() + "..."
        lines.append(
            f"- [{memory_id or 'memory'}; owner={item.get('owner_kind')}:{item.get('owner_id')}; "
            f"kind={item.get('kind')}] {content}"
        )
    if conflicts:
        lines.extend(
            [
                "",
                "Explicit memory conflicts (do not silently choose one):",
                json.dumps(conflicts, ensure_ascii=False, sort_keys=True)[:6000],
            ]
        )
    return item_ids, "\n".join(lines).strip()


def activity_context_summary_text(
    db, *, activity_id: str, node_id: str = "", attempt_id: str = ""
) -> str:
    """Render retry/resume summaries without crossing Activity or Node scope."""
    memory_service = getattr(db, "conversation_memory", None)
    if memory_service is None or not activity_id:
        return ""
    parts: list[str] = []
    activity_summary = memory_service.latest_activity_summary(activity_id)
    if activity_summary:
        parts.extend(
            [
                "Activity context summary (trusted compressed projection for this Activity only):",
                json.dumps(activity_summary.get("summary") or {}, ensure_ascii=False, sort_keys=True)[:6000],
            ]
        )
    if node_id and attempt_id:
        node_summary = memory_service.latest_node_attempt_summary(
            activity_id, node_id, attempt_id
        )
        if node_summary:
            parts.extend(
                [
                    "Node attempt summary (trusted projection for this node attempt only; do not expose it to other nodes):",
                    json.dumps(node_summary.get("summary") or {}, ensure_ascii=False, sort_keys=True)[:6000],
                ]
            )
    return "\n\n".join(parts).strip()


def ensure_mission_activity_context_snapshot(
    db,
    *,
    mission: dict,
    conversation_session_id: str,
    leader_participant: str,
) -> dict:
    """Freeze the conversation inputs a Mission may consume implicitly."""
    memory_service = getattr(db, "conversation_memory", None)
    mission_id = str((mission or {}).get("mission_id") or "").strip()
    if memory_service is None or not mission_id or not conversation_session_id:
        return {}
    activity_id = f"mission:{mission_id}"
    existing = memory_service.latest_activity_snapshot(activity_id)
    if existing:
        return existing
    metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    visible = memory_service.list_visible(
        MemoryAccessContext(
            conversation_session_id=conversation_session_id,
            actor_participant_id=leader_participant,
            actor_role="leader",
            activity_id=activity_id,
        ),
        statuses=("committed",),
        limit=100,
    )
    shared_memory_ids = [
        str(item.get("memory_id") or "")
        for item in visible
        if item.get("owner_kind") == "conversation" and item.get("memory_id")
    ]
    try:
        return memory_service.create_activity_snapshot(
            conversation_session_id=conversation_session_id,
            activity_id=activity_id,
            objective=str(mission.get("objective") or mission.get("title") or "Team activity"),
            conversation_revision=memory_service.current_conversation_revision(
                conversation_session_id
            ),
            selected_memory_ids=shared_memory_ids,
            team_snapshot=metadata.get("team_capability_snapshot") or {},
            workspace_snapshot={
                "workspace_id": str(mission.get("workspace_id") or ""),
                "workspace_path": str(mission.get("workspace_path") or ""),
            },
            expected_revision=0,
        )
    except RuntimeError:
        return memory_service.latest_activity_snapshot(activity_id)


def team_memory_for_node(
    db, params: dict, mission: dict, node: dict, *, objective: str
) -> tuple[dict, str]:
    if team_memory_disabled(params, mission):
        return {"disabled": True, "reason": "disabled_by_request_or_policy"}, ""
    mission_id = str(mission.get("mission_id") or "").strip()
    node_id = str(node.get("node_id") or "").strip()
    if not mission_id or not node_id:
        return {}, ""
    memory_service = getattr(db, "conversation_memory", None)
    if memory_service is None:
        return {"disabled": True, "reason": "conversation_memory_unavailable"}, ""
    metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    conversation_session_id = str(
        mission.get("conversation_session_id")
        or metadata.get("conversation_session_id")
        or metadata.get("conversationTeamSessionId")
        or mission.get("leader_session_id")
        or ""
    ).strip()
    conversation_id = str(mission.get("conversation_id") or mission_id).strip()
    activity_id = f"mission:{mission_id}"
    role = node_role(node)
    node_metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    participant_id = (
        leader_participant_id(conversation_id)
        if role == "leader"
        else member_participant_id(
            str(node.get("member_id") or node_metadata.get("member_id") or node_id).strip()
        )
    )
    snapshot = ensure_mission_activity_context_snapshot(
        db,
        mission=mission,
        conversation_session_id=conversation_session_id,
        leader_participant=leader_participant_id(conversation_id),
    )
    frozen_shared_ids = set(snapshot.get("selected_memory_ids") or [])
    try:
        visible = memory_service.list_visible(
            MemoryAccessContext(
                conversation_session_id=conversation_session_id,
                actor_participant_id=participant_id,
                actor_role=role or "worker",
                activity_id=activity_id,
                node_id=node_id,
            ),
            statuses=("committed",),
            limit=max(1, min(int(params.get("memory_limit") or params.get("memoryLimit") or 12), 50)),
        )
    except Exception as exc:
        return {"disabled": True, "reason": f"memory_build_failed: {exc}"}, ""
    items = [
        item
        for item in visible
        if (
            item.get("owner_kind") == "activity"
            or (item.get("owner_kind") == "node" and item.get("owner_id") == node_id)
            or (
                item.get("owner_kind") == "conversation"
                and item.get("memory_id") in frozen_shared_ids
            )
        )
    ]
    base_payload = {
        "kind": "activity_memory_slice",
        "conversation_session_id": conversation_session_id,
        "item_ids": [
            str(item.get("memory_id") or "") for item in items if item.get("memory_id")
        ],
        "activity_context_snapshot_id": str(snapshot.get("snapshot_id") or ""),
    }
    if not items:
        return {**base_payload, "artifact_refs": []}, ""
    lines = [
        "Frozen Activity memory slice (trusted runtime context; not user input):",
        "Only this Activity, this Node, and Conversation memory selected by the immutable ActivitySnapshot are present.",
    ]
    artifact_refs: list[dict] = []
    for item in items:
        lines.append(
            f"- [{item.get('memory_id')}; owner={item.get('owner_kind')}:{item.get('owner_id')}; kind={item.get('kind')}] "
            f"{str(item.get('content') or '')[:1000]}"
        )
        payload = item.get("structured_payload") if isinstance(item.get("structured_payload"), dict) else {}
        artifact_refs.extend(
            dict(ref) for ref in payload.get("artifact_refs") or [] if isinstance(ref, dict)
        )
    return {**base_payload, "artifact_refs": artifact_refs}, "\n".join(lines)


__all__ = [
    "activity_context_summary_text",
    "actor_context_snapshot_fields",
    "actor_conversation_memory_text",
    "ensure_mission_activity_context_snapshot",
    "memory_context_text",
    "memory_items_from_payload",
    "team_memory_disabled",
    "team_memory_for_node",
    "team_memory_include_team_scope",
]
