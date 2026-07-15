from __future__ import annotations

import asyncio
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
    
            vc = self._voice_clients.pop(guild_id, None)
            if vc and vc.is_connected():
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
