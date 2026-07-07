from __future__ import annotations

import contextvars
from typing import Any

from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


_active_profile_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "active_profile_context",
    default=None,
)


def profile_context_for_params(params: dict | None = None) -> dict | None:
    params = params or {}
    profile = params.get("dovie_profile") or params.get("dovieProfile") or params.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    profile_id = str(
        params.get("agent_profile_id")
        or params.get("agentProfileId")
        or profile.get("id")
        or ""
    ).strip()
    version_id = str(
        params.get("agent_profile_version_id")
        or params.get("agentProfileVersionId")
        or profile.get("agentProfileVersionId")
        or profile.get("agent_profile_version_id")
        or ""
    ).strip()
    draft_id = str(
        params.get("agent_profile_draft_id")
        or params.get("agentProfileDraftId")
        or profile.get("draftId")
        or profile.get("draft_id")
        or ""
    ).strip()
    hermes_home = str(
        params.get("hermes_home")
        or params.get("hermesHome")
        or params.get("hermesHomePath")
        or profile.get("hermesHomePath")
        or profile.get("hermes_home")
        or ""
    ).strip()
    runtime_scope_key = str(
        params.get("runtime_scope_key")
        or params.get("runtimeScopeKey")
        or profile.get("runtimeScopeKey")
        or profile.get("runtime_scope_key")
        or ""
    ).strip()
    runtime_executor = str(
        params.get("runtime_executor")
        or params.get("runtimeExecutor")
        or profile.get("runtimeExecutor")
        or profile.get("runtime_executor")
        or ""
    ).strip()
    codex_home = str(
        params.get("codex_home")
        or params.get("codexHome")
        or params.get("codexHomePath")
        or profile.get("codexHomePath")
        or profile.get("codexHome")
        or profile.get("codex_home")
        or ""
    ).strip()
    codex_account_mode = str(
        params.get("codex_account_mode")
        or params.get("codexAccountMode")
        or profile.get("codexAccountMode")
        or profile.get("codex_account_mode")
        or ""
    ).strip()
    codex_extra_env = {}
    for candidate in (
        params.get("codex_extra_env"),
        params.get("codexExtraEnv"),
        profile.get("codexExtraEnv"),
        profile.get("codex_extra_env"),
    ):
        if isinstance(candidate, dict) and candidate:
            codex_extra_env = {
                str(k): str(v)
                for k, v in candidate.items()
                if v is not None
            }
            break
    provider = str(
        params.get("provider")
        or params.get("model_provider")
        or params.get("modelProvider")
        or profile.get("provider")
        or profile.get("model_provider")
        or profile.get("modelProvider")
        or ""
    ).strip()
    if not any((profile_id, version_id, draft_id, hermes_home, runtime_scope_key, runtime_executor, codex_home)):
        return None
    if not runtime_scope_key:
        if draft_id:
            runtime_scope_key = f"draft:{draft_id}"
        elif profile_id:
            runtime_scope_key = f"profile:{profile_id}"
    return {
        "id": profile_id,
        "agent_profile_version_id": version_id,
        "agent_profile_draft_id": draft_id,
        "hermes_home": hermes_home,
        "runtime_scope_key": runtime_scope_key,
        "runtime_executor": runtime_executor,
        "codex_home": codex_home,
        "codex_account_mode": codex_account_mode,
        "codex_extra_env": codex_extra_env,
        "provider": provider,
    }


def active_profile_context() -> dict | None:
    profile_context = _active_profile_context.get()
    return dict(profile_context) if isinstance(profile_context, dict) else None


def enter_profile_context(profile_context: dict | None, *, apply_env: bool = True) -> Any:
    if not profile_context:
        return None
    context_token = _active_profile_context.set(profile_context)
    hermes_home = str(profile_context.get("hermes_home") or "").strip()
    if not apply_env or not hermes_home:
        return context_token
    return [
        ("profile_context", context_token),
        ("hermes_home_override", set_hermes_home_override(hermes_home)),
    ]


def leave_profile_context(token: Any) -> None:
    if isinstance(token, list):
        for item in reversed(token):
            leave_profile_context(item)
        return
    if isinstance(token, tuple) and len(token) == 2:
        kind, inner = token
        if kind == "profile_context":
            _active_profile_context.reset(inner)
            return
        if kind == "hermes_home_override":
            reset_hermes_home_override(inner)
            return
    if token is not None:
        _active_profile_context.reset(token)


def active_hermes_home(*, fallback: str, default_home: str | None = None) -> str:
    profile_context = _active_profile_context.get()
    if isinstance(profile_context, dict) and profile_context.get("hermes_home"):
        return str(profile_context["hermes_home"])
    if default_home is not None:
        return default_home
    try:
        return str(get_hermes_home())
    except Exception:
        return fallback


# ════════════════════════════════════════════════════════════════════
# Sub-sidecar deprecation: ProfileContext class + ProfileRegistry.
#
# Below is scaffolding for the refactor that removes the per-profile
# sub-sidecar process (the duplicate per-profile ``dovie_sidecar``
# child). That design relied on PROCESS-LEVEL isolation to keep one
# profile's module-level state (approval queue, clarify pending, etc.)
# from leaking into another's. After removal the main sidecar hosts
# every profile at once, so each of those module-level dicts must be
# keyed by ``scope_key``.
#
# This block defines that bucket type (``ProfileContext``), a
# process-wide ``scope_key → ProfileContext`` registry, a
# ``ContextVar`` set at the ``@method`` dispatch boundary in Phase 2,
# and per-field accessors that ``tools/approval.py`` /
# ``tools/clarify_gateway.py`` switch to in Phase 1b. The accessors
# return the per-profile bucket when ``current_profile`` is set and
# fall back to the caller's module-level dict otherwise — so this
# entire block is dormant until Phase 2, and the legacy scoped-sidecar
# code paths keep working unchanged through the transition.
# ════════════════════════════════════════════════════════════════════

import threading as _threading  # noqa: E402 — module-bottom additions to avoid touching imports above
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Callable, Dict, List, Optional, Set  # noqa: E402


@dataclass
class ProfileContext:
    """All runtime state previously isolated by the sub-sidecar
    process boundary, now keyed by ``scope_key`` so the main sidecar
    can host every profile simultaneously.

    Each field corresponds 1:1 to a module-level dict / set in a
    ``tools/*.py`` module — see the inline cross-references. When you
    add a new module-level singleton anywhere, add the equivalent
    field here and an accessor at the bottom of this file.
    """

    scope_key: str
    agent_profile_id: str = ""
    hermes_home: Optional[Path] = None

    # ── tools/approval.py mirror ──────────────────────────────────
    approval_pending: Dict[str, dict] = field(default_factory=dict)
    approval_session_approved: Dict[str, Set[str]] = field(default_factory=dict)
    approval_session_yolo: Set[str] = field(default_factory=set)
    approval_gateway_queues: Dict[str, list] = field(default_factory=dict)
    approval_gateway_notify_cbs: Dict[str, object] = field(default_factory=dict)

    # ── tools/clarify_gateway.py mirror ──────────────────────────
    clarify_entries: Dict[str, object] = field(default_factory=dict)
    clarify_session_index: Dict[str, List[str]] = field(default_factory=dict)
    clarify_notify_cbs: Dict[str, Callable[[object], None]] = field(default_factory=dict)


class _ProfileRegistry:
    """Process-wide ``scope_key → ProfileContext`` map. Thread-safe.

    ``get_or_create`` is the canonical accessor — it constructs a
    context lazily on first reference and returns the same instance
    thereafter, so identity fields (``agent_profile_id`` etc.) survive
    across handler calls.
    """

    def __init__(self) -> None:
        self._contexts: Dict[str, ProfileContext] = {}
        self._lock = _threading.RLock()

    def get_or_create(
        self,
        scope_key: str,
        *,
        agent_profile_id: str = "",
        hermes_home: Optional[Path] = None,
    ) -> ProfileContext:
        scope_key = str(scope_key or "").strip()
        if not scope_key:
            raise ValueError("ProfileContext requires a non-empty scope_key")
        with self._lock:
            ctx = self._contexts.get(scope_key)
            if ctx is None:
                ctx = ProfileContext(
                    scope_key=scope_key,
                    agent_profile_id=str(agent_profile_id or ""),
                    hermes_home=hermes_home,
                )
                self._contexts[scope_key] = ctx
                return ctx
            # Backfill identity fields on a context first created with
            # only the scope_key (e.g. when the very first call into a
            # profile was by stable_session_id, before the profile
            # resolver attached the agent_profile_id).
            if agent_profile_id and not ctx.agent_profile_id:
                ctx.agent_profile_id = str(agent_profile_id)
            if hermes_home and not ctx.hermes_home:
                ctx.hermes_home = hermes_home
            return ctx

    def get(self, scope_key: str) -> Optional[ProfileContext]:
        scope_key = str(scope_key or "").strip()
        if not scope_key:
            return None
        with self._lock:
            return self._contexts.get(scope_key)

    def remove(self, scope_key: str) -> None:
        scope_key = str(scope_key or "").strip()
        if not scope_key:
            return
        with self._lock:
            self._contexts.pop(scope_key, None)

    def all_scope_keys(self) -> List[str]:
        with self._lock:
            return list(self._contexts.keys())


profile_registry = _ProfileRegistry()


# ``current_profile`` is set by the ``@method`` dispatch wrapper in
# Phase 2. Async handlers and the threads they spawn inherit it via
# the standard ``contextvars`` semantics. While it's unset (every
# call site today, plus future CLI/test paths) the accessors below
# return the caller's module-level fallback — so introducing this
# variable doesn't change any behavior until Phase 2 wires it up.
current_profile: contextvars.ContextVar[Optional[ProfileContext]] = contextvars.ContextVar(
    "tui_gateway.current_profile", default=None
)


# ── Per-field accessors ──────────────────────────────────────────
# Each accessor returns the right dict/set for the current call.
# ``tools/*.py`` modules use these via ``MOD_DICT = staticmethod(fn)``
# or direct call sites; see Phase 1b commits. Phase 7 removes the
# fallback parameter and inlines the ``current_profile.get()`` path.


def approval_pending_for_current(fallback: Dict[str, dict]) -> Dict[str, dict]:
    ctx = current_profile.get()
    return ctx.approval_pending if ctx is not None else fallback


def approval_session_approved_for_current(
    fallback: Dict[str, Set[str]],
) -> Dict[str, Set[str]]:
    ctx = current_profile.get()
    return ctx.approval_session_approved if ctx is not None else fallback


def approval_session_yolo_for_current(fallback: Set[str]) -> Set[str]:
    ctx = current_profile.get()
    return ctx.approval_session_yolo if ctx is not None else fallback


def approval_gateway_queues_for_current(fallback: Dict[str, list]) -> Dict[str, list]:
    ctx = current_profile.get()
    return ctx.approval_gateway_queues if ctx is not None else fallback


def approval_gateway_notify_cbs_for_current(
    fallback: Dict[str, object],
) -> Dict[str, object]:
    ctx = current_profile.get()
    return ctx.approval_gateway_notify_cbs if ctx is not None else fallback


def clarify_entries_for_current(fallback: Dict[str, object]) -> Dict[str, object]:
    ctx = current_profile.get()
    return ctx.clarify_entries if ctx is not None else fallback


def clarify_session_index_for_current(
    fallback: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    ctx = current_profile.get()
    return ctx.clarify_session_index if ctx is not None else fallback


def clarify_notify_cbs_for_current(
    fallback: Dict[str, Callable[[object], None]],
) -> Dict[str, Callable[[object], None]]:
    ctx = current_profile.get()
    return ctx.clarify_notify_cbs if ctx is not None else fallback


# ── Per-profile proxies ───────────────────────────────────────────
# Transparent dict / set wrappers that route every operation to the
# right underlying container:
#   - ``current_profile.get()`` non-None → that profile's bucket
#   - otherwise → the caller-provided module-level fallback
#
# Each ``tools/*.py`` module that used to keep its state as flat
# module-level dicts replaces those definitions with these proxies.
# All call sites (``_pending[key]``, ``_pending.pop``, etc.) stay
# unchanged — the proxy implements every dict/set method actually
# used in this codebase. Phase 7 inlines the ``current_profile.get()``
# branch and drops the fallback once nobody hits the dormant path.


class _PerProfileDict:
    """Dict proxy → routes to ``ProfileContext.<attr_name>`` or
    falls back to ``fallback`` when no profile is active.

    Implements only the dict methods actually used by call sites in
    ``tools/approval.py`` / ``tools/clarify_gateway.py``. Don't add
    methods speculatively — every method here is a potential bug
    surface during the migration.
    """

    __slots__ = ("_attr_name", "_fallback")

    def __init__(self, attr_name: str, fallback: Dict[Any, Any]) -> None:
        self._attr_name = attr_name
        self._fallback = fallback

    def _target(self) -> Dict[Any, Any]:
        ctx = current_profile.get()
        if ctx is not None:
            return getattr(ctx, self._attr_name)
        return self._fallback

    # Read ops
    def __getitem__(self, key):
        return self._target()[key]

    def __contains__(self, key) -> bool:
        return key in self._target()

    def __len__(self) -> int:
        return len(self._target())

    def __iter__(self):
        return iter(self._target())

    def __bool__(self) -> bool:
        return bool(self._target())

    def __eq__(self, other) -> bool:
        return self._target() == other

    def get(self, key, default=None):
        return self._target().get(key, default)

    def keys(self):
        return self._target().keys()

    def values(self):
        return self._target().values()

    def items(self):
        return self._target().items()

    # Write ops
    def __setitem__(self, key, value) -> None:
        self._target()[key] = value

    def __delitem__(self, key) -> None:
        del self._target()[key]

    def pop(self, key, *args):
        return self._target().pop(key, *args)

    def setdefault(self, key, default=None):
        return self._target().setdefault(key, default)

    def update(self, *args, **kwargs) -> None:
        self._target().update(*args, **kwargs)

    def clear(self) -> None:
        self._target().clear()

    def __repr__(self) -> str:
        return f"_PerProfileDict(attr={self._attr_name!r})"


# ── stable_session_id → scope_key resolver cache ─────────────────
# The @method dispatch wrapper (Phase 2) needs to map a request to a
# ProfileContext as fast as possible. Some methods carry the scope
# directly (``runtime_scope_key`` in params), but many only carry a
# ``stored_session_id`` — for those we need to look up which profile
# the session belongs to. Hitting the DB on every method call would
# blow latency, so cache the mapping here.
#
# The cache is intentionally unbounded for now: scope_key strings are
# small (a few dozen chars) and sessions are O(thousands) at most;
# even with no eviction memory stays in the low MB. If session count
# explodes in the future, swap in functools.lru_cache or a bounded
# OrderedDict.


_stable_to_scope_cache: Dict[str, str] = {}
_stable_to_scope_lock = _threading.RLock()


def cache_stable_session_scope(stable_session_id: str, scope_key: str) -> None:
    """Record the ``stable_session_id → scope_key`` mapping. Called
    by any code path that just resolved the binding (typically the
    runtime-scope routing layer)."""
    stable = str(stable_session_id or "").strip()
    scope = str(scope_key or "").strip()
    if not stable or not scope:
        return
    with _stable_to_scope_lock:
        _stable_to_scope_cache[stable] = scope


def lookup_stable_session_scope(stable_session_id: str) -> Optional[str]:
    """Return the cached scope_key for a stable_session_id, or
    ``None`` if it's never been seen. Phase 2 dispatch wrapper falls
    through to a DB lookup on miss."""
    stable = str(stable_session_id or "").strip()
    if not stable:
        return None
    with _stable_to_scope_lock:
        return _stable_to_scope_cache.get(stable)


def forget_stable_session_scope(stable_session_id: str) -> None:
    """Drop the cache entry when a session is deleted / its scope
    changes. Currently unused — Phase 2 wires up the call site."""
    stable = str(stable_session_id or "").strip()
    if not stable:
        return
    with _stable_to_scope_lock:
        _stable_to_scope_cache.pop(stable, None)


class _PerProfileSet:
    """Set counterpart of ``_PerProfileDict``."""

    __slots__ = ("_attr_name", "_fallback")

    def __init__(self, attr_name: str, fallback: Set[Any]) -> None:
        self._attr_name = attr_name
        self._fallback = fallback

    def _target(self) -> Set[Any]:
        ctx = current_profile.get()
        if ctx is not None:
            return getattr(ctx, self._attr_name)
        return self._fallback

    def __contains__(self, value) -> bool:
        return value in self._target()

    def __len__(self) -> int:
        return len(self._target())

    def __iter__(self):
        return iter(self._target())

    def __bool__(self) -> bool:
        return bool(self._target())

    def __eq__(self, other) -> bool:
        return self._target() == other

    def add(self, value) -> None:
        self._target().add(value)

    def discard(self, value) -> None:
        self._target().discard(value)

    def remove(self, value) -> None:
        self._target().remove(value)

    def update(self, *iterables) -> None:
        self._target().update(*iterables)

    def clear(self) -> None:
        self._target().clear()

    def __repr__(self) -> str:
        return f"_PerProfileSet(attr={self._attr_name!r})"
