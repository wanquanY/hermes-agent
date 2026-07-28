from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Optional

from agent.secret_scope import get_profile_env
from channels.platforms.telegram_security import redact_telegram_error

try:
    from telegram import LinkPreviewOptions, Update
    from telegram.ext import (
        Application,
        CommandHandler,
        CallbackQueryHandler,
        MessageHandler as TelegramMessageHandler,
        filters,
    )
    from telegram.request import HTTPXRequest
    TELEGRAM_AVAILABLE = True
except ImportError:  # pragma: no cover - optional platform dependency
    LinkPreviewOptions = None
    Update = Any
    Application = Any
    CommandHandler = Any
    CallbackQueryHandler = Any
    TelegramMessageHandler = Any
    HTTPXRequest = Any
    filters = None
    TELEGRAM_AVAILABLE = False

from channels.platforms.telegram_network import (
    TelegramFallbackTransport,
    discover_fallback_ips,
    parse_fallback_ip_env,
)
from channels.platforms.base import resolve_proxy_url
from channels.platforms.telegram import MAX_COMMANDS_PER_SCOPE

logger = logging.getLogger(__name__)


def _telegram_public_attr(name: str, fallback: Any = None) -> Any:
    import sys

    public_module = sys.modules.get("channels.platforms.telegram")
    if public_module is None:
        return fallback
    return getattr(public_module, name, fallback)


class TelegramConnectionMixin:
    def _fallback_ips(self) -> list[str]:
        """Return validated fallback IPs from config (populated by _apply_env_overrides)."""
        configured = self.config.extra.get("fallback_ips", []) if getattr(self.config, "extra", None) else []
        if isinstance(configured, str):
            configured = configured.split(",")
        return parse_fallback_ip_env(",".join(str(v) for v in configured) if configured else None)
    
    @staticmethod
    def _looks_like_polling_conflict(error: Exception) -> bool:
        text = str(error).lower()
        return (
            error.__class__.__name__.lower() == "conflict"
            or "terminated by other getupdates request" in text
            or "another bot instance is running" in text
        )
    
    @staticmethod
    def _looks_like_network_error(error: Exception) -> bool:
        """Return True for transient network errors that warrant a reconnect attempt."""
        name = error.__class__.__name__.lower()
        if name in {"networkerror", "timedout", "connectionerror"}:
            return True
        try:
            from telegram.error import NetworkError, TimedOut
            if isinstance(error, (NetworkError, TimedOut)):
                return True
        except ImportError:
            pass
        return isinstance(error, OSError)
    
    @staticmethod
    def _looks_like_connect_timeout(error: Exception) -> bool:
        """Return True when a Telegram TimedOut wraps a connect-timeout.
    
        A plain Telegram TimedOut may mean the request reached Telegram and
        should not be re-sent. A ConnectTimeout means the TCP connection was
        never established, so retrying is safe and prevents silent drops.
        """
        seen: set[int] = set()
        stack: list[BaseException] = [error]
        while stack:
            cur = stack.pop()
            ident = id(cur)
            if ident in seen:
                continue
            seen.add(ident)
            name = cur.__class__.__name__.lower()
            text = str(cur).lower()
            if "connecttimeout" in name or "connect timeout" in text or "connect timed out" in text:
                return True
            cause = getattr(cur, "__cause__", None)
            context = getattr(cur, "__context__", None)
            if cause is not None:
                stack.append(cause)
            if context is not None:
                stack.append(context)
        return False
    
    @staticmethod
    def _looks_like_pool_timeout(error: Exception) -> bool:
        """Return True when a Telegram TimedOut wraps an httpx pool timeout.
    
        PTB converts ``httpx.PoolTimeout`` into ``telegram.error.TimedOut`` with
        a message that explicitly states the request was *not* sent
        (``"Pool timeout: All connections in the connection pool are occupied.
        Request was *not* sent to Telegram."``). Because the request never left
        the process, re-sending is safe and cannot duplicate -- the opposite of
        a generic TimedOut, which may have reached Telegram. We match the
        wrapped ``httpx.PoolTimeout`` class as well as the message string so the
        check survives PTB message-wording changes.
        """
        seen: set[int] = set()
        stack: list[BaseException] = [error]
        while stack:
            cur = stack.pop()
            ident = id(cur)
            if ident in seen:
                continue
            seen.add(ident)
            name = cur.__class__.__name__.lower()
            text = str(cur).lower()
            if "pooltimeout" in name or "pool timeout" in text or (
                "connection pool" in text and "occupied" in text
            ):
                return True
            cause = getattr(cur, "__cause__", None)
            context = getattr(cur, "__context__", None)
            if cause is not None:
                stack.append(cause)
            if context is not None:
                stack.append(context)
        return False
    
    def _coerce_bool_extra(self, key: str, default: bool = False) -> bool:
        value = self.config.extra.get(key) if getattr(self.config, "extra", None) else None
        if value is None:
            return default
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
            return default
        return bool(value)
    
    def _link_preview_kwargs(self) -> Dict[str, Any]:
        if not getattr(self, "_disable_link_previews", False):
            return {}
        if LinkPreviewOptions is not None:
            return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
        return {"disable_web_page_preview": True}
    
    async def _create_dm_topic(
        self,
        chat_id: int,
        name: str,
        icon_color: Optional[int] = None,
        icon_custom_emoji_id: Optional[str] = None,
    ) -> Optional[int]:
        """Create a forum topic in a private (DM) chat.
    
        Uses Bot API 9.4's createForumTopic which now works for 1-on-1 chats.
        Returns the message_thread_id on success, None on failure.
        """
        if not self._bot:
            return None
        try:
            kwargs: Dict[str, Any] = {"chat_id": chat_id, "name": name}
            if icon_color is not None:
                kwargs["icon_color"] = icon_color
            if icon_custom_emoji_id:
                kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id
    
            topic = await self._bot.create_forum_topic(**kwargs)
            thread_id = topic.message_thread_id
            logger.info(
                "[%s] Created DM topic '%s' in chat %s -> thread_id=%s",
                self.name, name, chat_id, thread_id,
            )
            return thread_id
        except Exception as e:
            error_text = str(e).lower()
            # If topic already exists, try to find it via getForumTopicIconStickers
            # or we just log and skip — Telegram doesn't provide a "list topics" API
            if "topic_name_duplicate" in error_text or "already" in error_text:
                logger.info(
                    "[%s] DM topic '%s' already exists in chat %s (will be mapped from incoming messages)",
                    self.name, name, chat_id,
                )
            elif "not a forum" in error_text or "forums_disabled" in error_text:
                logger.warning(
                    "[%s] Cannot create DM topic '%s' in chat %s: Topics mode is not enabled. "
                    "The user must open the DM with this bot in Telegram, tap the bot name "
                    "at the top, and enable 'Topics' in chat settings before topics can be created.",
                    self.name, name, chat_id,
                )
            else:
                logger.warning(
                    "[%s] Failed to create DM topic '%s' in chat %s: %s",
                    self.name, name, chat_id, redact_telegram_error(e),
                )
            return None
    
    async def create_handoff_thread(
        self,
        parent_chat_id: str,
        name: str,
    ) -> Optional[str]:
        """Create a forum topic for a session handoff.
    
        Works for DM topics (Bot API 9.4+, requires user to enable Topics
        in their chat with the bot) and forum supergroups. Returns the
        ``message_thread_id`` as a string, or ``None`` on failure.
        """
        try:
            chat_id_int = int(parent_chat_id)
        except (TypeError, ValueError):
            return None
        thread_id = await self._create_dm_topic(chat_id_int, name=name)
        return str(thread_id) if thread_id else None
    
    async def ensure_dm_topic(self, chat_id: str, topic_name: str, force_create: bool = False) -> Optional[str]:
        """Return a private DM topic thread id, creating and persisting it if needed."""
        name = str(topic_name or "").strip()
        if not name:
            return None
        try:
            chat_id_int = int(chat_id)
        except (TypeError, ValueError):
            return None
    
        cache_key = f"{chat_id_int}:{name}"
        cached = self._dm_topics.get(cache_key)
        if cached and not force_create:
            return str(cached)
    
        topic_conf: Optional[Dict[str, Any]] = None
        chat_entry: Optional[Dict[str, Any]] = None
        for entry in self._dm_topics_config:
            if str(entry.get("chat_id")) != str(chat_id_int):
                continue
            chat_entry = entry
            for candidate in entry.get("topics", []):
                if candidate.get("name") == name:
                    topic_conf = candidate
                    break
            break
    
        if topic_conf and topic_conf.get("thread_id") and not force_create:
            thread_id = int(topic_conf["thread_id"])
            self._dm_topics[cache_key] = thread_id
            return str(thread_id)
    
        if chat_entry is None:
            chat_entry = {"chat_id": chat_id_int, "topics": []}
            self._dm_topics_config.append(chat_entry)
        if topic_conf is None:
            topic_conf = {"name": name}
            chat_entry.setdefault("topics", []).append(topic_conf)
    
        thread_id = await self._create_dm_topic(
            chat_id_int,
            name=name,
            icon_color=topic_conf.get("icon_color"),
            icon_custom_emoji_id=topic_conf.get("icon_custom_emoji_id"),
        )
        if not thread_id:
            return None
    
        topic_conf["thread_id"] = thread_id
        self._dm_topics[cache_key] = int(thread_id)
        self._persist_dm_topic_thread_id(chat_id_int, name, int(thread_id), replace_existing=force_create)
        return str(thread_id)
    
    async def rename_dm_topic(
        self,
        chat_id: int,
        thread_id: int,
        name: str,
    ) -> None:
        """Rename a forum topic in a private (DM) chat."""
        if not self._bot:
            return
        try:
            chat_id_arg = int(chat_id)
        except (TypeError, ValueError):
            chat_id_arg = chat_id
        await self._bot.edit_forum_topic(
            chat_id=chat_id_arg,
            message_thread_id=int(thread_id),
            name=name,
        )
        logger.info(
            "[%s] Renamed DM topic in chat %s thread_id=%s -> '%s'",
            self.name, chat_id, thread_id, name,
        )
    
    def _persist_dm_topic_thread_id(
        self,
        chat_id: int,
        topic_name: str,
        thread_id: int,
        replace_existing: bool = False,
    ) -> None:
        """Save a newly created thread_id back into config.yaml so it persists across restarts."""
        try:
            from hermes_constants import get_hermes_home
            config_path = get_hermes_home() / "config.yaml"
            if not config_path.exists():
                logger.warning("[%s] Config file not found at %s, cannot persist thread_id", self.name, config_path)
                return
    
            import yaml as _yaml
            with open(config_path, "r", encoding="utf-8") as f:
                config = _yaml.safe_load(f) or {}
    
            # Navigate to platforms.telegram.extra.dm_topics, creating the path
            # when a named delivery target asks us to create a topic that was
            # not predeclared in config.yaml.
            platforms = config.setdefault("platforms", {})
            telegram_config = platforms.setdefault("telegram", {})
            extra = telegram_config.setdefault("extra", {})
            dm_topics = extra.setdefault("dm_topics", [])
    
            changed = False
            matching_chat_entry = None
            for chat_entry in dm_topics:
                try:
                    chat_matches = int(chat_entry.get("chat_id", 0)) == int(chat_id)
                except (TypeError, ValueError):
                    chat_matches = False
                if not chat_matches:
                    continue
                matching_chat_entry = chat_entry
                for t in chat_entry.setdefault("topics", []):
                    if t.get("name") == topic_name:
                        if replace_existing or not t.get("thread_id"):
                            if t.get("thread_id") != thread_id:
                                t["thread_id"] = thread_id
                                changed = True
                        break
                else:
                    chat_entry.setdefault("topics", []).append(
                        {"name": topic_name, "thread_id": thread_id}
                    )
                    changed = True
                break
    
            if matching_chat_entry is None:
                dm_topics.append({
                    "chat_id": chat_id,
                    "topics": [{"name": topic_name, "thread_id": thread_id}],
                })
                changed = True
    
            if changed:
                from hermes_cli.config import atomic_config_write

                atomic_config_write(
                    config_path,
                    config,
                    default_flow_style=False,
                    sort_keys=False,
                )
                logger.info(
                    "[%s] Persisted thread_id=%s for topic '%s' in config.yaml",
                    self.name, thread_id, topic_name,
                )
        except Exception as e:
            logger.warning(
                "[%s] Failed to persist thread_id to config: %s",
                self.name,
                redact_telegram_error(e),
            )
    
    async def _setup_dm_topics(self) -> None:
        """Load or create configured DM topics for specified chats.
    
        Reads config.extra['dm_topics'] — a list of dicts:
        [
            {
                "chat_id": 123456789,
                "topics": [
                    {"name": "General", "icon_color": 7322096, "thread_id": 100},
                    {"name": "Accessibility Auditor", "icon_color": 9367192, "skill": "accessibility-auditor"}
                ]
            }
        ]
    
        If a topic already has a thread_id in the config (persisted from a previous
        creation), it is loaded into the cache without calling createForumTopic.
        Only topics without a thread_id are created via the API, and their thread_id
        is then saved back to config.yaml for future restarts.
        """
        if not self._dm_topics_config:
            return
    
        for chat_entry in self._dm_topics_config:
            chat_id = chat_entry.get("chat_id")
            topics = chat_entry.get("topics", [])
            if not chat_id or not topics:
                continue
    
            logger.info(
                "[%s] Setting up %d DM topic(s) for chat %s",
                self.name, len(topics), chat_id,
            )
    
            for topic_conf in topics:
                topic_name = topic_conf.get("name")
                if not topic_name:
                    continue
    
                cache_key = f"{chat_id}:{topic_name}"
    
                # If thread_id is already persisted in config, just load into cache
                existing_thread_id = topic_conf.get("thread_id")
                if existing_thread_id:
                    self._dm_topics[cache_key] = int(existing_thread_id)
                    logger.info(
                        "[%s] DM topic loaded from config: %s -> thread_id=%s",
                        self.name, cache_key, existing_thread_id,
                    )
                    continue
    
                # No persisted thread_id — create the topic via API
                icon_color = topic_conf.get("icon_color")
                icon_emoji = topic_conf.get("icon_custom_emoji_id")
    
                thread_id = await self._create_dm_topic(
                    chat_id=int(chat_id),
                    name=topic_name,
                    icon_color=icon_color,
                    icon_custom_emoji_id=icon_emoji,
                )
    
                if thread_id:
                    self._dm_topics[cache_key] = thread_id
                    logger.info(
                        "[%s] DM topic cached: %s -> thread_id=%s",
                        self.name, cache_key, thread_id,
                    )
                    # Persist thread_id to config so we don't recreate on next restart
                    self._persist_dm_topic_thread_id(int(chat_id), topic_name, thread_id)
    
                    # Send a seed message so the topic is visible in Telegram's client.
                    # Empty topics are hidden by the client UI until they contain a message.
                    try:
                        await self._bot.send_message(
                            chat_id=int(chat_id),
                            message_thread_id=thread_id,
                            text=f"\U0001f4cc {topic_name}",
                        )
                    except Exception as seed_err:
                        logger.debug(
                            "[%s] Could not send seed message to topic '%s': %s",
                            self.name, topic_name, seed_err,
                        )
    
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to Telegram via polling or webhook.
    
        By default, uses long polling (outbound connection to Telegram).
        If ``TELEGRAM_WEBHOOK_URL`` is set, starts an HTTP webhook server
        instead.  Webhook mode is useful for cloud deployments (Fly.io,
        Railway) where inbound HTTP can wake a suspended machine.
    
        Env vars for webhook mode::
    
            TELEGRAM_WEBHOOK_URL    Public HTTPS URL (e.g. https://app.fly.dev/telegram)
            TELEGRAM_WEBHOOK_PORT   Local listen port (default 8443)
            TELEGRAM_WEBHOOK_SECRET Secret token for update verification
        """
        # Only an explicit connection attempt may reopen lifecycle state after
        # a completed teardown; background recovery never clears this fence.
        self._polling_teardown_started = False
        self._webhook_mode = False

        if not _telegram_public_attr("TELEGRAM_AVAILABLE", TELEGRAM_AVAILABLE):
            logger.error(
                "[%s] python-telegram-bot not installed. Run: pip install python-telegram-bot",
                self.name,
            )
            return False
        
        if not self.config.token:
            logger.error("[%s] No bot token configured", self.name)
            return False
        
        try:
            if not self._acquire_platform_lock('telegram-bot-token', self.config.token, 'Telegram bot token'):
                return False
    
            # Build the application
            builder = _telegram_public_attr("Application", Application).builder().token(self.config.token)
            custom_base_url = self.config.extra.get("base_url")
            if custom_base_url:
                builder = builder.base_url(custom_base_url)
                builder = builder.base_file_url(
                    self.config.extra.get("base_file_url", custom_base_url)
                )
                logger.info(
                    "[%s] Using custom Telegram base_url: %s",
                    self.name, custom_base_url,
                )
            # In local-mode telegram-bot-api, file_path is an absolute path on the
            # server's filesystem rather than a relative HTTP path. PTB needs
            # local_mode=True so download_*() reads from disk instead of issuing
            # an HTTP GET that would 404. Requires that the same path is
            # readable by the Hermes process (shared mount, same machine, etc.).
            if self.config.extra.get("local_mode"):
                builder = builder.local_mode(True)
                logger.info("[%s] Using Telegram local_mode (read files from disk)", self.name)
    
            # PTB defaults (pool_timeout=1s) are too aggressive on flaky networks and
            # can trigger "Pool timeout: All connections in the connection pool are occupied"
            # during reconnect/bootstrap. Use safer defaults and allow env overrides.
            def _env_int(name: str, default: int) -> int:
                try:
                    return int(get_profile_env(name, str(default)))
                except (TypeError, ValueError):
                    return default
    
            def _env_float(name: str, default: float) -> float:
                try:
                    return float(get_profile_env(name, str(default)))
                except (TypeError, ValueError):
                    return default
    
            request_kwargs = {
                "connection_pool_size": _env_int("HERMES_TELEGRAM_HTTP_POOL_SIZE", 512),
                "pool_timeout": _env_float("HERMES_TELEGRAM_HTTP_POOL_TIMEOUT", 8.0),
                "connect_timeout": _env_float("HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT", 10.0),
                "read_timeout": _env_float("HERMES_TELEGRAM_HTTP_READ_TIMEOUT", 20.0),
                "write_timeout": _env_float("HERMES_TELEGRAM_HTTP_WRITE_TIMEOUT", 20.0),
            }
    
            disable_fallback = (
                get_profile_env(
                    "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS", ""
                ).strip().lower()
                in {"1", "true", "yes", "on"}
            )
            fallback_ips = self._fallback_ips()
            if not fallback_ips:
                fallback_ips = await _telegram_public_attr("discover_fallback_ips", discover_fallback_ips)()
                logger.info(
                    "[%s] Auto-discovered Telegram fallback IPs: %s",
                    self.name,
                    ", ".join(fallback_ips),
                )
    
            proxy_targets = ["api.telegram.org", *fallback_ips]
            proxy_url = resolve_proxy_url(
                "TELEGRAM_PROXY",
                target_hosts=proxy_targets,
                explicit_url=self.config.extra.get("proxy_url"),
            )
            if fallback_ips and not proxy_url and not disable_fallback:
                logger.info(
                    "[%s] Telegram fallback IPs active: %s",
                    self.name,
                    ", ".join(fallback_ips),
                )
                # Keep request/update pools separate to reduce contention during
                # polling reconnect + bot API bootstrap/delete_webhook calls.
                request = _telegram_public_attr("HTTPXRequest", HTTPXRequest)(
                    **request_kwargs,
                    httpx_kwargs={"transport": _telegram_public_attr("TelegramFallbackTransport", TelegramFallbackTransport)(fallback_ips)},
                )
                get_updates_request = _telegram_public_attr("HTTPXRequest", HTTPXRequest)(
                    **request_kwargs,
                    httpx_kwargs={"transport": _telegram_public_attr("TelegramFallbackTransport", TelegramFallbackTransport)(fallback_ips)},
                )
            elif proxy_url:
                logger.info("[%s] Proxy detected; passing explicitly to HTTPXRequest: %s", self.name, proxy_url)
                request = _telegram_public_attr("HTTPXRequest", HTTPXRequest)(**request_kwargs, proxy=proxy_url)
                get_updates_request = _telegram_public_attr("HTTPXRequest", HTTPXRequest)(**request_kwargs, proxy=proxy_url)
            else:
                if disable_fallback:
                    logger.info("[%s] Telegram fallback-IP transport disabled via env", self.name)
                request = _telegram_public_attr("HTTPXRequest", HTTPXRequest)(**request_kwargs)
                get_updates_request = _telegram_public_attr("HTTPXRequest", HTTPXRequest)(**request_kwargs)
    
            get_updates_request = self._instrument_polling_request(get_updates_request)
            builder = builder.request(request).get_updates_request(get_updates_request)
            self._app = builder.build()
            self._bot = self._app.bot
            
            # Register handlers
            self._app.add_handler(_telegram_public_attr("TelegramMessageHandler", TelegramMessageHandler)(
                _telegram_public_attr("filters", filters).TEXT & ~_telegram_public_attr("filters", filters).COMMAND,
                self._handle_text_message
            ))
            self._app.add_handler(_telegram_public_attr("TelegramMessageHandler", TelegramMessageHandler)(
                _telegram_public_attr("filters", filters).COMMAND,
                self._handle_command
            ))
            self._app.add_handler(_telegram_public_attr("TelegramMessageHandler", TelegramMessageHandler)(
                _telegram_public_attr("filters", filters).LOCATION | getattr(_telegram_public_attr("filters", filters), "VENUE", _telegram_public_attr("filters", filters).LOCATION),
                self._handle_location_message
            ))
            self._app.add_handler(_telegram_public_attr("TelegramMessageHandler", TelegramMessageHandler)(
                _telegram_public_attr("filters", filters).PHOTO | _telegram_public_attr("filters", filters).VIDEO | _telegram_public_attr("filters", filters).AUDIO | _telegram_public_attr("filters", filters).VOICE | _telegram_public_attr("filters", filters).Document.ALL | _telegram_public_attr("filters", filters).Sticker.ALL,
                self._handle_media_message
            ))
            # Handle inline keyboard button callbacks (update prompts)
            self._app.add_handler(_telegram_public_attr("CallbackQueryHandler", CallbackQueryHandler)(self._handle_callback_query))
            
            # Start polling — retry initialize() for transient TLS resets
            try:
                from telegram.error import NetworkError, TimedOut
            except ImportError:
                NetworkError = TimedOut = OSError  # type: ignore[misc,assignment]
            _max_connect = 8
            for _attempt in range(_max_connect):
                try:
                    await self._app.initialize()
                    break
                except (NetworkError, TimedOut, OSError) as init_err:
                    if _attempt < _max_connect - 1:
                        wait = min(2 ** _attempt, 15)
                        logger.warning(
                            "[%s] Connect attempt %d/%d failed: %s — retrying in %ds",
                            self.name,
                            _attempt + 1,
                            _max_connect,
                            redact_telegram_error(init_err),
                            wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise
            await self._app.start()
    
            # Decide between webhook and polling mode
            webhook_url = str(
                self.config.extra.get("webhook_url")
                or get_profile_env("TELEGRAM_WEBHOOK_URL", "")
            ).strip()
    
            if webhook_url:
                # ── Webhook mode ─────────────────────────────────────
                # Telegram pushes updates to our HTTP endpoint.  This
                # enables cloud platforms (Fly.io, Railway) to auto-wake
                # suspended machines on inbound HTTP traffic.
                #
                # SECURITY: TELEGRAM_WEBHOOK_SECRET is REQUIRED. Without it,
                # python-telegram-bot passes secret_token=None and the
                # webhook endpoint accepts any HTTP POST — attackers can
                # inject forged updates as if from Telegram. Refuse to
                # start rather than silently run in fail-open mode.
                # See GHSA-3vpc-7q5r-276h.
                raw_webhook_port = self.config.extra.get("webhook_port")
                if raw_webhook_port is None:
                    raw_webhook_port = get_profile_env(
                        "TELEGRAM_WEBHOOK_PORT", "8443"
                    )
                try:
                    webhook_port = int(raw_webhook_port)
                except (TypeError, ValueError):
                    webhook_port = 8443
                webhook_secret = str(
                    self.config.extra.get("webhook_secret")
                    or get_profile_env("TELEGRAM_WEBHOOK_SECRET", "")
                ).strip()
                if not webhook_secret:
                    raise RuntimeError(
                        "TELEGRAM_WEBHOOK_SECRET is required when "
                        "TELEGRAM_WEBHOOK_URL is set. Without it, the "
                        "webhook endpoint accepts forged updates from "
                        "anyone who can reach it — see "
                        "https://github.com/NousResearch/hermes-agent/"
                        "security/advisories/GHSA-3vpc-7q5r-276h.\n\n"
                        "Generate a secret and set it in your .env:\n"
                        "  export TELEGRAM_WEBHOOK_SECRET=\"$(openssl rand -hex 32)\"\n\n"
                        "Then register it with Telegram when setting the "
                        "webhook via setWebhook's secret_token parameter."
                    )
                from urllib.parse import urlparse
                webhook_path = urlparse(webhook_url).path or "/telegram"
    
                await self._app.updater.start_webhook(
                    listen="0.0.0.0",
                    port=webhook_port,
                    url_path=webhook_path,
                    webhook_url=webhook_url,
                    secret_token=webhook_secret,
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=not is_reconnect,
                )
                self._webhook_mode = True
                self._polling_progress_accepting = False
                self._send_path_degraded = False
                logger.info(
                    "[%s] Webhook server listening on 0.0.0.0:%d%s",
                    self.name, webhook_port, webhook_path,
                )
            else:
                # ── Polling mode (default) ───────────────────────────
                # Clear any stale webhook first so polling doesn't inherit a
                # previous webhook registration and silently stop receiving updates.
                await self._delete_webhook_best_effort()
    
                loop = asyncio.get_running_loop()
    
                def _polling_error_callback(error: Exception) -> None:
                    if self._polling_teardown_started:
                        return
                    if self._polling_error_task and not self._polling_error_task.done():
                        return
                    self._disarm_ptb_retry_loop()
                    if self._looks_like_polling_conflict(error):
                        self._polling_error_task = loop.create_task(self._handle_polling_conflict(error))
                    elif self._looks_like_network_error(error):
                        logger.warning(
                            "[%s] Telegram network error, scheduling reconnect: %s",
                            self.name,
                            redact_telegram_error(error),
                        )
                        self._polling_error_task = loop.create_task(self._handle_polling_network_error(error))
                    else:
                        logger.error(
                            "[%s] Telegram polling error: %s",
                            self.name,
                            redact_telegram_error(error),
                        )
                    if self._polling_error_task is not None:
                        self._background_tasks.add(self._polling_error_task)
                        self._polling_error_task.add_done_callback(
                            self._background_tasks.discard
                        )
    
                # Store reference for retry use in _handle_polling_conflict
                self._polling_error_callback_ref = _polling_error_callback
    
                await self._start_polling_resilient(
                    drop_pending_updates=not is_reconnect,
                    error_callback=_polling_error_callback,
                )

                heartbeat = self._polling_heartbeat_task
                if heartbeat is not None and not heartbeat.done():
                    heartbeat.cancel()
                self._polling_heartbeat_task = loop.create_task(
                    self._polling_heartbeat_loop()
                )
                self._background_tasks.add(self._polling_heartbeat_task)
                self._polling_heartbeat_task.add_done_callback(
                    self._background_tasks.discard
                )
            
            # Register bot commands so Telegram shows a hint menu when users type /
            # List is derived from the central COMMAND_REGISTRY — adding a new
            # gateway command there automatically adds it to the Telegram menu.
            try:
                from telegram import (
                    BotCommand,
                    BotCommandScopeAllPrivateChats,
                    BotCommandScopeAllGroupChats,
                    BotCommandScopeDefault,
                )
                from hermes_cli.commands import telegram_menu_commands
                # Telegram allows up to 100 commands but has an undocumented
                # payload size limit (~4KB total).  Limit to 30 core commands
                # to stay well under the threshold while covering all categories.
                menu_commands, hidden_count = telegram_menu_commands(max_commands=MAX_COMMANDS_PER_SCOPE)
                bot_commands = [BotCommand(name, desc) for name, desc in menu_commands]
                # Register for all scopes independently — Telegram picks the
                # narrowest matching scope per chat type (forum topics fall
                # through to AllGroupChats or Default).
                for scope_cls in (BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats):
                    scope_name = scope_cls.__name__
                    try:
                        await self._bot.set_my_commands(bot_commands, scope=scope_cls())
                        logger.info("[%s] set_my_commands OK for scope %s (%d cmds)", self.name, scope_name, len(bot_commands))
                    except Exception as scope_err:
                        logger.warning(
                            "[%s] set_my_commands FAILED for scope %s: %s",
                            self.name,
                            scope_name,
                            redact_telegram_error(scope_err),
                        )
                # Forum topics don't inherit AllGroupChats — Telegram resolves
                # commands via BotCommandScopeChat(chat_id) for forum groups.
                # Lazy registration happens in _ensure_forum_commands on first
                # message from a forum topic (see _handle_text_message).
                if hidden_count:
                    logger.info(
                        "[%s] Telegram menu: %d commands registered, %d hidden (over %d limit). Use /commands for full list.",
                        self.name, len(menu_commands), hidden_count, 30,
                    )
            except Exception as e:
                logger.warning(
                    "[%s] Could not register Telegram command menu: %s",
                    self.name,
                    redact_telegram_error(e),
                )
            
            self._mark_connected()
            mode = "webhook" if self._webhook_mode else "polling"
            logger.info("[%s] Connected to Telegram (%s mode)", self.name, mode)
    
            # Surface the gateway as "Online" in the bot's short description
            # (opt-in via extra.status_indicator). Non-fatal.
            try:
                await self._set_status_indicator(online=True)
            except Exception:
                pass
    
            # Set up DM topics (Bot API 9.4 — Private Chat Topics)
            # Runs after connection is established so the bot can call createForumTopic.
            # Failures here are non-fatal — the bot works fine without topics.
            try:
                await self._setup_dm_topics()
            except Exception as topics_err:
                logger.warning(
                    "[%s] DM topics setup failed (non-fatal): %s",
                    self.name, redact_telegram_error(topics_err),
                )
    
            return True
            
        except Exception as e:
            self._release_platform_lock()
            safe_error = redact_telegram_error(e)
            message = f"Telegram startup failed: {safe_error}"
            self._set_fatal_error("telegram_connect_error", message, retryable=True)
            logger.error(
                "[%s] Failed to connect to Telegram: %s",
                self.name,
                safe_error,
            )
            return False
    
    async def _set_status_indicator(self, online: bool) -> None:
        """Set the bot's short description to the online/offline status text.
    
        The short description is the line shown under the bot's name in its
        profile. It is the closest Bot API surface to a presence indicator —
        bots have no real online/offline dot (that's a user-account feature).
    
        No-op unless ``extra.status_indicator`` is enabled. Best-effort: any
        failure is logged at debug and swallowed so it never blocks connect or
        disconnect. The default (no language_code) description applies to every
        user who doesn't have a language-specific one set.
        """
        if not getattr(self, "_status_indicator_enabled", False):
            return
        bot = self._bot
        if bot is None:
            return
        text = self._status_online_text if online else self._status_offline_text
        # Telegram caps short_description at 120 chars.
        text = text[:120]
        try:
            await bot.set_my_short_description(short_description=text)
            logger.info("[%s] Set bot status indicator to %r", self.name, text)
        except Exception as e:
            logger.debug(
                "[%s] Failed to set bot status indicator to %r: %s",
                self.name, text, redact_telegram_error(e),
            )

    async def _cancel_pending_delivery_tasks(self) -> None:
        """Cancel delayed inbound batching without racing disconnect teardown."""
        current = asyncio.current_task()
        pending: list[asyncio.Task] = []
        seen: set[int] = set()
        task_maps = (
            self._media_group_tasks,
            self._pending_photo_batch_tasks,
            getattr(self, "_pending_text_batch_tasks", {}),
        )
        for task_map in task_maps:
            for task in list(task_map.values()):
                if task is None or task.done() or task is current or id(task) in seen:
                    continue
                seen.add(id(task))
                task.cancel()
                if inspect.isawaitable(task):
                    pending.append(task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._media_group_tasks.clear()
        self._media_group_events.clear()
        self._pending_photo_batch_tasks.clear()
        self._pending_photo_batches.clear()
        getattr(self, "_pending_text_batch_tasks", {}).clear()
        getattr(self, "_pending_text_batches", {}).clear()

    async def disconnect(self) -> None:
        """Fence recovery, stop PTB boundedly, and release all adapter state."""
        self._mark_disconnected()
        self._fence_polling_teardown()
        await self._cancel_polling_lifecycle_tasks()

        # Mark the bot "Offline" in its short description while the bot's HTTP
        # client is still alive (before app shutdown closes it). Opt-in via
        # extra.status_indicator. Non-fatal. This is the clean-shutdown path;
        # a hard crash leaves the last-known status, which is the expected
        # limitation of a profile-text indicator.
        try:
            await self._set_status_indicator(online=False)
        except Exception:
            pass

        await self._cancel_pending_delivery_tasks()

        app = self._app
        if app:
            try:
                if app.updater and app.updater.running:
                    try:
                        await asyncio.wait_for(
                            app.updater.stop(),
                            timeout=_telegram_public_attr(
                                "_UPDATER_STOP_TIMEOUT",
                                15.0,
                            ),
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[%s] updater.stop() timed out during disconnect; "
                            "continuing teardown",
                            self.name,
                        )
                if app.running:
                    await app.stop()
                await app.shutdown()
            except Exception as e:
                logger.warning(
                    "[%s] Error during Telegram disconnect: %s",
                    self.name,
                    redact_telegram_error(e),
                )
        self._release_platform_lock()

        self._app = None
        self._bot = None
        logger.info("[%s] Disconnected from Telegram", self.name)
