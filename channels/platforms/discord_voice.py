from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

try:
    import discord
    DISCORD_AVAILABLE = True
except ImportError:  # pragma: no cover - optional platform dependency
    discord = None
    DISCORD_AVAILABLE = False

logger = logging.getLogger(__name__)


def _discord_public_attr(name: str, fallback: Any = None) -> Any:
    import sys

    public_module = sys.modules.get("channels.platforms.discord")
    if public_module is None:
        return fallback
    return getattr(public_module, name, fallback)


class DiscordVoiceMixin:
    def _load_voice_fx_config(self) -> Dict[str, Any]:
        """Load non-secret voice-mixer behavior from ``discord.voice_fx``."""
        defaults: Dict[str, Any] = {
            "enabled": False,
            "ambient_enabled": True,
            "ambient_path": "",
            "ambient_gain": 0.18,
            "duck_gain": 0.06,
            "speech_gain": 1.0,
            "ack_enabled": True,
            "ack_phrases": [
                "Let me look into that.",
                "One moment.",
                "Checking on that now.",
                "Give me a sec.",
                "On it.",
            ],
        }
        try:
            from hermes_cli.config import read_raw_config

            config = read_raw_config() or {}
            voice_fx = ((config.get("discord") or {}).get("voice_fx") or {})
            if isinstance(voice_fx, dict):
                for key, value in voice_fx.items():
                    if key in defaults and value is not None:
                        defaults[key] = value
        except Exception:
            logger.debug("Could not load discord.voice_fx config", exc_info=True)
        return defaults

    def _get_ambient_pcm(self) -> Optional[bytes]:
        if self._ambient_pcm_cache is not None:
            return self._ambient_pcm_cache
        if not self._voice_fx_cfg.get("ambient_enabled"):
            return None
        from channels.platforms.discord_voice_mixer import (
            decode_to_pcm,
            synth_ambient_pcm,
        )

        pcm: Optional[bytes] = None
        path = str(self._voice_fx_cfg.get("ambient_path") or "").strip()
        if path and os.path.isfile(path):
            pcm = decode_to_pcm(path)
            if not pcm:
                logger.warning("Ambient file %s failed to decode; using synth bed", path)
        if not pcm:
            pcm = synth_ambient_pcm()
        self._ambient_pcm_cache = pcm
        return pcm

    async def _install_voice_mixer(self, guild_id: int, voice_client: Any) -> None:
        from channels.platforms.discord_voice_mixer import VoiceMixer

        mixer = VoiceMixer(
            ambient_gain=float(self._voice_fx_cfg.get("ambient_gain", 0.18)),
            duck_gain=float(self._voice_fx_cfg.get("duck_gain", 0.06)),
            speech_gain=float(self._voice_fx_cfg.get("speech_gain", 1.0)),
        )
        ambient = await asyncio.to_thread(self._get_ambient_pcm)
        if ambient:
            mixer.set_ambient(ambient)

        def after(error: Optional[Exception]) -> None:
            if error:
                logger.error("Voice mixer stream error (guild=%d): %s", guild_id, error)

        if voice_client.is_playing():
            voice_client.stop()
        voice_client.play(mixer, after=after)
        self._voice_mixers[guild_id] = mixer

    async def play_ack_in_voice(
        self,
        guild_id: int,
        phrase: Optional[str] = None,
    ) -> bool:
        """Layer a short acknowledgement over the continuous ambient bed."""
        if not self._voice_fx_cfg.get("ack_enabled"):
            return False
        mixer = self._voice_mixers.get(guild_id)
        if mixer is None:
            return False
        if phrase is None:
            import random

            phrase = random.choice(
                self._voice_fx_cfg.get("ack_phrases") or ["One moment."]
            )
        import uuid

        audio_path = os.path.join(
            tempfile.gettempdir(),
            "hermes_voice",
            f"ack_{uuid.uuid4().hex[:12]}.mp3",
        )
        os.makedirs(os.path.dirname(audio_path), exist_ok=True)
        actual_path = audio_path
        try:
            from channels.platforms.discord_voice_mixer import decode_to_pcm
            from tools.tts_tool import text_to_speech_tool

            result = json.loads(
                await asyncio.to_thread(
                    text_to_speech_tool,
                    text=phrase,
                    output_path=audio_path,
                )
            )
            actual_path = str(result.get("file_path") or audio_path)
            if not result.get("success") or not os.path.isfile(actual_path):
                return False
            pcm = await asyncio.to_thread(decode_to_pcm, actual_path)
            if not pcm:
                return False
            mixer.play_speech(
                pcm,
                gain=float(self._voice_fx_cfg.get("speech_gain", 1.0)),
            )
            self._reset_voice_timeout(guild_id)
            return True
        except Exception:
            logger.debug("play_ack_in_voice failed", exc_info=True)
            return False
        finally:
            for path in {audio_path, actual_path}:
                if path and os.path.isfile(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def voice_mixer_active(self, guild_id: int) -> bool:
        mixers = getattr(self, "_voice_mixers", None)
        return bool(mixers) and mixers.get(guild_id) is not None

    async def join_voice_channel(self, channel) -> bool:
        """Join a Discord voice channel. Returns True on success."""
        if not self._client or not _discord_public_attr("DISCORD_AVAILABLE", DISCORD_AVAILABLE):
            return False
        guild_id = channel.guild.id
    
        async with self._voice_locks.setdefault(guild_id, asyncio.Lock()):
            # Already connected in this guild?
            existing = self._voice_clients.get(guild_id)
            if existing and existing.is_connected():
                if existing.channel.id == channel.id:
                    self._reset_voice_timeout(guild_id)
                    return True
                await existing.move_to(channel)
                self._reset_voice_timeout(guild_id)
                return True
    
            vc = await channel.connect()
            self._voice_clients[guild_id] = vc
            self._reset_voice_timeout(guild_id)
    
            # Start voice receiver (Phase 2: listen to users)
            try:
                receiver_cls = _discord_public_attr("VoiceReceiver")
                receiver = receiver_cls(vc, allowed_user_ids=self._allowed_user_ids)
                receiver.start()
                self._voice_receivers[guild_id] = receiver
                self._voice_listen_tasks[guild_id] = asyncio.ensure_future(
                    self._voice_listen_loop(guild_id)
                )
            except Exception as e:
                logger.warning("Voice receiver failed to start: %s", e)

            if self._voice_fx_cfg.get("enabled"):
                try:
                    await self._install_voice_mixer(guild_id, vc)
                except Exception:
                    logger.warning("Voice mixer failed to start", exc_info=True)
    
            return True
    
    async def leave_voice_channel(self, guild_id: int) -> None:
        """Disconnect from the voice channel in a guild."""
        async with self._voice_locks.setdefault(guild_id, asyncio.Lock()):
            # Stop voice receiver first
            receiver = self._voice_receivers.pop(guild_id, None)
            if receiver:
                receiver.stop()
            listen_task = self._voice_listen_tasks.pop(guild_id, None)
            if listen_task:
                listen_task.cancel()

            self._voice_mixers.pop(guild_id, None)

            vc = self._voice_clients.pop(guild_id, None)
            if vc and vc.is_connected():
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect()
            task = self._voice_timeout_tasks.pop(guild_id, None)
            if task:
                task.cancel()
            self._voice_text_channels.pop(guild_id, None)
            self._voice_sources.pop(guild_id, None)
    
    PLAYBACK_TIMEOUT = 120
    
    async def play_in_voice_channel(self, guild_id: int, audio_path: str) -> bool:
        """Play an audio file in the connected voice channel."""
        vc = self._voice_clients.get(guild_id)
        if not vc or not vc.is_connected():
            return False

        mixer = self._voice_mixers.get(guild_id)
        if mixer is not None:
            from channels.platforms.discord_voice_mixer import decode_to_pcm

            pcm = await asyncio.to_thread(decode_to_pcm, audio_path)
            if pcm:
                mixer.play_speech(
                    pcm,
                    gain=float(self._voice_fx_cfg.get("speech_gain", 1.0)),
                )
                started = time.monotonic()
                while mixer.speech_active:
                    if time.monotonic() - started > self.PLAYBACK_TIMEOUT:
                        logger.warning(
                            "Mixer speech playback timed out after %ds",
                            self.PLAYBACK_TIMEOUT,
                        )
                        mixer.stop_speech()
                        break
                    await asyncio.sleep(0.05)
                self._reset_voice_timeout(guild_id)
                return True
            logger.warning(
                "Mixer decode failed for %s; falling back to one-shot playback",
                audio_path,
            )

        # Pause voice receiver while playing (echo prevention)
        receiver = self._voice_receivers.get(guild_id)
        if receiver:
            receiver.pause()
    
        try:
            # Wait for current playback to finish (with timeout)
            wait_start = time.monotonic()
            while vc.is_playing():
                if time.monotonic() - wait_start > self.PLAYBACK_TIMEOUT:
                    logger.warning("Timed out waiting for previous playback to finish")
                    vc.stop()
                    break
                await asyncio.sleep(0.1)
    
            done = asyncio.Event()
            loop = asyncio.get_running_loop()
    
            def _after(error):
                if error:
                    logger.error("Voice playback error: %s", error)
                loop.call_soon_threadsafe(done.set)
    
            source = discord.FFmpegPCMAudio(audio_path)
            source = discord.PCMVolumeTransformer(source, volume=1.0)
            vc.play(source, after=_after)
            try:
                await asyncio.wait_for(done.wait(), timeout=self.PLAYBACK_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Voice playback timed out after %ds", self.PLAYBACK_TIMEOUT)
                vc.stop()
            self._reset_voice_timeout(guild_id)
            return True
        finally:
            if receiver:
                receiver.resume()
    
    async def get_user_voice_channel(self, guild_id: int, user_id: str):
        """Return the voice channel the user is currently in, or None."""
        if not self._client:
            return None
        guild = self._client.get_guild(guild_id)
        if not guild:
            return None
        member = guild.get_member(int(user_id))
        if not member or not member.voice:
            return None
        return member.voice.channel
    
    def _reset_voice_timeout(self, guild_id: int) -> None:
        """Reset the auto-disconnect inactivity timer."""
        task = self._voice_timeout_tasks.pop(guild_id, None)
        if task:
            task.cancel()
        self._voice_timeout_tasks[guild_id] = asyncio.ensure_future(
            self._voice_timeout_handler(guild_id)
        )
    
    async def _voice_timeout_handler(self, guild_id: int) -> None:
        """Auto-disconnect after VOICE_TIMEOUT seconds of inactivity."""
        try:
            await asyncio.sleep(self.VOICE_TIMEOUT)
        except asyncio.CancelledError:
            return
        text_ch_id = self._voice_text_channels.get(guild_id)
        await self.leave_voice_channel(guild_id)
        # Notify the runner so it can clean up voice_mode state
        if self._on_voice_disconnect and text_ch_id:
            try:
                self._on_voice_disconnect(str(text_ch_id))
            except Exception:
                pass
        if text_ch_id and self._client:
            ch = self._client.get_channel(text_ch_id)
            if ch:
                try:
                    await ch.send("Left voice channel (inactivity timeout).")
                except Exception:
                    pass
    
    def is_in_voice_channel(self, guild_id: int) -> bool:
        """Check if the bot is connected to a voice channel in this guild."""
        vc = self._voice_clients.get(guild_id)
        return vc is not None and vc.is_connected()
    
    def get_voice_channel_info(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Return voice channel awareness info for the given guild.
    
        Returns None if the bot is not in a voice channel.  Otherwise
        returns a dict with channel name, member list, count, and
        currently-speaking user IDs (from SSRC mapping).
        """
        vc = self._voice_clients.get(guild_id)
        if not vc or not vc.is_connected():
            return None
    
        channel = vc.channel
        if not channel:
            return None
    
        # Members currently in the voice channel (includes bot)
        members_info = []
        bot_user = self._client.user if self._client else None
        for m in channel.members:
            if bot_user and m.id == bot_user.id:
                continue  # skip the bot itself
            members_info.append({
                "user_id": m.id,
                "display_name": m.display_name,
                "is_bot": m.bot,
            })
    
        # Currently speaking users (from SSRC mapping + active buffers)
        speaking_user_ids: set = set()
        receiver = self._voice_receivers.get(guild_id)
        if receiver:
            now = time.monotonic()
            with receiver._lock:
                for ssrc, last_t in receiver._last_packet_time.items():
                    # Consider "speaking" if audio received within last 2 seconds
                    if now - last_t < 2.0:
                        uid = receiver._ssrc_to_user.get(ssrc)
                        if uid:
                            speaking_user_ids.add(uid)
    
        # Tag speaking status on members
        for info in members_info:
            info["is_speaking"] = info["user_id"] in speaking_user_ids
    
        return {
            "channel_name": channel.name,
            "member_count": len(members_info),
            "members": members_info,
            "speaking_count": len(speaking_user_ids),
        }
    
    def get_voice_channel_context(self, guild_id: int) -> str:
        """Return a human-readable voice channel context string.
    
        Suitable for injection into the system/ephemeral prompt so the
        agent is always aware of voice channel state.
        """
        info = self.get_voice_channel_info(guild_id)
        if not info:
            return ""
    
        parts = [f"[Voice channel: #{info['channel_name']} — {info['member_count']} participant(s)]"]
        for m in info["members"]:
            status = " (speaking)" if m["is_speaking"] else ""
            parts.append(f"  - {m['display_name']}{status}")
    
        return "\n".join(parts)
    
    _KEEPALIVE_INTERVAL = 15
    
    async def _voice_listen_loop(self, guild_id: int):
        """Periodically check for completed utterances and process them."""
        receiver = self._voice_receivers.get(guild_id)
        if not receiver:
            return
        last_keepalive = time.monotonic()
        try:
            while receiver._running:
                await asyncio.sleep(0.2)
    
                # Send periodic UDP keepalive to prevent Discord from
                # dropping the UDP session after ~60s of silence.
                now = time.monotonic()
                if now - last_keepalive >= self._KEEPALIVE_INTERVAL:
                    last_keepalive = now
                    try:
                        vc = self._voice_clients.get(guild_id)
                        if vc and vc.is_connected():
                            vc._connection.send_packet(b'\xf8\xff\xfe')
                    except Exception:
                        pass
    
                completed = receiver.check_silence()
                # Voice inputs always originate from a specific guild
                # (guild_id is in scope). Pass it so role checks are
                # guild-scoped and not cross-guild.
                _vc_guild = self._client.get_guild(guild_id) if self._client is not None else None
                for user_id, pcm_data in completed:
                    if not self._is_allowed_user(
                        str(user_id),
                        guild=_vc_guild,
                        is_dm=False,
                    ):
                        continue
                    await self._process_voice_input(guild_id, user_id, pcm_data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Voice listen loop error: %s", e, exc_info=True)
    
    async def _process_voice_input(self, guild_id: int, user_id: int, pcm_data: bytes):
        """Convert PCM -> WAV -> STT -> callback."""
        from tools.voice_mode import is_whisper_hallucination
    
        tmp_f = tempfile.NamedTemporaryFile(suffix=".wav", prefix="vc_listen_", delete=False)
        wav_path = tmp_f.name
        tmp_f.close()
        try:
            receiver_cls = _discord_public_attr("VoiceReceiver")
            await asyncio.to_thread(receiver_cls.pcm_to_wav, pcm_data, wav_path)
    
            from tools.transcription_tools import transcribe_audio
            result = await asyncio.to_thread(transcribe_audio, wav_path)
    
            if not result.get("success"):
                return
            transcript = result.get("transcript", "").strip()
            if not transcript or is_whisper_hallucination(transcript):
                return
    
            logger.info("Voice input from user %d: %s", user_id, transcript[:100])
    
            if self._voice_input_callback:
                await self._voice_input_callback(
                    guild_id=guild_id,
                    user_id=user_id,
                    transcript=transcript,
                )
        except Exception as e:
            logger.warning("Voice input processing failed: %s", e, exc_info=True)
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
