"""Pure value normalization helpers shared by Team Mission gateway services."""

from __future__ import annotations


def truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def falsey(value) -> bool:
    if isinstance(value, bool):
        return not value
    if value is None:
        return False
    return str(value).strip().lower() in {"0", "false", "no", "off"}


def node_role(node: dict) -> str:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    return str(metadata.get("role") or node.get("kind") or "worker").strip()


def task_id_from_metadata(metadata: dict) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    active_task = (
        metadata.get("active_task")
        if isinstance(metadata.get("active_task"), dict)
        else {}
    )
    return str(
        metadata.get("task_id")
        or metadata.get("taskId")
        or metadata.get("active_task_id")
        or metadata.get("activeTaskId")
        or metadata.get("submitted_task_id")
        or metadata.get("submittedTaskId")
        or active_task.get("task_id")
        or active_task.get("taskId")
        or ""
    ).strip()


__all__ = ["falsey", "node_role", "task_id_from_metadata", "truthy"]
