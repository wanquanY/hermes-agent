"""Matrix gateway adapter.

Connects to any Matrix homeserver (self-hosted or matrix.org) via the
mautrix Python SDK.  Supports optional end-to-end encryption (E2EE)
when installed with ``pip install "mautrix[encryption]"``.

Environment variables:
    MATRIX_HOMESERVER           Homeserver URL (e.g. https://matrix.example.org)
    MATRIX_ACCESS_TOKEN         Access token (preferred auth method)
    MATRIX_USER_ID              Full user ID (@bot:server) — required for password login
    MATRIX_PASSWORD             Password (alternative to access token)
    MATRIX_ENCRYPTION           Set "true" to enable E2EE
    MATRIX_DEVICE_ID            Stable device ID for E2EE persistence across restarts
    MATRIX_PROXY                HTTP(S) or SOCKS proxy URL for Matrix traffic
    MATRIX_ALLOWED_USERS    Comma-separated Matrix user IDs (@user:server)
    MATRIX_HOME_ROOM        Room ID for cron/notification delivery
    MATRIX_REACTIONS        Set "false" to disable processing lifecycle reactions
                            (eyes/checkmark/cross). Default: true
    MATRIX_REQUIRE_MENTION      Require @mention in rooms (default: true)
    MATRIX_FREE_RESPONSE_ROOMS  Comma-separated room IDs exempt from mention requirement (alias of matrix.free_response_rooms)
    MATRIX_ALLOWED_ROOMS    Comma-separated room IDs; if set, bot ONLY responds in these rooms (whitelist, DMs exempt; alias of matrix.allowed_rooms)
    MATRIX_AUTO_THREAD          Auto-create threads for room messages (default: true)
    MATRIX_DM_AUTO_THREAD       Auto-create threads for DM messages (default: false)
    MATRIX_RECOVERY_KEY         Recovery key for cross-signing verification after device key rotation
    MATRIX_DM_MENTION_THREADS   Create a thread when bot is @mentioned in a DM (default: false)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time

from typing import Any, Dict, Optional, Set

from agent.secret_scope import get_profile_env
from channels.config import Platform, PlatformConfig
from channels.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    resolve_proxy_url,
    proxy_kwargs_for_aiohttp,
)
from channels.platforms.helpers import ThreadParticipationTracker

logger = logging.getLogger(__name__)


from channels.platforms import matrix_support as _matrix_support
from channels.platforms.matrix_crypto import MatrixCryptoMixin
from channels.platforms.matrix_choice_picker import MatrixChoicePickerMixin
from channels.platforms.matrix_formatting import MatrixFormattingMixin
from channels.platforms.matrix_reactions import MatrixReactionMixin
from channels.platforms.matrix_room_ops import MatrixRoomOpsMixin
from channels.platforms.matrix_support import (
    ContentURI,
    DEFAULT_MAX_MESSAGE_LENGTH,
    EventID,
    EventType,
    PaginationDirection,
    RoomID,
    SyncToken,
    TrustState,
    UserID,
    _CRYPTO_DB_PATH,
    _CryptoStateStore,
    _E2EE_INSTALL_HINT,
    _MatrixApprovalPrompt,
    _STARTUP_GRACE_SECONDS,
    _STORE_DIR,
    _create_matrix_session,
    _looks_like_matrix_image_filename,
    resolve_max_message_length,
)

_SUPPORT_CHECK_E2EE_DEPS = _matrix_support._check_e2ee_deps


def _check_e2ee_deps() -> bool:
    return _SUPPORT_CHECK_E2EE_DEPS()


def check_matrix_requirements() -> bool:
    original = _matrix_support._check_e2ee_deps
    _matrix_support._check_e2ee_deps = _check_e2ee_deps
    try:
        return _matrix_support.check_matrix_requirements()
    finally:
        _matrix_support._check_e2ee_deps = original


def _apply_yaml_config(_yaml_cfg: dict, matrix_cfg: dict) -> dict | None:
    """Bridge the Matrix-specific chunk limit into runtime config.

    Returning the field keeps it profile-local in ``PlatformConfig.extra``;
    seeding the environment preserves compatibility with standalone callers
    that construct an adapter without going through gateway config loading.
    """
    if "max_message_length" not in matrix_cfg:
        return None
    value = matrix_cfg["max_message_length"]
    if not os.getenv("MATRIX_MAX_MESSAGE_LENGTH"):
        os.environ["MATRIX_MAX_MESSAGE_LENGTH"] = str(value)
    return {"max_message_length": value}


class MatrixAdapter(MatrixChoicePickerMixin, MatrixReactionMixin, MatrixRoomOpsMixin, MatrixFormattingMixin, MatrixCryptoMixin, BasePlatformAdapter):
    """Gateway adapter for Matrix (any homeserver)."""

    max_message_length = DEFAULT_MAX_MESSAGE_LENGTH
    _split_threshold = DEFAULT_MAX_MESSAGE_LENGTH - 100
    _SPLIT_THRESHOLD = _split_threshold

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.MATRIX)

        self.max_message_length = resolve_max_message_length(config)
        self.MAX_MESSAGE_LENGTH = self.max_message_length
        self._split_threshold = max(100, self.max_message_length - 100)
        self._SPLIT_THRESHOLD = self._split_threshold

        self._homeserver: str = (
            config.extra.get("homeserver", "")
            or get_profile_env("MATRIX_HOMESERVER", "")
        ).rstrip("/")
        self._access_token: str = config.token or get_profile_env(
            "MATRIX_ACCESS_TOKEN", ""
        )
        self._user_id: str = config.extra.get("user_id", "") or get_profile_env(
            "MATRIX_USER_ID", ""
        )
        self._password: str = config.extra.get("password", "") or get_profile_env(
            "MATRIX_PASSWORD", ""
        )
        self._encryption: bool = config.extra.get(
            "encryption",
            get_profile_env("MATRIX_ENCRYPTION", "").lower()
            in {"true", "1", "yes"},
        )
        self._device_id: str = config.extra.get("device_id", "") or get_profile_env(
            "MATRIX_DEVICE_ID", ""
        )

        self._client: Any = None  # mautrix.client.Client
        self._crypto_db: Any = None  # mautrix.util.async_db.Database
        self._sync_task: Optional[asyncio.Task] = None
        self._closing = False
        self._startup_ts: float = 0.0
        # Clock-skew detection: count grace-check drops that happen well
        # after startup (i.e. not initial-sync backfill).  If the host's
        # system clock is set ahead of real time, the startup grace check
        # `event_ts < startup_ts - 5` silently drops every live message.
        # See #12614 — the symptom is "bot joins rooms but never replies".
        # Drops only count when their skew matches the first sampled drop
        # (within 60s), so varied-age backfill from freshly-invited rooms
        # doesn't trip the heuristic.
        self._late_grace_drops: int = 0
        self._late_grace_skew: float = 0.0
        self._clock_skew_warned: bool = False

        # Cache: room_id → bool (is DM)
        self._dm_rooms: Dict[str, bool] = {}
        # Set of room IDs we've joined
        self._joined_rooms: Set[str] = set()
        # Event deduplication (bounded deque keeps newest entries)
        from collections import deque

        self._processed_events: deque = deque(maxlen=1000)
        self._processed_events_set: set = set()

        # Buffer for undecrypted events pending key receipt.
        # Each entry: (room_id, event, timestamp)

        # Thread participation tracking (for require_mention bypass)
        self._threads = ThreadParticipationTracker("matrix")

        # Mention/thread gating — parsed once from env vars.
        self._require_mention: bool = get_profile_env(
            "MATRIX_REQUIRE_MENTION", "true"
        ).lower() not in {"false", "0", "no"}
        self._thread_require_mention: bool = self._parse_thread_require_mention(config)
        free_rooms_raw = config.extra.get("free_response_rooms")
        if free_rooms_raw is None:
            free_rooms_raw = get_profile_env("MATRIX_FREE_RESPONSE_ROOMS", "")
        if isinstance(free_rooms_raw, list):
            self._free_rooms: Set[str] = {
                str(r).strip() for r in free_rooms_raw if str(r).strip()
            }
        else:
            self._free_rooms: Set[str] = {
                r.strip() for r in str(free_rooms_raw).split(",") if r.strip()
            }
        # If non-empty, bot ONLY responds in these rooms (whitelist); DMs exempt.
        allowed_rooms_raw = config.extra.get("allowed_rooms")
        if allowed_rooms_raw is None:
            allowed_rooms_raw = get_profile_env("MATRIX_ALLOWED_ROOMS", "")
        if isinstance(allowed_rooms_raw, list):
            self._allowed_rooms: Set[str] = {
                str(r).strip() for r in allowed_rooms_raw if str(r).strip()
            }
        else:
            self._allowed_rooms: Set[str] = {
                r.strip() for r in str(allowed_rooms_raw).split(",") if r.strip()
            }
        self._auto_thread: bool = get_profile_env(
            "MATRIX_AUTO_THREAD", "true"
        ).lower() in {
            "true",
            "1",
            "yes",
        }
        self._dm_auto_thread: bool = get_profile_env(
            "MATRIX_DM_AUTO_THREAD", "false"
        ).lower() in {"true", "1", "yes"}
        self._dm_mention_threads: bool = get_profile_env(
            "MATRIX_DM_MENTION_THREADS", "false"
        ).lower() in {"true", "1", "yes"}

        # Reactions: configurable via MATRIX_REACTIONS (default: true).
        self._reactions_enabled: bool = get_profile_env(
            "MATRIX_REACTIONS", "true"
        ).lower() not in {"false", "0", "no"}
        self._pending_reactions: dict[tuple[str, str], str] = {}
        # Delay before redacting reactions so Matrix homeservers have time to
        # deliver the final message event without tripping "missing event"
        # errors in some clients.  5s is empirically safe; not user-tunable —
        # if that changes, add a config.yaml entry rather than an env var.
        self._reaction_redaction_delay_seconds = 5.0
        self._reaction_redaction_tasks: Set[asyncio.Task] = set()

        # Proxy support — resolve once at init, reuse for all HTTP traffic.
        self._proxy_url: str | None = resolve_proxy_url(platform_env_var="MATRIX_PROXY")
        if self._proxy_url:
            logger.info("Matrix: proxy configured — %s", self._proxy_url)

        # Text batching: merge rapid successive messages (Telegram-style).
        # Matrix clients split long messages around 4000 chars.
        self._text_batch_delay_seconds = float(
            get_profile_env("HERMES_MATRIX_TEXT_BATCH_DELAY_SECONDS", "0.6")
        )
        self._text_batch_split_delay_seconds = float(
            get_profile_env(
                "HERMES_MATRIX_TEXT_BATCH_SPLIT_DELAY_SECONDS", "2.0"
            )
        )
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}

        # Matrix reaction-based dangerous command approvals.
        self._approval_reaction_map = {
            "✅": "once",
            "❎": "deny",
        }
        self._approval_prompts_by_event: Dict[str, _MatrixApprovalPrompt] = {}
        self._approval_prompt_by_session: Dict[str, str] = {}
        allowed_users_raw = get_profile_env("MATRIX_ALLOWED_USERS", "")
        self._allowed_user_ids: Set[str] = {
            u.strip() for u in allowed_users_raw.split(",") if u.strip()
        }

    def _is_duplicate_event(self, event_id) -> bool:
        """Return True if this event was already processed. Tracks the ID otherwise."""
        if not event_id:
            return False
        if event_id in self._processed_events_set:
            return True
        if len(self._processed_events) == self._processed_events.maxlen:
            evicted = self._processed_events[0]
            self._processed_events_set.discard(evicted)
        self._processed_events.append(event_id)
        self._processed_events_set.add(event_id)
        return False

    @staticmethod
    def _parse_thread_require_mention(config) -> bool:
        """Parse thread_require_mention from config.extra or env var.

        Handles both YAML booleans and string values (``\"true\"``, ``\"false\"``,
        ``\"yes\"``, ``\"no\"``, ``\"on\"``, ``\"off\"``, ``\"1\"``, ``\"0\"``).
        Falls back to ``MATRIX_THREAD_REQUIRE_MENTION`` env var, default ``false``.
        Mirrors Discord adapter's parsing pattern.
        """
        configured = config.extra.get("thread_require_mention")
        if configured is not None:
            if isinstance(configured, bool):
                return configured
            if isinstance(configured, str):
                return configured.lower() not in {"false", "0", "no", "off"}
            # int, float, etc. — truthiness fallback
            return bool(configured)
        return get_profile_env(
            "MATRIX_THREAD_REQUIRE_MENTION", "false"
        ).lower() in {"true", "1", "yes", "on"}

    # ------------------------------------------------------------------
    # E2EE helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Required overrides
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to the Matrix homeserver and start syncing."""
        from mautrix.api import HTTPAPI
        from mautrix.client import Client
        from mautrix.client.state_store import MemoryStateStore, MemorySyncStore

        if not self._homeserver:
            logger.error("Matrix: homeserver URL not configured")
            return False

        # Ensure store dir exists for E2EE key persistence.
        _STORE_DIR.mkdir(parents=True, exist_ok=True)

        # Create the HTTP API layer.
        client_session = _create_matrix_session(self._proxy_url)
        api = HTTPAPI(
            base_url=self._homeserver,
            token=self._access_token or "",
            client_session=client_session,
        )

        # Create the client.
        state_store = MemoryStateStore()
        sync_store = MemorySyncStore()
        client = Client(
            mxid=UserID(self._user_id) if self._user_id else UserID(""),
            device_id=self._device_id or None,
            api=api,
            state_store=state_store,
            sync_store=sync_store,
        )

        self._client = client

        # Authenticate.
        if self._access_token:
            api.token = self._access_token

            # Validate the token and learn user_id / device_id.
            try:
                resp = await client.whoami()
                resolved_user_id = getattr(resp, "user_id", "") or self._user_id
                resolved_device_id = getattr(resp, "device_id", "")
                if resolved_user_id:
                    self._user_id = str(resolved_user_id)
                    client.mxid = UserID(self._user_id)

                # Prefer user-configured device_id for stable E2EE identity.
                effective_device_id = self._device_id or resolved_device_id
                if effective_device_id:
                    client.device_id = effective_device_id

                logger.info(
                    "Matrix: using access token for %s%s",
                    self._user_id or "(unknown user)",
                    f" (device {effective_device_id})" if effective_device_id else "",
                )
            except Exception as exc:
                logger.error(
                    "Matrix: whoami failed — check MATRIX_ACCESS_TOKEN and MATRIX_HOMESERVER: %s",
                    exc,
                    exc_info=True,
                )
                await api.session.close()
                return False
        elif self._password and self._user_id:
            try:
                resp = await client.login(
                    identifier=self._user_id,
                    password=self._password,
                    device_name="Hermes Agent",
                    device_id=self._device_id or None,
                )
                if resp and hasattr(resp, "device_id"):
                    client.device_id = resp.device_id
                logger.info("Matrix: logged in as %s", self._user_id)
            except Exception as exc:
                logger.error("Matrix: login failed — %s", exc)
                await api.session.close()
                return False
        else:
            logger.error(
                "Matrix: need MATRIX_ACCESS_TOKEN or MATRIX_USER_ID + MATRIX_PASSWORD"
            )
            await api.session.close()
            return False

        # Set up E2EE if requested.
        if self._encryption:
            if not _check_e2ee_deps():
                logger.error(
                    "Matrix: MATRIX_ENCRYPTION=true but E2EE dependencies are missing. %s. "
                    "Refusing to connect — encrypted rooms would silently fail.",
                    _E2EE_INSTALL_HINT,
                )
                await api.session.close()
                return False
            try:
                from mautrix.crypto import OlmMachine
                from mautrix.crypto.store.asyncpg import PgCryptoStore
                from mautrix.util.async_db import Database

                _STORE_DIR.mkdir(parents=True, exist_ok=True)

                # Remove legacy pickle file from pre-SQLite era.
                legacy_pickle = _STORE_DIR / "crypto_store.pickle"
                if legacy_pickle.exists():
                    logger.info(
                        "Matrix: removing legacy crypto_store.pickle (migrated to SQLite)"
                    )
                    legacy_pickle.unlink()

                # Open SQLite-backed crypto store.
                crypto_db = Database.create(
                    f"sqlite:///{_CRYPTO_DB_PATH}",
                    upgrade_table=PgCryptoStore.upgrade_table,
                )
                await crypto_db.start()
                self._crypto_db = crypto_db

                _acct_id = self._user_id or "hermes"
                _pickle_key = f"{_acct_id}:{self._device_id or 'default'}"
                crypto_store = PgCryptoStore(
                    account_id=_acct_id,
                    pickle_key=_pickle_key,
                    db=crypto_db,
                )
                await crypto_store.open()

                # Bind the store to the runtime device_id before any
                # put_account() runs. PgCryptoStore defaults _device_id
                # to "" and its crypto_account UPSERT never updates the
                # device_id column on conflict — so once put_account
                # writes blank, it stays blank forever. That breaks
                # every downstream device-scoped olm operation: peer
                # to-device ciphertext can't find our identity key and
                # no megolm sessions ever land. Setting _device_id here
                # (in-memory; the on-disk row may not exist yet) makes
                # the first put_account write the correct value.
                # DeviceID is a NewType(str) so plain str works at runtime.
                if client.device_id:
                    await crypto_store.put_device_id(client.device_id)

                crypto_state = _CryptoStateStore(state_store, self._joined_rooms)
                olm = OlmMachine(client, crypto_store, crypto_state)

                # Accept unverified devices so senders share Megolm
                # session keys with us automatically.
                olm.share_keys_min_trust = TrustState.UNVERIFIED
                olm.send_keys_min_trust = TrustState.UNVERIFIED

                await olm.load()

                # Verify our device keys are still on the homeserver.
                if not await self._verify_device_keys_on_server(client, olm):
                    await crypto_db.stop()
                    await api.session.close()
                    return False

                # Proactively flush one-time keys to detect stale OTK
                # conflicts early.  When crypto state is wiped but the
                # same device ID is reused, the server may still hold OTKs
                # signed with the old ed25519 key.  Identity key re-upload
                # succeeds but OTK uploads fail ("already exists" with
                # mismatched signature).  Peers then cannot establish Olm
                # sessions and all new messages are undecryptable.
                try:
                    await olm.share_keys()
                except Exception as exc:
                    exc_str = str(exc)
                    if "already exists" in exc_str:
                        logger.error(
                            "Matrix: device %s has stale one-time keys on the "
                            "server signed with a previous identity key. "
                            "Peers cannot establish new Olm sessions with "
                            "this device. Delete the device from the "
                            "homeserver and restart, or generate a new "
                            "access token to get a fresh device ID.",
                            client.device_id,
                        )
                        await crypto_db.stop()
                        await api.session.close()
                        return False
                    # Non-OTK errors are transient (network, etc.) — log
                    # but allow startup to continue.
                    logger.warning(
                        "Matrix: share_keys() warning during startup: %s",
                        exc,
                    )

                # Import cross-signing private keys from SSSS and self-sign
                # the current device. Required after any device-key rotation
                # (fresh crypto.db, share_keys re-upload) — otherwise the
                # device's self-signing signature is stale and peers refuse
                # to share Megolm sessions with the rotated device.
                recovery_key = get_profile_env("MATRIX_RECOVERY_KEY", "").strip()
                if recovery_key:
                    try:
                        await olm.verify_with_recovery_key(recovery_key)
                        logger.info("Matrix: cross-signing verified via recovery key")
                    except Exception as exc:
                        logger.warning(
                            "Matrix: recovery key verification failed: %s", exc
                        )
                else:
                    # No recovery key — bootstrap cross-signing if the bot
                    # has none yet. Without this, Element shows "Encrypted
                    # by a device not verified by its owner" on every
                    # message from this bot, indefinitely. mautrix's
                    # generate_recovery_key does the full flow: generates
                    # MSK/SSK/USK, uploads private keys to SSSS, publishes
                    # public keys to the homeserver, and signs the current
                    # device with the new SSK. Some homeservers require UIA
                    # for /keys/device_signing/upload — those will need an
                    # alternate path; Continuwuity and Synapse-with-shared-
                    # secret accept the unauthenticated upload.
                    try:
                        own_xsign = await olm.get_own_cross_signing_public_keys()
                    except Exception as exc:
                        own_xsign = None
                        logger.warning(
                            "Matrix: cross-signing key lookup failed: %s", exc
                        )
                    if own_xsign is None:
                        try:
                            new_recovery_key = await olm.generate_recovery_key()
                            logger.warning(
                                "Matrix: bootstrapped cross-signing for %s. "
                                "SAVE THIS RECOVERY KEY — set "
                                "MATRIX_RECOVERY_KEY for future restarts so "
                                "the bot can re-sign its device after key "
                                "rotation: %s",
                                client.mxid,
                                new_recovery_key,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Matrix: cross-signing bootstrap failed "
                                "(non-fatal — Element will show 'not "
                                "verified by its owner'): %s",
                                exc,
                            )

                client.crypto = olm
                logger.info(
                    "Matrix: E2EE enabled (store: %s%s)",
                    str(_CRYPTO_DB_PATH),
                    f", device_id={client.device_id}" if client.device_id else "",
                )
            except Exception as exc:
                logger.error(
                    "Matrix: failed to create E2EE client: %s. %s",
                    exc,
                    _E2EE_INSTALL_HINT,
                )
                await api.session.close()
                return False

        # Register event handlers.
        from mautrix.client import InternalEventType as IntEvt
        from mautrix.client.dispatcher import MembershipEventDispatcher

        # Without this the INVITE handler below never fires.
        client.add_dispatcher(MembershipEventDispatcher)

        client.add_event_handler(EventType.ROOM_MESSAGE, self._on_room_message)
        client.add_event_handler(EventType.REACTION, self._on_reaction)
        client.add_event_handler(IntEvt.INVITE, self._on_invite)

        # Initial sync to catch up, then start background sync.
        self._startup_ts = time.time()
        # Reset clock-skew detector for each connect cycle so a reconnect
        # after the user fixes NTP doesn't inherit stale counters.
        self._late_grace_drops = 0
        self._late_grace_skew = 0.0
        self._clock_skew_warned = False
        self._closing = False

        try:
            sync_data = await client.sync(timeout=10000, full_state=True)
            if isinstance(sync_data, dict):
                rooms_join = sync_data.get("rooms", {}).get("join", {})
                self._joined_rooms.clear()
                self._joined_rooms.update(rooms_join.keys())
                # Store the next_batch token so incremental syncs start
                # from where the initial sync left off.
                nb = sync_data.get("next_batch")
                if nb:
                    await client.sync_store.put_next_batch(nb)
                logger.info(
                    "Matrix: initial sync complete, joined %d rooms",
                    len(self._joined_rooms),
                )
                # Build DM room cache from m.direct account data.
                await self._refresh_dm_cache()

                # Dispatch events from the initial sync so the OlmMachine
                # receives to-device key shares queued while we were offline.
                try:
                    tasks = client.handle_sync(sync_data)
                    if tasks:
                        await asyncio.gather(*tasks)
                except Exception as exc:
                    logger.warning("Matrix: initial sync event dispatch error: %s", exc)
                await self._join_pending_invites(sync_data)
            else:
                logger.warning(
                    "Matrix: initial sync returned unexpected type %s",
                    type(sync_data).__name__,
                )
        except Exception as exc:
            logger.warning("Matrix: initial sync error: %s", exc)

        # Share keys after initial sync if E2EE is enabled.
        if self._encryption and getattr(client, "crypto", None):
            try:
                await client.crypto.share_keys()
            except Exception as exc:
                logger.warning("Matrix: initial key share failed: %s", exc)

        # Start the sync loop.
        self._sync_task = asyncio.create_task(self._sync_loop())
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        """Disconnect from Matrix."""
        self._closing = True

        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except (asyncio.CancelledError, Exception):
                pass

        redaction_tasks = list(self._reaction_redaction_tasks)
        for task in redaction_tasks:
            if not task.done():
                task.cancel()
        if redaction_tasks:
            await asyncio.gather(*redaction_tasks, return_exceptions=True)
        self._reaction_redaction_tasks.clear()

        # Close the SQLite crypto store database.
        if hasattr(self, "_crypto_db") and self._crypto_db:
            try:
                await self._crypto_db.stop()
            except Exception as exc:
                logger.debug("Matrix: could not close crypto DB on disconnect: %s", exc)

        if self._client:
            try:
                await self._client.api.session.close()
            except Exception:
                pass
            self._client = None

        logger.info("Matrix: disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message to a Matrix room."""

        if not content:
            return SendResult(success=True)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.max_message_length)

        last_event_id = None
        for i, chunk in enumerate(chunks):
            msg_content = self._build_text_message_content(chunk)

            # Reply-to support.
            if reply_to:
                msg_content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}

            # Thread support: if metadata has thread_id, send as threaded reply.
            thread_id = (metadata or {}).get("thread_id")
            if thread_id:
                relates_to = msg_content.get("m.relates_to", {})
                relates_to["rel_type"] = "m.thread"
                relates_to["event_id"] = thread_id
                relates_to["is_falling_back"] = True
                if reply_to and "m.in_reply_to" not in relates_to:
                    relates_to["m.in_reply_to"] = {"event_id": reply_to}
                msg_content["m.relates_to"] = relates_to

            try:
                event_id = await asyncio.wait_for(
                    self._client.send_message_event(
                        RoomID(chat_id),
                        EventType.ROOM_MESSAGE,
                        msg_content,
                    ),
                    timeout=45,
                )
                last_event_id = str(event_id)
                logger.info("Matrix: sent event %s to %s", last_event_id, chat_id)
            except Exception as exc:
                # On E2EE errors, retry after sharing keys.
                if self._encryption and getattr(self._client, "crypto", None):
                    try:
                        await self._client.crypto.share_keys()
                        event_id = await asyncio.wait_for(
                            self._client.send_message_event(
                                RoomID(chat_id),
                                EventType.ROOM_MESSAGE,
                                msg_content,
                            ),
                            timeout=45,
                        )
                        last_event_id = str(event_id)
                        logger.info(
                            "Matrix: sent event %s to %s (after key share)",
                            last_event_id,
                            chat_id,
                        )
                        continue
                    except Exception as retry_exc:
                        logger.error(
                            "Matrix: failed to send to %s after retry: %s",
                            chat_id,
                            retry_exc,
                        )
                        return SendResult(success=False, error=str(retry_exc))
                logger.error("Matrix: failed to send to %s: %s", chat_id, exc)
                return SendResult(success=False, error=str(exc))

        return SendResult(success=True, message_id=last_event_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return room name and type (dm/group)."""
        name = chat_id
        chat_type = "dm" if await self._is_dm_room(chat_id) else "group"

        if self._client:
            try:
                name_evt = await self._client.get_state_event(
                    RoomID(chat_id),
                    EventType.ROOM_NAME,
                )
                if name_evt and hasattr(name_evt, "name") and name_evt.name:
                    name = name_evt.name
            except Exception:
                pass

        return {"name": name, "type": chat_type}

    # ------------------------------------------------------------------
    # Optional overrides
    # ------------------------------------------------------------------

    async def send_typing(
        self, chat_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send a typing indicator."""
        if self._client:
            try:
                await self._client.set_typing(RoomID(chat_id), timeout=30000)
            except Exception:
                pass

    async def stop_typing(self, chat_id: str) -> None:
        """Clear the typing indicator."""
        if self._client:
            try:
                await self._client.set_typing(RoomID(chat_id), timeout=0)
            except Exception:
                pass


    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False
    ) -> SendResult:
        """Edit an existing message (via m.replace)."""

        formatted = self.format_message(content)
        new_content = self._build_text_message_content(formatted)
        msg_content: Dict[str, Any] = {
            "msgtype": "m.text",
            "body": f"* {formatted}",
            "m.new_content": new_content,
        }
        if "m.mentions" in new_content:
            msg_content["m.mentions"] = new_content["m.mentions"]
        if "formatted_body" in new_content:
            msg_content["format"] = "org.matrix.custom.html"
            msg_content["formatted_body"] = f'* {new_content["formatted_body"]}'
        msg_content["m.relates_to"] = {
            "rel_type": "m.replace",
            "event_id": message_id,
        }

        try:
            event_id = await self._client.send_message_event(
                RoomID(chat_id),
                EventType.ROOM_MESSAGE,
                msg_content,
            )
            return SendResult(success=True, message_id=str(event_id))
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Download an image URL and upload it to Matrix."""
        from tools.url_safety import is_safe_url

        if not is_safe_url(image_url):
            logger.warning("Matrix: blocked unsafe image URL (SSRF protection)")
            return await super().send_image(
                chat_id, image_url, caption, reply_to, metadata=metadata
            )

        try:
            # Try aiohttp first (always available), fall back to httpx
            try:
                import aiohttp as _aiohttp
                _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(self._proxy_url)
                async with _aiohttp.ClientSession(**_sess_kw) as http:
                    async with http.get(
                        image_url,
                        timeout=_aiohttp.ClientTimeout(total=30),
                        **_req_kw,
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.read()
                        ct = resp.content_type or "image/png"
                        fname = (
                            image_url.rsplit("/", 1)[-1].split("?")[0] or "image.png"
                        )
            except ImportError:
                import httpx
                _httpx_kw: dict = {}
                if self._proxy_url:
                    _httpx_kw["proxy"] = self._proxy_url
                async with httpx.AsyncClient(**_httpx_kw) as http:
                    resp = await http.get(image_url, follow_redirects=True, timeout=30)
                    resp.raise_for_status()
                    data = resp.content
                    ct = resp.headers.get("content-type", "image/png")
                    fname = image_url.rsplit("/", 1)[-1].split("?")[0] or "image.png"
        except Exception as exc:
            logger.warning("Matrix: failed to download image %s: %s", image_url, exc)
            return await self.send(
                chat_id, f"{caption or ''}\n{image_url}".strip(), reply_to
            )

        return await self._upload_and_send(
            chat_id, data, fname, ct, "m.image", caption, reply_to, metadata
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local image file to Matrix."""
        return await self._send_local_file(
            chat_id, image_path, "m.image", caption, reply_to, metadata=metadata
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local file as a document."""
        return await self._send_local_file(
            chat_id, file_path, "m.file", caption, reply_to, file_name, metadata
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload an audio file as a voice message (MSC3245 native voice)."""
        return await self._send_local_file(
            chat_id,
            audio_path,
            "m.audio",
            caption,
            reply_to,
            metadata=metadata,
            is_voice=True,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a video file."""
        return await self._send_local_file(
            chat_id, video_path, "m.video", caption, reply_to, metadata=metadata
        )

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[dict] = None,
    ) -> SendResult:
        """Send a reaction-based exec approval prompt for Matrix."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        cmd_preview = command[:2000] + "..." if len(command) > 2000 else command
        text = (
            "⚠️ **Dangerous command requires approval**\n"
            f"```\n{cmd_preview}\n```\n"
            f"Reason: {description}\n\n"
            "Reply `/approve` to execute, `/approve session` to approve this pattern for the session, "
            "`/approve always` to approve permanently, or `/deny` to cancel.\n\n"
            "You can also click the reaction to approve:\n"
            "✅ = /approve\n"
            "❎ = /deny"
        )

        result = await self.send(chat_id, text, metadata=metadata)
        if not result.success or not result.message_id:
            return result

        prompt = _MatrixApprovalPrompt(
            session_key=session_key,
            chat_id=chat_id,
            message_id=result.message_id,
        )
        old_event = self._approval_prompt_by_session.get(session_key)
        if old_event:
            self._approval_prompts_by_event.pop(old_event, None)
        self._approval_prompts_by_event[result.message_id] = prompt
        self._approval_prompt_by_session[session_key] = result.message_id

        for emoji in ("✅", "❎"):
            try:
                reaction_result = await self._send_reaction(chat_id, result.message_id, emoji)
                # Save the bot's reaction event_id for later cleanup
                if reaction_result:
                    prompt.bot_reaction_events[emoji] = str(reaction_result)
            except Exception as exc:
                logger.debug("Matrix: failed to add approval reaction %s: %s", emoji, exc)

        return result

    def format_message(self, content: str) -> str:
        """Pass-through — Matrix supports standard Markdown natively."""
        # Strip image markdown; media is uploaded separately.
        content = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", content)
        return content

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    async def _upload_and_send(
        self,
        room_id: str,
        data: bytes,
        filename: str,
        content_type: str,
        msgtype: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        is_voice: bool = False,
    ) -> SendResult:
        """Upload bytes to Matrix and send as a media message."""

        upload_data = data
        encrypted_file = None
        if self._encryption and getattr(self._client, "crypto", None):
            state_store = getattr(self._client, "state_store", None)
            if state_store:
                try:
                    room_encrypted = bool(await state_store.is_encrypted(RoomID(room_id)))
                except Exception:
                    room_encrypted = False
                if room_encrypted:
                    try:
                        from mautrix.crypto.attachments import encrypt_attachment
                        upload_data, encrypted_file = encrypt_attachment(data)
                    except Exception as exc:
                        logger.error("Matrix: attachment encryption failed: %s", exc)
                        return SendResult(success=False, error=str(exc))

        # Upload to homeserver.
        try:
            mxc_url = await self._client.upload_media(
                upload_data,
                mime_type=content_type,
                filename=filename,
                size=len(upload_data),
            )
        except Exception as exc:
            logger.error("Matrix: upload failed: %s", exc)
            return SendResult(success=False, error=str(exc))

        # Build media message content.
        msg_content: Dict[str, Any] = {
            "msgtype": msgtype,
            "body": caption or filename,
            "info": {
                "mimetype": content_type,
                "size": len(data),
            },
        }
        if encrypted_file is not None:
            file_payload = encrypted_file.serialize()
            file_payload["url"] = str(mxc_url)
            msg_content["file"] = file_payload
        else:
            msg_content["url"] = str(mxc_url)

        # Add MSC3245 voice flag for native voice messages.
        if is_voice:
            msg_content["org.matrix.msc3245.voice"] = {}

        if reply_to:
            msg_content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}

        thread_id = (metadata or {}).get("thread_id")
        if thread_id:
            relates_to = msg_content.get("m.relates_to", {})
            relates_to["rel_type"] = "m.thread"
            relates_to["event_id"] = thread_id
            relates_to["is_falling_back"] = True
            msg_content["m.relates_to"] = relates_to

        try:
            event_id = await self._client.send_message_event(
                RoomID(room_id),
                EventType.ROOM_MESSAGE,
                msg_content,
            )
            return SendResult(success=True, message_id=str(event_id))
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def _send_local_file(
        self,
        room_id: str,
        file_path: str,
        msgtype: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        file_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        is_voice: bool = False,
    ) -> SendResult:
        """Read a local file and upload it."""
        p = Path(file_path).expanduser()
        if not p.exists():
            return await self.send(
                room_id, f"{caption or ''}\n(file not found: {file_path})", reply_to
            )

        fname = file_name or p.name
        ct = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        data = p.read_bytes()

        return await self._upload_and_send(
            room_id, data, fname, ct, msgtype, caption, reply_to, metadata, is_voice
        )

    # ------------------------------------------------------------------
    # Sync loop
    # ------------------------------------------------------------------

    async def _sync_loop(self) -> None:
        """Continuously sync with the homeserver."""
        client = self._client
        # Resume from the token stored during the initial sync.
        next_batch = await client.sync_store.get_next_batch()
        while not self._closing:
            try:
                # Wrap in asyncio.wait_for to guard against TCP-level hangs
                # that the Matrix long-poll timeout cannot catch. Long-poll
                # is 30s, so 45s gives 15s slack for network drain.
                sync_data = await asyncio.wait_for(
                    client.sync(
                        since=next_batch,
                        timeout=30000,
                    ),
                    timeout=45.0,
                )

                # nio returns SyncError objects (not exceptions) for auth
                # failures like M_UNKNOWN_TOKEN.  Detect and stop immediately.
                _sync_msg = getattr(sync_data, "message", None)
                if _sync_msg and isinstance(_sync_msg, str):
                    _lower = _sync_msg.lower()
                    if "m_unknown_token" in _lower or "unknown_token" in _lower:
                        logger.error(
                            "Matrix: permanent auth error from sync: %s — stopping",
                            _sync_msg,
                        )
                        return

                if isinstance(sync_data, dict):
                    # Update joined rooms from sync response.
                    rooms_join = sync_data.get("rooms", {}).get("join", {})
                    if rooms_join:
                        self._joined_rooms.update(rooms_join.keys())

                    # Advance the sync token so the next request is
                    # incremental instead of a full initial sync.
                    nb = sync_data.get("next_batch")
                    if nb:
                        next_batch = nb
                        await client.sync_store.put_next_batch(nb)

                    # Dispatch events to registered handlers so that
                    # _on_room_message / _on_reaction / _on_invite fire.
                    try:
                        tasks = client.handle_sync(sync_data)
                        if tasks:
                            await asyncio.gather(*tasks)
                    except Exception as exc:
                        logger.warning("Matrix: sync event dispatch error: %s", exc)
                    await self._join_pending_invites(sync_data)

            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._closing:
                    return
                # Detect permanent auth/permission failures.
                err_str = str(exc).lower()
                if (
                    "401" in err_str
                    or "403" in err_str
                    or "unauthorized" in err_str
                    or "forbidden" in err_str
                ):
                    logger.error(
                        "Matrix: permanent auth error: %s — stopping sync", exc
                    )
                    return
                logger.warning("Matrix: sync error: %s — retrying in 5s", exc)
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Event callbacks
    # ------------------------------------------------------------------

    def _is_self_sender(self, sender: str) -> bool:
        """Return True if the sender refers to the bot's own account.

        Matrix user IDs are byte-compared after trimming whitespace and
        lowercasing — some homeservers normalize the localpart case
        differently at different API surfaces, and the reply-loop tail
        of the "hall of mirrors" bug (#15763) has been observed with the
        bot's own account bypassing a case-sensitive equality check.

        When ``self._user_id`` is empty (whoami hasn't resolved yet, or
        login failed), we cannot prove a sender is NOT us, so we return
        True defensively — an unidentified bot dropping its own events
        is always preferable to falling into an echo loop.
        """
        own = (self._user_id or "").strip().lower()
        if not own:
            return True
        return sender.strip().lower() == own

    @staticmethod
    def _is_system_or_bridge_sender(sender: str) -> bool:
        """Return True if the sender looks like a system / bridge / appservice
        identity rather than a real user.

        Appservice namespaces on Matrix conventionally prefix bot / puppet
        user IDs with an underscore (e.g. ``@_telegram_12345:server``,
        ``@_discord_999:server``, ``@_slack_...:server``).  Server-notices
        bots and bridge-controller bots on many homeservers use the same
        pattern.

        We treat these as system identities for pairing purposes: they
        should never be offered a pairing code, because an operator
        approving the code would hand the bridge itself permanent
        authorization — and every outbound message relayed by the bridge
        would then loop back into the agent as an "authorized user
        message", which is the root of issue #15763.

        Matches:
            ``@_something:server``   — appservice namespace convention
            ``@:server``             — malformed / empty localpart
            ``:server``              — malformed, no leading ``@``
        """
        s = (sender or "").strip()
        if not s:
            return True
        # Localpart is everything between leading '@' and ':'
        if s.startswith("@"):
            s = s[1:]
        if ":" in s:
            localpart, _, _ = s.partition(":")
        else:
            localpart = s
        if not localpart:
            return True
        return localpart.startswith("_")

    async def _on_room_message(self, event: Any) -> None:
        """Handle incoming room message events (text, media)."""
        room_id = str(getattr(event, "room_id", ""))
        sender = str(getattr(event, "sender", ""))

        # Diagnostic: confirm the callback is firing at all when DEBUG is on.
        # Helps users troubleshoot silent inbound issues like #5819, #7914, #12614.
        logger.debug(
            "Matrix: callback fired — event %s from %s in %s",
            getattr(event, "event_id", "?"),
            sender,
            room_id,
        )

        # Ignore own messages (case-insensitive; also drops when our own
        # user_id hasn't been resolved yet — see _is_self_sender docstring
        # and issue #15763).
        if self._is_self_sender(sender):
            return

        # Ignore appservice / bridge / system identities so they never
        # trigger the pairing flow.  Once a bridge user is paired, every
        # outbound message it relays would loop back as an authorized
        # user message (the "hall of mirrors" in #15763).
        if self._is_system_or_bridge_sender(sender):
            logger.debug(
                "Matrix: ignoring system/bridge sender %s in %s",
                sender,
                room_id,
            )
            return

        # Deduplicate by event ID.
        event_id = str(getattr(event, "event_id", ""))
        if self._is_duplicate_event(event_id):
            return

        # Startup grace: ignore old messages from initial sync.
        raw_ts = (
            getattr(event, "timestamp", None)
            or getattr(event, "server_timestamp", None)
            or 0
        )
        event_ts = raw_ts / 1000.0 if raw_ts else 0.0
        if event_ts and event_ts < self._startup_ts - _STARTUP_GRACE_SECONDS:
            # If we are well past startup but events are still being dropped
            # by the grace check, the host clock is probably set ahead of
            # real time — every live event then looks "older than startup".
            # Warn once so users can fix NTP instead of chasing a ghost.
            # See #12614 (Schnurzel700, April 2026).
            #
            # Filter out backfill (events legitimately old) by requiring:
            #  - we are >30s past startup (initial-sync replay window closed)
            #  - the skew is *consistent* across consecutive drops, which is
            #    the signature of a constant clock offset rather than a
            #    variable-age room history.  Backfill from a freshly invited
            #    room can deliver events spanning hours/days — those skews
            #    will be all over the place and reset the counter.
            if not self._clock_skew_warned and (
                time.time() - self._startup_ts > 30
            ):
                skew = self._startup_ts - event_ts
                # Sanity bound: malformed events with negative or absurd
                # timestamps shouldn't count.
                if 5 < skew < 86400:
                    if self._late_grace_drops == 0:
                        self._late_grace_skew = skew
                        self._late_grace_drops = 1
                    elif abs(skew - self._late_grace_skew) < 60:
                        # Consistent offset → likely real clock skew.
                        self._late_grace_drops += 1
                    else:
                        # Varied skew → likely backfill, restart sampling.
                        self._late_grace_skew = skew
                        self._late_grace_drops = 1
                    if self._late_grace_drops >= 3:
                        logger.warning(
                            "Matrix: dropped %d consecutive live events as "
                            "'too old' more than 30s after startup (skew "
                            "≈ %.0fs). The host system clock is likely set "
                            "ahead of real time, which causes the startup "
                            "grace filter to silently discard every incoming "
                            "message. Run `timedatectl set-ntp true` (or "
                            "sync NTP) and restart the bot.",
                            self._late_grace_drops,
                            skew,
                        )
                        self._clock_skew_warned = True
            return

        # Extract content from the event.
        content = getattr(event, "content", None)
        if content is None:
            return

        # Get msgtype — either from content object or raw dict.
        if hasattr(content, "msgtype"):
            msgtype = str(content.msgtype)
        elif isinstance(content, dict):
            msgtype = content.get("msgtype", "")
        else:
            msgtype = ""

        # Determine source content dict for relation/thread extraction.
        if isinstance(content, dict):
            source_content = content
        elif hasattr(content, "serialize"):
            source_content = content.serialize()
        else:
            source_content = {}

        relates_to = source_content.get("m.relates_to", {})

        # Skip edits (m.replace relation).
        if relates_to.get("rel_type") == "m.replace":
            return

        # Ignore m.notice to prevent bot-to-bot loops (m.notice is the
        # conventional msgtype for bot responses in the Matrix ecosystem).
        if msgtype == "m.notice":
            return

        # Dispatch by msgtype.
        media_msgtypes = ("m.image", "m.audio", "m.video", "m.file")
        if msgtype in media_msgtypes:
            await self._handle_media_message(
                room_id, sender, event_id, event_ts, source_content, relates_to, msgtype
            )
        elif msgtype == "m.text":
            await self._handle_text_message(
                room_id, sender, event_id, event_ts, source_content, relates_to
            )

    async def _resolve_message_context(
        self,
        room_id: str,
        sender: str,
        event_id: str,
        body: str,
        source_content: dict,
        relates_to: dict,
    ) -> Optional[tuple]:
        """Shared mention/thread/DM gating for text and media handlers.

        Returns (body, is_dm, chat_type, thread_id, display_name, source)
        or None if the message should be dropped (mention gating).
        """
        is_dm = await self._is_dm_room(room_id)
        chat_type = "dm" if is_dm else "group"

        thread_id = None
        if relates_to.get("rel_type") == "m.thread":
            thread_id = relates_to.get("event_id")

        formatted_body = source_content.get("formatted_body")
        # m.mentions.user_ids (MSC3952 / Matrix v1.7) — authoritative mention signal.
        mentions_block = source_content.get("m.mentions") or {}
        mention_user_ids = (
            mentions_block.get("user_ids") if isinstance(mentions_block, dict) else None
        )
        is_mentioned = self._is_bot_mentioned(body, formatted_body, mention_user_ids)

        # Require-mention gating.
        if not is_dm:
            # allowed_rooms check (whitelist — must pass before other gating).
            # When set, messages from rooms NOT in this whitelist are silently
            # ignored, even if @mentioned.  DMs are already excluded above.
            if self._allowed_rooms and room_id not in self._allowed_rooms:
                logger.debug(
                    "Matrix: ignoring message %s in %s — room not in "
                    "MATRIX_ALLOWED_ROOMS whitelist",
                    event_id,
                    room_id,
                )
                return None

            is_free_room = room_id in self._free_rooms
            in_bot_thread = bool(thread_id and thread_id in self._threads)
            if self._require_mention and not is_free_room and not in_bot_thread:
                if not is_mentioned:
                    logger.debug(
                        "Matrix: ignoring message %s in %s — no @mention "
                        "(set MATRIX_REQUIRE_MENTION=false to disable)",
                        event_id,
                        room_id,
                    )
                    return None

            # Thread-level @mention gating: even in a bot-participated thread,
            # require @mention when thread_require_mention is enabled.
            # Prevents infinite reply loops in multi-agent shared rooms
            # where multiple bots all participate in the same thread.
            elif (self._thread_require_mention and in_bot_thread
                  and not is_free_room):
                if not is_mentioned:
                    logger.debug(
                        "Matrix: ignoring message %s in thread %s — "
                        "no @mention (thread_require_mention=true)",
                        event_id,
                        thread_id,
                    )
                    return None

        # DM mention-thread.
        if is_dm and not thread_id and self._dm_mention_threads and is_mentioned:
            thread_id = event_id
            self._threads.mark(thread_id)

        # Strip mention from body (only when mention-gating is active).
        if is_mentioned and self._require_mention:
            body = self._strip_mention(body)

        # Auto-thread.
        if not thread_id and ((not is_dm and self._auto_thread) or (is_dm and self._dm_auto_thread)):
            thread_id = event_id
            self._threads.mark(thread_id)

        display_name = await self._get_display_name(room_id, sender)
        source = self.build_source(
            chat_id=room_id,
            chat_type=chat_type,
            user_id=sender,
            user_name=display_name,
            thread_id=thread_id,
        )

        if thread_id:
            self._threads.mark(thread_id)

        self._background_read_receipt(room_id, event_id)

        return body, is_dm, chat_type, thread_id, display_name, source

    async def _handle_text_message(
        self,
        room_id: str,
        sender: str,
        event_id: str,
        event_ts: float,
        source_content: dict,
        relates_to: dict,
    ) -> None:
        """Process a text message event."""
        body = source_content.get("body", "") or ""
        if not body:
            return

        ctx = await self._resolve_message_context(
            room_id,
            sender,
            event_id,
            body,
            source_content,
            relates_to,
        )
        if ctx is None:
            return
        body, is_dm, chat_type, thread_id, display_name, source = ctx

        # Reply-to detection.
        reply_to = None
        in_reply_to = relates_to.get("m.in_reply_to", {})
        if in_reply_to:
            reply_to = in_reply_to.get("event_id")

        # Strip reply fallback from body.
        if reply_to and body.startswith("> "):
            lines = body.split("\n")
            stripped = []
            past_fallback = False
            for line in lines:
                if not past_fallback:
                    if line.startswith("> ") or line == ">":
                        continue
                    if line == "":
                        past_fallback = True
                        continue
                    past_fallback = True
                stripped.append(line)
            body = "\n".join(stripped) if stripped else body

        msg_type = MessageType.TEXT
        if body.startswith(("!", "/")):
            msg_type = MessageType.COMMAND

        msg_event = MessageEvent(
            text=body,
            message_type=msg_type,
            source=source,
            raw_message=source_content,
            message_id=event_id,
            reply_to_message_id=reply_to,
        )

        if msg_type == MessageType.TEXT and self._text_batch_delay_seconds > 0:
            self._enqueue_text_event(msg_event)
        else:
            await self.handle_message(msg_event)

    async def _handle_media_message(
        self,
        room_id: str,
        sender: str,
        event_id: str,
        event_ts: float,
        source_content: dict,
        relates_to: dict,
        msgtype: str,
    ) -> None:
        """Process a media message event (image, audio, video, file)."""
        body = source_content.get("body", "") or ""
        url = source_content.get("url", "")

        # Convert mxc:// to HTTP URL for downstream processing.
        http_url = ""
        if url and url.startswith("mxc://"):
            http_url = self._mxc_to_http(url)

        # Extract MIME type from content info.
        content_info = source_content.get("info", {})
        if not isinstance(content_info, dict):
            content_info = {}
        event_mimetype = content_info.get("mimetype", "")

        # For encrypted media, the URL may be in file.url.
        file_content = source_content.get("file", {})
        if not url and isinstance(file_content, dict):
            url = file_content.get("url", "") or ""
            if url and url.startswith("mxc://"):
                http_url = self._mxc_to_http(url)

        is_encrypted_media = bool(
            file_content and isinstance(file_content, dict) and file_content.get("url")
        )

        media_type = "application/octet-stream"
        msg_type = MessageType.DOCUMENT
        is_voice_message = False

        if msgtype == "m.image":
            msg_type = MessageType.PHOTO
            media_type = event_mimetype or "image/png"
        elif msgtype == "m.audio":
            if source_content.get("org.matrix.msc3245.voice") is not None:
                is_voice_message = True
                msg_type = MessageType.VOICE
            else:
                msg_type = MessageType.AUDIO
            media_type = event_mimetype or "audio/ogg"
        elif msgtype == "m.video":
            msg_type = MessageType.VIDEO
            media_type = event_mimetype or "video/mp4"
        elif event_mimetype:
            media_type = event_mimetype

        # Cache media locally when downstream tools need a real file path.
        cached_path = None
        should_cache_locally = msg_type in {
            MessageType.PHOTO, MessageType.AUDIO, MessageType.VIDEO, MessageType.DOCUMENT,
        } or is_voice_message or is_encrypted_media
        if should_cache_locally and url:
            try:
                file_bytes = await self._client.download_media(ContentURI(url))
                if file_bytes is not None:
                    if is_encrypted_media:
                        from mautrix.crypto.attachments import decrypt_attachment

                        hashes_value = (
                            file_content.get("hashes")
                            if isinstance(file_content, dict)
                            else None
                        )
                        hash_value = (
                            hashes_value.get("sha256")
                            if isinstance(hashes_value, dict)
                            else None
                        )

                        key_value = (
                            file_content.get("key")
                            if isinstance(file_content, dict)
                            else None
                        )
                        if isinstance(key_value, dict):
                            key_value = key_value.get("k")

                        iv_value = (
                            file_content.get("iv")
                            if isinstance(file_content, dict)
                            else None
                        )

                        if key_value and hash_value and iv_value:
                            file_bytes = decrypt_attachment(
                                file_bytes, key_value, hash_value, iv_value
                            )
                        else:
                            logger.warning(
                                "[Matrix] Encrypted media event missing decryption metadata for %s",
                                event_id,
                            )
                            file_bytes = None

                    if file_bytes is not None:
                        from channels.platforms.base import (
                            cache_audio_from_bytes,
                            cache_document_from_bytes,
                            cache_image_from_bytes,
                        )

                        if msg_type == MessageType.PHOTO:
                            ext_map = {
                                "image/jpeg": ".jpg",
                                "image/png": ".png",
                                "image/gif": ".gif",
                                "image/webp": ".webp",
                            }
                            ext = ext_map.get(media_type, ".jpg")
                            cached_path = cache_image_from_bytes(file_bytes, ext=ext)
                            logger.info("[Matrix] Cached user image at %s", cached_path)
                        elif msg_type in {MessageType.AUDIO, MessageType.VOICE}:
                            ext = (
                                Path(
                                    body
                                    or (
                                        "voice.ogg" if is_voice_message else "audio.ogg"
                                    )
                                ).suffix
                                or ".ogg"
                            )
                            cached_path = cache_audio_from_bytes(file_bytes, ext=ext)
                        else:
                            filename = body or (
                                "video.mp4"
                                if msg_type == MessageType.VIDEO
                                else "document"
                            )
                            cached_path = cache_document_from_bytes(
                                file_bytes, filename
                            )
            except Exception as e:
                logger.warning("[Matrix] Failed to cache media: %s", e)

        ctx = await self._resolve_message_context(
            room_id,
            sender,
            event_id,
            body,
            source_content,
            relates_to,
        )
        if ctx is None:
            return
        body, is_dm, chat_type, thread_id, display_name, source = ctx

        if msgtype == "m.image" and _looks_like_matrix_image_filename(body):
            body = ""

        allow_http_fallback = bool(http_url) and not is_encrypted_media
        media_urls = (
            [cached_path]
            if cached_path
            else ([http_url] if allow_http_fallback else None)
        )
        media_types = [media_type] if media_urls else None

        msg_event = MessageEvent(
            text=body,
            message_type=msg_type,
            source=source,
            raw_message=source_content,
            message_id=event_id,
            media_urls=media_urls,
            media_types=media_types,
        )

        await self.handle_message(msg_event)

    async def _on_invite(self, event: Any) -> None:
        """Auto-join rooms when invited."""

        room_id = str(getattr(event, "room_id", ""))

        logger.info(
            "Matrix: invited to %s — joining",
            room_id,
        )
        await self._join_room_by_id(room_id)

    async def _join_room_by_id(self, room_id: str) -> bool:
        """Join a room by ID and refresh local caches on success."""
        if not room_id:
            return False
        if room_id in self._joined_rooms:
            return True
        try:
            await self._client.join_room(RoomID(room_id))
            self._joined_rooms.add(room_id)
            logger.info("Matrix: joined %s", room_id)
            await self._refresh_dm_cache()
            return True
        except Exception as exc:
            logger.warning("Matrix: error joining %s: %s", room_id, exc)
            return False

    async def _join_pending_invites(self, sync_data: Dict[str, Any]) -> None:
        """Join rooms still present in rooms.invite after sync processing."""
        rooms = sync_data.get("rooms", {}) if isinstance(sync_data, dict) else {}
        invites = rooms.get("invite", {})
        if not isinstance(invites, dict):
            return
        for room_id in invites:
            if room_id in self._joined_rooms:
                continue
            logger.info("Matrix: reconciling pending invite for %s", room_id)
            await self._join_room_by_id(str(room_id))

    # ------------------------------------------------------------------
    # Reactions (send, receive, processing lifecycle)
    # ------------------------------------------------------------------

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Session-scoped key for text message batching."""
        from channels.session_identity import build_session_key

        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get(
                "group_sessions_per_user", True
            ),
            thread_sessions_per_user=self.config.extra.get(
                "thread_sessions_per_user", False
            ),
        )

    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer a text event and reset the flush timer."""
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
            if event.text:
                existing.text = (
                    f"{existing.text}\n{event.text}" if existing.text else event.text
                )
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)

        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(
            self._flush_text_batch(key)
        )

    async def _flush_text_batch(self, key: str) -> None:
        """Wait for the quiet period then dispatch the aggregated text."""
        current_task = asyncio.current_task()
        try:
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            if last_len >= self._split_threshold:
                delay = self._text_batch_split_delay_seconds
            else:
                delay = self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            logger.info(
                "[Matrix] Flushing text batch %s (%d chars)",
                key,
                len(event.text or ""),
            )
            await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    # ------------------------------------------------------------------
    # Read receipts
    # ------------------------------------------------------------------
