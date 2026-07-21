"""Inbound media enrichment for gateway messages."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import List, Optional

from channels.platforms.base import MessageType

from hermes_gateway.media_context import probe_audio_duration

logger = logging.getLogger(__name__)


class GatewayInboundMediaMixin:
    async def _enrich_message_with_vision(
        self,
        user_text: str,
        image_paths: List[str],
    ) -> str:
        """
        Auto-analyze user-attached images with the vision tool and prepend
        the descriptions to the message text.

        Each image is analyzed with a general-purpose prompt.  The resulting
        description *and* the local cache path are injected so the model can:
          1. Immediately understand what the user sent (no extra tool call).
          2. Re-examine the image with vision_analyze if it needs more detail.

        Args:
            user_text:   The user's original caption / message text.
            image_paths: List of local file paths to cached images.

        Returns:
            The enriched message string with vision descriptions prepended.
        """
        from tools.vision_tools import vision_analyze_tool
        from agent.memory_manager import sanitize_context

        analysis_prompt = (
            "Describe everything visible in this image in thorough detail. "
            "Include any text, code, data, objects, people, layout, colors, "
            "and any other notable visual information."
        )

        enriched_parts = []
        for path in image_paths:
            try:
                logger.debug("Auto-analyzing user image: %s", path)
                result_json = await vision_analyze_tool(
                    image_url=path,
                    user_prompt=analysis_prompt,
                )
                result = json.loads(result_json)
                if result.get("success"):
                    description = result.get("analysis", "")
                    description = sanitize_context(description)
                    enriched_parts.append(
                        f"[The user sent an image~ Here's what I can see:\n{description}]\n"
                        f"[If you need a closer look, use vision_analyze with "
                        f"image_url: {path} ~]"
                    )
                else:
                    enriched_parts.append(
                        "[The user sent an image but I couldn't quite see it "
                        "this time (>_<) You can try looking at it yourself "
                        f"with vision_analyze using image_url: {path}]"
                    )
            except Exception as e:
                logger.error("Vision auto-analysis error: %s", e)
                enriched_parts.append(
                    f"[The user sent an image but something went wrong when I "
                    f"tried to look at it~ You can try examining it yourself "
                    f"with vision_analyze using image_url: {path}]"
                )

        # Combine: vision descriptions first, then the user's original text
        if enriched_parts:
            prefix = "\n\n".join(enriched_parts)
            if user_text:
                return f"{prefix}\n\n{user_text}"
            return prefix
        return user_text
    async def _enrich_message_with_transcription(
        self,
        user_text: str,
        audio_paths: List[str],
    ) -> tuple[str, List[str]]:
        """
        Auto-transcribe user voice/audio messages using the configured STT provider
        and prepend the transcript to the message text.

        Args:
            user_text:   The user's original caption / message text.
            audio_paths: List of local file paths to cached audio files.

        Returns:
            The enriched message string with transcriptions prepended.
        """
        if not getattr(self.config, "stt_enabled", True):
            notes = []
            for path in audio_paths:
                abs_path = os.path.abspath(path)
                duration_str = await probe_audio_duration(abs_path)
                if duration_str:
                    notes.append(
                        f"[The user sent a voice message: {abs_path} (duration: {duration_str})]"
                    )
                else:
                    notes.append(f"[The user sent a voice message: {abs_path}]")
            if not notes:
                return user_text, []
            prefix = "\n\n".join(notes)
            _placeholder = "(The user sent a message with no text content)"
            if user_text and user_text.strip() == _placeholder:
                return prefix, []
            if user_text:
                return f"{prefix}\n\n{user_text}", []
            return prefix, []

        from tools.transcription_tools import transcribe_audio

        enriched_parts = []
        successful_transcripts: List[str] = []
        for path in audio_paths:
            try:
                logger.debug("Transcribing user voice: %s", path)
                result = await asyncio.to_thread(transcribe_audio, path)
                if result["success"]:
                    transcript = result["transcript"]
                    successful_transcripts.append(transcript)
                    enriched_parts.append(f'"{transcript}"')
                else:
                    error = result.get("error", "unknown error")
                    logger.info("Voice transcription failed for %s: %s", path, error)
                    enriched_parts.append("[voice message could not be transcribed]")
            except Exception as e:
                logger.error("Transcription error: %s", e)
                enriched_parts.append("[voice message could not be transcribed]")

        if enriched_parts:
            prefix = "\n\n".join(enriched_parts)
            # Strip the empty-content placeholder from the Discord adapter
            # when we successfully transcribed the audio — it's redundant.
            _placeholder = "(The user sent a message with no text content)"
            if user_text and user_text.strip() == _placeholder:
                return prefix, successful_transcripts
            if user_text:
                return f"{prefix}\n\n{user_text}", successful_transcripts
            return prefix, successful_transcripts
        return user_text, successful_transcripts

    @staticmethod
    def _event_media_is_stt_input(event, index: int) -> bool:
        message_type = getattr(event, "message_type", None)
        if message_type in {MessageType.AUDIO, MessageType.DOCUMENT}:
            return False
        media_types = getattr(event, "media_types", None) or []
        media_type = media_types[index] if index < len(media_types) else ""
        return message_type == MessageType.VOICE or str(media_type).startswith("audio/")

    def _pending_event_audio_paths(self, event) -> List[str]:
        return [
            path
            for index, path in enumerate(getattr(event, "media_urls", None) or [])
            if self._event_media_is_stt_input(event, index)
        ]

    async def _transcribe_pending_audio_event_once(
        self,
        event,
        user_text: Optional[str] = None,
    ) -> tuple[str | None, List[str]]:
        if hasattr(event, "_gateway_pending_stt_text"):
            return (
                getattr(event, "_gateway_pending_stt_text"),
                list(getattr(event, "_gateway_pending_stt_transcripts", []) or []),
            )
        audio_paths = self._pending_event_audio_paths(event)
        if not audio_paths:
            text = user_text if user_text is not None else getattr(event, "text", None)
            return text or "", []
        text = user_text if user_text is not None else (getattr(event, "text", "") or "")
        enriched, transcripts = await self._enrich_message_with_transcription(
            text,
            audio_paths,
        )
        setattr(event, "_gateway_pending_stt_text", enriched)
        setattr(event, "_gateway_pending_stt_transcripts", list(transcripts))
        return enriched, transcripts

    def _should_echo_stt_transcripts(self) -> bool:
        return bool(getattr(self.config, "stt_echo_transcripts", True))

    async def _echo_pending_stt_transcripts_once(
        self,
        event,
        adapter,
        source,
        transcripts: List[str],
        *,
        metadata=None,
        log_context: str = "Transcript",
    ) -> None:
        if (
            not transcripts
            or not self._should_echo_stt_transcripts()
            or adapter is None
            or getattr(event, "_gateway_pending_stt_echo_sent", False)
        ):
            return
        setattr(event, "_gateway_pending_stt_echo_sent", True)
        for transcript in transcripts:
            try:
                await adapter.send(
                    source.chat_id,
                    f'🎙️ "{transcript}"',
                    metadata=metadata,
                )
            except Exception as error:
                logger.debug("%s echo failed (non-fatal): %s", log_context, error)

    async def _transcribe_and_echo_pending_voice(
        self,
        event,
        adapter,
        source,
        text: str,
        *,
        log_context: str,
        metadata=None,
    ) -> tuple[str, List[str]]:
        if not self._pending_event_audio_paths(event):
            return text, []
        enriched, transcripts = await self._transcribe_pending_audio_event_once(
            event,
            text,
        )
        await self._echo_pending_stt_transcripts_once(
            event,
            adapter,
            source,
            transcripts,
            metadata=metadata,
            log_context=log_context,
        )
        return enriched or text, transcripts
