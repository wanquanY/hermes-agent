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

CACHE_BUSTING_CONFIG_KEYS: tuple = (
    ("model", "context_length"),
    ("model", "max_tokens"),
    ("compression", "enabled"),
    ("compression", "threshold"),
    ("compression", "target_ratio"),
    ("compression", "protect_last_n"),
    ("agent", "disabled_toolsets"),
)


class GatewayAgentCacheService:
    """AIAgent cache signature, eviction, and idle sweep behavior."""

    def __init__(self, runner):
        self._runner = runner

    @staticmethod
    def agent_config_signature(
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

    @staticmethod
    def extract_cache_busting_config(user_config: dict | None) -> dict:
        """Pull values that must bust the cached agent."""
        out: dict[str, Any] = {}
        cfg = user_config if isinstance(user_config, dict) else {}
        for section, key in CACHE_BUSTING_CONFIG_KEYS:
            section_val = cfg.get(section)
            if isinstance(section_val, dict):
                out[f"{section}.{key}"] = section_val.get(key)
            else:
                out[f"{section}.{key}"] = None
        try:
            from tools.registry import registry

            out["tools.registry_generation"] = getattr(registry, "_generation", None)
        except Exception as exc:
            logger.debug("Could not read tool registry generation for agent cache signature: %s", exc)
            out["tools.registry_generation"] = None
        return out

    def evict_cached_agent(self, session_key: str) -> None:
        """Remove a cached agent for a session and soft-release its clients."""
        runner = self._runner
        agent = self.pop_cached_agent(session_key)
        if agent is None or agent is AGENT_PENDING_SENTINEL:
            return

        running_ids = {
            id(a)
            for a in getattr(runner, "_running_agents", {}).values()
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

    def pop_cached_agent(self, session_key: str) -> Any:
        """Atomically remove and return one cached agent without cleanup.

        Callers that require hard teardown (for example ``/new``) own that
        lifecycle after this method releases the cache lock. Ordinary cache
        invalidation should continue to use :meth:`evict_cached_agent`.
        """

        runner = self._runner
        lock = getattr(runner, "_agent_cache_lock", None)
        evicted = None
        if lock:
            with lock:
                evicted = runner._agent_cache.pop(session_key, None)
        else:
            cache = getattr(runner, "_agent_cache", None)
            if cache is not None:
                evicted = cache.pop(session_key, None)

        agent = evicted[0] if isinstance(evicted, tuple) and evicted else evicted
        return None if agent is AGENT_PENDING_SENTINEL else agent

    @staticmethod
    def init_cached_agent_for_turn(agent: Any, interrupt_depth: int) -> None:
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
                self._runner._cleanup_agent_resources(agent)
        except Exception as exc:
            logger.debug("Soft release of cache-evicted agent failed: %s", exc)
        if hasattr(agent, "_session_messages"):
            agent._session_messages = []

    def enforce_agent_cache_cap(self) -> None:
        """Evict oldest cached agents when cache exceeds ``AGENT_CACHE_MAX_SIZE``."""
        runner = self._runner
        cache = getattr(runner, "_agent_cache", None)
        if cache is None:
            return
        if not hasattr(cache, "move_to_end"):
            return

        running_ids = {
            id(a)
            for a in getattr(runner, "_running_agents", {}).values()
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

    def sweep_idle_cached_agents(self) -> int:
        """Evict cached agents idle past ``AGENT_CACHE_IDLE_TTL_SECS``."""
        runner = self._runner
        cache = getattr(runner, "_agent_cache", None)
        lock = getattr(runner, "_agent_cache_lock", None)
        if cache is None or lock is None:
            return 0
        now = time.time()
        to_evict: List[tuple] = []
        running_ids = {
            id(a)
            for a in getattr(runner, "_running_agents", {}).values()
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


def agent_cache_for(runner) -> GatewayAgentCacheService:
    service = getattr(runner, "agent_cache", None)
    if isinstance(service, GatewayAgentCacheService):
        return service
    service = GatewayAgentCacheService(runner)
    runner.agent_cache = service
    return service
