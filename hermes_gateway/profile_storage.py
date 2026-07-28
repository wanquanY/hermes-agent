"""Context-local gateway persistence for multiplexed profiles."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Literal

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
ResourceKind = Literal["session_store", "session_db"]


class ProfileStorageRouter:
    """Stable dependency object that delegates to the current profile."""

    def __init__(self, service: "GatewayProfileStorageService", kind: ResourceKind):
        self._service = service
        self._kind = kind

    def __getattr__(self, name: str):
        resource = self._service.current(self._kind)
        if resource is None:
            raise AttributeError(
                f"{self._kind} is unavailable for {get_hermes_home()}"
            )
        return getattr(resource, name)

    def __bool__(self) -> bool:
        return self._service.current(self._kind) is not None

    def close(self) -> None:
        self._service.close_kind(self._kind)


class GatewayProfileStorageService:
    """Own per-profile SessionStore and state.db lifecycles."""

    def __init__(self, runner):
        self._runner = runner
        self._lock = threading.RLock()
        self._session_stores: dict[Path, Any] = {}
        self._session_dbs: dict[Path, Any] = {}
        self._db_errors: dict[Path, str] = {}
        self.session_store_router = ProfileStorageRouter(self, "session_store")
        self.session_db_router = ProfileStorageRouter(self, "session_db")

    @staticmethod
    def _key(home: Path | str) -> Path:
        return Path(home).expanduser().resolve()

    def install_primary(
        self,
        home: Path,
        *,
        session_store,
        session_db,
        session_db_error: str | None = None,
    ) -> None:
        key = self._key(home)
        with self._lock:
            self._session_stores[key] = session_store
            self._session_dbs[key] = session_db
            if session_db_error:
                self._db_errors[key] = session_db_error

    def current(self, kind: ResourceKind):
        key = self._key(get_hermes_home())
        mapping = (
            self._session_stores
            if kind == "session_store"
            else self._session_dbs
        )
        with self._lock:
            if key not in mapping:
                self.preload(key)
            return mapping.get(key)

    def preload(self, profile_home: Path) -> None:
        """Open one profile's stores once under its own runtime scope."""
        key = self._key(profile_home)
        with self._lock:
            if key in self._session_stores and key in self._session_dbs:
                return

            from hermes_gateway.config import load_gateway_config
            from hermes_gateway.profile_runtime import profile_runtime_scope
            from hermes_gateway.session import SessionStore
            from tools.process_registry import process_registry

            with profile_runtime_scope(key):
                config = load_gateway_config()
                session_store = SessionStore(
                    config.sessions_dir,
                    config,
                    has_active_processes_fn=(
                        lambda session_key: process_registry.has_active_for_session(
                            session_key
                        )
                    ),
                )
                session_db = None
                try:
                    from hermes_agent.composition.cli_session_store import (
                        open_cli_session_store,
                    )

                    session_db = open_cli_session_store()
                except Exception as exc:
                    self._db_errors[key] = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "SQLite session store unavailable for profile home %s: %s",
                        key,
                        exc,
                    )
            self._session_stores[key] = session_store
            self._session_dbs[key] = session_db

    def all_session_stores(self) -> tuple[Any, ...]:
        with self._lock:
            return tuple(self._session_stores.values())

    def session_store_items(self) -> tuple[tuple[Path, Any], ...]:
        with self._lock:
            return tuple(self._session_stores.items())

    def close_kind(self, kind: ResourceKind) -> None:
        mapping = (
            self._session_stores
            if kind == "session_store"
            else self._session_dbs
        )
        with self._lock:
            resources = list(mapping.values())
            mapping.clear()
        closed: set[int] = set()
        for resource in resources:
            close = getattr(resource, "close", None)
            if resource is None or not callable(close) or id(resource) in closed:
                continue
            closed.add(id(resource))
            try:
                close()
            except Exception:
                logger.debug(
                    "Failed to close profile %s",
                    kind,
                    exc_info=True,
                )


def install_profile_storage(runner, primary_home: Path) -> None:
    if not getattr(runner.config, "multiplex_profiles", False):
        return
    service = GatewayProfileStorageService(runner)
    service.install_primary(
        primary_home,
        session_store=runner.session_store,
        session_db=runner._session_db,
        session_db_error=runner._session_db_error,
    )
    runner.profile_storage = service
    runner.session_store = service.session_store_router
    runner._session_db = service.session_db_router


def profile_storage_for(runner) -> GatewayProfileStorageService | None:
    service = getattr(runner, "profile_storage", None)
    return service if isinstance(service, GatewayProfileStorageService) else None
