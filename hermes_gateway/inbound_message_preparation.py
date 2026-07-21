"""Inbound message preparation for gateway agent turns."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional

from channels.platforms.base import MessageEvent, MessageType
from hermes_agent.gateway.runtime_config import (
    load_gateway_runtime_config,
    resolve_runtime_agent_kwargs,
)
from hermes_constants import get_hermes_home, get_hermes_home_override
from hermes_gateway.media_context import build_document_context_note
from hermes_gateway.session import SessionSource, is_shared_multi_user_session

logger = logging.getLogger(__name__)
_hermes_home = get_hermes_home()


def _runtime_home():
    override = get_hermes_home_override()
    return override or _hermes_home


class GatewayInboundMessagePreparationMixin:
    async def _prepare_inbound_message_text(
        self,
        *,
        event: MessageEvent,
        source: SessionSource,
        history: List[Dict[str, Any]],
    ) -> Optional[str]:
        """Prepare inbound event text for the agent."""
        history = history or []
        message_text = event.text or ""
        group_sessions_per_user = getattr(self.config, "group_sessions_per_user", True)
        thread_sessions_per_user = getattr(self.config, "thread_sessions_per_user", False)
        session_key = self._session_key_for_source(source)
        self._consume_pending_native_image_paths(session_key)

        if (
            is_shared_multi_user_session(
                source,
                group_sessions_per_user=group_sessions_per_user,
                thread_sessions_per_user=thread_sessions_per_user,
            )
            and source.user_name
        ):
            message_text = f"[{source.user_name}] {message_text}"

        if getattr(event, "channel_context", None):
            message_text = f"{event.channel_context}\n\n[New message]\n{message_text}"

        audio_file_paths: list[str] = []
        if event.media_urls:
            image_paths: list[str] = []
            audio_paths: list[str] = []
            for index, path in enumerate(event.media_urls):
                media_type = event.media_types[index] if index < len(event.media_types) else ""
                if media_type.startswith("image/") or event.message_type == MessageType.PHOTO:
                    image_paths.append(path)
                if event.message_type == MessageType.AUDIO:
                    audio_file_paths.append(path)
                elif event.message_type == MessageType.VOICE or (
                    media_type.startswith("audio/")
                    and event.message_type not in {MessageType.AUDIO, MessageType.DOCUMENT}
                ):
                    audio_paths.append(path)

            if image_paths:
                # Capability lookup may fetch models.dev metadata or probe a
                # local Ollama server. Keep that blocking I/O off the shared
                # gateway event loop so one image cannot stall every session.
                image_mode = await asyncio.to_thread(self._decide_image_input_mode)
                if image_mode == "native":
                    pending_native = getattr(self, "_pending_native_image_paths_by_session", None)
                    if pending_native is None:
                        pending_native = {}
                        self._pending_native_image_paths_by_session = pending_native
                    pending_native[session_key] = list(image_paths)
                    logger.info(
                        "Image routing: native (model supports vision). %d image(s) will be attached inline.",
                        len(image_paths),
                    )
                else:
                    logger.info(
                        "Image routing: text (mode=%s). Pre-analyzing %d image(s) via vision_analyze.",
                        image_mode,
                        len(image_paths),
                    )
                    message_text = await self._enrich_message_with_vision(message_text, image_paths)

            if audio_paths:
                message_text, transcripts = await self._transcribe_pending_audio_event_once(
                    event,
                    message_text,
                )
                adapter = self._adapter_for_source(source)
                metadata = self._thread_metadata_for_source(
                    source,
                    self._reply_anchor_for_event(event),
                )
                await self._echo_pending_stt_transcripts_once(
                    event,
                    adapter,
                    source,
                    transcripts,
                    metadata=metadata,
                    log_context="Transcript",
                )
                if _transcription_unavailable(message_text):
                    await self._notify_stt_unavailable(source, event)

        if audio_file_paths:
            from tools.credential_files import to_agent_visible_cache_path as to_agent_path

            for audio_path in audio_file_paths:
                display = _safe_media_display_name(audio_path)
                agent_path = to_agent_path(audio_path)
                note = (
                    f"[The user sent an audio file attachment: '{display}'. "
                    f"It is saved at: {agent_path}. "
                    f"Ask the user what they'd like you to do with it, or pass the path to a transcription or media tool.]"
                )
                message_text = f"{note}\n\n{message_text}"

        if event.media_urls and event.message_type == MessageType.DOCUMENT:
            message_text = _prepend_document_context(event, message_text)

        if getattr(event, "reply_to_text", None) and event.reply_to_message_id:
            reply_snippet = event.reply_to_text[:500]
            if getattr(event, "reply_to_is_own_message", False):
                message_text = (
                    f'[Replying to your previous message: "{reply_snippet}"]\n\n'
                    f"{message_text}"
                )
            else:
                message_text = f'[Replying to: "{reply_snippet}"]\n\n{message_text}'

        if "@" in message_text:
            expanded_text = await self._expand_context_references(message_text, source)
            if expanded_text is None:
                return None
            message_text = expanded_text

        return message_text

    async def _notify_stt_unavailable(self, source: SessionSource, event: MessageEvent) -> None:
        adapter = self._adapter_for_source(source)
        if not adapter:
            return
        metadata = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
        message = (
            "🎤 I received your voice message but can't transcribe it — "
            "no speech-to-text provider is configured.\n\n"
            "To enable voice: install faster-whisper "
            "(`pip install faster-whisper` in the Hermes venv) "
            "and set `stt.enabled: true` in config.yaml, "
            "then /restart the gateway."
        )
        if self._has_setup_skill():
            message += "\n\nFor full setup instructions, type: `/skill hermes-agent-setup`"
        try:
            await adapter.send(source.chat_id, message, metadata=metadata)
        except Exception as exc:
            logger.debug("Failed to send STT unavailable notice: %s", exc)

    async def _expand_context_references(
        self,
        message_text: str,
        source: SessionSource,
    ) -> Optional[str]:
        try:
            from agent.context_references import preprocess_context_references_async
            from agent.model_metadata import get_model_context_length

            cwd = os.environ.get("TERMINAL_CWD", os.path.expanduser("~"))
            runtime = resolve_runtime_agent_kwargs(_runtime_home())
            config_context_length = _configured_context_length()
            context_length = get_model_context_length(
                getattr(self, "_model", ""),
                base_url=getattr(self, "_base_url", None) or runtime.get("base_url") or "",
                api_key=runtime.get("api_key") or "",
                config_context_length=config_context_length,
                allow_network_discovery=False,
            )
            result = await preprocess_context_references_async(
                message_text,
                cwd=cwd,
                context_length=context_length,
                allowed_root=cwd,
            )
            if result.blocked:
                adapter = self._adapter_for_source(source)
                if adapter:
                    await adapter.send(
                        source.chat_id,
                        "\n".join(result.warnings) or "Context injection refused.",
                    )
                return None
            if result.expanded:
                return result.message
        except Exception as exc:
            logger.debug("@ context reference expansion failed: %s", exc)
        return message_text


def _transcription_unavailable(message_text: str) -> bool:
    markers = (
        "No STT provider",
        "STT is disabled",
        "can't listen",
        "VOICE_TOOLS_OPENAI_KEY",
    )
    return any(marker in message_text for marker in markers)


def _safe_media_display_name(path: str) -> str:
    basename = os.path.basename(path)
    parts = basename.split("_", 2)
    display = parts[2] if len(parts) >= 3 else basename
    return re.sub(r"[^\w.\- ]", "_", display)


def _prepend_document_context(event: MessageEvent, message_text: str) -> str:
    import mimetypes
    from tools.credential_files import to_agent_visible_cache_path

    text_extensions = {
        ".txt",
        ".md",
        ".csv",
        ".log",
        ".json",
        ".xml",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
    }
    for index, path in enumerate(event.media_urls):
        media_type = event.media_types[index] if index < len(event.media_types) else ""
        if media_type in {"", "application/octet-stream"}:
            extension = os.path.splitext(path)[1].lower()
            if extension in text_extensions:
                media_type = "text/plain"
            else:
                guessed, _ = mimetypes.guess_type(path)
                if guessed:
                    media_type = guessed
        if not media_type.startswith(("application/", "text/")):
            continue

        display_name = _safe_media_display_name(path)
        agent_path = to_agent_visible_cache_path(path)
        context_note = build_document_context_note(display_name, agent_path, media_type)
        message_text = f"{context_note}\n\n{message_text}"
    return message_text


def _configured_context_length() -> int | None:
    try:
        config = load_gateway_runtime_config(_runtime_home())
        model_config = config.get("model", {})
        if isinstance(model_config, dict):
            raw_context_length = model_config.get("context_length")
            if raw_context_length is not None:
                return int(raw_context_length)
    except Exception as exc:
        logger.debug("Failed to load configured context_length: %s", exc)
    return None
