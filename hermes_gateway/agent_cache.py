"""Gateway AIAgent cache ownership."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, List

logger = logging.getLogger(__name__)

AGENT_CACHE_MAX_SIZE = 128
AGENT_CACHE_IDLE_TTL_SECS = 3600.0
AGENT_PENDING_SENTINEL = object()


class GatewayAgentCacheMixin:
    """AIAgent cache signature, eviction, and idle sweep behavior."""

    @staticmethod
    def _agent_config_signature(
        model: str,
        runtime: dict,
        enabled_toolsets: list,
        ephemeral_prompt: str,
        cache_keys: dict | None = None,
    ) -> str:
        """Compute a stable string key from agent config values."""
        api_key = str(runtime.get("api_key", "") or "")
        api_key_fingerprint = hashlib.sha256(api_key.encode()).hexdigest() if api_key else ""
        cache_keys_sorted = sorted((cache_keys or {}).items())

        blob = json.dumps(
            [
                model,
                api_key_fingerprint,
                runtime.get("base_url", ""),
                runtime.get("provider", ""),
                runtime.get("api_mode", ""),
                sorted(enabled_toolsets) if enabled_toolsets else [],
                ephemeral_prompt or "",
                cache_keys_sorted,
            ],
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def _evict_cached_agent(self, session_key: str) -> None:
        """Remove a cached agent for a session and soft-release its clients."""
        lock = getattr(self, "_agent_cache_lock", None)
        evicted = None
        if lock:
            with lock:
                evicted = self._agent_cache.pop(session_key, None)
        else:
            cache = getattr(self, "_agent_cache", None)
            if cache is not None:
                evicted = cache.pop(session_key, None)

        agent = evicted[0] if isinstance(evicted, tuple) and evicted else evicted
        if agent is None or agent is AGENT_PENDING_SENTINEL:
            return

        running_ids = {
            id(a)
            for a in getattr(self, "_running_agents", {}).values()
            if a is not None and a is not AGENT_PENDING_SENTINEL
        }
        if id(agent) in running_ids:
            return

        try:
            threading.Thread(
                target=self._release_evicted_agent_soft,
                args=(agent,),
                daemon=True,
                name=f"agent-evict-{str(session_key)[:24]}",
            ).start()
        except Exception as exc:
            logger.debug("Falling back to inline agent cache release for %s: %s", session_key, exc)
            try:
                self._release_evicted_agent_soft(agent)
            except Exception as release_exc:
                logger.debug("Inline agent cache release failed for %s: %s", session_key, release_exc)

    @staticmethod
    def _init_cached_agent_for_turn(agent: Any, interrupt_depth: int) -> None:
        """Reset per-turn state on a cached agent before a new turn starts."""
        if interrupt_depth == 0:
            agent._last_activity_ts = time.time()
            agent._last_activity_desc = "starting new turn (cached)"
        agent._api_call_count = 0

    def _release_evicted_agent_soft(self, agent: Any) -> None:
        """Soft cleanup for cache-evicted agents while preserving session tool state."""
        if agent is None:
            return
        try:
            if hasattr(agent, "release_clients"):
                agent.release_clients()
            else:
                self._cleanup_agent_resources(agent)
        except Exception as exc:
            logger.debug("Soft release of cache-evicted agent failed: %s", exc)
        if hasattr(agent, "_session_messages"):
            agent._session_messages = []

    def _enforce_agent_cache_cap(self) -> None:
        """Evict oldest cached agents when cache exceeds ``AGENT_CACHE_MAX_SIZE``."""
        cache = getattr(self, "_agent_cache", None)
        if cache is None:
            return
        if not hasattr(cache, "move_to_end"):
            return

        running_ids = {
            id(a)
            for a in getattr(self, "_running_agents", {}).values()
            if a is not None and a is not AGENT_PENDING_SENTINEL
        }

        excess = max(0, len(cache) - AGENT_CACHE_MAX_SIZE)
        evict_plan: List[tuple] = []
        if excess > 0:
            ordered_keys = list(cache.keys())
            for key in ordered_keys[:excess]:
                entry = cache.get(key)
                agent = entry[0] if isinstance(entry, tuple) and entry else None
                if agent is not None and id(agent) in running_ids:
                    continue
                evict_plan.append((key, agent))

        for key, _ in evict_plan:
            cache.pop(key, None)

        remaining_over_cap = len(cache) - AGENT_CACHE_MAX_SIZE
        if remaining_over_cap > 0:
            logger.warning(
                "Agent cache over cap (%d > %d); %d excess slot(s) held by mid-turn agents.",
                len(cache),
                AGENT_CACHE_MAX_SIZE,
                remaining_over_cap,
            )

        for key, agent in evict_plan:
            logger.info("Agent cache at cap; evicting LRU session=%s (cache_size=%d)", key, len(cache))
            if agent is not None:
                threading.Thread(
                    target=self._release_evicted_agent_soft,
                    args=(agent,),
                    daemon=True,
                    name=f"agent-cache-evict-{key[:24]}",
                ).start()

    def _sweep_idle_cached_agents(self) -> int:
        """Evict cached agents idle past ``AGENT_CACHE_IDLE_TTL_SECS``."""
        cache = getattr(self, "_agent_cache", None)
        lock = getattr(self, "_agent_cache_lock", None)
        if cache is None or lock is None:
            return 0
        now = time.time()
        to_evict: List[tuple] = []
        running_ids = {
            id(a)
            for a in getattr(self, "_running_agents", {}).values()
            if a is not None and a is not AGENT_PENDING_SENTINEL
        }
        with lock:
            for key, entry in list(cache.items()):
                agent = entry[0] if isinstance(entry, tuple) and entry else None
                if agent is None:
                    continue
                if id(agent) in running_ids:
                    continue
                last_activity = getattr(agent, "_last_activity_ts", None)
                if last_activity is None:
                    continue
                if (now - last_activity) > AGENT_CACHE_IDLE_TTL_SECS:
                    to_evict.append((key, agent))
            for key, _ in to_evict:
                cache.pop(key, None)
        for key, agent in to_evict:
            logger.info(
                "Agent cache idle-TTL evict: session=%s (idle=%.0fs)",
                key,
                now - getattr(agent, "_last_activity_ts", now),
            )
            threading.Thread(
                target=self._release_evicted_agent_soft,
                args=(agent,),
                daemon=True,
                name=f"agent-cache-idle-{key[:24]}",
            ).start()
        return len(to_evict)
