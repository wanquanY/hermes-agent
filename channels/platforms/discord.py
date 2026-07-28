from __future__ import annotations

"""
Discord platform adapter.

Uses discord.py library for:
- Receiving messages from servers and DMs
- Sending responses back
- Handling threads and channels
"""

import asyncio
import hashlib
import json
import logging
import os
import struct
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Any, Tuple

from agent.secret_scope import get_profile_env
logger = logging.getLogger(__name__)

VALID_THREAD_AUTO_ARCHIVE_MINUTES = {60, 1440, 4320, 10080}
_DISCORD_COMMAND_SYNC_POLICIES = {"safe", "bulk", "off"}
_DISCORD_COMMAND_SYNC_STATE_SUBDIR = "gateway"
_DISCORD_COMMAND_SYNC_STATE_FILENAME = "discord_command_sync_state.json"
_DISCORD_COMMAND_SYNC_MUTATION_INTERVAL_SECONDS = 4.5
_DISCORD_COMMAND_SYNC_MAX_RATE_LIMIT_SLEEP_SECONDS = 30.0

try:
    import discord
    from discord import Message as DiscordMessage, Intents
    from discord.ext import commands
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False
    discord = None
    DiscordMessage = Any
    Intents = Any
    commands = None

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from channels.config import Platform, PlatformConfig
import re

from channels.platforms.helpers import MessageDeduplicator, ThreadParticipationTracker
from channels.platforms.discord_command_sync import DiscordCommandSyncMixin
from channels.platforms.discord_delivery import DiscordDeliveryMixin
from channels.platforms.discord_voice import DiscordVoiceMixin
from channels.platforms.discord_slash import DiscordSlashCommandMixin
from channels.platforms.discord_context import DiscordContextMixin
from channels.platforms.discord_inbound import DiscordInboundMixin
from channels.platforms.discord_ingress import DiscordIngressMixin
from channels.platforms.discord_liveness import (
    DiscordLivenessMixin,
    discord_ready_timeout_seconds,
    wait_for_ready_or_bot_exit,
)
from channels.platforms.discord_recovery import DiscordRecoveryMixin
from utils import atomic_json_write
from channels.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    cache_image_from_url,
    cache_image_from_bytes,
    cache_audio_from_url,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    SUPPORTED_DOCUMENT_TYPES,
    validate_inbound_media_size,
)
from tools.url_safety import is_safe_url


def _clean_discord_id(entry: str) -> str:
    """Strip common prefixes from a Discord user ID or username entry.

    Users sometimes paste IDs with prefixes like ``user:123``, ``<@123>``,
    or ``<@!123>`` from Discord's UI or other tools.  This normalises the
    entry to just the bare ID or username.
    """
    entry = entry.strip()
    # Strip Discord mention syntax: <@123> or <@!123>
    if entry.startswith("<@") and entry.endswith(">"):
        entry = entry.lstrip("<@!").rstrip(">")
    # Strip "user:" prefix (seen in some Discord tools / onboarding pastes)
    if entry.lower().startswith("user:"):
        entry = entry[5:]
    return entry.strip()


def check_discord_requirements() -> bool:
    """Check if Discord dependencies are available.

    Lazy-installs discord.py via ``tools.lazy_deps.ensure("platform.discord")``
    on first call if not present. After successful install, re-binds module
    globals so ``DISCORD_AVAILABLE`` becomes True.
    """
    global DISCORD_AVAILABLE, discord, DiscordMessage, Intents, commands
    if DISCORD_AVAILABLE:
        return True
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("platform.discord", prompt=False)
    except Exception:
        return False
    try:
        import discord as _discord
        from discord import Message as _DM, Intents as _Intents
        from discord.ext import commands as _commands
    except ImportError:
        return False
    discord = _discord
    DiscordMessage = _DM
    Intents = _Intents
    commands = _commands
    DISCORD_AVAILABLE = True
    _define_discord_view_classes()
    return True


def _build_allowed_mentions(config: Optional[dict] = None):
    """Build Discord ``AllowedMentions`` with safe defaults, overridable via env.

    Discord bots default to parsing ``@everyone``, ``@here``, role pings, and
    user pings when ``allowed_mentions`` is unset on the client — any LLM
    output or echoed user content that contains ``@everyone`` would therefore
    ping the whole server. We explicitly deny ``@everyone`` and role pings
    by default and keep user / replied-user pings enabled so normal
    conversation still works.

    Override via environment variables (or ``discord.allow_mentions.*`` in
    config.yaml):

        DISCORD_ALLOW_MENTION_EVERYONE      default false  — @everyone + @here
        DISCORD_ALLOW_MENTION_ROLES         default false  — @role pings
        DISCORD_ALLOW_MENTION_USERS         default true   — @user pings
        DISCORD_ALLOW_MENTION_REPLIED_USER  default true   — reply-ping author
    """
    if not DISCORD_AVAILABLE:
        return None

    yaml_values = config or {}

    def _b(name: str, key: str, default: bool) -> bool:
        raw = get_profile_env(name, "").strip().lower()
        if not raw and key in yaml_values:
            raw = str(yaml_values[key]).strip().lower()
        if not raw:
            return default
        return raw in {"true", "1", "yes", "on"}

    return discord.AllowedMentions(
        everyone=_b("DISCORD_ALLOW_MENTION_EVERYONE", "everyone", False),
        roles=_b("DISCORD_ALLOW_MENTION_ROLES", "roles", False),
        users=_b("DISCORD_ALLOW_MENTION_USERS", "users", True),
        replied_user=_b(
            "DISCORD_ALLOW_MENTION_REPLIED_USER", "replied_user", True
        ),
    )


class VoiceReceiver:
    """Captures and decodes voice audio from a Discord voice channel.

    Attaches to a VoiceClient's socket listener, decrypts RTP packets
    (NaCl transport + DAVE E2EE), decodes Opus to PCM, and buffers
    per-user audio.  A polling loop detects silence and delivers
    completed utterances via a callback.
    """

    SILENCE_THRESHOLD = 1.5    # seconds of silence → end of utterance
    MIN_SPEECH_DURATION = 0.5  # minimum seconds to process (skip noise)
    SAMPLE_RATE = 48000        # Discord native rate
    CHANNELS = 2               # Discord sends stereo

    def __init__(self, voice_client, allowed_user_ids: set = None):
        self._vc = voice_client
        self._allowed_user_ids = allowed_user_ids or set()
        self._running = False

        # Decryption
        self._secret_key: Optional[bytes] = None
        self._dave_session = None
        self._bot_ssrc: int = 0

        # SSRC -> user_id mapping (populated from SPEAKING events)
        self._ssrc_to_user: Dict[int, int] = {}
        self._lock = threading.Lock()

        # Per-user audio buffers
        self._buffers: Dict[int, bytearray] = defaultdict(bytearray)
        self._last_packet_time: Dict[int, float] = {}

        # Opus decoder per SSRC (each user needs own decoder state)
        self._decoders: Dict[int, object] = {}

        # Pause flag: don't capture while bot is playing TTS
        self._paused = False

        # Debug logging counter (instance-level to avoid cross-instance races)
        self._packet_debug_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start listening for voice packets."""
        conn = self._vc._connection
        self._secret_key = bytes(conn.secret_key)
        self._dave_session = conn.dave_session
        self._bot_ssrc = conn.ssrc

        self._install_speaking_hook(conn)
        conn.add_socket_listener(self._on_packet)
        self._running = True
        logger.info("VoiceReceiver started (bot_ssrc=%d)", self._bot_ssrc)

    def stop(self):
        """Stop listening and clean up."""
        self._running = False
        try:
            self._vc._connection.remove_socket_listener(self._on_packet)
        except Exception:
            pass
        with self._lock:
            self._buffers.clear()
            self._last_packet_time.clear()
            self._decoders.clear()
            self._ssrc_to_user.clear()
        logger.info("VoiceReceiver stopped")

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    # ------------------------------------------------------------------
    # SSRC -> user_id mapping via SPEAKING opcode hook
    # ------------------------------------------------------------------

    def map_ssrc(self, ssrc: int, user_id: int):
        with self._lock:
            self._ssrc_to_user[ssrc] = user_id

    def _install_speaking_hook(self, conn):
        """Wrap the voice websocket hook to capture SPEAKING events (op 5).

        VoiceConnectionState stores the hook as ``conn.hook`` (public attr).
        It is passed to DiscordVoiceWebSocket on each (re)connect, so we
        must wrap it on the VoiceConnectionState level AND on the current
        live websocket instance.
        """
        original_hook = conn.hook
        receiver_self = self

        async def wrapped_hook(ws, msg):
            if isinstance(msg, dict) and msg.get("op") == 5:
                data = msg.get("d", {})
                ssrc = data.get("ssrc")
                user_id = data.get("user_id")
                if ssrc and user_id:
                    logger.info("SPEAKING event: ssrc=%d -> user=%s", ssrc, user_id)
                    receiver_self.map_ssrc(int(ssrc), int(user_id))
            if original_hook:
                await original_hook(ws, msg)

        # Set on connection state (for future reconnects)
        conn.hook = wrapped_hook
        # Set on the current live websocket (for immediate effect)
        try:
            from discord.utils import MISSING
            if hasattr(conn, 'ws') and conn.ws is not MISSING:
                conn.ws._hook = wrapped_hook
                logger.info("Speaking hook installed on live websocket")
        except Exception as e:
            logger.warning("Could not install hook on live ws: %s", e)

    # ------------------------------------------------------------------
    # Packet handler (called from SocketReader thread)
    # ------------------------------------------------------------------

    def _on_packet(self, data: bytes):
        if not self._running or self._paused:
            return

        # Log first few raw packets for debugging
        self._packet_debug_count += 1
        if self._packet_debug_count <= 5:
            logger.debug(
                "Raw UDP packet: len=%d, first_bytes=%s",
                len(data), data[:4].hex() if len(data) >= 4 else "short",
            )

        if len(data) < 16:
            return

        # RTP version check: top 2 bits must be 10 (version 2).
        # Lower bits may vary (padding, extension, CSRC count).
        # Payload type (byte 1 lower 7 bits) = 0x78 (120) for voice.
        if (data[0] >> 6) != 2 or (data[1] & 0x7F) != 0x78:
            if self._packet_debug_count <= 5:
                logger.debug("Skipped non-RTP: byte0=0x%02x byte1=0x%02x", data[0], data[1])
            return

        first_byte = data[0]
        _, _, seq, timestamp, ssrc = struct.unpack_from(">BBHII", data, 0)

        # Skip bot's own audio
        if ssrc == self._bot_ssrc:
            return

        # Calculate dynamic RTP header size (RFC 9335 / rtpsize mode)
        cc = first_byte & 0x0F  # CSRC count
        has_extension = bool(first_byte & 0x10)  # extension bit
        has_padding = bool(first_byte & 0x20)  # padding bit (RFC 3550 §5.1)
        header_size = 12 + (4 * cc) + (4 if has_extension else 0)

        if len(data) < header_size + 4:  # need at least header + nonce
            return

        # Read extension length from preamble (for skipping after decrypt)
        ext_data_len = 0
        if has_extension:
            ext_preamble_offset = 12 + (4 * cc)
            ext_words = struct.unpack_from(">H", data, ext_preamble_offset + 2)[0]
            ext_data_len = ext_words * 4

        if self._packet_debug_count <= 10:
            with self._lock:
                known_user = self._ssrc_to_user.get(ssrc, "unknown")
            logger.debug(
                "RTP packet: ssrc=%d, seq=%d, user=%s, hdr=%d, ext_data=%d",
                ssrc, seq, known_user, header_size, ext_data_len,
            )

        header = bytes(data[:header_size])
        payload_with_nonce = data[header_size:]

        # --- NaCl transport decrypt (aead_xchacha20_poly1305_rtpsize) ---
        if len(payload_with_nonce) < 4:
            return
        nonce = bytearray(24)
        nonce[:4] = payload_with_nonce[-4:]
        encrypted = bytes(payload_with_nonce[:-4])

        try:
            import nacl.secret  # noqa: E402 — delayed import, only in voice path
            box = nacl.secret.Aead(self._secret_key)
            decrypted = box.decrypt(encrypted, header, bytes(nonce))
        except Exception as e:
            if self._packet_debug_count <= 10:
                logger.warning("NaCl decrypt failed: %s (hdr=%d, enc=%d)", e, header_size, len(encrypted))
            return

        # Skip encrypted extension data to get the actual opus payload
        if ext_data_len and len(decrypted) > ext_data_len:
            decrypted = decrypted[ext_data_len:]

        # --- Strip RTP padding (RFC 3550 §5.1) ---
        # When the P bit is set, the last payload byte holds the count of
        # trailing padding bytes (including itself) that must be removed
        # before further processing. Skipping this passes padding-contaminated
        # bytes into DAVE/Opus and corrupts inbound audio.
        if has_padding:
            if not decrypted:
                if self._packet_debug_count <= 10:
                    logger.warning(
                        "RTP padding bit set but no payload (ssrc=%d)", ssrc,
                    )
                return
            pad_len = decrypted[-1]
            if pad_len == 0 or pad_len > len(decrypted):
                if self._packet_debug_count <= 10:
                    logger.warning(
                        "Invalid RTP padding length %d for payload size %d (ssrc=%d)",
                        pad_len, len(decrypted), ssrc,
                    )
                return
            decrypted = decrypted[:-pad_len]
            if not decrypted:
                # Padding consumed entire payload — nothing to decode
                return

        # --- DAVE E2EE decrypt ---
        if self._dave_session:
            with self._lock:
                user_id = self._ssrc_to_user.get(ssrc, 0)
            if user_id:
                try:
                    import davey
                    decrypted = self._dave_session.decrypt(
                        user_id, davey.MediaType.audio, decrypted
                    )
                except Exception as e:
                    # Unencrypted passthrough — use NaCl-decrypted data as-is
                    if "Unencrypted" not in str(e):
                        if self._packet_debug_count <= 10:
                            logger.warning("DAVE decrypt failed for ssrc=%d: %s", ssrc, e)
                        return
            # If SSRC unknown (no SPEAKING event yet), skip DAVE and try
            # Opus decode directly — audio may be in passthrough mode.
            # Buffer will get a user_id when SPEAKING event arrives later.

        # --- Opus decode -> PCM ---
        try:
            if ssrc not in self._decoders:
                self._decoders[ssrc] = discord.opus.Decoder()
            pcm = self._decoders[ssrc].decode(decrypted)
            with self._lock:
                self._buffers[ssrc].extend(pcm)
                self._last_packet_time[ssrc] = time.monotonic()
        except Exception as e:
            logger.debug("Opus decode error for SSRC %s: %s", ssrc, e)
            return

    # ------------------------------------------------------------------
    # Silence detection
    # ------------------------------------------------------------------

    def _infer_user_for_ssrc(self, ssrc: int) -> int:
        """Try to infer user_id for an unmapped SSRC.

        When the bot rejoins a voice channel, Discord may not resend
        SPEAKING events for users already speaking.  If exactly one
        allowed user is in the channel, map the SSRC to them.
        """
        try:
            channel = self._vc.channel
            if not channel:
                return 0
            bot_id = self._vc.user.id if self._vc.user else 0
            allowed = self._allowed_user_ids
            candidates = [
                m.id for m in channel.members
                if m.id != bot_id and (not allowed or str(m.id) in allowed)
            ]
            if len(candidates) == 1:
                uid = candidates[0]
                self._ssrc_to_user[ssrc] = uid
                logger.info("Auto-mapped ssrc=%d -> user=%d (sole allowed member)", ssrc, uid)
                return uid
        except Exception:
            pass
        return 0

    def check_silence(self) -> list:
        """Return list of (user_id, pcm_bytes) for completed utterances."""
        now = time.monotonic()
        completed = []

        with self._lock:
            ssrc_user_map = dict(self._ssrc_to_user)
            ssrc_list = list(self._buffers.keys())

            for ssrc in ssrc_list:
                last_time = self._last_packet_time.get(ssrc, now)
                silence_duration = now - last_time
                buf = self._buffers[ssrc]
                # 48kHz, 16-bit, stereo = 192000 bytes/sec
                buf_duration = len(buf) / (self.SAMPLE_RATE * self.CHANNELS * 2)

                if silence_duration >= self.SILENCE_THRESHOLD and buf_duration >= self.MIN_SPEECH_DURATION:
                    user_id = ssrc_user_map.get(ssrc, 0)
                    if not user_id:
                        # SSRC not mapped (SPEAKING event missing after bot rejoin).
                        # Infer from allowed users in the voice channel.
                        user_id = self._infer_user_for_ssrc(ssrc)
                    if user_id:
                        completed.append((user_id, bytes(buf)))
                    self._buffers[ssrc] = bytearray()
                    self._last_packet_time.pop(ssrc, None)
                elif silence_duration >= self.SILENCE_THRESHOLD * 2:
                    # Stale buffer with no valid user — discard
                    self._buffers.pop(ssrc, None)
                    self._last_packet_time.pop(ssrc, None)

        return completed

    # ------------------------------------------------------------------
    # PCM -> WAV conversion (for Whisper STT)
    # ------------------------------------------------------------------

    @staticmethod
    def pcm_to_wav(pcm_data: bytes, output_path: str,
                   src_rate: int = 48000, src_channels: int = 2):
        """Convert raw PCM to 16kHz mono WAV via ffmpeg."""
        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
            f.write(pcm_data)
            pcm_path = f.name
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "s16le",
                    "-ar", str(src_rate),
                    "-ac", str(src_channels),
                    "-i", pcm_path,
                    "-ar", "16000",
                    "-ac", "1",
                    output_path,
                ],
                check=True,
                timeout=10,
            )
        finally:
            try:
                os.unlink(pcm_path)
            except OSError:
                pass


def _read_dm_role_auth_guild() -> Optional[int]:
    """Return the guild ID opted-in for DM role-based auth, or None.

    Reads ``discord.dm_role_auth_guild`` from config.yaml. This is
    deliberately a config.yaml-only setting (not an env var): per repo
    policy, ``~/.hermes/.env`` is for secrets only, and this is a
    behavioral setting. Guild IDs aren't secrets.

    Accepts ints or numeric strings in the config. Anything else
    (empty, malformed, None) returns None, which keeps the secure
    default (DM role-auth disabled).
    """
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config() or {}
        discord_cfg = cfg.get("discord", {}) or {}
        raw = discord_cfg.get("dm_role_auth_guild")
    except Exception:
        return None
    if raw is None or raw == "":
        return None
    try:
        guild_id = int(raw)
    except (TypeError, ValueError):
        return None
    return guild_id if guild_id > 0 else None


class DiscordAdapter(
    DiscordRecoveryMixin,
    DiscordIngressMixin,
    DiscordLivenessMixin,
    DiscordInboundMixin,
    DiscordContextMixin,
    DiscordSlashCommandMixin,
    DiscordVoiceMixin,
    DiscordDeliveryMixin,
    DiscordCommandSyncMixin,
    BasePlatformAdapter,
):
    """
    Discord bot adapter.

    Handles:
    - Receiving messages from servers and DMs
    - Sending responses with Discord markdown
    - Thread support
    - Native slash commands (/ask, /reset, /status, /stop)
    - Button-based exec approvals
    - Auto-threading for long conversations
    - Reaction-based feedback
    """

    # Discord message limits
    MAX_MESSAGE_LENGTH = 2000
    _SPLIT_THRESHOLD = 1900  # near the 2000-char split point

    # Auto-disconnect from voice channel after this many seconds of inactivity
    VOICE_TIMEOUT = 300

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DISCORD)
        self._client: Optional[commands.Bot] = None
        self._ready_event = asyncio.Event()
        self._allowed_user_ids: set = set()  # For button approval authorization
        self._allowed_role_ids: set = set()  # For DISCORD_ALLOWED_ROLES filtering
        self.gateway_runner = None  # Set by gateway/run.py for cross-platform delivery
        # Voice channel state (per-guild)
        self._voice_clients: Dict[int, Any] = {}  # guild_id -> VoiceClient
        self._voice_locks: Dict[int, asyncio.Lock] = {}  # guild_id -> serialize join/leave
        # Text batching: merge rapid successive messages (Telegram-style)
        self._text_batch_delay_seconds = float(
            get_profile_env("HERMES_DISCORD_TEXT_BATCH_DELAY_SECONDS", "0.6")
        )
        self._text_batch_split_delay_seconds = float(
            get_profile_env(
                "HERMES_DISCORD_TEXT_BATCH_SPLIT_DELAY_SECONDS", "2.0"
            )
        )
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}
        self._voice_text_channels: Dict[int, int] = {}  # guild_id -> text_channel_id
        self._voice_sources: Dict[int, Dict[str, Any]] = {}  # guild_id -> linked text channel source metadata
        self._voice_timeout_tasks: Dict[int, asyncio.Task] = {}  # guild_id -> timeout task
        # Phase 2: voice listening
        self._voice_receivers: Dict[int, VoiceReceiver] = {}  # guild_id -> VoiceReceiver
        self._voice_listen_tasks: Dict[int, asyncio.Task] = {}  # guild_id -> listen loop
        self._voice_input_callback: Optional[Callable] = None  # set by run.py
        self._on_voice_disconnect: Optional[Callable] = None  # set by run.py
        # Continuous outgoing mixer: one Discord AudioSource per guild with
        # ambient and speech children mixed upstream of discord.py.
        self._voice_mixers: Dict[int, Any] = {}
        self._ambient_pcm_cache: Optional[bytes] = None
        self._voice_fx_cfg: Dict[str, Any] = self._load_voice_fx_config()
        # Track threads where the bot has participated so follow-up messages
        # in those threads don't require @mention.  Persisted to disk so the
        # set survives gateway restarts.
        self._threads = ThreadParticipationTracker("discord")
        # Persistent typing indicator loops per channel (DMs don't reliably
        # show the standard typing gateway event for bots)
        self._typing_tasks: Dict[str, asyncio.Task] = {}
        self._bot_task: Optional[asyncio.Task] = None
        self._post_connect_task: Optional[asyncio.Task] = None
        # Dedup cache: prevents duplicate bot responses when Discord
        # RESUME replays events after reconnects.
        self._dedup = MessageDeduplicator()
        # Reply threading mode: "off" (no replies), "first" (reply on first
        # chunk only, default), "all" (reply-reference on every chunk).
        self._reply_to_mode: str = getattr(config, 'reply_to_mode', 'first') or 'first'
        self._slash_commands: bool = self.config.extra.get("slash_commands", True)
        # In-memory cache of the bot's last message ID per channel, used by
        # history backfill to skip the full scan on hot paths.  Falls back to
        # scanning channel.history() on cache miss (cold start / restart).
        self._last_self_message_id: Dict[str, str] = {}
        # Streaming edits saturate at Discord's 2,000-character limit.  Keep
        # the last rendered preview per message so identical over-limit frames
        # do not burn edit rate-limit budget while the final response grows.
        self._last_overflow_preview: Dict[tuple[str, str], str] = {}
        self._init_discord_liveness()
        self._init_discord_recovery()

    async def connect(self) -> bool:
        """Connect to Discord and start receiving events."""
        if not DISCORD_AVAILABLE:
            logger.error("[%s] discord.py not installed. Run: pip install discord.py", self.name)
            return False

        # Load opus codec for voice channel support
        if not discord.opus.is_loaded():
            import ctypes.util
            opus_path = ctypes.util.find_library("opus")
            # ctypes.util.find_library fails on macOS with Homebrew-installed libs,
            # so fall back to known Homebrew paths if needed.
            if not opus_path:
                _homebrew_paths = (
                    "/opt/homebrew/lib/libopus.dylib",  # Apple Silicon
                    "/usr/local/lib/libopus.dylib",     # Intel Mac
                )
                if sys.platform == "darwin":
                    for _hp in _homebrew_paths:
                        if os.path.isfile(_hp):
                            opus_path = _hp
                            break
            if opus_path:
                try:
                    discord.opus.load_opus(opus_path)
                except Exception:
                    logger.warning("Opus codec found at %s but failed to load", opus_path)
            if not discord.opus.is_loaded():
                logger.warning("Opus codec not found — voice channel playback disabled")

        if not self.config.token:
            logger.error("[%s] No bot token configured", self.name)
            return False

        try:
            if not self._acquire_platform_lock('discord-bot-token', self.config.token, 'Discord bot token'):
                return False

            # Parse allowed user entries (may contain usernames or IDs)
            allowed_config = get_profile_env("DISCORD_ALLOWED_USERS", "")
            if not allowed_config:
                allowed_config = self.config.extra.get("allow_from", "")
            if isinstance(allowed_config, str):
                allowed_entries = allowed_config.split(",")
            elif isinstance(allowed_config, (list, tuple, set, frozenset)):
                allowed_entries = allowed_config
            else:
                allowed_entries = ()
            if allowed_entries:
                self._allowed_user_ids = {
                    _clean_discord_id(str(uid))
                    for uid in allowed_entries
                    if str(uid).strip()
                }

            # Parse DISCORD_ALLOWED_ROLES — comma-separated role IDs.
            # Users with ANY of these roles can interact with the bot.
            roles_config = get_profile_env("DISCORD_ALLOWED_ROLES", "")
            if not roles_config:
                roles_config = self.config.extra.get("allowed_roles", "")
            if isinstance(roles_config, str):
                role_entries = roles_config.split(",")
            elif isinstance(roles_config, (list, tuple, set, frozenset)):
                role_entries = roles_config
            else:
                role_entries = ()
            if role_entries:
                self._allowed_role_ids = {
                    int(str(rid).strip())
                    for rid in role_entries
                    if str(rid).strip().isdigit()
                }

            # Set up intents.
            # Message Content is required for normal text replies.
            # Server Members is only needed when the allowlist contains usernames
            # that must be resolved to numeric IDs. Requesting privileged intents
            # that aren't enabled in the Discord Developer Portal can prevent the
            # bot from coming online at all, so avoid requesting members intent
            # unless it is actually necessary.
            intents = Intents.default()
            intents.message_content = True
            intents.dm_messages = True
            intents.guild_messages = True
            intents.members = (
                any(not entry.isdigit() for entry in self._allowed_user_ids)
                or bool(self._allowed_role_ids)  # Need members intent for role lookup
            )
            intents.voice_states = True

            # Resolve proxy (DISCORD_PROXY > generic env vars > macOS system proxy)
            from channels.platforms.base import resolve_proxy_url, proxy_kwargs_for_bot
            proxy_url = resolve_proxy_url(platform_env_var="DISCORD_PROXY")
            if proxy_url:
                logger.info("[%s] Using proxy for Discord: %s", self.name, proxy_url)

            # Create bot — proxy= for HTTP, connector= for SOCKS.
            # allowed_mentions is set with safe defaults (no @everyone/roles)
            # so LLM output or echoed user content can't ping the whole
            # server; override per DISCORD_ALLOW_MENTION_* env vars or the
            # discord.allow_mentions.* block in config.yaml.

            # Close any existing client to prevent zombie websocket connections
            # on reconnect (see #18187). Without this, the old client remains
            # connected to Discord gateway and both fire on_message, causing
            # double responses.
            if self._client is not None:
                try:
                    if not self._client.is_closed():
                        await self._client.close()
                except Exception:
                    logger.debug("[%s] Failed to close previous Discord client", self.name)
                finally:
                    self._client = None
                    self._ready_event.clear()

            self._client = commands.Bot(
                command_prefix="!",  # Not really used, we handle raw messages
                intents=intents,
                allowed_mentions=_build_allowed_mentions(
                    self.config.extra.get("allow_mentions")
                ),
                **proxy_kwargs_for_bot(proxy_url),
            )
            adapter_self = self  # capture for closure

            # Register event handlers
            @self._client.event
            async def on_ready():
                logger.info("[%s] Connected as %s", adapter_self.name, adapter_self._client.user)

                # Resolve any usernames in the allowed list to numeric IDs
                await adapter_self._resolve_allowed_usernames()
                adapter_self._ready_event.set()

                if adapter_self._post_connect_task and not adapter_self._post_connect_task.done():
                    adapter_self._post_connect_task.cancel()
                adapter_self._post_connect_task = asyncio.create_task(
                    adapter_self._run_post_connect_initialization()
                )
                if adapter_self._missed_message_backfill_enabled():
                    adapter_self._ensure_missed_message_backfill_task()

            @self._client.event
            async def on_message(message: DiscordMessage):
                await adapter_self._dispatch_discord_message(message)
            @self._client.event
            async def on_voice_state_update(member, before, after):
                """Track voice channel join/leave events."""
                # Only track channels where the bot is connected
                bot_guild_ids = set(adapter_self._voice_clients.keys())
                if not bot_guild_ids:
                    return
                guild_id = member.guild.id
                if guild_id not in bot_guild_ids:
                    return
                # Ignore the bot itself
                if member == adapter_self._client.user:
                    return

                joined = before.channel is None and after.channel is not None
                left = before.channel is not None and after.channel is None
                switched = (
                    before.channel is not None
                    and after.channel is not None
                    and before.channel != after.channel
                )

                if joined or left or switched:
                    logger.info(
                        "Voice state: %s (%d) %s (guild %d)",
                        member.display_name,
                        member.id,
                        "joined " + after.channel.name if joined
                        else "left " + before.channel.name if left
                        else f"moved {before.channel.name} -> {after.channel.name}",
                        guild_id,
                    )

            # Register slash commands
            if self._slash_commands:
                self._register_slash_commands()

            # Start the bot in background
            self._disconnecting = False
            self._bot_task = asyncio.create_task(self._client.start(self.config.token))
            self._bot_task.add_done_callback(self._handle_bot_task_done)

            ready_timeout = discord_ready_timeout_seconds()
            await wait_for_ready_or_bot_exit(
                self._ready_event,
                self._bot_task,
                timeout=None if ready_timeout <= 0 else ready_timeout,
            )

            self._running = True
            self._start_liveness_probe()
            return True

        except asyncio.TimeoutError:
            logger.error("[%s] Timeout waiting for connection to Discord", self.name, exc_info=True)
            await self._cancel_bot_task()
            self._release_platform_lock()
            return False
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to connect to Discord: %s", self.name, e, exc_info=True)
            await self._cancel_bot_task()
            self._release_platform_lock()
            return False

    async def disconnect(self) -> None:
        """Disconnect from Discord."""
        self._disconnecting = True
        await self._cancel_liveness_task()
        await self._cancel_bot_task()
        # Clean up all active voice connections before closing the client
        for guild_id in list(self._voice_clients.keys()):
            try:
                await self.leave_voice_channel(guild_id)
            except Exception as e:  # pragma: no cover - defensive logging
                logger.debug("[%s] Error leaving voice channel %s: %s", self.name, guild_id, e)

        if self._client:
            try:
                await self._client.close()
            except Exception as e:  # pragma: no cover - defensive logging
                logger.warning("[%s] Error during disconnect: %s", self.name, e, exc_info=True)

        if self._post_connect_task and not self._post_connect_task.done():
            self._post_connect_task.cancel()
            try:
                await self._post_connect_task
            except asyncio.CancelledError:
                pass
        await self._cancel_missed_message_backfill_task()

        self._running = False
        self._client = None
        self._ready_event.clear()
        self._post_connect_task = None

        self._release_platform_lock()

        logger.info("[%s] Disconnected", self.name)























    # ------------------------------------------------------------------
    # Voice channel methods (join / leave / play)
    # ------------------------------------------------------------------



    # Maximum seconds to wait for voice playback before giving up








    # ------------------------------------------------------------------
    # Voice listening (Phase 2)
    # ------------------------------------------------------------------

    # UDP keepalive interval in seconds — prevents Discord from dropping
    # the UDP route after ~60s of silence.




    # ── Slash command authorization ─────────────────────────────────────
    # Slash commands (``_run_simple_slash`` and ``_handle_thread_create_slash``)
    # are a separate Discord interaction surface from regular messages and
    # historically ran with NO authorization check — bypassing every gate
    # ``on_message`` enforces (DISCORD_ALLOWED_USERS, DISCORD_ALLOWED_ROLES,
    # DISCORD_ALLOWED_CHANNELS, DISCORD_IGNORED_CHANNELS). Any guild member
    # could invoke ``/background``, ``/restart``, ``/sethome``, etc. as the
    # operator. ``_check_slash_authorization`` mirrors the on_message gates
    # one-for-one so the slash surface honors the same trust boundary.
    #
    # By design, this is a no-op for deployments with no allowlist env vars
    # set — ``_is_allowed_user`` returns True and the channel checks early-out
    # — preserving the existing "single-tenant, all guild members trusted"
    # default. Deployments that DO set any DISCORD_ALLOWED_* var get slash
    # parity with on_message.






















    # ------------------------------------------------------------------
    # Thread creation helpers
    # ------------------------------------------------------------------

















    # ------------------------------------------------------------------
    # Auto-thread helpers
    # ------------------------------------------------------------------












    # ------------------------------------------------------------------
    # Attachment download helpers
    #
    # Discord attachments (images / audio / documents) are fetched via the
    # authenticated bot session whenever the Attachment object exposes
    # ``read()``. That sidesteps two classes of bug that hit the older
    # plain-HTTP path:
    #
    #   1. ``cdn.discordapp.com`` URLs increasingly require bot auth on
    #      download — unauthenticated httpx sees 403 Forbidden.
    #      (issue #8242)
    #   2. Some user environments (VPNs, corporate DNS, tunnels) resolve
    #      ``cdn.discordapp.com`` to private-looking IPs that our
    #      ``is_safe_url`` guard classifies as SSRF risks. Routing the
    #      fetch through discord.py's own HTTP client handles DNS
    #      internally so our guard isn't consulted for the attachment
    #      path. (issue #6587)
    #
    # If ``att.read()`` is unavailable (unexpected object shape / test
    # stub) or the bot session fetch fails, we fall back to the existing
    # SSRF-gated URL downloaders. The fallback keeps defense-in-depth
    # against any future Discord payload-schema drift that could slip a
    # non-CDN URL into the ``att.url`` field. (issue #11345)
    # ------------------------------------------------------------------






    # ------------------------------------------------------------------
    # Text message aggregation (handles Discord client-side splits)
    # ------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Discord UI Components (outside the adapter class)
# ---------------------------------------------------------------------------


def _component_check_auth(
    interaction,
    allowed_user_ids: Optional[set],
    allowed_role_ids: Optional[set],
) -> bool:
    """Shared user-or-role OR semantics for component view button clicks.

    Mirrors ``DiscordAdapter._is_allowed_user`` / the slash and on_message
    gates so every Discord interaction surface honors the same trust
    boundary. Component views (ExecApprovalView, SlashConfirmView,
    UpdatePromptView, ModelPickerView) used to receive only
    ``allowed_user_ids``: in role-only deployments
    (DISCORD_ALLOWED_ROLES set, DISCORD_ALLOWED_USERS empty) the user
    set was empty and the legacy "no allowlist = allow everyone" branch
    let any guild member click the buttons -- approving exec commands,
    cancelling slash confirmations, switching the model.

    Behavior:

      - both allowlists empty -> allow (preserves existing no-allowlist
        deployments, no regression)
      - user is in user allowlist -> allow
      - role allowlist set + user has a role in it -> allow
      - role allowlist set + interaction.user has no resolvable
        ``roles`` attribute (e.g. DM context with a role policy active)
        -> reject (fail closed)
      - otherwise -> reject
    """
    user_set = allowed_user_ids or set()
    role_set = allowed_role_ids or set()
    has_users = bool(user_set)
    has_roles = bool(role_set)
    if not has_users and not has_roles:
        return True

    user = getattr(interaction, "user", None)
    if user is None:
        return False

    if has_users:
        try:
            uid = str(user.id)
        except AttributeError:
            uid = ""
        if uid and uid in user_set:
            return True

    if has_roles:
        roles_attr = getattr(user, "roles", None)
        if roles_attr is None:
            # Role policy is configured but the interaction doesn't
            # carry role data (DM-context Member, raw User payload).
            # Fail closed: a user without a resolvable role list cannot
            # satisfy a role allowlist.
            return False
        try:
            user_role_ids = {getattr(r, "id", None) for r in roles_attr}
        except TypeError:
            return False
        if user_role_ids & role_set:
            return True

    return False


def _define_discord_view_classes() -> None:
    """Register Discord UI view classes as module globals.

    Called at module load (when discord.py is pre-installed) and also from
    check_discord_requirements() after a lazy install, so view classes are
    always defined whenever DISCORD_AVAILABLE is True.  Without this,
    ExecApprovalView and siblings are only defined at import time; a later
    lazy install sets DISCORD_AVAILABLE=True but leaves the classes
    undefined, causing NameError on the first button interaction.
    """
    global ExecApprovalView, SlashConfirmView, UpdatePromptView, ModelPickerView, ChoicePickerView, ClarifyChoiceView

    from channels.platforms.discord_choice_picker import (
        create_choice_picker_view_class,
    )

    ChoicePickerView = create_choice_picker_view_class(
        discord,
        _component_check_auth,
    )

    class ExecApprovalView(discord.ui.View):
        """
        Interactive button view for exec approval of dangerous commands.

        Shows four buttons: Allow Once, Allow Session, Always Allow, Deny.
        Clicking a button calls ``resolve_gateway_approval()`` to unblock the
        waiting agent thread — the same mechanism as the text ``/approve`` flow.
        Only users in the allowed list can click.  Times out after 5 minutes.
        """

        def __init__(
            self,
            session_key: str,
            allowed_user_ids: set,
            allowed_role_ids: Optional[set] = None,
        ):
            super().__init__(timeout=300)  # 5-minute timeout
            self.session_key = session_key
            self.allowed_user_ids = allowed_user_ids
            self.allowed_role_ids = allowed_role_ids or set()
            self.resolved = False

        def _check_auth(self, interaction: discord.Interaction) -> bool:
            """Verify the user clicking is authorized."""
            return _component_check_auth(
                interaction, self.allowed_user_ids, self.allowed_role_ids,
            )

        async def _resolve(
            self, interaction: discord.Interaction, choice: str,
            color: discord.Color, label: str,
        ):
            """Resolve the approval via the gateway approval queue and update the embed."""
            if self.resolved:
                await interaction.response.send_message(
                    "This approval has already been resolved~", ephemeral=True
                )
                return

            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized to approve commands~", ephemeral=True
                )
                return

            self.resolved = True

            # Update the embed with the decision
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                embed.color = color
                embed.set_footer(text=f"{label} by {interaction.user.display_name}")

            # Disable all buttons
            for child in self.children:
                child.disabled = True

            await interaction.response.edit_message(embed=embed, view=self)

            # Unblock the waiting agent thread via the gateway approval queue
            try:
                from tools.approval import resolve_gateway_approval
                count = resolve_gateway_approval(self.session_key, choice)
                logger.info(
                    "Discord button resolved %d approval(s) for session %s (choice=%s, user=%s)",
                    count, self.session_key, choice, interaction.user.display_name,
                )
            except Exception as exc:
                logger.error("Failed to resolve gateway approval from button: %s", exc)

        @discord.ui.button(label="Allow Once", style=discord.ButtonStyle.green)
        async def allow_once(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "once", discord.Color.green(), "Approved once")

        @discord.ui.button(label="Allow Session", style=discord.ButtonStyle.grey)
        async def allow_session(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "session", discord.Color.blue(), "Approved for session")

        @discord.ui.button(label="Always Allow", style=discord.ButtonStyle.blurple)
        async def allow_always(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "always", discord.Color.purple(), "Approved permanently")

        @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
        async def deny(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "deny", discord.Color.red(), "Denied")

        async def on_timeout(self):
            """Handle view timeout -- disable buttons and mark as expired."""
            self.resolved = True
            for child in self.children:
                child.disabled = True

    class SlashConfirmView(discord.ui.View):
        """Three-button view for generic slash-command confirmations.

        Used by ``/reload-mcp`` and any future slash command routed through
        ``channels.slash_commands.confirmation.request_slash_confirm``.  Buttons map to the
        gateway's three choices:

          * "Approve Once"   → ``choice="once"``
          * "Always Approve" → ``choice="always"``
          * "Cancel"         → ``choice="cancel"``

        Clicking calls the module-level
        ``tools.slash_confirm.resolve(session_key, confirm_id, choice)``
        which runs the handler the runner stored for this ``session_key``.
        Only users in the adapter's allowlist can click.  Times out after
        5 minutes (matches the gateway primitive's timeout).
        """

        def __init__(
            self,
            session_key: str,
            confirm_id: str,
            allowed_user_ids: set,
            allowed_role_ids: Optional[set] = None,
        ):
            super().__init__(timeout=300)
            self.session_key = session_key
            self.confirm_id = confirm_id
            self.allowed_user_ids = allowed_user_ids
            self.allowed_role_ids = allowed_role_ids or set()
            self.resolved = False

        def _check_auth(self, interaction: discord.Interaction) -> bool:
            return _component_check_auth(
                interaction, self.allowed_user_ids, self.allowed_role_ids,
            )

        async def _resolve(
            self, interaction: discord.Interaction, choice: str,
            color: discord.Color, label: str,
        ):
            if self.resolved:
                await interaction.response.send_message(
                    "This prompt has already been resolved~", ephemeral=True,
                )
                return
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized to answer this prompt~", ephemeral=True,
                )
                return

            self.resolved = True

            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                embed.color = color
                embed.set_footer(text=f"{label} by {interaction.user.display_name}")

            for child in self.children:
                child.disabled = True

            await interaction.response.edit_message(embed=embed, view=self)

            # Resolve via the module-level primitive.  If the handler
            # returns a follow-up message, post it in the same channel.
            try:
                from tools import slash_confirm as _slash_confirm_mod
                result_text = await _slash_confirm_mod.resolve(
                    self.session_key, self.confirm_id, choice,
                )
                if result_text:
                    await interaction.followup.send(result_text)
                logger.info(
                    "Discord button resolved slash-confirm for session %s "
                    "(choice=%s, user=%s)",
                    self.session_key, choice, interaction.user.display_name,
                )
            except Exception as exc:
                logger.error("Discord slash-confirm resolve failed: %s", exc, exc_info=True)

        @discord.ui.button(label="Approve Once", style=discord.ButtonStyle.green)
        async def approve_once(
            self, interaction: discord.Interaction, button: discord.ui.Button,
        ):
            await self._resolve(interaction, "once", discord.Color.green(), "Approved once")

        @discord.ui.button(label="Always Approve", style=discord.ButtonStyle.blurple)
        async def approve_always(
            self, interaction: discord.Interaction, button: discord.ui.Button,
        ):
            await self._resolve(interaction, "always", discord.Color.purple(), "Always approved")

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
        async def cancel(
            self, interaction: discord.Interaction, button: discord.ui.Button,
        ):
            await self._resolve(interaction, "cancel", discord.Color.greyple(), "Cancelled")

        async def on_timeout(self):
            self.resolved = True
            for child in self.children:
                child.disabled = True

    class UpdatePromptView(discord.ui.View):
        """Interactive Yes/No buttons for ``hermes update`` prompts.

        Clicking a button writes the answer to ``.update_response`` so the
        detached update process can pick it up.  Only authorized users can
        click.  Times out after 5 minutes (the update process also has a
        5-minute timeout on its side).
        """

        def __init__(
            self,
            session_key: str,
            allowed_user_ids: set,
            allowed_role_ids: Optional[set] = None,
        ):
            super().__init__(timeout=300)
            self.session_key = session_key
            self.allowed_user_ids = allowed_user_ids
            self.allowed_role_ids = allowed_role_ids or set()
            self.resolved = False

        def _check_auth(self, interaction: discord.Interaction) -> bool:
            return _component_check_auth(
                interaction, self.allowed_user_ids, self.allowed_role_ids,
            )

        async def _respond(
            self, interaction: discord.Interaction, answer: str,
            color: discord.Color, label: str,
        ):
            if self.resolved:
                await interaction.response.send_message(
                    "Already answered~", ephemeral=True
                )
                return
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized~", ephemeral=True
                )
                return

            self.resolved = True

            # Update embed
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                embed.color = color
                embed.set_footer(text=f"{label} by {interaction.user.display_name}")

            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)

            # Write response file
            try:
                from hermes_constants import get_hermes_home
                home = get_hermes_home()
                response_path = home / ".update_response"
                tmp = response_path.with_suffix(".tmp")
                tmp.write_text(answer)
                tmp.replace(response_path)
                logger.info(
                    "Discord update prompt answered '%s' by %s",
                    answer, interaction.user.display_name,
                )
            except Exception as exc:
                logger.error("Failed to write update response: %s", exc)

        @discord.ui.button(label="Yes", style=discord.ButtonStyle.green, emoji="✓")
        async def yes_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._respond(interaction, "y", discord.Color.green(), "Yes")

        @discord.ui.button(label="No", style=discord.ButtonStyle.red, emoji="✗")
        async def no_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._respond(interaction, "n", discord.Color.red(), "No")

        async def on_timeout(self):
            self.resolved = True
            for child in self.children:
                child.disabled = True

    class ModelPickerView(discord.ui.View):
        """Interactive select-menu view for model switching.

        Two-step drill-down: provider dropdown → model dropdown.
        Edits the original message in-place as the user navigates.
        Times out after 2 minutes.
        """

        def __init__(
            self,
            providers: list,
            current_model: str,
            current_provider: str,
            session_key: str,
            on_model_selected,
            allowed_user_ids: set,
            allowed_role_ids: Optional[set] = None,
        ):
            super().__init__(timeout=120)
            self.providers = providers
            self.current_model = current_model
            self.current_provider = current_provider
            self.session_key = session_key
            self.on_model_selected = on_model_selected
            self.allowed_user_ids = allowed_user_ids
            self.allowed_role_ids = allowed_role_ids or set()
            self.resolved = False
            self._selected_provider: str = ""
            self._pending_expensive_model: str = ""

            self._build_provider_select()

        def _check_auth(self, interaction: discord.Interaction) -> bool:
            return _component_check_auth(
                interaction, self.allowed_user_ids, self.allowed_role_ids,
            )

        def _build_provider_select(self):
            """Build the provider dropdown menu."""
            self.clear_items()
            options = []
            for p in self.providers:
                count = p.get("total_models", len(p.get("models", [])))
                label = f"{p['name']} ({count} models)"
                desc = "current" if p.get("is_current") else None
                options.append(
                    discord.SelectOption(
                        label=label[:100],
                        value=p["slug"],
                        description=desc,
                    )
                )
            if not options:
                return

            select = discord.ui.Select(
                placeholder="Choose a provider...",
                options=options[:25],
                custom_id="model_provider_select",
            )
            select.callback = self._on_provider_selected
            self.add_item(select)

            cancel_btn = discord.ui.Button(
                label="Cancel", style=discord.ButtonStyle.red, custom_id="model_cancel"
            )
            cancel_btn.callback = self._on_cancel
            self.add_item(cancel_btn)

        def _build_model_select(self, provider_slug: str):
            """Build the model dropdown for a specific provider."""
            self.clear_items()
            provider = next(
                (p for p in self.providers if p["slug"] == provider_slug), None
            )
            if not provider:
                return

            models = provider.get("models", [])
            options = []
            for model_id in models[:25]:
                short = model_id.split("/")[-1] if "/" in model_id else model_id
                options.append(
                    discord.SelectOption(
                        label=short[:100],
                        value=model_id[:100],
                    )
                )
            if not options:
                return

            select = discord.ui.Select(
                placeholder=f"Choose a model from {provider.get('name', provider_slug)}...",
                options=options,
                custom_id="model_model_select",
            )
            select.callback = self._on_model_selected
            self.add_item(select)

            back_btn = discord.ui.Button(
                label="◀ Back", style=discord.ButtonStyle.grey, custom_id="model_back"
            )
            back_btn.callback = self._on_back
            self.add_item(back_btn)

            cancel_btn = discord.ui.Button(
                label="Cancel", style=discord.ButtonStyle.red, custom_id="model_cancel2"
            )
            cancel_btn.callback = self._on_cancel
            self.add_item(cancel_btn)

        def _build_expensive_confirm(self, model_id: str):
            """Build confirmation buttons for unusually expensive models."""
            self.clear_items()
            self._pending_expensive_model = model_id

            confirm_btn = discord.ui.Button(
                label="Switch anyway",
                style=discord.ButtonStyle.red,
                custom_id="model_expensive_confirm",
            )
            confirm_btn.callback = self._on_expensive_confirm
            self.add_item(confirm_btn)

            cancel_btn = discord.ui.Button(
                label="Cancel",
                style=discord.ButtonStyle.grey,
                custom_id="model_expensive_cancel",
            )
            cancel_btn.callback = self._on_cancel
            self.add_item(cancel_btn)

        async def _expensive_warning_for(self, model_id: str):
            try:
                from hermes_cli.model_cost_guard import expensive_model_warning

                # Pricing lookup can hit models.dev / a /models endpoint on a
                # cache miss — keep it off the event loop.
                return await asyncio.to_thread(
                    expensive_model_warning,
                    model_id,
                    provider=self._selected_provider,
                )
            except Exception:
                return None

        async def _on_provider_selected(self, interaction: discord.Interaction):
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized~", ephemeral=True
                )
                return

            provider_slug = interaction.data["values"][0]
            self._selected_provider = provider_slug
            provider = next(
                (p for p in self.providers if p["slug"] == provider_slug), None
            )
            pname = provider.get("name", provider_slug) if provider else provider_slug

            self._build_model_select(provider_slug)

            total = provider.get("total_models", 0) if provider else 0
            shown = min(len(provider.get("models", [])), 25) if provider else 0
            extra = f"\n*{total - shown} more available — type `/model <name>` directly*" if total > shown else ""

            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚙ Model Configuration",
                    description=f"Provider: **{pname}**\nSelect a model:{extra}",
                    color=discord.Color.blue(),
                ),
                view=self,
            )

        async def _switch_selected_model(
            self,
            interaction: discord.Interaction,
            model_id: str,
        ):
            if self.resolved:
                await interaction.response.send_message(
                    "Already resolved~", ephemeral=True
                )
                return
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized~", ephemeral=True
                )
                return

            self.resolved = True
            self.clear_items()
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚙ Switching Model",
                    description=f"Switching to `{model_id}`...",
                    color=discord.Color.blue(),
                ),
                view=None,
            )

            try:
                result_text = await self.on_model_selected(
                    str(interaction.channel_id),
                    model_id,
                    self._selected_provider,
                )
            except Exception as exc:
                result_text = f"Error switching model: {exc}"

            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="⚙ Model Switched",
                    description=result_text,
                    color=discord.Color.green(),
                ),
                view=None,
            )

        async def _on_model_selected(self, interaction: discord.Interaction):
            if self.resolved:
                await interaction.response.send_message(
                    "Already resolved~", ephemeral=True
                )
                return
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized~", ephemeral=True
                )
                return

            model_id = interaction.data["values"][0]
            warning = await self._expensive_warning_for(model_id)
            if warning is not None:
                self._build_expensive_confirm(model_id)
                await interaction.response.edit_message(
                    embed=discord.Embed(
                        title="⚠ Expensive Model Warning",
                        description=warning.message,
                        color=discord.Color.red(),
                    ),
                    view=self,
                )
                return

            await self._switch_selected_model(interaction, model_id)

        async def _on_expensive_confirm(self, interaction: discord.Interaction):
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized~", ephemeral=True
                )
                return
            if not self._pending_expensive_model:
                await interaction.response.send_message(
                    "Model selection expired.", ephemeral=True
                )
                return
            await self._switch_selected_model(
                interaction,
                self._pending_expensive_model,
            )

        async def _on_back(self, interaction: discord.Interaction):
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized~", ephemeral=True
                )
                return

            self._build_provider_select()

            try:
                from hermes_cli.providers import get_label
                provider_label = get_label(self.current_provider)
            except Exception:
                provider_label = self.current_provider

            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚙ Model Configuration",
                    description=(
                        f"Current model: `{self.current_model or 'unknown'}`\n"
                        f"Provider: {provider_label}\n\n"
                        f"Select a provider:"
                    ),
                    color=discord.Color.blue(),
                ),
                view=self,
            )

        async def _on_cancel(self, interaction: discord.Interaction):
            self.resolved = True
            self.clear_items()
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚙ Model Configuration",
                    description="Model selection cancelled.",
                    color=discord.Color.greyple(),
                ),
                view=self,
            )

        async def on_timeout(self):
            self.resolved = True
            self.clear_items()


    class ClarifyChoiceView(discord.ui.View):
        """Interactive button view for the clarify tool's multiple-choice prompts.

        Renders one button per choice (max 24) plus a final ``✏️ Other`` button.
        Picking a numeric choice resolves the gateway clarify entry immediately;
        picking ``Other`` flips the entry into text-capture mode so the next
        user message in the session becomes the response (the gateway's
        text-intercept handles the resolution).

        Auth gating mirrors ``ExecApprovalView`` — only users/roles in the
        Discord adapter's allowlist may answer. Single-use: after the first
        valid click all buttons disable and the embed updates to show who
        answered and what they chose.
        """

        def __init__(
            self,
            choices: List[str],
            clarify_id: str,
            allowed_user_ids: set,
            allowed_role_ids: Optional[set] = None,
        ):
            super().__init__(timeout=300)  # 5-minute timeout
            self.choices = list(choices)[:24]
            self.clarify_id = clarify_id
            self.allowed_user_ids = allowed_user_ids
            self.allowed_role_ids = allowed_role_ids or set()
            self.resolved = False

            for index, choice in enumerate(self.choices):
                # Discord button labels are capped at 80 chars.
                label_body = choice if len(choice) <= 75 else choice[:72] + "..."
                button = discord.ui.Button(
                    label=f"{index + 1}. {label_body}",
                    style=discord.ButtonStyle.primary,
                    custom_id=f"clarify:{clarify_id}:{index}",
                )
                button.callback = self._make_choice_callback(index, choice)
                self.add_item(button)

            other_btn = discord.ui.Button(
                label="✏️ Other (type answer)",
                style=discord.ButtonStyle.secondary,
                custom_id=f"clarify:{clarify_id}:other",
            )
            other_btn.callback = self._on_other
            self.add_item(other_btn)

        def _check_auth(self, interaction: "discord.Interaction") -> bool:
            return _component_check_auth(
                interaction, self.allowed_user_ids, self.allowed_role_ids,
            )

        def _make_choice_callback(self, index: int, choice: str):
            async def _callback(interaction: "discord.Interaction"):
                await self._resolve_choice(interaction, index, choice)
            return _callback

        async def _resolve_choice(
            self,
            interaction: "discord.Interaction",
            index: int,
            choice: str,
        ) -> None:
            """Resolve the clarify with a chosen option."""
            if self.resolved:
                await interaction.response.send_message(
                    "This prompt has already been answered~", ephemeral=True,
                )
                return
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized to answer this prompt~", ephemeral=True,
                )
                return

            self.resolved = True
            for child in self.children:
                child.disabled = True

            embed = interaction.message.embeds[0] if (
                interaction.message and interaction.message.embeds
            ) else None
            if embed:
                user = getattr(interaction, "user", None)
                display_name = getattr(user, "display_name", "user")
                embed.color = discord.Color.green()
                embed.set_footer(text=f"Answered by {display_name}: {choice}")

            try:
                await interaction.response.edit_message(embed=embed, view=self)
            except Exception:
                logger.debug(
                    "Discord clarify edit_message failed for %s",
                    self.clarify_id,
                    exc_info=True,
                )
                try:
                    await interaction.response.defer()
                except Exception:
                    pass

            # Resolve via the gateway clarify primitive — same mechanism as
            # Telegram. Look up the canonical choice text from the entry so
            # we round-trip the original value, not a button-label variant.
            resolved_text: Optional[str] = None
            try:
                from tools.clarify_gateway import _entries as _clarify_entries  # type: ignore
                entry = _clarify_entries.get(self.clarify_id)
                if entry and entry.choices and 0 <= index < len(entry.choices):
                    resolved_text = entry.choices[index]
            except Exception:
                resolved_text = None
            if resolved_text is None:
                resolved_text = choice

            try:
                from tools.clarify_gateway import resolve_gateway_clarify
                resolved = resolve_gateway_clarify(self.clarify_id, resolved_text)
                logger.info(
                    "Discord clarify button resolved (id=%s, choice=%r, user=%s, ok=%s)",
                    self.clarify_id, resolved_text,
                    getattr(getattr(interaction, "user", None), "display_name", "?"),
                    resolved,
                )
            except Exception as exc:
                logger.error(
                    "Discord clarify resolve_gateway_clarify failed (id=%s): %s",
                    self.clarify_id, exc,
                )

        async def _on_other(self, interaction: "discord.Interaction") -> None:
            """Flip the clarify entry into text-capture mode."""
            if self.resolved:
                await interaction.response.send_message(
                    "This prompt has already been answered~", ephemeral=True,
                )
                return
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized to answer this prompt~", ephemeral=True,
                )
                return

            # Don't pop the entry — the gateway's text-intercept needs it
            # until the user actually types. Just mark it as awaiting text
            # and disable the buttons so the user can't double-click.
            try:
                from tools.clarify_gateway import mark_awaiting_text
                mark_awaiting_text(self.clarify_id)
            except Exception as exc:
                logger.warning(
                    "Discord clarify mark_awaiting_text failed (id=%s): %s",
                    self.clarify_id, exc,
                )

            self.resolved = True
            for child in self.children:
                child.disabled = True

            embed = interaction.message.embeds[0] if (
                interaction.message and interaction.message.embeds
            ) else None
            if embed:
                user = getattr(interaction, "user", None)
                display_name = getattr(user, "display_name", "user")
                embed.color = discord.Color.blue()
                embed.set_footer(
                    text=f"Awaiting typed response from {display_name}…",
                )

            try:
                await interaction.response.edit_message(embed=embed, view=self)
            except Exception:
                try:
                    await interaction.response.defer()
                except Exception:
                    pass

        async def on_timeout(self):
            self.resolved = True
            for child in self.children:
                child.disabled = True


if DISCORD_AVAILABLE:
    _define_discord_view_classes()
