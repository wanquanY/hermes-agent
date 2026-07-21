"""QQBot voice transcription and outbound media helpers."""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from agent.secret_scope import get_profile_env
from urllib.parse import urlparse

try:
    import aiohttp
except ImportError:  # pragma: no cover - optional dependency gate stays in adapter
    aiohttp = None  # type: ignore[assignment]

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency gate stays in adapter
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

from channels.platforms.base import (
    SendResult,
    _ssrf_redirect_guard,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from channels.platforms.qqbot.chunked_upload import (
    ChunkedUploader,
    UploadDailyLimitExceededError,
    UploadFileTooLargeError,
)
from channels.platforms.qqbot.constants import (
    API_BASE,
    DEFAULT_API_TIMEOUT,
    FILE_UPLOAD_TIMEOUT,
    MEDIA_TYPE_FILE,
    MEDIA_TYPE_IMAGE,
    MEDIA_TYPE_VIDEO,
    MEDIA_TYPE_VOICE,
    MSG_TYPE_INPUT_NOTIFY,
    MSG_TYPE_MARKDOWN,
    MSG_TYPE_MEDIA,
    MSG_TYPE_TEXT,
    RATE_LIMIT_DELAY,
)
from channels.platforms.qqbot.keyboards import (
    ApprovalRequest,
    InlineKeyboard,
    build_approval_keyboard,
    build_update_prompt_keyboard,
)

logger = logging.getLogger(__name__)


class QQMediaMixin:
    async def _stt_voice_attachment(
            self,
            url: str,
            content_type: str,
            filename: str,
            *,
            asr_refer_text: Optional[str] = None,
            voice_wav_url: Optional[str] = None,
    ) -> Optional[str]:
        """Download a voice attachment, convert to wav, and transcribe.

        Priority:
        1. QQ's built-in ``asr_refer_text`` (Tencent's own ASR — free, no API call).
        2. Self-hosted STT on ``voice_wav_url`` (pre-converted WAV from QQ, avoids SILK decoding).
        3. Self-hosted STT on the original attachment URL (requires SILK→WAV conversion).

        Returns the transcript text, or None on failure.
        """
        # 1. Use QQ's built-in ASR text if available
        if asr_refer_text:
            logger.debug(
                "[%s] STT: using QQ asr_refer_text: %r", self._log_tag, asr_refer_text[:100]
            )
            return asr_refer_text

        # Determine which URL to download (prefer voice_wav_url — already WAV)
        download_url = url
        is_pre_wav = False
        if voice_wav_url:
            if voice_wav_url.startswith("//"):
                voice_wav_url = f"https:{voice_wav_url}"
            download_url = voice_wav_url
            is_pre_wav = True
            logger.debug("[%s] STT: using voice_wav_url (pre-converted WAV)", self._log_tag)

        from tools.url_safety import is_safe_url
        if not is_safe_url(download_url):
            logger.warning("[QQ] STT blocked unsafe URL: %s", download_url[:80])
            return None

        try:
            # 2. Download audio (QQ CDN requires Authorization header)
            if not self._http_client:
                logger.warning("[%s] STT: no HTTP client", self._log_tag)
                return None

            download_headers = self._qq_media_headers()
            logger.debug(
                "[%s] STT: downloading voice from %s (pre_wav=%s, headers=%s)",
                self._log_tag,
                download_url[:80],
                is_pre_wav,
                bool(download_headers),
            )
            resp = await self._http_client.get(
                download_url,
                timeout=30.0,
                headers=download_headers,
                follow_redirects=True,
            )
            resp.raise_for_status()
            audio_data = resp.content
            logger.debug(
                "[%s] STT: downloaded %d bytes, content_type=%s",
                self._log_tag,
                len(audio_data),
                resp.headers.get("content-type", "unknown"),
            )

            if len(audio_data) < 10:
                logger.warning(
                    "[%s] STT: downloaded data too small (%d bytes), skipping",
                    self._log_tag,
                    len(audio_data),
                )
                return None

            # 3. Convert to wav (skip if we already have a pre-converted WAV)
            if is_pre_wav:
                import tempfile

                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp.write(audio_data)
                    wav_path = tmp.name
                logger.debug(
                    "[%s] STT: using pre-converted WAV directly (%d bytes)",
                    self._log_tag,
                    len(audio_data),
                )
            else:
                logger.debug(
                    "[%s] STT: converting to wav, filename=%r", self._log_tag, filename
                )
                wav_path = await self._convert_audio_to_wav_file(audio_data, filename)
                if not wav_path or not Path(wav_path).exists():
                    logger.warning(
                        "[%s] STT: ffmpeg conversion produced no output", self._log_tag
                    )
                    return None

            # 4. Call STT API
            logger.debug("[%s] STT: calling ASR on %s", self._log_tag, wav_path)
            transcript = await self._call_stt(wav_path)

            # 5. Cleanup temp file
            try:
                os.unlink(wav_path)
            except OSError:
                pass

            if transcript:
                logger.debug("[%s] STT success: %r", self._log_tag, transcript[:100])
            else:
                logger.warning("[%s] STT: ASR returned empty transcript", self._log_tag)
            return transcript
        except (httpx.HTTPStatusError, httpx.TransportError, IOError) as exc:
            logger.warning(
                "[%s] STT failed for voice attachment: %s: %s",
                self._log_tag,
                type(exc).__name__,
                exc,
            )
            return None

    async def _convert_audio_to_wav_file(
            self, audio_data: bytes, filename: str
    ) -> Optional[str]:
        """Convert audio bytes to a temp .wav file using pilk (SILK) or ffmpeg.

        QQ voice messages are typically SILK format which ffmpeg cannot decode.
        Strategy: always try pilk first, fall back to ffmpeg if pilk fails.

        Returns the wav file path, or None on failure.
        """
        import tempfile

        ext = (
            Path(filename).suffix.lower()
            if Path(filename).suffix
            else self._guess_ext_from_data(audio_data)
        )
        logger.info(
            "[%s] STT: audio_data size=%d, ext=%r, first_20_bytes=%r",
            self._log_tag,
            len(audio_data),
            ext,
            audio_data[:20],
        )

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_src:
            tmp_src.write(audio_data)
            src_path = tmp_src.name

        wav_path = src_path.rsplit(".", 1)[0] + ".wav"

        # Try pilk first (handles SILK and many other formats)
        result = await self._convert_silk_to_wav(src_path, wav_path)

        # If pilk failed, try ffmpeg
        if not result:
            result = await self._convert_ffmpeg_to_wav(src_path, wav_path)

        # If ffmpeg also failed, try writing raw PCM as WAV (last resort)
        if not result:
            result = await self._convert_raw_to_wav(audio_data, wav_path)

        # Cleanup source file
        try:
            os.unlink(src_path)
        except OSError:
            pass

        return result

    @staticmethod
    def _guess_ext_from_data(data: bytes) -> str:
        """Guess file extension from magic bytes."""
        if data[:9] == b"#!SILK_V3" or data[:5] == b"#!SILK":
            return ".silk"
        if data[:2] == b"\x02!":
            return ".silk"
        if data[:4] == b"RIFF":
            return ".wav"
        if data[:4] == b"fLaC":
            return ".flac"
        if data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
            return ".mp3"
        if data[:4] == b"\x30\x26\xb2\x75" or data[:4] == b"\x4f\x67\x67\x53":
            return ".ogg"
        if data[:4] == b"\x00\x00\x00\x20" or data[:4] == b"\x00\x00\x00\x1c":
            return ".amr"
        # Default to .amr for unknown (QQ's most common voice format)
        return ".amr"

    @staticmethod
    def _looks_like_silk(data: bytes) -> bool:
        """Check if bytes look like a SILK audio file."""
        return data[:4] == b"#!SILK" or data[:2] == b"\x02!" or data[:9] == b"#!SILK_V3"

    async def _convert_silk_to_wav(self, src_path: str, wav_path: str) -> Optional[str]:
        """Convert audio file to WAV using the pilk library.

        Tries the file as-is first, then as .silk if the extension differs.
        pilk can handle SILK files with various headers (or no header).
        """
        try:
            import pilk
        except ImportError:
            logger.warning(
                "[%s] pilk not installed — cannot decode SILK audio. Run: pip install pilk",
                self._log_tag,
            )
            return None

        # Try converting the file as-is
        try:
            pilk.silk_to_wav(src_path, wav_path, rate=16000)
            if Path(wav_path).exists() and Path(wav_path).stat().st_size > 44:
                logger.debug(
                    "[%s] pilk converted %s to wav (%d bytes)",
                    self._log_tag,
                    Path(src_path).name,
                    Path(wav_path).stat().st_size,
                )
                return wav_path
        except Exception as exc:
            logger.debug("[%s] pilk direct conversion failed: %s", self._log_tag, exc)

        # Try renaming to .silk and converting (pilk checks the extension)
        silk_path = src_path.rsplit(".", 1)[0] + ".silk"
        try:
            import shutil

            shutil.copy2(src_path, silk_path)
            pilk.silk_to_wav(silk_path, wav_path, rate=16000)
            if Path(wav_path).exists() and Path(wav_path).stat().st_size > 44:
                logger.debug(
                    "[%s] pilk converted %s (as .silk) to wav (%d bytes)",
                    self._log_tag,
                    Path(src_path).name,
                    Path(wav_path).stat().st_size,
                )
                return wav_path
        except Exception as exc:
            logger.debug("[%s] pilk .silk conversion failed: %s", self._log_tag, exc)
        finally:
            try:
                os.unlink(silk_path)
            except OSError:
                pass

        return None

    async def _convert_raw_to_wav(self, audio_data: bytes, wav_path: str) -> Optional[str]:
        """Last resort: try writing audio data as raw PCM 16-bit mono 16kHz WAV.

        This will produce garbage if the data isn't raw PCM, but at least
        the ASR engine won't crash — it'll just return empty.
        """
        try:
            import wave

            with wave.open(wav_path, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(audio_data)
            return wav_path
        except Exception as exc:
            logger.debug("[%s] raw PCM fallback failed: %s", self._log_tag, exc)
            return None

    async def _convert_ffmpeg_to_wav(self, src_path: str, wav_path: str) -> Optional[str]:
        """Convert audio file to WAV using ffmpeg."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                src_path,
                "-ar",
                "16000",
                "-ac",
                "1",
                wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
            if proc.returncode != 0:
                stderr = await proc.stderr.read() if proc.stderr else b""
                logger.warning(
                    "[%s] ffmpeg failed for %s: %s",
                    self._log_tag,
                    Path(src_path).name,
                    stderr[:200].decode(errors="replace"),
                )
                return None
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            logger.warning("[%s] ffmpeg conversion error: %s", self._log_tag, exc)
            return None

        if not Path(wav_path).exists() or Path(wav_path).stat().st_size <= 44:
            logger.warning(
                "[%s] ffmpeg produced no/small output for %s",
                self._log_tag,
                Path(src_path).name,
            )
            return None
        logger.debug(
            "[%s] ffmpeg converted %s to wav (%d bytes)",
            self._log_tag,
            Path(src_path).name,
            Path(wav_path).stat().st_size,
        )
        return wav_path

    def _resolve_stt_config(self) -> Optional[Dict[str, str]]:
        """Resolve STT backend configuration from config/environment.

        Priority:
        1. Plugin-specific: ``channels.qqbot.stt`` in config.yaml → ``self.config.extra["stt"]``
        2. QQ-specific env vars: ``QQ_STT_API_KEY`` / ``QQ_STT_BASE_URL`` / ``QQ_STT_MODEL``
        3. Return None if nothing is configured (STT will be skipped, QQ built-in ASR still works).
        """
        extra = self.config.extra or {}

        # 1. Plugin-specific STT config (matches OpenClaw's channels.qqbot.stt)
        stt_cfg = extra.get("stt")
        if isinstance(stt_cfg, dict) and stt_cfg.get("enabled") is not False:
            base_url = stt_cfg.get("baseUrl") or stt_cfg.get("base_url", "")
            api_key = stt_cfg.get("apiKey") or stt_cfg.get("api_key", "")
            model = stt_cfg.get("model", "")
            if base_url and api_key:
                return {
                    "base_url": base_url.rstrip("/"),
                    "api_key": api_key,
                    "model": model or "whisper-1",
                }
            # Provider-only config: just model name, use default provider
            if api_key:
                provider = stt_cfg.get("provider", "zai")
                # Map provider to base URL
                _PROVIDER_BASE_URLS = {
                    "zai": "https://open.bigmodel.cn/api/coding/paas/v4",
                    "openai": "https://api.openai.com/v1",
                    "glm": "https://open.bigmodel.cn/api/coding/paas/v4",
                }
                base_url = _PROVIDER_BASE_URLS.get(provider, "")
                if base_url:
                    return {
                        "base_url": base_url,
                        "api_key": api_key,
                        "model": model
                                 or ("glm-asr" if provider in {"zai", "glm"} else "whisper-1"),
                    }

        # 2. QQ-specific env vars (set by `hermes setup gateway` / `hermes gateway`)
        qq_stt_key = get_profile_env("QQ_STT_API_KEY", "")
        if qq_stt_key:
            base_url = get_profile_env(
                "QQ_STT_BASE_URL",
                "https://open.bigmodel.cn/api/coding/paas/v4",
            )
            model = get_profile_env("QQ_STT_MODEL", "glm-asr")
            return {
                "base_url": base_url.rstrip("/"),
                "api_key": qq_stt_key,
                "model": model,
            }

        return None

    async def _call_stt(self, wav_path: str) -> Optional[str]:
        """Call an OpenAI-compatible STT API to transcribe a wav file.

        Uses the provider configured in ``channels.qqbot.stt`` config,
        falling back to QQ's built-in ``asr_refer_text`` if not configured.
        Returns None if STT is not configured or the call fails.
        """
        stt_cfg = self._resolve_stt_config()
        if not stt_cfg:
            logger.warning(
                "[%s] STT not configured (no stt config or QQ_STT_API_KEY)",
                self._log_tag,
            )
            return None

        base_url = stt_cfg["base_url"]
        api_key = stt_cfg["api_key"]
        model = stt_cfg["model"]

        try:
            with open(wav_path, "rb") as f:
                resp = await self._http_client.post(
                    f"{base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (Path(wav_path).name, f, "audio/wav")},
                    data={"model": model},
                    timeout=30.0,
                )
            resp.raise_for_status()
            result = resp.json()
            # Zhipu/GLM format: {"choices": [{"message": {"content": "transcript text"}}]}
            choices = result.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                if content.strip():
                    return content.strip()
            # OpenAI/Whisper format: {"text": "transcript text"}
            text = result.get("text", "")
            if text.strip():
                return text.strip()
            return None
        except (httpx.HTTPStatusError, IOError) as exc:
            logger.warning(
                "[%s] STT API call failed (model=%s, base=%s): %s",
                self._log_tag,
                model,
                base_url[:50],
                exc,
            )
            return None

    async def _convert_audio_to_wav(
            self, audio_data: bytes, source_url: str
    ) -> Optional[str]:
        """Convert audio bytes to .wav using pilk (SILK) or ffmpeg, caching the result."""
        import tempfile

        # Determine source format from magic bytes or URL
        ext = (
            Path(urlparse(source_url).path).suffix.lower()
            if urlparse(source_url).path
            else ""
        )
        if not ext or ext not in {
                ".silk",
                ".amr",
                ".mp3",
                ".wav",
                ".ogg",
                ".m4a",
                ".aac",
                ".flac",
        }:
            ext = self._guess_ext_from_data(audio_data)

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_src:
            tmp_src.write(audio_data)
            src_path = tmp_src.name

        wav_path = src_path.rsplit(".", 1)[0] + ".wav"
        try:
            is_silk = ext == ".silk" or self._looks_like_silk(audio_data)
            if is_silk:
                result = await self._convert_silk_to_wav(src_path, wav_path)
            else:
                result = await self._convert_ffmpeg_to_wav(src_path, wav_path)

            if not result:
                logger.warning(
                    "[%s] audio conversion failed for %s (format=%s)",
                    self._log_tag,
                    source_url[:60],
                    ext,
                )
                return cache_document_from_bytes(audio_data, f"qq_voice{ext}")
        except Exception:
            return cache_document_from_bytes(audio_data, f"qq_voice{ext}")
        finally:
            try:
                os.unlink(src_path)
            except OSError:
                pass

        # Verify output and cache
        try:
            wav_data = Path(wav_path).read_bytes()
            os.unlink(wav_path)
            return cache_document_from_bytes(wav_data, "qq_voice.wav")
        except Exception as exc:
            logger.debug("[%s] Failed to read converted wav: %s", self._log_tag, exc)
            return None

    # ------------------------------------------------------------------
    # Outbound messaging — REST API
    # ------------------------------------------------------------------

    async def _api_request(
            self,
            method: str,
            path: str,
            body: Optional[Dict[str, Any]] = None,
            timeout: float = DEFAULT_API_TIMEOUT,
    ) -> Dict[str, Any]:
        """Make an authenticated REST API request to QQ Bot API."""
        if not self._http_client:
            raise RuntimeError("HTTP client not initialized — not connected?")

        token = await self._ensure_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "User-Agent": build_user_agent(),
        }

        try:
            resp = await self._http_client.request(
                method,
                f"{API_BASE}{path}",
                headers=headers,
                json=body,
                timeout=timeout,
            )
            data = resp.json()
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"QQ Bot API error [{resp.status_code}] {path}: "
                    f"{data.get('message', data)}"
                )
            return data
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"QQ Bot API timeout [{path}]: {exc}") from exc

    async def _upload_media(
            self,
            target_type: str,
            target_id: str,
            file_type: int,
            url: Optional[str] = None,
            file_data: Optional[str] = None,
            srv_send_msg: bool = False,
            file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload media and return file_info."""
        path = (
            f"/v2/users/{target_id}/files"
            if target_type == "c2c"
            else f"/v2/groups/{target_id}/files"
        )

        body: Dict[str, Any] = {
            "file_type": file_type,
            "srv_send_msg": srv_send_msg,
        }
        if url:
            body["url"] = url
        elif file_data:
            body["file_data"] = file_data
        if file_type == MEDIA_TYPE_FILE and file_name:
            body["file_name"] = file_name

        # Retry transient upload failures
        for attempt in range(3):
            try:
                return await self._api_request(
                    "POST", path, body, timeout=FILE_UPLOAD_TIMEOUT
                )
            except RuntimeError as exc:
                err_msg = str(exc)
                if any(
                        kw in err_msg
                        for kw in ("400", "401", "Invalid", "timeout", "Timeout")
                ):
                    raise
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                else:
                    raise

    # Maximum time (seconds) to wait for reconnection before giving up on send.
    _RECONNECT_WAIT_SECONDS = 15.0
    # How often (seconds) to poll is_connected while waiting.
    _RECONNECT_POLL_INTERVAL = 0.5

    async def _wait_for_reconnection(self) -> bool:
        """Wait for the WebSocket listener to reconnect.

        The listener loop (_listen_loop) auto-reconnects on disconnect, but
        there is a race window where send() is called right after a disconnect
        and before the reconnect completes.  This method polls is_connected
        for up to _RECONNECT_WAIT_SECONDS.

        Returns True if reconnected, False if still disconnected.
        """
        logger.info("[%s] Not connected — waiting for reconnection (up to %.0fs)",
                    self._log_tag, self._RECONNECT_WAIT_SECONDS)
        waited = 0.0
        while waited < self._RECONNECT_WAIT_SECONDS:
            await asyncio.sleep(self._RECONNECT_POLL_INTERVAL)
            waited += self._RECONNECT_POLL_INTERVAL
            if self.is_connected:
                logger.info("[%s] Reconnected after %.1fs", self._log_tag, waited)
                return True
        logger.warning("[%s] Still not connected after %.0fs", self._log_tag, self._RECONNECT_WAIT_SECONDS)
        return False

    async def send(
            self,
            chat_id: str,
            content: str,
            reply_to: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text or markdown message to a QQ user or group.

        Applies format_message(), splits long messages via truncate_message(),
        and retries transient failures with exponential backoff.
        """
        del metadata

        if not self.is_connected:
            if not await self._wait_for_reconnection():
                return SendResult(success=False, error="Not connected", retryable=True)

        if not content or not content.strip():
            return SendResult(success=True)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)

        last_result = SendResult(success=False, error="No chunks")
        for chunk in chunks:
            last_result = await self._send_chunk(chat_id, chunk, reply_to)
            if not last_result.success:
                return last_result
            # Only reply_to the first chunk
            reply_to = None
        return last_result

    async def _send_chunk(
            self,
            chat_id: str,
            content: str,
            reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send a single chunk with retry + exponential backoff."""
        last_exc: Optional[Exception] = None
        chat_type = self._guess_chat_type(chat_id)

        for attempt in range(3):
            try:
                if chat_type == "c2c":
                    return await self._send_c2c_text(chat_id, content, reply_to)
                elif chat_type == "group":
                    return await self._send_group_text(chat_id, content, reply_to)
                elif chat_type == "guild":
                    return await self._send_guild_text(chat_id, content, reply_to)
                else:
                    return SendResult(
                        success=False, error=f"Unknown chat type for {chat_id}"
                    )
            except Exception as exc:
                last_exc = exc
                err = str(exc).lower()
                # Permanent errors — don't retry
                if any(
                        k in err
                        for k in ("invalid", "forbidden", "not found", "bad request")
                ):
                    break
                # Transient — back off and retry
                if attempt < 2:
                    delay = 1.0 * (2 ** attempt)
                    logger.warning(
                        "[%s] send retry %d/3 after %.1fs: %s",
                        self._log_tag,
                        attempt + 1,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

        error_msg = str(last_exc) if last_exc else "Unknown error"
        logger.error("[%s] Send failed: %s", self._log_tag, error_msg)
        retryable = not any(
            k in error_msg.lower() for k in ("invalid", "forbidden", "not found")
        )
        return SendResult(success=False, error=error_msg, retryable=retryable)

    async def _send_c2c_text(
            self,
            openid: str,
            content: str,
            reply_to: Optional[str] = None,
            keyboard: Optional[InlineKeyboard] = None,
    ) -> SendResult:
        """Send text to a C2C user via REST API.

        :param keyboard: Optional inline keyboard attached to the message.
        """
        self._next_msg_seq(reply_to or openid)
        body = self._build_text_body(content, reply_to)
        if reply_to:
            body["msg_id"] = reply_to
        if keyboard is not None:
            body["keyboard"] = keyboard.to_dict()

        data = await self._api_request("POST", f"/v2/users/{openid}/messages", body)
        msg_id = str(data.get("id", uuid.uuid4().hex[:12]))
        return SendResult(success=True, message_id=msg_id, raw_response=data)

    async def _send_group_text(
            self,
            group_openid: str,
            content: str,
            reply_to: Optional[str] = None,
            keyboard: Optional[InlineKeyboard] = None,
    ) -> SendResult:
        """Send text to a group via REST API.

        :param keyboard: Optional inline keyboard attached to the message.
        """
        self._next_msg_seq(reply_to or group_openid)
        body = self._build_text_body(content, reply_to)
        if reply_to:
            body["msg_id"] = reply_to
        if keyboard is not None:
            body["keyboard"] = keyboard.to_dict()

        data = await self._api_request(
            "POST", f"/v2/groups/{group_openid}/messages", body
        )
        msg_id = str(data.get("id", uuid.uuid4().hex[:12]))
        return SendResult(success=True, message_id=msg_id, raw_response=data)

    async def _send_guild_text(
            self, channel_id: str, content: str, reply_to: Optional[str] = None
    ) -> SendResult:
        """Send text to a guild channel via REST API."""
        body: Dict[str, Any] = {"content": content[: self.MAX_MESSAGE_LENGTH]}
        if reply_to:
            body["msg_id"] = reply_to

        data = await self._api_request("POST", f"/channels/{channel_id}/messages", body)
        msg_id = str(data.get("id", uuid.uuid4().hex[:12]))
        return SendResult(success=True, message_id=msg_id, raw_response=data)

    # ------------------------------------------------------------------
    # Inline-keyboard outbound helpers (approval / update-prompt flows)
    # ------------------------------------------------------------------

    async def send_with_keyboard(
            self,
            chat_id: str,
            content: str,
            keyboard: InlineKeyboard,
            reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send a single text message with an inline keyboard attached.

        Unlike :meth:`send`, this does NOT split long content into chunks —
        a keyboard message has exactly one interactive surface, and splitting
        would orphan the buttons from the first chunk. Callers should keep
        approval/update-prompt bodies short.

        Guild (channel) chats don't support inline keyboards; returns a
        non-retryable failure for those.
        """
        if not self.is_connected:
            if not await self._wait_for_reconnection():
                return SendResult(
                    success=False, error="Not connected", retryable=True
                )

        chat_type = self._guess_chat_type(chat_id)
        formatted = self.format_message(content)
        truncated = formatted[: self.MAX_MESSAGE_LENGTH]
        try:
            if chat_type == "c2c":
                return await self._send_c2c_text(
                    chat_id, truncated, reply_to, keyboard=keyboard,
                )
            if chat_type == "group":
                return await self._send_group_text(
                    chat_id, truncated, reply_to, keyboard=keyboard,
                )
            return SendResult(
                success=False,
                error=(
                    f"Inline keyboards not supported for chat_type "
                    f"{chat_type!r}"
                ),
                retryable=False,
            )
        except Exception as exc:
            logger.error(
                "[%s] send_with_keyboard failed: %s", self._log_tag, exc
            )
            return SendResult(success=False, error=str(exc))

    async def send_approval_request(
            self,
            chat_id: str,
            req: ApprovalRequest,
            reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send a 3-button approval request (``allow-once / allow-always / deny``).

        The rendered text comes from :func:`build_approval_text`; callers can
        override by passing a custom :class:`ApprovalRequest`.

        Users click the button → ``INTERACTION_CREATE`` fires → the adapter's
        registered :meth:`set_interaction_callback` handler decodes
        ``button_data`` via :func:`parse_approval_button_data`.
        """
        from channels.platforms.qqbot.keyboards import build_approval_text
        return await self.send_with_keyboard(
            chat_id,
            build_approval_text(req),
            build_approval_keyboard(req.session_key),
            reply_to=reply_to,
        )

    # ------------------------------------------------------------------
    # Cross-adapter gateway contract — send_exec_approval + send_update_prompt
    # ------------------------------------------------------------------
    #
    # These mirror the signatures that gateway/run.py detects on the adapter
    # class (e.g. type(adapter).send_exec_approval, type(adapter).send_update_prompt)
    # for button-based approval / update-confirm UX. Discord, Telegram, Slack,
    # Matrix, and Feishu already implement the same contract.

    async def send_exec_approval(
            self,
            chat_id: str,
            command: str,
            session_key: str,
            description: str = "dangerous command",
            metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a button-based exec-approval prompt for a dangerous command.

        Called by ``gateway/run.py``'s ``_approval_notify_sync`` when the
        agent is blocked waiting for approval. Button clicks resolve via
        :func:`tools.approval.resolve_gateway_approval` — dispatched by the
        adapter's interaction callback (:meth:`_default_interaction_dispatch`).
        """
        del metadata  # QQ doesn't have thread_id / DM targeting overrides.

        # Use the reply-to message for passive-message context when we have one.
        # QQ requires a msg_id on outbound messages to a user we've never
        # seen; the last inbound msg_id is the natural choice.
        msg_id = self._last_msg_id.get(chat_id)

        req = ApprovalRequest(
            session_key=session_key,
            title=f"Execute this command?",
            description=description,
            command_preview=command,
            timeout_sec=self._APPROVAL_TIMEOUT_SECONDS,
        )
        return await self.send_approval_request(
            chat_id, req, reply_to=msg_id,
        )

    _APPROVAL_TIMEOUT_SECONDS = 300  # matches gateway's default gateway_timeout

    async def send_update_prompt(
            self,
            chat_id: str,
            prompt: str,
            default: str = "",
            session_key: str = "",
            metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a Yes/No update-confirmation prompt with inline buttons.

        Matches the cross-adapter contract used by
        ``gateway/run.py``'s ``hermes update --gateway`` watcher. Button
        clicks surface as ``INTERACTION_CREATE`` with
        ``button_data = 'update_prompt:y'`` or ``'update_prompt:n'``;
        the adapter's interaction callback writes the answer to
        ``~/.hermes/.update_response`` so the detached update process
        can read it.
        """
        del session_key, metadata  # present for contract parity only.

        default_hint = f" (default: {default})" if default else ""
        content = f"⚕ **Update Needs Your Input**\n\n{prompt}{default_hint}"
        msg_id = self._last_msg_id.get(chat_id)
        return await self.send_with_keyboard(
            chat_id,
            content,
            build_update_prompt_keyboard(),
            reply_to=msg_id,
        )

    def _build_text_body(
            self, content: str, reply_to: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build the message body for C2C/group text sending."""
        msg_seq = self._next_msg_seq(reply_to or "default")

        if self._markdown_support:
            body: Dict[str, Any] = {
                "markdown": {"content": content[: self.MAX_MESSAGE_LENGTH]},
                "msg_type": MSG_TYPE_MARKDOWN,
                "msg_seq": msg_seq,
            }
        else:
            body = {
                "content": content[: self.MAX_MESSAGE_LENGTH],
                "msg_type": MSG_TYPE_TEXT,
                "msg_seq": msg_seq,
            }

        if reply_to:
            # For non-markdown mode, add message_reference
            if not self._markdown_support:
                body["message_reference"] = {"message_id": reply_to}

        return body

    # ------------------------------------------------------------------
    # Native media sending
    # ------------------------------------------------------------------

    async def send_image(
            self,
            chat_id: str,
            image_url: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image natively via QQ Bot API upload."""
        del metadata

        result = await self._send_media(
            chat_id, image_url, MEDIA_TYPE_IMAGE, "image", caption, reply_to
        )
        if result.success or not self._is_url(image_url):
            return result

        # Fallback to text URL
        logger.warning(
            "[%s] Image send failed, falling back to text: %s",
            self._log_tag,
            result.error,
        )
        fallback = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id=chat_id, content=fallback, reply_to=reply_to)

    async def send_image_file(
            self,
            chat_id: str,
            image_path: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            **kwargs,
    ) -> SendResult:
        """Send a local image file natively."""
        del kwargs
        return await self._send_media(
            chat_id, image_path, MEDIA_TYPE_IMAGE, "image", caption, reply_to
        )

    async def send_voice(
            self,
            chat_id: str,
            audio_path: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            **kwargs,
    ) -> SendResult:
        """Send a voice message natively."""
        del kwargs
        return await self._send_media(
            chat_id, audio_path, MEDIA_TYPE_VOICE, "voice", caption, reply_to
        )

    async def send_video(
            self,
            chat_id: str,
            video_path: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            **kwargs,
    ) -> SendResult:
        """Send a video natively."""
        del kwargs
        return await self._send_media(
            chat_id, video_path, MEDIA_TYPE_VIDEO, "video", caption, reply_to
        )

    async def send_document(
            self,
            chat_id: str,
            file_path: str,
            caption: Optional[str] = None,
            file_name: Optional[str] = None,
            reply_to: Optional[str] = None,
            **kwargs,
    ) -> SendResult:
        """Send a file/document natively."""
        del kwargs
        return await self._send_media(
            chat_id,
            file_path,
            MEDIA_TYPE_FILE,
            "file",
            caption,
            reply_to,
            file_name=file_name,
        )

    async def _send_media(
            self,
            chat_id: str,
            media_source: str,
            file_type: int,
            kind: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            file_name: Optional[str] = None,
    ) -> SendResult:
        """Upload media and send as a native message.

        Upload strategy:

        - **HTTP(S) URLs** → single ``POST /v2/{users|groups}/{id}/files``
          with ``url=...``. The QQ platform fetches the URL directly; fastest
          path when the source is already hosted.
        - **Local files** → three-step chunked upload (prepare / PUT parts /
          complete). Handles files up to the platform's ~100 MB per-file
          limit without the ~10 MB inline-base64 cap of the old adapter.
        """
        if not self.is_connected:
            if not await self._wait_for_reconnection():
                return SendResult(success=False, error="Not connected", retryable=True)

        chat_type = self._guess_chat_type(chat_id)
        if chat_type == "guild":
            # Guild channels don't support native media upload in the same way.
            return SendResult(
                success=False,
                error="Guild media send not supported via this path",
            )

        try:
            if self._is_url(media_source):
                # URL upload — let the platform fetch it directly.
                resolved_name = (
                    file_name
                    or Path(urlparse(media_source).path).name
                    or "media"
                )
                upload = await self._upload_media(
                    chat_type,
                    chat_id,
                    file_type,
                    url=media_source,
                    srv_send_msg=False,
                    file_name=resolved_name if file_type == MEDIA_TYPE_FILE else None,
                )
            else:
                # Local file — chunked upload (prepare / PUT parts / complete).
                resolved_name, upload = await self._upload_local_file(
                    chat_type,
                    chat_id,
                    media_source,
                    file_type,
                    file_name,
                )

            file_info = upload.get("file_info") or (
                upload.get("data", {}) or {}
            ).get("file_info")
            if not file_info:
                return SendResult(
                    success=False,
                    error=f"Upload returned no file_info: {upload}",
                )

            # Send media message
            msg_seq = self._next_msg_seq(chat_id)
            body: Dict[str, Any] = {
                "msg_type": MSG_TYPE_MEDIA,
                "media": {"file_info": file_info},
                "msg_seq": msg_seq,
            }
            if caption:
                body["content"] = caption[: self.MAX_MESSAGE_LENGTH]
            if reply_to:
                body["msg_id"] = reply_to

            send_data = await self._api_request(
                "POST",
                (
                    f"/v2/users/{chat_id}/messages"
                    if chat_type == "c2c"
                    else f"/v2/groups/{chat_id}/messages"
                ),
                body,
            )
            return SendResult(
                success=True,
                message_id=str(send_data.get("id", uuid.uuid4().hex[:12])),
                raw_response=send_data,
            )
        except UploadDailyLimitExceededError as exc:
            # Non-retryable: daily quota hit. Give the caller actionable text
            # so the model can compose a helpful reply.
            logger.warning(
                "[%s] Daily upload limit exceeded for %s (%s)",
                self._log_tag, exc.file_name, exc.file_size_human,
            )
            return SendResult(
                success=False,
                error=(
                    f"QQ daily upload limit exceeded for {exc.file_name!r} "
                    f"({exc.file_size_human}). Retry tomorrow."
                ),
                retryable=False,
            )
        except UploadFileTooLargeError as exc:
            logger.warning(
                "[%s] File too large: %s (%s, platform limit %s)",
                self._log_tag, exc.file_name, exc.file_size_human, exc.limit_human,
            )
            return SendResult(
                success=False,
                error=(
                    f"{exc.file_name!r} ({exc.file_size_human}) exceeds the "
                    f"QQ per-file upload limit ({exc.limit_human})."
                ),
                retryable=False,
            )
        except Exception as exc:
            logger.error("[%s] Media send failed: %s", self._log_tag, exc)
            return SendResult(success=False, error=str(exc))

    async def _upload_local_file(
            self,
            chat_type: str,
            chat_id: str,
            media_source: str,
            file_type: int,
            file_name: Optional[str],
    ) -> Tuple[str, Dict[str, Any]]:
        """Chunked-upload a local file and return ``(resolved_name, complete_response)``.

        The returned ``complete_response`` contains the ``file_info`` token
        that goes into the subsequent RichMedia message body.

        :raises UploadDailyLimitExceededError: On biz_code 40093002.
        :raises UploadFileTooLargeError: When the file exceeds the platform limit.
        :raises FileNotFoundError: If the path does not exist.
        :raises ValueError: If the path looks like a placeholder (``<path>``).
        :raises RuntimeError: If the HTTP client is not initialized.
        """
        if not self._http_client:
            raise RuntimeError("HTTP client not initialized — not connected?")

        local_path = Path(media_source).expanduser()
        if not local_path.is_absolute():
            local_path = (Path.cwd() / local_path).resolve()

        if not local_path.exists() or not local_path.is_file():
            if media_source.startswith("<") or len(media_source) < 3:
                raise ValueError(
                    f"Invalid media source (looks like a placeholder): {media_source!r}"
                )
            raise FileNotFoundError(f"Media file not found: {local_path}")

        resolved_name = file_name or local_path.name
        uploader = ChunkedUploader(
            api_request=self._api_request,
            http_put=self._http_client.put,
            log_tag=self._log_tag,
        )
        complete = await uploader.upload(
            chat_type=chat_type,
            target_id=chat_id,
            file_path=str(local_path),
            file_type=file_type,
            file_name=resolved_name,
        )
        return resolved_name, complete

    async def _load_media(
            self, source: str, file_name: Optional[str] = None
    ) -> Tuple[str, str, str]:
        """Load media from URL or local path. Returns (base64_or_url, content_type, filename)."""
        source = str(source).strip()
        if not source:
            raise ValueError("Media source is required")

        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            # For URLs, pass through directly to the upload API
            content_type = mimetypes.guess_type(source)[0] or "application/octet-stream"
            resolved_name = file_name or Path(parsed.path).name or "media"
            return source, content_type, resolved_name

        # Local file — encode as raw base64 for QQ Bot API file_data field.
        # The QQ API expects plain base64, NOT a data URI.
        local_path = Path(source).expanduser()
        if not local_path.is_absolute():
            local_path = (Path.cwd() / local_path).resolve()

        if not local_path.exists() or not local_path.is_file():
            # Guard against placeholder paths like "<path>" that the LLM
            # sometimes emits instead of real file paths.
            if source.startswith("<") or len(source) < 3:
                raise ValueError(
                    f"Invalid media source (looks like a placeholder): {source!r}"
                )
            raise FileNotFoundError(f"Media file not found: {local_path}")

        raw = local_path.read_bytes()
        resolved_name = file_name or local_path.name
        content_type = (
                mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
        )
        b64 = base64.b64encode(raw).decode("ascii")
        return b64, content_type, resolved_name
