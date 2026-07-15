"""Yuanbao sign-token acquisition and caching."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from channels.platforms.yuanbao_proto import HERMES_INSTANCE_ID

try:
    from hermes_cli import __version__ as _HERMES_VERSION
except ImportError:
    _HERMES_VERSION = "0.0.0"

_APP_VERSION = _HERMES_VERSION
_BOT_VERSION = _HERMES_VERSION
_YUANBAO_INSTANCE_ID = str(HERMES_INSTANCE_ID)
_OPERATION_SYSTEM = sys.platform

logger = logging.getLogger(__name__)

class SignManager:
    """Encapsulates all sign-token related logic for the Yuanbao platform.

    Manages token acquisition, caching, signature computation, and
    automatic retry.  All state (cache, locks) is kept as class-level
    attributes so that a single shared client serves the whole process.
    """

    # -- Constants ---------------------------------------------------------

    TOKEN_PATH = "/api/v5/robotLogic/sign-token"

    RETRYABLE_CODE = 10099
    MAX_RETRIES = 3
    RETRY_DELAY_S = 1.0

    #: Early refresh margin (seconds), treat as expiring 60s before actual expiry
    CACHE_REFRESH_MARGIN_S = 60

    #: HTTP timeout (seconds)
    HTTP_TIMEOUT_S = 10.0

    # -- Class-level shared state ------------------------------------------

    # key: app_key → {"token", "bot_id", "expire_ts", ...}
    _cache: dict[str, dict[str, Any]] = {}

    # Per-app_key refresh locks — prevents concurrent duplicate sign-token
    # requests.  Created lazily inside get_refresh_lock() which is only called
    # from async context, so the Lock is always bound to the correct loop.
    # disconnect() clears this dict to prevent stale locks across reconnects.
    _locks: dict[str, asyncio.Lock] = {}

    # -- Internal helpers --------------------------------------------------

    @classmethod
    def get_refresh_lock(cls, app_key: str) -> asyncio.Lock:
        """Return (creating if needed) the per-app_key refresh lock.

        Must only be called from within a running event loop (async context).
        """
        if app_key not in cls._locks:
            cls._locks[app_key] = asyncio.Lock()
        return cls._locks[app_key]

    @staticmethod
    def compute_signature(nonce: str, timestamp: str, app_key: str, app_secret: str) -> str:
        """Compute HMAC-SHA256 signature (aligned with TypeScript original).

        plain     = nonce + timestamp + app_key + app_secret
        signature = HMAC-SHA256(key=app_secret, msg=plain).hexdigest()
        """
        plain = nonce + timestamp + app_key + app_secret
        return hmac.new(app_secret.encode(), plain.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def build_timestamp() -> str:
        """Build Beijing-time ISO-8601 timestamp (no milliseconds).

        Format: 2006-01-02T15:04:05+08:00
        """
        bjtime = datetime.now(tz=timezone(timedelta(hours=8)))
        return bjtime.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    @classmethod
    def is_cache_valid(cls, entry: dict[str, Any]) -> bool:
        """Determine whether the cache entry is valid (not expired with margin)."""
        return entry["expire_ts"] - time.time() > cls.CACHE_REFRESH_MARGIN_S

    @classmethod
    def clear_locks(cls) -> None:
        """Clear all per-app_key refresh locks (called on disconnect)."""
        cls._locks.clear()

    @classmethod
    def purge_expired(cls) -> int:
        """Remove all expired entries from the token cache.

        Returns the number of entries purged.  Called lazily from
        ``get_token()`` so that stale app_key entries don't accumulate
        indefinitely in long-running processes.
        """
        now = time.time()
        expired_keys = [
            k for k, v in cls._cache.items()
            if now - v.get("expire_ts", 0) > 0
        ]
        for k in expired_keys:
            cls._cache.pop(k, None)
        return len(expired_keys)

    # -- Core: fetch -------------------------------------------------------

    @classmethod
    async def fetch(
        cls,
        app_key: str,
        app_secret: str,
        api_domain: str,
        route_env: str = "",
    ) -> dict[str, Any]:
        """Send sign-ticket HTTP request with auto-retry (up to MAX_RETRIES times)."""
        url = f"{api_domain.rstrip('/')}{cls.TOKEN_PATH}"
        async with httpx.AsyncClient(timeout=cls.HTTP_TIMEOUT_S) as client:
            for attempt in range(cls.MAX_RETRIES + 1):
                nonce = secrets.token_hex(16)
                timestamp = cls.build_timestamp()
                signature = cls.compute_signature(nonce, timestamp, app_key, app_secret)

                payload = {
                    "app_key": app_key,
                    "nonce": nonce,
                    "signature": signature,
                    "timestamp": timestamp,
                }

                headers = {
                    "Content-Type": "application/json",
                    "X-AppVersion": _APP_VERSION,
                    "X-OperationSystem": _OPERATION_SYSTEM,
                    "X-Instance-Id": _YUANBAO_INSTANCE_ID,
                    "X-Bot-Version": _BOT_VERSION,
                }
                if route_env:
                    headers["X-Route-Env"] = route_env

                logger.info(
                    "Sign token request: url=%s%s",
                    url,
                    f" (retry {attempt}/{cls.MAX_RETRIES})" if attempt > 0 else "",
                )

                response = await client.post(url, json=payload, headers=headers)

                if response.status_code != 200:
                    body = response.text
                    raise RuntimeError(f"Sign token API returned {response.status_code}: {body[:200]}")

                try:
                    result_data: dict[str, Any] = response.json()
                except Exception as exc:
                    raise ValueError(f"Sign token response parse error: {exc}") from exc

                code = result_data.get("code")
                if code == 0:
                    data = result_data.get("data")
                    if not isinstance(data, dict):
                        raise ValueError(f"Sign token response missing 'data' field: {result_data}")
                    logger.info("Sign token success: bot_id=%s", data.get("bot_id"))
                    return data

                if code == cls.RETRYABLE_CODE and attempt < cls.MAX_RETRIES:
                    logger.warning(
                        "Sign token retryable: code=%s, retrying in %ss (attempt=%d/%d)",
                        code,
                        cls.RETRY_DELAY_S,
                        attempt + 1,
                        cls.MAX_RETRIES,
                    )
                    await asyncio.sleep(cls.RETRY_DELAY_S)
                    continue

                msg = result_data.get("msg", "")
                raise RuntimeError(f"Sign token error: code={code}, msg={msg}")

        raise RuntimeError("Sign token failed: max retries exceeded")

    # -- Public API: get (with cache) --------------------------------------

    @classmethod
    async def get_token(
        cls,
        app_key: str,
        app_secret: str,
        api_domain: str,
        route_env: str = "",
    ) -> dict[str, Any]:
        """Get WS auth token (with cache).

        Return directly on cache hit without re-requesting; treat as expiring
        60 seconds before actual expiry, triggering refresh.
        """
        # Lazily evict stale entries from other app_keys
        cls.purge_expired()

        cached = cls._cache.get(app_key)
        if cached and cls.is_cache_valid(cached):
            remain = int(cached["expire_ts"] - time.time())
            logger.info("Using cached token (%ds remaining)", remain)
            return dict(cached)

        async with cls.get_refresh_lock(app_key):
            cached = cls._cache.get(app_key)
            if cached and cls.is_cache_valid(cached):
                return dict(cached)

            data = await cls.fetch(app_key, app_secret, api_domain, route_env)

            duration: int = data.get("duration", 0)
            expire_ts = time.time() + duration if duration > 0 else time.time() + 3600

            cls._cache[app_key] = {
                "token": data.get("token", ""),
                "bot_id": data.get("bot_id", ""),
                "duration": duration,
                "product": data.get("product", ""),
                "source": data.get("source", ""),
                "expire_ts": expire_ts,
            }

        return dict(cls._cache[app_key])

    # -- Public API: force refresh -----------------------------------------

    @classmethod
    async def force_refresh(
        cls,
        app_key: str,
        app_secret: str,
        api_domain: str,
        route_env: str = "",
    ) -> dict[str, Any]:
        """Force refresh token (clear cache and re-sign)."""
        logger.warning("[force-refresh] Clearing cache and re-signing token: app_key=****%s", app_key[-4:])
        async with cls.get_refresh_lock(app_key):
            cls._cache.pop(app_key, None)
            data = await cls.fetch(app_key, app_secret, api_domain, route_env)

            duration: int = data.get("duration", 0)
            expire_ts = time.time() + duration if duration > 0 else time.time() + 3600

            cls._cache[app_key] = {
                "token": data.get("token", ""),
                "bot_id": data.get("bot_id", ""),
                "duration": duration,
                "product": data.get("product", ""),
                "source": data.get("source", ""),
                "expire_ts": expire_ts,
            }

        return dict(cls._cache[app_key])


