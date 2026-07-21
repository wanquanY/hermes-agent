"""Process-level bootstrap helpers for ``run_agent``.

Three concerns, all tied to ``AIAgent`` boot-time / runtime IO setup:

1. **Lazy OpenAI SDK import** — ``_load_openai_cls`` + ``_OpenAIProxy``
   defer the 240ms-ish ``from openai import OpenAI`` cost until first use,
   while preserving ``isinstance(client, OpenAI)`` checks and
   ``patch("run_agent.OpenAI", ...)`` test patterns.

2. **Crash-resistant stdio** — ``_SafeWriter`` wraps stdout/stderr so
   ``OSError: Input/output error`` from broken pipes (systemd, Docker,
   thread teardown races) cannot crash the agent.  ``_install_safe_stdio``
   applies the wrapper.

3. **HTTP proxy resolution** — ``_get_proxy_from_env`` reads
   ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY``;
   ``_get_proxy_for_base_url`` respects ``NO_PROXY`` for the given base URL.

``run_agent`` re-exports every name so existing
``from run_agent import _get_proxy_from_env`` imports keep working
unchanged.
"""

from __future__ import annotations

import os
import logging
import sys
import urllib.request
from typing import Optional

from utils import base_url_hostname, normalize_proxy_url

logger = logging.getLogger(__name__)


# Cached at module level so we only pay the OpenAI SDK import cost once
# per process (after the first lazy load).
_OPENAI_CLS_CACHE = None


def _load_openai_cls() -> type:
    """Import and cache ``openai.OpenAI``."""
    global _OPENAI_CLS_CACHE
    if _OPENAI_CLS_CACHE is None:
        from openai import OpenAI as _cls
        _OPENAI_CLS_CACHE = _cls
    return _OPENAI_CLS_CACHE


class _OpenAIProxy:
    """Module-level proxy that looks like ``openai.OpenAI`` but imports lazily."""

    __slots__ = ()

    def __call__(self, *args, **kwargs):
        if "http_client" not in kwargs:
            http_client = build_provider_http_client(kwargs.get("base_url", ""))
            if http_client is not None:
                kwargs["http_client"] = http_client
        return _load_openai_cls()(*args, **kwargs)

    def __instancecheck__(self, obj):
        return isinstance(obj, _load_openai_cls())

    def __repr__(self):
        return "<lazy openai.OpenAI proxy>"


class _SafeWriter:
    """Transparent stdio wrapper that catches OSError/ValueError from broken pipes.

    When hermes-agent runs as a systemd service, Docker container, or headless
    daemon, the stdout/stderr pipe can become unavailable (idle timeout, buffer
    exhaustion, socket reset). Any print() call then raises
    ``OSError: [Errno 5] Input/output error``, which can crash agent setup or
    run_conversation() — especially via double-fault when an except handler
    also tries to print.

    Additionally, when subagents run in ThreadPoolExecutor threads, the shared
    stdout handle can close between thread teardown and cleanup, raising
    ``ValueError: I/O operation on closed file`` instead of OSError.

    This wrapper delegates all writes to the underlying stream and silently
    catches both OSError and ValueError. It is transparent when the wrapped
    stream is healthy.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def write(self, data):
        try:
            return self._inner.write(data)
        except (OSError, ValueError):
            return len(data) if isinstance(data, str) else 0

    def flush(self):
        try:
            self._inner.flush()
        except (OSError, ValueError):
            pass

    def fileno(self):
        return self._inner.fileno()

    def isatty(self):
        try:
            return self._inner.isatty()
        except (OSError, ValueError):
            return False

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _get_proxy_from_env() -> Optional[str]:
    """Read proxy URL from environment variables.

    Checks HTTPS_PROXY, HTTP_PROXY, ALL_PROXY (and lowercase variants) in order.
    Returns the first valid proxy URL found, or None if no proxy is configured.
    """
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                "https_proxy", "http_proxy", "all_proxy"):
        value = os.environ.get(key, "").strip()
        if value:
            return normalize_proxy_url(value)
    return None


def _get_proxy_for_base_url(base_url: Optional[str]) -> Optional[str]:
    """Return an env-configured proxy unless NO_PROXY excludes this base URL."""
    proxy = _get_proxy_from_env()
    if not proxy or not base_url:
        return proxy

    host = base_url_hostname(base_url)
    if not host:
        return proxy

    try:
        if urllib.request.proxy_bypass_environment(host):
            return None
    except Exception:
        pass

    return proxy


def _positive_env_number(name: str, default: float, *, integer: bool = False):
    raw = os.getenv(name, "").strip()
    if not raw:
        return int(default) if integer else default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        return int(default) if integer else default
    if value <= 0:
        return int(default) if integer else default
    return value


def build_provider_http_client(
    base_url: str = "",
    *,
    async_mode: bool = False,
    verify=None,
):
    """Build the canonical httpx client for OpenAI-compatible providers.

    Idle pooled connections are reaped before common reverse-proxy idle
    deadlines.  The factory intentionally leaves OS socket options untouched:
    replacing httpx's defaults previously removed TCP_NODELAY and destabilized
    TLS/SSE through OpenResty and Cloudflare.  Proxy policy is resolved once
    from environment + NO_PROXY and ``trust_env`` is then disabled so mounted
    transports cannot silently choose a different route.
    """

    try:
        import httpx

        if verify is None:
            try:
                from agent.ssl_verify import resolve_httpx_verify
                from hermes_cli.config import get_custom_provider_tls_settings

                tls = get_custom_provider_tls_settings(str(base_url or ""))
                verify = resolve_httpx_verify(
                    ca_bundle=tls.get("ssl_ca_cert"),
                    ssl_verify=tls.get("ssl_verify"),
                    base_url=str(base_url or ""),
                )
            except Exception:
                logger.debug("provider TLS settings resolution failed", exc_info=True)
                verify = True

        keepalive_expiry = _positive_env_number(
            "HERMES_PROVIDER_HTTPX_KEEPALIVE_EXPIRY",
            20.0,
        )
        max_keepalive = _positive_env_number(
            "HERMES_PROVIDER_HTTPX_MAX_KEEPALIVE",
            20,
            integer=True,
        )
        max_connections = _positive_env_number(
            "HERMES_PROVIDER_HTTPX_MAX_CONNECTIONS",
            100,
            integer=True,
        )
        limits = httpx.Limits(
            max_keepalive_connections=max_keepalive,
            max_connections=max_connections,
            keepalive_expiry=keepalive_expiry,
        )
        timeout = httpx.Timeout(
            connect=15.0,
            read=None,
            write=15.0,
            pool=10.0,
        )
        client_cls = httpx.AsyncClient if async_mode else httpx.Client
        return client_cls(
            limits=limits,
            timeout=timeout,
            proxy=_get_proxy_for_base_url(base_url),
            trust_env=False,
            verify=verify,
        )
    except Exception as exc:
        logger.warning("Could not build provider HTTP client: %s", exc)
        return None


def _install_safe_stdio() -> None:
    """Wrap stdout/stderr so best-effort console output cannot crash the agent."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and not isinstance(stream, _SafeWriter):
            setattr(sys, stream_name, _SafeWriter(stream))


# Module-level proxy instance — drops in for ``openai.OpenAI``.  Imported as
# ``from agent.process_bootstrap import OpenAI`` (or re-exported via
# ``run_agent`` for legacy tests).
OpenAI = _OpenAIProxy()


__all__ = [
    "OpenAI",
    "_OpenAIProxy",
    "_load_openai_cls",
    "_SafeWriter",
    "_install_safe_stdio",
    "_get_proxy_from_env",
    "_get_proxy_for_base_url",
    "build_provider_http_client",
]
