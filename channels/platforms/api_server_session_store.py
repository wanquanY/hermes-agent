"""Profile-aware, non-blocking session-store lifecycle for the API server."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from hermes_agent.composition.cli_session_store import open_cli_session_store

logger = logging.getLogger(__name__)


class APIServerSessionStoreMixin:
    """Own lazy session-store construction, caching, and shutdown."""

    def _initialize_session_store_runtime(self) -> None:
        self._session_db: Optional[Any] = None
        self._session_dbs: dict[str, Any] = {}
        self._session_db_lock: Optional[asyncio.Lock] = None
        self._session_db_cache_lock = threading.RLock()

    def _open_and_cache_session_db(self, home: Path) -> Optional[Any]:
        """Open one store per profile home and return its cached instance."""
        key = str(Path(home).resolve())
        with self._session_db_cache_lock:
            cached = self._session_dbs.get(key)
            if cached is not None:
                return cached
            store = open_cli_session_store(Path(home) / "state.db")
            self._session_dbs[key] = store
            return store

    def _ensure_session_db(self):
        """Synchronous store access for agent construction in worker threads."""
        if self._session_db is not None:
            return self._session_db
        try:
            from hermes_constants import get_hermes_home

            return self._open_and_cache_session_db(get_hermes_home())
        except Exception as exc:
            logger.debug("Session store unavailable for API server: %s", exc)
            return None

    async def _ensure_session_db_async(self):
        """Open the active profile's store without blocking aiohttp's loop."""
        if self._session_db is not None:
            return self._session_db
        try:
            from hermes_constants import get_hermes_home

            home = get_hermes_home()
            key = str(Path(home).resolve())
            with self._session_db_cache_lock:
                cached = self._session_dbs.get(key)
            if cached is not None:
                return cached
            if self._session_db_lock is None:
                self._session_db_lock = asyncio.Lock()
            async with self._session_db_lock:
                with self._session_db_cache_lock:
                    cached = self._session_dbs.get(key)
                if cached is not None:
                    return cached
                return await asyncio.to_thread(
                    self._open_and_cache_session_db,
                    home,
                )
        except Exception as exc:
            logger.debug("Session store unavailable for API server: %s", exc)
            return None

    async def _close_session_stores(self) -> None:
        """Close every profile store once and clear the cache."""
        with self._session_db_cache_lock:
            stores = list(self._session_dbs.values())
            self._session_dbs.clear()
        if self._session_db is not None and all(
            self._session_db is not store for store in stores
        ):
            stores.append(self._session_db)
        self._session_db = None
        seen: set[int] = set()
        for store in stores:
            if store is None or id(store) in seen:
                continue
            seen.add(id(store))
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    await asyncio.to_thread(close)
                except Exception:
                    logger.debug(
                        "Failed to close API session store",
                        exc_info=True,
                    )
