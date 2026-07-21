"""Matrix adapter support types, constants, and dependency checks."""

from __future__ import annotations

import logging
import mimetypes
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.secret_scope import get_profile_env

try:
    from mautrix.types import (
        ContentURI,
        EventID,
        EventType,
        PaginationDirection,
        PresenceState,
        RoomCreatePreset,
        RoomID,
        SyncToken,
        TrustState,
        UserID,
    )
except ImportError:
    ContentURI = EventID = RoomID = SyncToken = UserID = str  # type: ignore[misc,assignment]

    class _EventTypeStub:  # type: ignore[no-redef]
        ROOM_MESSAGE = "m.room.message"
        REACTION = "m.reaction"
        ROOM_ENCRYPTED = "m.room.encrypted"
        ROOM_NAME = "m.room.name"

    EventType = _EventTypeStub  # type: ignore[misc,assignment]

    class _PaginationDirectionStub:  # type: ignore[no-redef]
        BACKWARD = "b"
        FORWARD = "f"

    PaginationDirection = _PaginationDirectionStub  # type: ignore[misc,assignment]

    class _PresenceStateStub:  # type: ignore[no-redef]
        ONLINE = "online"
        OFFLINE = "offline"
        UNAVAILABLE = "unavailable"

    PresenceState = _PresenceStateStub  # type: ignore[misc,assignment]

    class _RoomCreatePresetStub:  # type: ignore[no-redef]
        PRIVATE = "private_chat"
        PUBLIC = "public_chat"
        TRUSTED_PRIVATE = "trusted_private_chat"

    RoomCreatePreset = _RoomCreatePresetStub  # type: ignore[misc,assignment]

    class _TrustStateStub:  # type: ignore[no-redef]
        UNVERIFIED = 0
        VERIFIED = 1

    TrustState = _TrustStateStub  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)

@dataclass
class _MatrixApprovalPrompt:
    """Tracks a pending Matrix reaction-based exec approval prompt."""

    def __init__(self, session_key: str, chat_id: str, message_id: str, resolved: bool = False):
        self.session_key = session_key
        self.chat_id = chat_id
        self.message_id = message_id
        self.resolved = resolved
        self.bot_reaction_events: dict[str, str] = {}  # emoji -> event_id


@dataclass
class _MatrixChoicePickerPrompt:
    """Tracks a pending reaction-based finite-choice command prompt."""

    chat_id: str
    message_id: str
    session_key: str
    choices: dict[str, str]
    on_choice_selected: Any
    requester_user_id: str | None = None
    expires_at: float | None = None
    resolved: bool = False
    bot_reaction_events: dict[str, str] = field(default_factory=dict)

# Matrix permits large events, but very large bodies still render poorly in
# some clients. 16K avoids needlessly splitting Markdown tables while staying
# well below the event-size ceiling.
DEFAULT_MAX_MESSAGE_LENGTH = 16_000
MATRIX_MAX_MESSAGE_LENGTH_CEILING = 65_535


def resolve_max_message_length(config: Any) -> int:
    """Resolve the per-adapter outbound chunk limit with explicit precedence."""
    extra = getattr(config, "extra", {}) or {}
    raw = extra.get("max_message_length")
    if raw is None:
        raw = get_profile_env("MATRIX_MAX_MESSAGE_LENGTH", "") or None
    if raw is None:
        try:
            from channels.platform_registry import platform_registry

            entry = platform_registry.get("matrix")
            if entry and entry.max_message_length:
                raw = entry.max_message_length
        except Exception:
            logger.debug("Matrix platform registry limit unavailable", exc_info=True)
    if raw is None:
        return DEFAULT_MAX_MESSAGE_LENGTH
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_MESSAGE_LENGTH
    return max(500, min(value, MATRIX_MAX_MESSAGE_LENGTH_CEILING))


# Backwards-compatible alias for external callers.
MAX_MESSAGE_LENGTH = DEFAULT_MAX_MESSAGE_LENGTH

# Store directory for E2EE keys and sync state.
# Uses get_hermes_home() so each profile gets its own Matrix store.
from hermes_constants import get_hermes_dir as _get_hermes_dir

_STORE_DIR = _get_hermes_dir("platforms/matrix/store", "matrix/store")
_CRYPTO_DB_PATH = _STORE_DIR / "crypto.db"

# Grace period: ignore messages older than this many seconds before startup.
_STARTUP_GRACE_SECONDS = 5

_OUTBOUND_MENTION_RE = re.compile(
    r"(?<![\w/])(@[0-9A-Za-z._=/-]+:[0-9A-Za-z.-]+(?::\d+)?)"
)

_E2EE_INSTALL_HINT = (
    "Install with: pip install 'mautrix[encryption]'  (requires libolm C library)"
)

_MATRIX_IMAGE_FILENAME_EXTS = frozenset({
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".heic",
    ".heif",
    ".avif",
})


def _looks_like_matrix_image_filename(text: str) -> bool:
    """Return True when Matrix image body text is probably just a transport filename.

    Matrix ``m.image`` events commonly populate ``content.body`` with the uploaded
    filename when the user did not add a caption. Treating that raw filename as
    user-authored text confuses downstream vision enrichment.
    """
    candidate = str(text or "").strip()
    if not candidate or "\n" in candidate or candidate.endswith("/"):
        return False

    name = Path(candidate).name
    if not name or name != candidate:
        return False

    suffix = Path(name).suffix.lower()
    if not suffix:
        return False

    guessed_type, _ = mimetypes.guess_type(name)
    if guessed_type and guessed_type.startswith("image/"):
        return True
    return suffix in _MATRIX_IMAGE_FILENAME_EXTS


def _create_matrix_session(proxy_url: str | None):
    """Create an ``aiohttp.ClientSession`` whose proxy applies to *all* requests.

    mautrix's ``HTTPAPI._send()`` calls ``session.request()`` without forwarding
    per-request ``proxy=`` kwargs.  For HTTP(S) proxies we use aiohttp's native
    ``proxy=`` session parameter which sets a default for every request.  For SOCKS
    we use ``aiohttp_socks.ProxyConnector`` (connector-level).
    When no proxy is configured we enable ``trust_env`` so standard env vars
    (``HTTP_PROXY`` / ``HTTPS_PROXY``) are honoured automatically.
    """
    import aiohttp

    if not proxy_url:
        return aiohttp.ClientSession(trust_env=True)

    if proxy_url.split("://")[0].lower().startswith("socks"):
        try:
            from aiohttp_socks import ProxyConnector

            return aiohttp.ClientSession(
                connector=ProxyConnector.from_url(proxy_url, rdns=True),
            )
        except ImportError:
            logger.warning(
                "aiohttp_socks not installed — SOCKS proxy %s ignored. "
                "Run: pip install aiohttp-socks",
                proxy_url,
            )
            return aiohttp.ClientSession(trust_env=True)

    return aiohttp.ClientSession(proxy=proxy_url)


def _check_e2ee_deps() -> bool:
    """Return True if mautrix E2EE dependencies (python-olm) are available."""
    try:
        from mautrix.crypto import OlmMachine  # noqa: F401

        return True
    except (ImportError, AttributeError):
        return False


def check_matrix_requirements() -> bool:
    """Return True if the Matrix adapter can be used.

    Lazy-installs mautrix via ``tools.lazy_deps.ensure("platform.matrix")``
    on first call if not present. Rebinds all module-level type globals on success.
    """
    token = get_profile_env("MATRIX_ACCESS_TOKEN", "")
    password = get_profile_env("MATRIX_PASSWORD", "")
    homeserver = get_profile_env("MATRIX_HOMESERVER", "")

    if not token and not password:
        logger.debug("Matrix: neither MATRIX_ACCESS_TOKEN nor MATRIX_PASSWORD set")
        return False
    if not homeserver:
        logger.warning("Matrix: MATRIX_HOMESERVER not set")
        return False
    try:
        import mautrix  # noqa: F401
    except ImportError:
        def _import():
            from mautrix.types import (
                ContentURI, EventID, EventType, PaginationDirection,
                PresenceState, RoomCreatePreset, RoomID, SyncToken,
                TrustState, UserID,
            )
            return {
                "ContentURI": ContentURI,
                "EventID": EventID,
                "EventType": EventType,
                "PaginationDirection": PaginationDirection,
                "PresenceState": PresenceState,
                "RoomCreatePreset": RoomCreatePreset,
                "RoomID": RoomID,
                "SyncToken": SyncToken,
                "TrustState": TrustState,
                "UserID": UserID,
            }

        from tools.lazy_deps import ensure_and_bind
        if not ensure_and_bind("platform.matrix", _import, globals(), prompt=False):
            logger.warning(
                "Matrix: mautrix not installed. Run: pip install 'mautrix[encryption]'"
            )
            return False

    # If encryption is requested, verify E2EE deps are available at startup
    # rather than silently degrading to plaintext-only at connect time.
    encryption_requested = get_profile_env("MATRIX_ENCRYPTION", "").lower() in {
        "true",
        "1",
        "yes",
    }
    if encryption_requested and not _check_e2ee_deps():
        logger.error(
            "Matrix: MATRIX_ENCRYPTION=true but E2EE dependencies are missing. %s. "
            "Without this, encrypted rooms will not work. "
            "Set MATRIX_ENCRYPTION=false to disable E2EE.",
            _E2EE_INSTALL_HINT,
        )
        return False

    return True


class _CryptoStateStore:
    """Adapter that satisfies the mautrix crypto StateStore interface.

    OlmMachine requires a StateStore with ``is_encrypted``,
    ``get_encryption_info``, and ``find_shared_rooms``.  The basic
    ``MemoryStateStore`` from ``mautrix.client`` doesn't implement these,
    so we provide simple implementations that consult the client's room
    state.
    """

    def __init__(self, client_state_store: Any, joined_rooms: set):
        self._ss = client_state_store
        self._joined_rooms = joined_rooms

    async def is_encrypted(self, room_id: str) -> bool:
        return (await self.get_encryption_info(room_id)) is not None

    async def get_encryption_info(self, room_id: str):
        if hasattr(self._ss, "get_encryption_info"):
            return await self._ss.get_encryption_info(room_id)
        return None

    async def find_shared_rooms(self, user_id: str) -> list:
        # Return all joined rooms — simple but correct for a single-user bot.
        return list(self._joined_rooms)
