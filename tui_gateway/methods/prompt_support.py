"""Pure prompt-submit normalization helpers shared by gateway methods."""

from __future__ import annotations

import hashlib
import sys
from typing import Any

from tui_gateway.services.prompt_attachments import submitted_attachments
from tui_gateway.services.prompt_attachments import submitted_image_paths


def attachment_path_helpers():
    from tui_gateway.services.attachment_paths import (
        IMAGE_EXTENSIONS,
        detect_file_drop,
        resolve_attachment_path,
        split_path_input,
    )

    cli_mod = sys.modules.get("cli")
    if cli_mod is not None:
        return (
            getattr(cli_mod, "_IMAGE_EXTENSIONS", IMAGE_EXTENSIONS),
            getattr(cli_mod, "_detect_file_drop", detect_file_drop),
            getattr(cli_mod, "_resolve_attachment_path", resolve_attachment_path),
            getattr(cli_mod, "_split_path_input", split_path_input),
        )
    return IMAGE_EXTENSIONS, detect_file_drop, resolve_attachment_path, split_path_input


def text_probe(value: Any) -> dict[str, Any]:
    text = str(value or "")
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return {
        "len": len(text),
        "sha1": digest,
        "preview": text[:80].replace("\n", "\\n"),
    }


def payload_text(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("delta", "text", "snapshot", "output", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def apply_dovie_product_runtime_policy(agent: Any, raw_context: Any) -> None:
    if agent is None or not isinstance(raw_context, dict):
        return
    team_mission = raw_context.get("team_mission") or raw_context.get("teamMission")
    team_mission = team_mission if isinstance(team_mission, dict) else {}
    setattr(
        agent,
        "_delegate_inherits_parent_tools",
        bool(
            team_mission.get("delegate_inherits_parent_tools")
            or team_mission.get("delegateInheritsParentTools")
        ),
    )


def prompt_terminal_status_from_result(result: dict, raw: Any) -> str:
    if result.get("interrupted"):
        return "interrupted"
    error = str(result.get("error") or "").strip()
    if not error:
        return "complete"
    raw_text = str(raw or "").strip()
    if not raw_text:
        return "error"
    if bool(result.get("failed")) and raw_text.lower().startswith(
        ("error:", "failed:", "exception:")
    ):
        return "error"
    return "complete"


def worker_bootstrap_model_is_preselected(
    session: dict,
    requested_model: str,
    _model_descriptor: dict,
) -> bool:
    """Return whether the control plane already selected this worker model."""
    from tui_gateway.process_role import is_worker_process

    if not is_worker_process() or session.get("agent") is not None:
        return False
    override = session.get("model_override")
    if not isinstance(override, dict):
        return False
    override_model = str(override.get("model") or "").strip()
    return bool(
        requested_model
        and override_model == requested_model
        and bool(override.get("model_explicit"))
    )


def turn_reasoning_config(params: dict) -> dict | None:
    raw = params.get("reasoning_config")
    if raw is None:
        raw = params.get("reasoningConfig")
    if not isinstance(raw, dict):
        return None
    config = dict(raw)
    if config.get("enabled") is False:
        return {"enabled": False}
    effort = str(config.get("effort") or "").strip()
    if effort:
        return {"effort": effort}
    return config or None


def turn_identity(metadata: dict | None) -> dict:
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        key: str(metadata.get(key) or "").strip()
        for key in ("run_id", "turn_id", "client_message_id")
        if str(metadata.get(key) or "").strip()
    }


def turn_matches(candidate: dict, target: dict) -> bool:
    if not candidate or not target:
        return False
    return any(
        candidate.get(key)
        and target.get(key)
        and candidate.get(key) == target.get(key)
        for key in ("run_id", "turn_id", "client_message_id")
    )


class MessageDeltaNormalizer:
    """Normalize string/snapshot callbacks into append-only text events."""

    def __init__(self) -> None:
        self.text = ""

    @staticmethod
    def _structured_value(value) -> dict:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _protocol_offset(value: str) -> int:
        return len(str(value or "").encode("utf-16-le")) // 2

    def feed(self, value) -> dict | None:
        if value is None:
            return None
        structured = self._structured_value(value)
        mode = str(structured.get("mode") or "").strip().lower()
        raw_value = (
            structured.get("delta")
            or structured.get("text")
            or structured.get("output")
            if structured
            else value
        )
        incoming = str(raw_value or "")
        if not incoming:
            return None
        if mode in {"snapshot", "replace", "cumulative"}:
            return self.feed_snapshot(incoming)
        offset = self._protocol_offset(self.text)
        self.text += incoming
        return {
            "mode": "append",
            "text": incoming,
            "delta": incoming,
            "offset": offset,
        }

    def feed_snapshot(self, value: str) -> dict | None:
        snapshot = str(value or "")
        if not snapshot or snapshot == self.text or not snapshot.startswith(self.text):
            return None
        delta = snapshot[len(self.text) :]
        if not delta:
            return None
        offset = self._protocol_offset(self.text)
        self.text = snapshot
        return {
            "mode": "append",
            "text": delta,
            "delta": delta,
            "offset": offset,
        }

    def reconcile_final_text(self, value: str) -> dict | None:
        return self.feed_snapshot(str(value or ""))

    def reset(self) -> None:
        self.text = ""


__all__ = [
    "MessageDeltaNormalizer",
    "apply_dovie_product_runtime_policy",
    "attachment_path_helpers",
    "payload_text",
    "prompt_terminal_status_from_result",
    "submitted_attachments",
    "submitted_image_paths",
    "text_probe",
    "turn_identity",
    "turn_matches",
    "turn_reasoning_config",
    "worker_bootstrap_model_is_preselected",
]
