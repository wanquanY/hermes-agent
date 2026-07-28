"""
Gateway configuration management.

Handles loading and validating configuration for:
- Connected platforms (Telegram, Discord, WhatsApp, Weixin, and more)
- Home channels for each platform
- Session reset policies
- Delivery preferences
"""

import logging
import os
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable

from channels.config import (
    _BUILTIN_PLATFORM_VALUES,
    HomeChannel,
    Platform,
    PlatformConfig,
)
from hermes_cli.config import get_hermes_home
from utils import is_truthy_value

logger = logging.getLogger(__name__)


def _coerce_bool(value: Any, default: bool = True) -> bool:
    """Coerce bool-ish config values, preserving a caller-provided default."""
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        return default
    return is_truthy_value(value, default=default)


def _coerce_float(value: Any, default: float) -> float:
    """Coerce numeric config values, falling back on malformed input."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    """Coerce integer config values, falling back on malformed input."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_unauthorized_dm_behavior(value: Any, default: str = "pair") -> str:
    """Normalize unauthorized DM behavior to a supported value."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"pair", "ignore"}:
            return normalized
    return default


def _normalize_notice_delivery(value: Any, default: str = "public") -> str:
    """Normalize notice delivery mode to a supported value."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"public", "private"}:
            return normalized
    return default


def _ensure_platform_extra_dict(platforms_data: dict, name: str) -> tuple[dict, dict]:
    """Get-or-create ``platforms_data[name]`` and its nested ``extra`` dict.

    Both slots are coerced to ``{}`` if a non-dict value is encountered, so
    callers can safely write keys without type-checking.  Returns
    ``(plat_data, extra)`` for in-place mutation.
    """
    plat_data = platforms_data.setdefault(name, {})
    if not isinstance(plat_data, dict):
        plat_data = {}
        platforms_data[name] = plat_data
    extra = plat_data.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        plat_data["extra"] = extra
    return plat_data, extra


@dataclass
class SessionResetPolicy:
    """
    Controls when sessions reset (lose context).
    
    Modes:
    - "daily": Reset at a specific hour each day
    - "idle": Reset after N minutes of inactivity
    - "both": Whichever triggers first (daily boundary OR idle timeout)
    - "none": Never auto-reset (context managed only by compression)
    """
    mode: str = "both"  # "daily", "idle", "both", or "none"
    at_hour: int = 4  # Hour for daily reset (0-23, local time)
    idle_minutes: int = 1440  # Minutes of inactivity before reset (24 hours)
    notify: bool = True  # Send a notification to the user when auto-reset occurs
    notify_exclude_platforms: tuple = ("api_server", "webhook")  # Platforms that don't get reset notifications
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "at_hour": self.at_hour,
            "idle_minutes": self.idle_minutes,
            "notify": self.notify,
            "notify_exclude_platforms": list(self.notify_exclude_platforms),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionResetPolicy":
        # Handle both missing keys and explicit null values (YAML null → None)
        mode = data.get("mode")
        at_hour = data.get("at_hour")
        idle_minutes = data.get("idle_minutes")
        notify = data.get("notify")
        exclude = data.get("notify_exclude_platforms")
        return cls(
            mode=mode if mode is not None else "both",
            at_hour=at_hour if at_hour is not None else 4,
            idle_minutes=idle_minutes if idle_minutes is not None else 1440,
            notify=_coerce_bool(notify, True),
            notify_exclude_platforms=tuple(exclude) if exclude is not None else ("api_server", "webhook"),
        )


# Streaming defaults — single source of truth so both StreamingConfig and
# StreamConsumerConfig agree on the out-of-the-box edit rhythm.  Tuned for
# Telegram's ~1 edit/s flood envelope: a touch under 1s lets the cadence
# breathe without bumping into rate limits, and a smaller buffer threshold
# makes short replies feel near-instant in DMs.
DEFAULT_STREAMING_EDIT_INTERVAL: float = 0.8
DEFAULT_STREAMING_BUFFER_THRESHOLD: int = 24
DEFAULT_STREAMING_CURSOR: str = " ▉"


def _normalize_streaming_transport(value: Any, default: str = "auto") -> str:
    """Normalize streaming mode tokens, including YAML 1.1 on/off booleans."""
    if value is None:
        return default
    if isinstance(value, bool):
        return "auto" if value else "off"
    return str(value).strip().lower() or default


@dataclass
class StreamingConfig:
    """Configuration for real-time token streaming to messaging platforms."""
    enabled: bool = False
    # Transport selection:
    #   "auto"  — prefer native streaming-draft updates when the platform
    #             supports them (Telegram sendMessageDraft, Bot API 9.5+);
    #             fall back to edit-based when not.
    #   "draft" — explicitly request native drafts; falls back to edit when
    #             the platform/chat doesn't support them.
    #   "edit"  — progressive editMessageText only (legacy/default
    #             behaviour).
    #   "off"   — disable streaming entirely.
    transport: str = "edit"
    edit_interval: float = DEFAULT_STREAMING_EDIT_INTERVAL
    buffer_threshold: int = DEFAULT_STREAMING_BUFFER_THRESHOLD
    cursor: str = DEFAULT_STREAMING_CURSOR
    # Ported from openclaw/openclaw#72038.  When >0, the final edit for
    # a long-running streamed response is delivered as a fresh message
    # if the original preview has been visible for at least this many
    # seconds, so the platform's visible timestamp reflects completion
    # time instead of the preview creation time.  Currently applied to
    # Telegram only (other platforms ignore the setting).  Default 60s
    # matches the OpenClaw rollout.  Set to 0 to disable.
    fresh_final_after_seconds: float = 60.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "transport": self.transport,
            "edit_interval": self.edit_interval,
            "buffer_threshold": self.buffer_threshold,
            "cursor": self.cursor,
            "fresh_final_after_seconds": self.fresh_final_after_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StreamingConfig":
        if not isinstance(data, dict) or not data:
            return cls()

        raw_transport = data.get("transport")
        raw_mode = data.get("mode")
        picked_transport = raw_transport if raw_transport is not None else raw_mode
        transport = _normalize_streaming_transport(picked_transport, default="edit")
        if "enabled" in data:
            enabled = _coerce_bool(data.get("enabled"), False)
        elif raw_mode is not None:
            enabled = _normalize_streaming_transport(raw_mode) != "off"
        else:
            # `transport` selects how an already-enabled stream is delivered;
            # only the ergonomic `mode` alias implies activation.
            enabled = False
        return cls(
            enabled=enabled,
            transport=transport,
            edit_interval=_coerce_float(
                data.get("edit_interval"), DEFAULT_STREAMING_EDIT_INTERVAL,
            ),
            buffer_threshold=_coerce_int(
                data.get("buffer_threshold"), DEFAULT_STREAMING_BUFFER_THRESHOLD,
            ),
            cursor=data.get("cursor", DEFAULT_STREAMING_CURSOR),
            fresh_final_after_seconds=_coerce_float(
                data.get("fresh_final_after_seconds"), 60.0
            ),
        )


# -----------------------------------------------------------------------------
# Built-in platform connection checkers
# -----------------------------------------------------------------------------
# Each callable receives a ``PlatformConfig`` and returns ``True`` when the
# platform is sufficiently configured to be considered "connected".  Platforms
# that rely on the generic ``token or api_key`` check (Telegram, Discord,
# Slack, Matrix, Mattermost, HomeAssistant) do not need an entry here.
_PLATFORM_CONNECTED_CHECKERS: dict[Platform, Callable[[PlatformConfig], bool]] = {
    Platform.WEIXIN: lambda cfg: bool(
        cfg.extra.get("account_id") and (cfg.token or cfg.extra.get("token"))
    ),
    Platform.WHATSAPP: lambda cfg: True,  # bridge handles auth
    Platform.SIGNAL: lambda cfg: bool(cfg.extra.get("http_url")),
    Platform.EMAIL: lambda cfg: bool(cfg.extra.get("address")),
    Platform.SMS: lambda cfg: bool(os.getenv("TWILIO_ACCOUNT_SID")),
    Platform.API_SERVER: lambda cfg: True,
    Platform.WEBHOOK: lambda cfg: True,
    Platform.MSGRAPH_WEBHOOK: lambda cfg: True,
    Platform.FEISHU: lambda cfg: bool(cfg.extra.get("app_id")),
    Platform.WECOM: lambda cfg: bool(cfg.extra.get("bot_id")),
    Platform.WECOM_CALLBACK: lambda cfg: bool(
        cfg.extra.get("corp_id") or cfg.extra.get("apps")
    ),
    Platform.BLUEBUBBLES: lambda cfg: bool(
        cfg.extra.get("server_url") and cfg.extra.get("password")
    ),
    Platform.QQBOT: lambda cfg: bool(
        cfg.extra.get("app_id") and cfg.extra.get("client_secret")
    ),
    Platform.YUANBAO: lambda cfg: bool(
        cfg.extra.get("app_id") and cfg.extra.get("app_secret")
    ),
    Platform.DINGTALK: lambda cfg: bool(
        (cfg.extra.get("client_id") or os.getenv("DINGTALK_CLIENT_ID"))
        and (cfg.extra.get("client_secret") or os.getenv("DINGTALK_CLIENT_SECRET"))
    ),
}


@dataclass
class GatewayConfig:
    """
    Main gateway configuration.
    
    Manages all platform connections, session policies, and delivery settings.
    """
    # Platform configurations
    platforms: Dict[Platform, PlatformConfig] = field(default_factory=dict)
    
    # Session reset policies by type
    default_reset_policy: SessionResetPolicy = field(default_factory=SessionResetPolicy)
    reset_by_type: Dict[str, SessionResetPolicy] = field(default_factory=dict)
    reset_by_platform: Dict[Platform, SessionResetPolicy] = field(default_factory=dict)
    
    # Reset trigger commands
    reset_triggers: List[str] = field(default_factory=lambda: ["/new", "/reset"])

    # User-defined quick commands (slash commands that bypass the agent loop)
    quick_commands: Dict[str, Any] = field(default_factory=dict)
    
    # Storage paths
    sessions_dir: Path = field(default_factory=lambda: get_hermes_home() / "sessions")
    
    # Delivery settings
    always_log_local: bool = True  # Always save cron outputs to local files

    # STT settings
    stt_enabled: bool = True  # Whether to auto-transcribe inbound voice messages
    stt_echo_transcripts: bool = True  # Whether to echo raw transcripts to the user

    # Session isolation in shared chats
    group_sessions_per_user: bool = True  # Isolate group/channel sessions per participant when user IDs are available
    thread_sessions_per_user: bool = False  # When False (default), threads are shared across all participants

    # One process can serve isolated profile runtimes. Sources are stamped
    # before authorization/session lookup, and every turn enters that profile's
    # home + credential scope.
    multiplex_profiles: bool = False
    profile_routes: list = field(default_factory=list)

    # Unauthorized DM policy
    unauthorized_dm_behavior: str = "pair"  # "pair" or "ignore"

    # Streaming configuration
    streaming: StreamingConfig = field(default_factory=StreamingConfig)

    # Session store pruning: drop SessionEntry records older than this many
    # days from the in-memory dict and sessions.json.  Keeps the store from
    # growing unbounded in gateways serving many chats/threads/users over
    # months.  Pruning is invisible to users — if they resume, they get a
    # fresh session exactly as if the reset policy had fired.  0 = disabled.
    session_store_max_age_days: int = 90

    def get_connected_platforms(self) -> List[Platform]:
        """Return configured platforms in a byte-stable order."""
        connected = []
        for platform, config in self.platforms.items():
            if not config.enabled:
                continue
            if self._is_platform_connected(platform, config):
                connected.append(platform)
        return sorted(connected, key=lambda platform: str(platform.value))

    def _is_platform_connected(self, platform: Platform, config: PlatformConfig) -> bool:
        """Check whether a single platform is sufficiently configured."""
        # Weixin requires both a token and an account_id (checked first so
        # the generic token branch doesn't let it through without account_id).
        if platform == Platform.WEIXIN:
            return bool(
                config.extra.get("account_id")
                and (config.token or config.extra.get("token"))
            )

        # Generic token/api_key auth covers Telegram, Discord, Slack, etc.
        if config.token or config.api_key:
            return True

        # Platform-specific check
        checker = _PLATFORM_CONNECTED_CHECKERS.get(platform)
        if checker is not None:
            return checker(config)

        # Plugin-registered platforms
        try:
            from channels.platform_registry import platform_registry
            entry = platform_registry.get(platform.value)
            if entry:
                if entry.is_connected is not None:
                    return entry.is_connected(config)
                if entry.validate_config is not None:
                    return entry.validate_config(config)
                return True
        except Exception:
            logger.debug("Platform registry unavailable while checking %s", platform.value, exc_info=True)

        return False
    
    def get_home_channel(self, platform: Platform) -> Optional[HomeChannel]:
        """Get the home channel for a platform."""
        config = self.platforms.get(platform)
        if config:
            return config.home_channel
        return None
    
    def get_reset_policy(
        self, 
        platform: Optional[Platform] = None,
        session_type: Optional[str] = None
    ) -> SessionResetPolicy:
        """
        Get the appropriate reset policy for a session.
        
        Priority: platform override > type override > default
        """
        # Platform-specific override takes precedence
        if platform and platform in self.reset_by_platform:
            return self.reset_by_platform[platform]
        
        # Type-specific override (dm, group, thread)
        if session_type and session_type in self.reset_by_type:
            return self.reset_by_type[session_type]
        
        return self.default_reset_policy
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "platforms": {
                p.value: c.to_dict() for p, c in self.platforms.items()
            },
            "default_reset_policy": self.default_reset_policy.to_dict(),
            "reset_by_type": {
                k: v.to_dict() for k, v in self.reset_by_type.items()
            },
            "reset_by_platform": {
                p.value: v.to_dict() for p, v in self.reset_by_platform.items()
            },
            "reset_triggers": self.reset_triggers,
            "quick_commands": self.quick_commands,
            "sessions_dir": str(self.sessions_dir),
            "always_log_local": self.always_log_local,
            "stt_enabled": self.stt_enabled,
            "stt_echo_transcripts": self.stt_echo_transcripts,
            "group_sessions_per_user": self.group_sessions_per_user,
            "thread_sessions_per_user": self.thread_sessions_per_user,
            "multiplex_profiles": self.multiplex_profiles,
            "profile_routes": [
                {
                    "name": route.name,
                    "platform": route.platform,
                    "profile": route.profile,
                    "scope_id": route.scope_id,
                    "chat_id": route.chat_id,
                    "thread_id": route.thread_id,
                    "enabled": route.enabled,
                }
                if hasattr(route, "profile")
                else route
                for route in self.profile_routes
            ],
            "unauthorized_dm_behavior": self.unauthorized_dm_behavior,
            "streaming": self.streaming.to_dict(),
            "session_store_max_age_days": self.session_store_max_age_days,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GatewayConfig":
        platforms = {}
        for platform_name, platform_data in data.get("platforms", {}).items():
            try:
                platform = Platform(platform_name)
                platforms[platform] = PlatformConfig.from_dict(platform_data)
            except ValueError:
                logger.debug("Skipping unknown gateway platform %s", platform_name)
        
        reset_by_type = {}
        for type_name, policy_data in data.get("reset_by_type", {}).items():
            reset_by_type[type_name] = SessionResetPolicy.from_dict(policy_data)
        
        reset_by_platform = {}
        for platform_name, policy_data in data.get("reset_by_platform", {}).items():
            try:
                platform = Platform(platform_name)
                reset_by_platform[platform] = SessionResetPolicy.from_dict(policy_data)
            except ValueError:
                logger.debug("Suppressed recoverable gateway exception", exc_info=True)
        
        default_policy = SessionResetPolicy()
        if "default_reset_policy" in data:
            default_policy = SessionResetPolicy.from_dict(data["default_reset_policy"])
        
        sessions_dir = get_hermes_home() / "sessions"
        if "sessions_dir" in data:
            sessions_dir = Path(data["sessions_dir"])
        
        quick_commands = data.get("quick_commands", {})
        if not isinstance(quick_commands, dict):
            quick_commands = {}

        stt_enabled = data.get("stt_enabled")
        if stt_enabled is None:
            stt_enabled = data.get("stt", {}).get("enabled") if isinstance(data.get("stt"), dict) else None
        stt_echo_transcripts = data.get("stt_echo_transcripts")
        if stt_echo_transcripts is None and isinstance(data.get("stt"), dict):
            stt_echo_transcripts = data["stt"].get("echo_transcripts")

        group_sessions_per_user = data.get("group_sessions_per_user")
        thread_sessions_per_user = data.get("thread_sessions_per_user")
        nested_gateway = data.get("gateway")
        if not isinstance(nested_gateway, dict):
            nested_gateway = {}
        multiplex_profiles = data.get(
            "multiplex_profiles",
            nested_gateway.get("multiplex_profiles"),
        )
        env_multiplex = os.getenv("GATEWAY_MULTIPLEX_PROFILES", "").strip().lower()
        if env_multiplex in {"true", "1", "yes", "on"}:
            multiplex_profiles = True
        elif env_multiplex in {"false", "0", "no", "off"}:
            multiplex_profiles = False
        from hermes_gateway.profile_routing import parse_profile_routes

        profile_routes = parse_profile_routes(
            data.get("profile_routes", nested_gateway.get("profile_routes")) or []
        )
        unauthorized_dm_behavior = _normalize_unauthorized_dm_behavior(
            data.get("unauthorized_dm_behavior"),
            "pair",
        )

        try:
            session_store_max_age_days = int(data.get("session_store_max_age_days", 90))
            session_store_max_age_days = max(session_store_max_age_days, 0)
        except (TypeError, ValueError):
            session_store_max_age_days = 90

        return cls(
            platforms=platforms,
            default_reset_policy=default_policy,
            reset_by_type=reset_by_type,
            reset_by_platform=reset_by_platform,
            reset_triggers=data.get("reset_triggers", ["/new", "/reset"]),
            quick_commands=quick_commands,
            sessions_dir=sessions_dir,
            always_log_local=_coerce_bool(data.get("always_log_local"), True),
            stt_enabled=_coerce_bool(stt_enabled, True),
            stt_echo_transcripts=_coerce_bool(stt_echo_transcripts, True),
            group_sessions_per_user=_coerce_bool(group_sessions_per_user, True),
            thread_sessions_per_user=_coerce_bool(thread_sessions_per_user, False),
            multiplex_profiles=_coerce_bool(multiplex_profiles, False),
            profile_routes=profile_routes,
            unauthorized_dm_behavior=unauthorized_dm_behavior,
            streaming=StreamingConfig.from_dict(data.get("streaming", {})),
            session_store_max_age_days=session_store_max_age_days,
        )

    def get_unauthorized_dm_behavior(self, platform: Optional[Platform] = None) -> str:
        """Return the effective unauthorized-DM behavior for a platform."""
        if platform:
            platform_cfg = self.platforms.get(platform)
            if platform_cfg and "unauthorized_dm_behavior" in platform_cfg.extra:
                return _normalize_unauthorized_dm_behavior(
                    platform_cfg.extra.get("unauthorized_dm_behavior"),
                    self.unauthorized_dm_behavior,
                )
        return self.unauthorized_dm_behavior

    def get_notice_delivery(self, platform: Optional[Platform] = None) -> str:
        """Return the effective notice-delivery mode for a platform."""
        if platform:
            platform_cfg = self.platforms.get(platform)
            if platform_cfg and "notice_delivery" in platform_cfg.extra:
                return _normalize_notice_delivery(
                    platform_cfg.extra.get("notice_delivery"),
                    "public",
                )
        return "public"
