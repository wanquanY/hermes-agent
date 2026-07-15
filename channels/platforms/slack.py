"""
Slack platform adapter.

Uses slack-bolt (Python) with Socket Mode for:
- Receiving messages from channels and DMs
- Sending responses back
- Handling slash commands
- Thread support
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Dict, Optional, Any, Tuple, List

try:
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_sdk.web.async_client import AsyncWebClient
    SLACK_AVAILABLE = True
except ImportError:
    SLACK_AVAILABLE = False
    AsyncApp = Any
    AsyncSocketModeHandler = Any
    AsyncWebClient = Any

try:
    import aiohttp
except ImportError:
    aiohttp = Any

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from channels.config import Platform, PlatformConfig
from channels.platforms.helpers import MessageDeduplicator
from channels.platforms.slack_inbound import SlackInboundMixin
from channels.platforms.slack_support import _ThreadContextCache, _slash_user_id
from channels.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    SUPPORTED_DOCUMENT_TYPES,
    is_host_excluded_by_no_proxy,
    resolve_proxy_url,
    safe_url_for_log,
    cache_document_from_bytes,
)


logger = logging.getLogger(__name__)


def check_slack_requirements() -> bool:
    """Check if Slack dependencies are available.

    Lazy-installs slack-bolt/slack-sdk via ``tools.lazy_deps.ensure("platform.slack")``
    on first call if not present. Rebinds all module-level globals on success.
    """
    if SLACK_AVAILABLE:
        return True

    def _import():
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_sdk.web.async_client import AsyncWebClient
        import aiohttp
        return {
            "AsyncApp": AsyncApp,
            "AsyncSocketModeHandler": AsyncSocketModeHandler,
            "AsyncWebClient": AsyncWebClient,
            "aiohttp": aiohttp,
            "SLACK_AVAILABLE": True,
        }

    from tools.lazy_deps import ensure_and_bind
    return ensure_and_bind("platform.slack", _import, globals(), prompt=False)



def _apply_slack_proxy(client: Any, proxy_url: Optional[str]) -> None:
    """Apply a resolved proxy to a Slack SDK client or clear it explicitly."""
    if hasattr(client, "proxy"):
        client.proxy = proxy_url


_SLACK_PROXY_HOSTS = (
    "slack.com",
    "files.slack.com",
    "wss-primary.slack.com",
)


def _resolve_slack_proxy_url() -> Optional[str]:
    """Resolve a proxy URL that Slack SDK clients can safely use."""
    proxy_url = resolve_proxy_url()
    if not proxy_url:
        return None

    normalized = proxy_url.lower()
    if not normalized.startswith(("http://", "https://")):
        logger.info(
            "[Slack] Ignoring unsupported proxy scheme for Slack transport: %s",
            safe_url_for_log(proxy_url),
        )
        return None

    if any(is_host_excluded_by_no_proxy(host) for host in _SLACK_PROXY_HOSTS):
        logger.info("[Slack] NO_PROXY bypasses Slack proxy configuration")
        return None

    return proxy_url


class SlackAdapter(SlackInboundMixin, BasePlatformAdapter):
    """
    Slack bot adapter using Socket Mode.

    Requires two tokens:
      - SLACK_BOT_TOKEN (xoxb-...) for API calls
      - SLACK_APP_TOKEN (xapp-...) for Socket Mode connection

    Features:
      - DMs and channel messages (mention-gated in channels)
      - Thread support
      - File/image/audio attachments
      - Slash commands (/hermes)
      - Typing indicators (not natively supported by Slack bots)
    """

    MAX_MESSAGE_LENGTH = 39000  # Slack API allows 40,000 chars; leave margin

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.SLACK)
        self._app: Optional[Any] = None
        self._handler: Optional[Any] = None
        self._bot_user_id: Optional[str] = None
        self._user_name_cache: Dict[str, str] = {}  # user_id → display name
        self._socket_mode_task: Optional[asyncio.Task] = None
        # Multi-workspace support
        self._team_clients: Dict[str, Any] = {}   # team_id → WebClient
        self._team_bot_user_ids: Dict[str, str] = {}          # team_id → bot_user_id
        self._channel_team: Dict[str, str] = {}                # channel_id → team_id
        # Dedup cache: prevents duplicate bot responses when Socket Mode
        # reconnects redeliver events.
        self._dedup = MessageDeduplicator()
        # Track pending approval message_ts → resolved flag to prevent
        # double-clicks on approval buttons.
        self._approval_resolved: Dict[str, bool] = {}
        # Track timestamps of messages sent by the bot so we can respond
        # to thread replies even without an explicit @mention.
        self._bot_message_ts: set = set()
        self._BOT_TS_MAX = 5000  # cap to avoid unbounded growth
        # Track threads where the bot has been @mentioned — once mentioned,
        # respond to ALL subsequent messages in that thread automatically.
        self._mentioned_threads: set = set()
        self._MENTIONED_THREADS_MAX = 5000
        # Assistant thread metadata keyed by (channel_id, thread_ts). Slack's
        # AI Assistant lifecycle events can arrive before/alongside message
        # events, and they carry the user/thread identity needed for stable
        # session + memory scoping.
        self._assistant_threads: Dict[Tuple[str, str], Dict[str, str]] = {}
        self._ASSISTANT_THREADS_MAX = 5000
        # Cache for _fetch_thread_context results: cache_key → _ThreadContextCache
        self._thread_context_cache: Dict[str, _ThreadContextCache] = {}
        self._THREAD_CACHE_TTL = 60.0
        # Track message IDs that should get reaction lifecycle (DMs / @mentions).
        self._reacting_message_ids: set = set()
        # Track active assistant thread status indicators so stop_typing can
        # clear them (chat_id → thread_ts).
        self._active_status_threads: Dict[str, str] = {}
        # Slash-command contexts: stash response_url + user_id so send()
        # can route the first reply ephemerally.  Keyed by
        # (channel_id, user_id) to avoid cross-user collisions.
        # Each value: {"response_url": str, "ts": float}
        self._slash_command_contexts: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def _describe_slack_api_error(self, response: Any, *, file_obj: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Convert Slack API auth/permission failures into actionable user-facing text."""
        if response is None or not hasattr(response, "get"):
            return None

        error = str(response.get("error", "") or "").strip()
        if not error:
            return None

        file_label = str((file_obj or {}).get("name") or (file_obj or {}).get("id") or "this attachment")
        needed = str(response.get("needed", "") or "").strip()
        provided = str(response.get("provided", "") or "").strip()
        reinstall_hint = " Update the Slack app scopes/settings and reinstall the app to the workspace."
        provided_hint = f" Current bot scopes: {provided}." if provided else ""

        if error == "missing_scope":
            needed_hint = f"Missing scope: {needed}." if needed else "Missing required Slack scope."
            return f"Slack attachment access failed for {file_label}. {needed_hint}{provided_hint}{reinstall_hint}"
        if error in {"not_authed", "invalid_auth", "account_inactive", "token_revoked"}:
            return f"Slack attachment access failed for {file_label} because the bot token is not authorized ({error}). Refresh the token/reinstall the app."
        if error in {"file_not_found", "file_deleted"}:
            return f"Slack attachment {file_label} is no longer available ({error})."
        if error in {"access_denied", "file_access_denied", "no_permission", "not_allowed_token_type", "restricted_action"}:
            return f"Slack attachment access failed for {file_label} because the bot does not have permission ({error}). Check workspace permissions/scopes and reinstall if needed."
        return None

    def _describe_slack_download_failure(self, exc: Exception, *, file_obj: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Translate Slack download exceptions into user-facing attachment diagnostics."""
        file_label = str((file_obj or {}).get("name") or (file_obj or {}).get("id") or "this attachment")

        response = getattr(exc, "response", None)
        api_detail = self._describe_slack_api_error(response, file_obj=file_obj)
        if api_detail:
            return api_detail

        try:
            import httpx
        except Exception:  # pragma: no cover
            httpx = None

        if httpx is not None and isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status == 401:
                return f"Slack attachment access failed for {file_label} with HTTP 401. The bot token is not authorized for this file."
            if status == 403:
                return f"Slack attachment access failed for {file_label} with HTTP 403. The bot likely lacks permission or scope to read this file."
            if status == 404:
                return f"Slack attachment {file_label} returned HTTP 404 and is no longer reachable."

        message = str(exc)
        if "Slack returned HTML instead of media" in message or "non-image data" in message:
            return (
                f"Slack attachment access failed for {file_label}: Slack returned an HTML/login or non-media response. "
                "This usually means a scope, auth, or file-permission problem."
            )
        return None

    # ------------------------------------------------------------------
    # Slash-command ephemeral helpers
    # ------------------------------------------------------------------

    _SLASH_CTX_TTL = 120.0  # seconds — response_url is valid for 30 min;
    # we use a much shorter TTL to avoid routing unrelated messages
    # as ephemeral if the command handler was slow or dropped.

    def _pop_slash_context(
        self, chat_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return and remove the slash-command context for *chat_id*, if fresh.

        Contexts older than ``_SLASH_CTX_TTL`` seconds are silently discarded.

        Uses the ``_slash_user_id`` ContextVar (set in ``_handle_slash_command``)
        to match the exact ``(channel_id, user_id)`` key.  This prevents a
        concurrent slash command from a different user on the same channel from
        stealing another user's ephemeral context.  Falls back to a
        channel-only scan when the ContextVar is unset (e.g. send() called
        from a non-slash code path — should not match anything).
        """
        now = time.monotonic()
        # Clean up stale entries on every lookup — dict is small.
        stale_keys = [
            k for k, v in self._slash_command_contexts.items()
            if now - v["ts"] > self._SLASH_CTX_TTL
        ]
        for k in stale_keys:
            self._slash_command_contexts.pop(k, None)

        # Precise match: (channel_id, user_id) from ContextVar.
        uid = _slash_user_id.get()
        if uid:
            return self._slash_command_contexts.pop((chat_id, uid), None)

        # Fallback: channel-only scan (only reachable when ContextVar is
        # unset, i.e. send() called outside a slash-command async context).
        match_key = None
        for key in list(self._slash_command_contexts):
            if key[0] == chat_id:
                match_key = key
                break
        if match_key is None:
            return None
        return self._slash_command_contexts.pop(match_key)

    async def _send_slash_ephemeral(
        self,
        ctx: Dict[str, Any],
        content: str,
    ) -> "SendResult":
        """Replace the initial ephemeral ack via ``response_url``.

        Slack's ``response_url`` accepts a POST with ``replace_original``
        for up to 30 minutes after the slash command was invoked.  This
        lets us swap the "Running /cmd…" placeholder with the real reply,
        and the message stays ephemeral ("Only visible to you").

        Falls back to a simple ``True`` SendResult if the POST fails —
        the user already saw the initial ack, so a delivery failure here
        is non-critical.
        """
        formatted = self.format_message(content)
        # Slack's response_url has the same ~40k char limit as chat_postMessage.
        # Truncate to MAX_MESSAGE_LENGTH and use only the first chunk — the
        # response_url replaces a single ephemeral ack, so multi-chunk isn't
        # possible.  Long responses are rare for command replies.
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)
        text = chunks[0] if chunks else formatted
        payload = {
            "response_type": "ephemeral",
            "replace_original": True,
            "text": text,
        }
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.post(
                    ctx["response_url"],
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        return SendResult(success=True, message_id=None)
                    body = await resp.text()
                    logger.warning(
                        "[Slack] response_url POST returned %s: %s",
                        resp.status,
                        body[:200],
                    )
        except Exception as e:
            logger.warning(
                "[Slack] response_url POST failed: %s", e,
            )
        # Non-fatal — the user saw the initial ack already.
        return SendResult(success=True, message_id=None)

    async def connect(self) -> bool:
        """Connect to Slack via Socket Mode."""
        if not SLACK_AVAILABLE:
            logger.error(
                "[Slack] slack-bolt not installed. Run: pip install slack-bolt",
            )
            return False

        raw_token = self.config.token
        app_token = os.getenv("SLACK_APP_TOKEN")

        if not raw_token:
            logger.error("[Slack] SLACK_BOT_TOKEN not set")
            return False
        if not app_token:
            logger.error("[Slack] SLACK_APP_TOKEN not set")
            return False

        proxy_url = _resolve_slack_proxy_url()
        if proxy_url:
            logger.info("[Slack] Using proxy for Slack transport: %s", safe_url_for_log(proxy_url))

        # Support comma-separated bot tokens for multi-workspace
        bot_tokens = [t.strip() for t in raw_token.split(",") if t.strip()]

        # Also load tokens from OAuth token file
        from hermes_constants import get_hermes_home
        tokens_file = get_hermes_home() / "slack_tokens.json"
        if tokens_file.exists():
            try:
                saved = json.loads(tokens_file.read_text(encoding="utf-8"))
                for team_id, entry in saved.items():
                    tok = entry.get("token", "") if isinstance(entry, dict) else ""
                    if tok and tok not in bot_tokens:
                        bot_tokens.append(tok)
                        team_label = entry.get("team_name", team_id) if isinstance(entry, dict) else team_id
                        logger.info("[Slack] Loaded saved token for workspace %s", team_label)
            except Exception as e:
                logger.warning("[Slack] Failed to read %s: %s", tokens_file, e)

        lock_acquired = False
        try:
            if not self._acquire_platform_lock('slack-app-token', app_token, 'Slack app token'):
                return False
            lock_acquired = True

            # Close any previous handler before creating a new one so that
            # calling connect() a second time (e.g. during a gateway restart or
            # in-process reconnect attempt) does not leave a zombie Socket Mode
            # connection alive.  Both the old and new connections would otherwise
            # receive every Slack event and dispatch it twice, producing double
            # responses — the same bug that affected DiscordAdapter (#18187).
            if self._handler is not None:
                try:
                    await self._handler.close_async()
                except Exception:
                    logger.debug("[%s] Failed to close previous Slack handler", self.name)
                finally:
                    self._handler = None
                    self._app = None

            # First token is the primary — used for AsyncApp / Socket Mode
            primary_token = bot_tokens[0]
            self._app = AsyncApp(token=primary_token)
            _apply_slack_proxy(self._app.client, proxy_url)

            # Register each bot token and map team_id → client
            for token in bot_tokens:
                client = AsyncWebClient(token=token)
                _apply_slack_proxy(client, proxy_url)
                auth_response = await client.auth_test()
                team_id = auth_response.get("team_id", "")
                bot_user_id = auth_response.get("user_id", "")
                bot_name = auth_response.get("user", "unknown")
                team_name = auth_response.get("team", "unknown")

                self._team_clients[team_id] = client
                self._team_bot_user_ids[team_id] = bot_user_id

                # First token sets the primary bot_user_id (backward compat)
                if self._bot_user_id is None:
                    self._bot_user_id = bot_user_id

                logger.info(
                    "[Slack] Authenticated as @%s in workspace %s (team: %s)",
                    bot_name, team_name, team_id,
                )

            # Register message event handler
            @self._app.event("message")
            async def handle_message_event(event, say):
                await self._handle_slack_message(event)

            # Handle app_mention explicitly. In some Slack app configurations,
            # channel mentions arrive only as app_mention events rather than the
            # generic message event. Forward them into the normal message
            # pipeline so @mentions reliably produce replies.
            # NOTE: when Slack fires BOTH message and app_mention for the same
            # @mention, they share the same event ts — the dedup in
            # _handle_slack_message (MessageDeduplicator) suppresses the second.
            @self._app.event("app_mention")
            async def handle_app_mention(event, say):
                await self._handle_slack_message(event)

            # File lifecycle events can arrive around snippet uploads even when
            # the actual user message is what we care about. Ack them so Slack
            # doesn't log noisy 404 "unhandled request" warnings.
            @self._app.event("file_shared")
            async def handle_file_shared(event, say):
                pass

            @self._app.event("file_created")
            async def handle_file_created(event, say):
                pass

            @self._app.event("file_change")
            async def handle_file_change(event, say):
                pass

            @self._app.event("assistant_thread_started")
            async def handle_assistant_thread_started(event, say):
                await self._handle_assistant_thread_lifecycle_event(event)

            @self._app.event("assistant_thread_context_changed")
            async def handle_assistant_thread_context_changed(event, say):
                await self._handle_assistant_thread_lifecycle_event(event)

            # Register slash command handler(s)
            #
            # Every gateway command from COMMAND_REGISTRY is a native Slack
            # slash, matching Discord and Telegram's model (e.g. /btw, /stop,
            # /model work directly without /hermes prefix). A single regex
            # matcher dispatches all of them to one handler so we don't need
            # N identical @app.command() decorators.
            #
            # The slash commands must ALSO be declared in the Slack app
            # manifest (see `hermes slack manifest`). In Socket Mode, Slack
            # routes the command event through the socket regardless of the
            # manifest's request URL, but it will not deliver an event for
            # a slash command the manifest doesn't declare.
            from hermes_cli.commands import slack_native_slashes
            import re as _re

            _slash_names = [name for name, _d, _h in slack_native_slashes()]
            if _slash_names:
                _slash_pattern = _re.compile(
                    r"^/(?:" + "|".join(_re.escape(n) for n in _slash_names) + r")$"
                )
            else:  # pragma: no cover - registry always non-empty
                _slash_pattern = _re.compile(r"^/hermes$")

            @self._app.command(_slash_pattern)
            async def handle_hermes_command(ack, command):
                slash = (command.get("command") or "").lstrip("/")
                await ack(
                    response_type="ephemeral",
                    text=f"Running `/{slash}`…",
                )
                await self._handle_slash_command(command)

            # Register Block Kit action handlers for approval buttons
            for _action_id in (
                "hermes_approve_once",
                "hermes_approve_session",
                "hermes_approve_always",
                "hermes_deny",
            ):
                self._app.action(_action_id)(self._handle_approval_action)

            # Register Block Kit action handlers for slash-confirm buttons
            # (generic three-option prompts; see tools/slash_confirm.py).
            for _action_id in (
                "hermes_confirm_once",
                "hermes_confirm_always",
                "hermes_confirm_cancel",
            ):
                self._app.action(_action_id)(self._handle_slash_confirm_action)

            # Start Socket Mode handler in background
            self._handler = AsyncSocketModeHandler(self._app, app_token, proxy=proxy_url)
            _apply_slack_proxy(self._handler.client, proxy_url)
            self._socket_mode_task = asyncio.create_task(self._handler.start_async())

            self._running = True
            logger.info(
                "[Slack] Socket Mode connected (%d workspace(s))",
                len(self._team_clients),
            )
            return True

        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[Slack] Connection failed: %s", e, exc_info=True)
            return False
        finally:
            if lock_acquired and not self._running:
                self._release_platform_lock()

    async def create_handoff_thread(
        self,
        parent_chat_id: str,
        name: str,
    ) -> Optional[str]:
        """Create a Slack thread anchor for a session handoff.

        Slack threads are anchored to a parent message (``thread_ts``), not
        a channel-level construct. So we post a seed message into the home
        channel and return its ``ts`` — the watcher uses that as the
        ``thread_id`` for subsequent sends.

        Returns the seed message ts as a string, or ``None`` on failure.
        """
        if not self._app:
            return None
        try:
            client = self._get_client(parent_chat_id)
            if client is None:
                return None
            seed_text = f":thread: Hermes handoff — *{(name or 'session').strip()[:80]}*"
            result = await client.chat_postMessage(
                channel=parent_chat_id,
                text=seed_text,
            )
            ts = result.get("ts") if isinstance(result, dict) else getattr(result, "get", lambda _k, _d=None: None)("ts")
            if ts:
                return str(ts)
        except Exception as exc:
            logger.warning(
                "[%s] Handoff thread: seed-post failed for channel %s: %s",
                self.name, parent_chat_id, exc,
            )
        return None

    async def disconnect(self) -> None:
        """Disconnect from Slack."""
        if self._handler:
            try:
                await self._handler.close_async()
            except Exception as e:  # pragma: no cover - defensive logging
                logger.warning("[Slack] Error while closing Socket Mode handler: %s", e, exc_info=True)
        self._running = False

        self._release_platform_lock()

        logger.info("[Slack] Disconnected")

    def _get_client(self, chat_id: str) -> Any:
        """Return the workspace-specific WebClient for a channel."""
        team_id = self._channel_team.get(chat_id)
        if team_id and team_id in self._team_clients:
            return self._team_clients[team_id]
        return self._app.client  # fallback to primary

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message to a Slack channel or DM."""
        if not self._app:
            return SendResult(success=False, error="Not connected")

        try:
            # Check for a pending slash-command context.  When the user ran a
            # native slash command (e.g. /q, /stop, /model), the initial ack
            # already showed an ephemeral "Running /cmd…" message.  If we have
            # a stashed response_url for this channel, replace that ack with
            # the actual command reply ephemerally instead of posting publicly.
            slash_ctx = self._pop_slash_context(chat_id)
            if slash_ctx:
                return await self._send_slash_ephemeral(
                    slash_ctx, content,
                )

            # Convert standard markdown → Slack mrkdwn
            formatted = self.format_message(content)

            # Split long messages, preserving code block boundaries
            chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)

            thread_ts = self._resolve_thread_ts(reply_to, metadata)
            last_result = None

            # reply_broadcast: also post thread replies to the main channel.
            # Controlled via platform config: gateway.slack.reply_broadcast
            broadcast = self.config.extra.get("reply_broadcast", False)

            for i, chunk in enumerate(chunks):
                kwargs = {
                    "channel": chat_id,
                    "text": chunk,
                    "mrkdwn": True,
                }
                if thread_ts:
                    kwargs["thread_ts"] = thread_ts
                    # Only broadcast the first chunk of the first reply
                    if broadcast and i == 0:
                        kwargs["reply_broadcast"] = True

                last_result = await self._get_client(chat_id).chat_postMessage(**kwargs)

            # Clear Slack Assistant status as soon as the final message is posted.
            if thread_ts:
                await self.stop_typing(chat_id)

            # Track the sent message ts so we can auto-respond to thread
            # replies without requiring @mention.
            sent_ts = last_result.get("ts") if last_result else None
            if sent_ts:
                self._bot_message_ts.add(sent_ts)
                # Also register the thread root so replies-to-my-replies work
                if thread_ts:
                    self._bot_message_ts.add(thread_ts)
                if len(self._bot_message_ts) > self._BOT_TS_MAX:
                    excess = len(self._bot_message_ts) - self._BOT_TS_MAX // 2
                    for old_ts in list(self._bot_message_ts)[:excess]:
                        self._bot_message_ts.discard(old_ts)

            return SendResult(
                success=True,
                message_id=sent_ts,
                raw_response=last_result,
            )

        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[Slack] Send error: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def send_private_notice(
        self,
        chat_id: str,
        user_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a Slack ephemeral message visible only to one user."""
        if not self._app:
            return SendResult(success=False, error="Not connected")
        if not chat_id or not user_id:
            return SendResult(success=False, error="chat_id and user_id are required")

        try:
            formatted = self.format_message(content)
            thread_ts = self._resolve_thread_ts(reply_to, metadata)
            kwargs = {
                "channel": chat_id,
                "user": user_id,
                "text": formatted,
                "mrkdwn": True,
            }
            if thread_ts:
                kwargs["thread_ts"] = thread_ts

            result = await self._get_client(chat_id).chat_postEphemeral(**kwargs)
            return SendResult(
                success=True,
                message_id=result.get("message_ts") or result.get("ts"),
                raw_response=result,
            )
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[Slack] Ephemeral send error: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit a previously sent Slack message."""
        if not self._app:
            return SendResult(success=False, error="Not connected")
        try:
            formatted = self.format_message(content)
            await self._get_client(chat_id).chat_update(
                channel=chat_id,
                ts=message_id,
                text=formatted,
            )
            if finalize:
                await self.stop_typing(chat_id)
            return SendResult(success=True, message_id=message_id)
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[Slack] Failed to edit message %s in channel %s: %s",
                message_id,
                chat_id,
                e,
                exc_info=True,
            )
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Show a typing/status indicator using assistant.threads.setStatus.

        Displays "is thinking..." next to the bot name in a thread.
        Requires the assistant:write or chat:write scope.
        Auto-clears when the bot sends a reply to the thread.
        """
        if not self._app:
            return

        thread_ts = None
        if metadata:
            thread_ts = metadata.get("thread_id") or metadata.get("thread_ts")

        if not thread_ts:
            return  # Can only set status in a thread context

        self._active_status_threads[chat_id] = thread_ts
        try:
            await self._get_client(chat_id).assistant_threads_setStatus(
                channel_id=chat_id,
                thread_ts=thread_ts,
                status="is thinking...",
            )
        except Exception as e:
            # Silently ignore — may lack assistant:write scope or not be
            # in an assistant-enabled context. Falls back to reactions.
            logger.debug("[Slack] assistant.threads.setStatus failed: %s", e)

    async def stop_typing(self, chat_id: str, metadata=None) -> None:
        """Clear the assistant thread status indicator."""
        if not self._app:
            return
        thread_ts = self._active_status_threads.pop(chat_id, None)
        if not thread_ts:
            return
        try:
            await self._get_client(chat_id).assistant_threads_setStatus(
                channel_id=chat_id,
                thread_ts=thread_ts,
                status="",
            )
        except Exception as e:
            logger.debug("[Slack] assistant.threads.setStatus clear failed: %s", e)

    def _dm_top_level_threads_as_sessions(self) -> bool:
        """Whether top-level Slack DMs get per-message session threads.

        Defaults to ``True`` so each visible DM reply thread is isolated as its
        own Hermes session — matching the per-thread behavior channels already
        have.  Set ``platforms.slack.extra.dm_top_level_threads_as_sessions``
        to ``false`` in config.yaml to revert to the legacy behavior where all
        top-level DMs share one continuous session.
        """
        raw = self.config.extra.get("dm_top_level_threads_as_sessions")
        if raw is None:
            return True  # default: each DM thread is its own session
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _resolve_thread_ts(
        self,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Resolve the correct thread_ts for a Slack API call.

        Prefers metadata thread_id (the thread parent's ts, set by the
        gateway) over reply_to (which may be a child message's ts).

        When ``reply_in_thread`` is ``false`` in the platform extra config,
        top-level channel messages receive direct channel replies instead of
        thread replies.  Messages that originate inside an existing thread are
        always replied to in-thread to preserve conversation context.
        """
        # When reply_in_thread is disabled (default: True for backward compat),
        # only thread messages that are already part of an existing thread.
        # For top-level channel messages, the inbound handler sets
        # metadata.thread_id to the message's own ts as a session-keying
        # fallback (see the `thread_ts = event.get("thread_ts") or ts` branch),
        # so metadata alone can't distinguish a real thread reply from a
        # top-level message. reply_to is the incoming message's own id, so
        # when thread_id == reply_to the "thread" is synthetic and we reply
        # directly in the channel instead.
        if not self.config.extra.get("reply_in_thread", True):
            md = metadata or {}
            existing_thread = md.get("thread_id") or md.get("thread_ts")
            if existing_thread and reply_to and existing_thread == reply_to:
                existing_thread = None
            return existing_thread or None

        if metadata:
            if metadata.get("thread_id"):
                return metadata["thread_id"]
            if metadata.get("thread_ts"):
                return metadata["thread_ts"]
        return reply_to

    async def _upload_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local file to Slack."""
        if not self._app:
            return SendResult(success=False, error="Not connected")

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        thread_ts = self._resolve_thread_ts(reply_to, metadata)
        last_exc = None
        for attempt in range(3):
            try:
                result = await self._get_client(chat_id).files_upload_v2(
                    channel=chat_id,
                    file=file_path,
                    filename=os.path.basename(file_path),
                    initial_comment=caption or "",
                    thread_ts=thread_ts,
                )
                self._record_uploaded_file_thread(chat_id, thread_ts)
                return SendResult(success=True, raw_response=result)
            except Exception as exc:
                last_exc = exc
                if not self._is_retryable_upload_error(exc) or attempt >= 2:
                    raise
                logger.debug(
                    "[Slack] Upload retry %d/2 for %s: %s",
                    attempt + 1,
                    file_path,
                    exc,
                )
                await asyncio.sleep(1.5 * (attempt + 1))

        raise last_exc

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Send a batch of images as a single Slack message with multiple file uploads.

        Uses ``files_upload_v2`` with its ``file_uploads`` parameter so all
        images show up attached to one ``initial_comment`` message instead
        of N separate messages. Falls back to the base per-image loop on
        any failure.

        The batch limit is 10 file uploads per call (Slack server-side cap).
        """
        if not self._app:
            return
        if not images:
            return

        try:
            import httpx as _httpx
            from urllib.parse import unquote as _unquote
            from tools.url_safety import is_safe_url as _is_safe_url
        except Exception:
            await super().send_multiple_images(chat_id, images, metadata, human_delay)
            return

        thread_ts = self._resolve_thread_ts(None, metadata)

        CHUNK = 10
        chunks = [images[i:i + CHUNK] for i in range(0, len(images), CHUNK)]

        for chunk_idx, chunk in enumerate(chunks):
            if human_delay > 0 and chunk_idx > 0:
                await asyncio.sleep(human_delay)

            file_uploads: List[Dict[str, Any]] = []
            initial_comment_parts: List[str] = []
            try:
                async with _httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
                    for image_url, alt_text in chunk:
                        if alt_text:
                            initial_comment_parts.append(alt_text)

                        if image_url.startswith("file://"):
                            local_path = _unquote(image_url[7:])
                            if not os.path.exists(local_path):
                                logger.warning("[Slack] Skipping missing image: %s", local_path)
                                continue
                            file_uploads.append({
                                "file": local_path,
                                "filename": os.path.basename(local_path),
                            })
                        else:
                            if not _is_safe_url(image_url):
                                logger.warning("[Slack] Blocked unsafe image URL in batch")
                                continue
                            try:
                                response = await http_client.get(image_url)
                                response.raise_for_status()
                                ext = "png"
                                ct = response.headers.get("content-type", "")
                                if "jpeg" in ct or "jpg" in ct:
                                    ext = "jpg"
                                elif "gif" in ct:
                                    ext = "gif"
                                elif "webp" in ct:
                                    ext = "webp"
                                file_uploads.append({
                                    "content": response.content,
                                    "filename": f"image_{len(file_uploads)}.{ext}",
                                })
                            except Exception as dl_err:
                                logger.warning(
                                    "[Slack] Download failed for %s: %s",
                                    safe_url_for_log(image_url), dl_err,
                                )
                                continue

                if not file_uploads:
                    continue

                initial_comment = "\n".join(initial_comment_parts) if initial_comment_parts else ""
                logger.info(
                    "[Slack] Sending %d image(s) in single files_upload_v2 (chunk %d/%d)",
                    len(file_uploads), chunk_idx + 1, len(chunks),
                )
                result = await self._get_client(chat_id).files_upload_v2(
                    channel=chat_id,
                    file_uploads=file_uploads,
                    initial_comment=initial_comment,
                    thread_ts=thread_ts,
                )
                self._record_uploaded_file_thread(chat_id, thread_ts)
                _ = result
            except Exception as e:
                logger.warning(
                    "[Slack] Multi-image files_upload_v2 failed (chunk %d/%d), falling back to per-image: %s",
                    chunk_idx + 1, len(chunks), e,
                    exc_info=True,
                )
                await super().send_multiple_images(chat_id, chunk, metadata, human_delay=human_delay)

    def _record_uploaded_file_thread(self, chat_id: str, thread_ts: Optional[str]) -> None:
        """Treat successful file uploads as bot participation in a thread."""
        if not thread_ts:
            return
        self._bot_message_ts.add(thread_ts)
        if len(self._bot_message_ts) > self._BOT_TS_MAX:
            excess = len(self._bot_message_ts) - self._BOT_TS_MAX // 2
            for old_ts in list(self._bot_message_ts)[:excess]:
                self._bot_message_ts.discard(old_ts)

    def _is_retryable_upload_error(self, exc: Exception) -> bool:
        """Best-effort detection for transient Slack upload failures."""
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code is not None:
            return status_code == 429 or status_code >= 500

        body = " ".join(
            str(part) for part in (
                exc,
                getattr(exc, "message", ""),
                getattr(exc, "response", None),
            ) if part
        ).lower()
        if "rate_limited" in body or "ratelimited" in body or "429" in body:
            return True
        if "connection reset" in body or "service unavailable" in body or "temporarily unavailable" in body:
            return True
        return self._is_retryable_error(body)

    # ----- Markdown → mrkdwn conversion -----

    def format_message(self, content: str) -> str:
        """Convert standard markdown to Slack mrkdwn format.

        Protected regions (code blocks, inline code) are extracted first so
        their contents are never modified.  Standard markdown constructs
        (headers, bold, italic, links) are translated to mrkdwn syntax.
        """
        if not content:
            return content

        placeholders: dict = {}
        counter = [0]

        def _ph(value: str) -> str:
            """Stash value behind a placeholder that survives later passes."""
            key = f"\x00SL{counter[0]}\x00"
            counter[0] += 1
            placeholders[key] = value
            return key

        text = content

        # 1) Protect fenced code blocks (``` ... ```)
        text = re.sub(
            r'(```(?:[^\n]*\n)?[\s\S]*?```)',
            lambda m: _ph(m.group(0)),
            text,
        )

        # 2) Protect inline code (`...`)
        text = re.sub(r'(`[^`]+`)', lambda m: _ph(m.group(0)), text)

        # 3) Convert markdown links [text](url) → <url|text>
        def _convert_markdown_link(m):
            label = m.group(1)
            url = m.group(2).strip()
            if url.startswith('<') and url.endswith('>'):
                url = url[1:-1].strip()
            return _ph(f'<{url}|{label}>')

        text = re.sub(
            r'(?<!!)\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)',
            _convert_markdown_link,
            text,
        )

        # 4) Protect existing Slack entities/manual links so escaping and later
        #    formatting passes don't break them.
        text = re.sub(
            r'(<(?:[@#!]|(?:https?|mailto|tel):)[^>\n]+>)',
            lambda m: _ph(m.group(1)),
            text,
        )

        # 5) Protect blockquote markers before escaping
        text = re.sub(r'^(>+\s)', lambda m: _ph(m.group(0)), text, flags=re.MULTILINE)

        # 6) Escape Slack control characters in remaining plain text.
        # Unescape first so already-escaped input doesn't get double-escaped.
        text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        # 7) Convert headers (## Title) → *Title* (bold)
        def _convert_header(m):
            inner = m.group(1).strip()
            # Strip redundant bold markers inside a header
            inner = re.sub(r'\*\*(.+?)\*\*', r'\1', inner)
            return _ph(f'*{inner}*')

        text = re.sub(
            r'^#{1,6}\s+(.+)$', _convert_header, text, flags=re.MULTILINE
        )

        # 8) Convert bold+italic: ***text*** → *_text_* (Slack bold wrapping italic)
        text = re.sub(
            r'\*\*\*(.+?)\*\*\*',
            lambda m: _ph(f'*_{m.group(1)}_*'),
            text,
        )

        # 9) Convert bold: **text** → *text* (Slack bold)
        text = re.sub(
            r'\*\*(.+?)\*\*',
            lambda m: _ph(f'*{m.group(1)}*'),
            text,
        )

        # 10) Convert italic: _text_ stays as _text_ (already Slack italic)
        #     Single *text* → _text_ (Slack italic), but only when the
        #     emphasized text touches non-whitespace on both sides so literal
        #     delimiters like "a * b * c" are preserved.
        text = re.sub(
            r'(?<!\*)\*(\S(?:[^*\n]*?\S)?)\*(?!\*)',
            lambda m: _ph(f'_{m.group(1)}_'),
            text,
        )

        # 11) Convert strikethrough: ~~text~~ → ~text~
        text = re.sub(
            r'~~(.+?)~~',
            lambda m: _ph(f'~{m.group(1)}~'),
            text,
        )

        # 12) Blockquotes: > prefix is already protected by step 5 above.

        # 13) Restore placeholders in reverse order
        for key in reversed(placeholders):
            text = text.replace(key, placeholders[key])

        return text

    # ----- Reactions -----

    async def _add_reaction(
        self, channel: str, timestamp: str, emoji: str
    ) -> bool:
        """Add an emoji reaction to a message. Returns True on success."""
        if not self._app:
            return False
        try:
            await self._get_client(channel).reactions_add(
                channel=channel, timestamp=timestamp, name=emoji
            )
            return True
        except Exception as e:
            # Don't log as error — may fail if already reacted or missing scope
            logger.debug("[Slack] reactions.add failed (%s): %s", emoji, e)
            return False

    async def _remove_reaction(
        self, channel: str, timestamp: str, emoji: str
    ) -> bool:
        """Remove an emoji reaction from a message. Returns True on success."""
        if not self._app:
            return False
        try:
            await self._get_client(channel).reactions_remove(
                channel=channel, timestamp=timestamp, name=emoji
            )
            return True
        except Exception as e:
            logger.debug("[Slack] reactions.remove failed (%s): %s", emoji, e)
            return False

    def _reactions_enabled(self) -> bool:
        """Check if message reactions are enabled via config/env."""
        return os.getenv("SLACK_REACTIONS", "true").lower() not in {"false", "0", "no"}

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add an in-progress reaction when message processing begins."""
        if not self._reactions_enabled():
            return
        ts = getattr(event, "message_id", None)
        if not ts or ts not in self._reacting_message_ids:
            return
        channel_id = getattr(event.source, "chat_id", None)
        if channel_id:
            await self._add_reaction(channel_id, ts, "eyes")

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Swap the in-progress reaction for a final success/failure reaction."""
        if not self._reactions_enabled():
            return
        ts = getattr(event, "message_id", None)
        if not ts or ts not in self._reacting_message_ids:
            return
        self._reacting_message_ids.discard(ts)
        channel_id = getattr(event.source, "chat_id", None)
        if not channel_id:
            return
        await self._remove_reaction(channel_id, ts, "eyes")
        if outcome == ProcessingOutcome.SUCCESS:
            await self._add_reaction(channel_id, ts, "white_check_mark")
        elif outcome == ProcessingOutcome.FAILURE:
            await self._add_reaction(channel_id, ts, "x")

    # ----- User identity resolution -----

    async def _resolve_user_name(self, user_id: str, chat_id: str = "") -> str:
        """Resolve a Slack user ID to a display name, with caching."""
        if not user_id:
            return ""
        if user_id in self._user_name_cache:
            return self._user_name_cache[user_id]

        if not self._app:
            return user_id

        try:
            client = self._get_client(chat_id) if chat_id else self._app.client
            result = await client.users_info(user=user_id)
            user = result.get("user", {})
            # Prefer display_name → real_name → user_id
            profile = user.get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
            self._user_name_cache[user_id] = name
            return name
        except Exception as e:
            logger.debug("[Slack] users.info failed for %s: %s", user_id, e)
            self._user_name_cache[user_id] = user_id
            return user_id

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local image file to Slack by uploading it."""
        try:
            return await self._upload_file(chat_id, image_path, caption, reply_to, metadata)
        except FileNotFoundError:
            return SendResult(success=False, error=f"Image file not found: {image_path}")
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[%s] Failed to send local Slack image %s: %s",
                self.name,
                image_path,
                e,
                exc_info=True,
            )
            text = f"🖼️ Image: {image_path}"
            if caption:
                text = f"{caption}\n{text}"
            return await self.send(chat_id, text, reply_to=reply_to, metadata=metadata)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image to Slack by uploading the URL as a file."""
        if not self._app:
            return SendResult(success=False, error="Not connected")

        from tools.url_safety import is_safe_url
        if not is_safe_url(image_url):
            logger.warning("[Slack] Blocked unsafe image URL (SSRF protection)")
            return await super().send_image(chat_id, image_url, caption, reply_to, metadata=metadata)

        try:
            import httpx

            async def _ssrf_redirect_guard(response):
                """Re-check redirect targets so public URLs cannot bounce into private IPs."""
                if response.is_redirect and response.next_request:
                    redirect_url = str(response.next_request.url)
                    if not is_safe_url(redirect_url):
                        raise ValueError("Blocked redirect to private/internal address")

            # Download the image first
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                event_hooks={"response": [_ssrf_redirect_guard]},
            ) as client:
                response = await client.get(image_url)
                response.raise_for_status()

            thread_ts = self._resolve_thread_ts(reply_to, metadata)
            result = await self._get_client(chat_id).files_upload_v2(
                channel=chat_id,
                content=response.content,
                filename="image.png",
                initial_comment=caption or "",
                thread_ts=thread_ts,
            )
            self._record_uploaded_file_thread(chat_id, thread_ts)

            return SendResult(success=True, raw_response=result)

        except Exception as e:  # pragma: no cover - defensive logging
            logger.warning(
                "[Slack] Failed to upload image from URL %s, falling back to text: %s",
                safe_url_for_log(image_url),
                e,
                exc_info=True,
            )
            # Fall back to sending the URL as text
            text = f"{caption}\n{image_url}" if caption else image_url
            return await self.send(
                chat_id=chat_id,
                content=text,
                reply_to=reply_to,
                metadata=metadata,
            )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send an audio file to Slack."""
        try:
            return await self._upload_file(chat_id, audio_path, caption, reply_to, metadata)
        except FileNotFoundError:
            return SendResult(success=False, error=f"Audio file not found: {audio_path}")
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[Slack] Failed to send audio file %s: %s",
                audio_path,
                e,
                exc_info=True,
            )
            return SendResult(success=False, error=str(e))

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a video file to Slack."""
        if not self._app:
            return SendResult(success=False, error="Not connected")

        if not os.path.exists(video_path):
            return SendResult(success=False, error=f"Video file not found: {video_path}")

        try:
            thread_ts = self._resolve_thread_ts(reply_to, metadata)
            last_exc = None
            for attempt in range(3):
                try:
                    result = await self._get_client(chat_id).files_upload_v2(
                        channel=chat_id,
                        file=video_path,
                        filename=os.path.basename(video_path),
                        initial_comment=caption or "",
                        thread_ts=thread_ts,
                    )
                    self._record_uploaded_file_thread(chat_id, thread_ts)
                    return SendResult(success=True, raw_response=result)
                except Exception as exc:
                    last_exc = exc
                    if not self._is_retryable_upload_error(exc) or attempt >= 2:
                        raise
                    logger.debug(
                        "[Slack] Video upload retry %d/2 for %s: %s",
                        attempt + 1,
                        video_path,
                        exc,
                    )
                    await asyncio.sleep(1.5 * (attempt + 1))

            raise last_exc

        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[%s] Failed to send video %s: %s",
                self.name,
                video_path,
                e,
                exc_info=True,
            )
            text = f"🎬 Video: {video_path}"
            if caption:
                text = f"{caption}\n{text}"
            return await self.send(chat_id, text, reply_to=reply_to, metadata=metadata)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a document/file attachment to Slack."""
        if not self._app:
            return SendResult(success=False, error="Not connected")

        if not os.path.exists(file_path):
            return SendResult(success=False, error=f"File not found: {file_path}")

        display_name = file_name or os.path.basename(file_path)
        thread_ts = self._resolve_thread_ts(reply_to, metadata)

        try:
            last_exc = None
            for attempt in range(3):
                try:
                    result = await self._get_client(chat_id).files_upload_v2(
                        channel=chat_id,
                        file=file_path,
                        filename=display_name,
                        initial_comment=caption or "",
                        thread_ts=thread_ts,
                    )
                    self._record_uploaded_file_thread(chat_id, thread_ts)
                    return SendResult(success=True, raw_response=result)
                except Exception as exc:
                    last_exc = exc
                    if not self._is_retryable_upload_error(exc) or attempt >= 2:
                        raise
                    logger.debug(
                        "[Slack] Document upload retry %d/2 for %s: %s",
                        attempt + 1,
                        file_path,
                        exc,
                    )
                    await asyncio.sleep(1.5 * (attempt + 1))

            raise last_exc

        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[%s] Failed to send document %s: %s",
                self.name,
                file_path,
                e,
                exc_info=True,
            )
            text = f"📎 File: {file_path}"
            if caption:
                text = f"{caption}\n{text}"
            return await self.send(chat_id, text, reply_to=reply_to, metadata=metadata)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a Slack channel."""
        if not self._app:
            return {"name": chat_id, "type": "unknown"}

        try:
            result = await self._get_client(chat_id).conversations_info(channel=chat_id)
            channel = result.get("channel", {})
            is_dm = channel.get("is_im", False)
            return {
                "name": channel.get("name", chat_id),
                "type": "dm" if is_dm else "group",
            }
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[Slack] Failed to fetch chat info for %s: %s",
                chat_id,
                e,
                exc_info=True,
            )
            return {"name": chat_id, "type": "unknown"}

    # ----- Internal handlers -----
