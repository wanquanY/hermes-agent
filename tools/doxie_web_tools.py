"""Doxie-managed SERPER search and webpage parsing tools.

These tools keep paid web/search credentials on the Doxie backend. Packaged
Hermes runtimes call the authenticated llm-proxy endpoints with the same
runtime token used for managed model and MinerU requests.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

from tools.registry import registry, tool_error

DOXIE_WEB_PROXY_DEFAULT_TIMEOUT_SECONDS = 45
DOXIE_WEB_PROXY_MAX_TIMEOUT_SECONDS = 60


SERPER_SEARCH_SCHEMA = {
    "name": "serper_search_tool",
    "description": (
        "Use Doxie-managed SERPER/Google Search. Supports region, language, "
        "page, result count, and time-range filters. Use this for current web "
        "search before parsing important result pages."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query.",
                "minLength": 1,
                "maxLength": 500,
            },
            "location": {
                "type": "string",
                "description": "Search location, for example United States, China, Japan, Germany.",
            },
            "gl": {
                "type": "string",
                "description": "Google country code, for example us, cn, jp, de.",
            },
            "hl": {
                "type": "string",
                "description": "Search language code, for example en, zh-CN, ja, ko.",
            },
            "page": {
                "type": "integer",
                "description": "Result page number, starting at 1.",
                "minimum": 1,
                "maximum": 10,
                "default": 1,
            },
            "num": {
                "type": "integer",
                "description": "Number of results to return.",
                "minimum": 1,
                "maximum": 100,
                "default": 10,
            },
            "time_range": {
                "type": "string",
                "description": "Optional time filter.",
                "enum": ["past_hour", "past_day", "past_week", "past_month", "past_year"],
            },
        },
        "required": ["query"],
    },
}


JINA_WEB_PARSER_SCHEMA = {
    "name": "jina_web_parser_tool",
    "description": (
        "Parse a webpage into clean Markdown or text through the Doxie-managed "
        "web reader. Use after search to read important pages."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "HTTP or HTTPS webpage URL to parse.",
            },
            "output_format": {
                "type": "string",
                "enum": ["markdown", "text"],
                "description": "Output format.",
                "default": "markdown",
            },
            "include_links": {
                "type": "boolean",
                "description": "Keep Markdown links in the parsed content.",
                "default": True,
            },
            "include_images": {
                "type": "boolean",
                "description": "Keep Markdown image references in the parsed content.",
                "default": True,
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds.",
                "minimum": 5,
                "maximum": 60,
                "default": 45,
            },
        },
        "required": ["url"],
    },
}


def _env_url(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def _runtime_token() -> str:
    return str(os.getenv("DOXIE_LLM_RUNTIME_TOKEN") or "").strip()


def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else default


def _bounded_timeout(*values: Any) -> int:
    candidates: list[int] = []
    for value in values:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            candidates.append(parsed)
    requested = candidates[0] if candidates else DOXIE_WEB_PROXY_DEFAULT_TIMEOUT_SECONDS
    return max(5, min(requested, DOXIE_WEB_PROXY_MAX_TIMEOUT_SECONDS))


def _post_json(url: str, payload: dict[str, Any], *, token: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except TimeoutError as exc:
        raise RuntimeError(f"Doxie web proxy request timed out after {timeout}s") from exc
    except socket.timeout as exc:
        raise RuntimeError(f"Doxie web proxy request timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Doxie web proxy request failed: {exc}") from exc

    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Doxie web proxy returned non-JSON response: HTTP {status}") from exc

    if status < 200 or status >= 300:
        detail = result.get("detail") if isinstance(result, dict) else None
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("error")
        else:
            message = detail
        raise RuntimeError(str(message or result.get("message") or f"HTTP {status}"))
    return result


def _proxy_result(env_name: str, payload: dict[str, Any]) -> str:
    url = _env_url(env_name)
    token = _runtime_token()
    if not url:
        return tool_error(f"{env_name} is not configured.")
    if not token:
        return tool_error("DOXIE_LLM_RUNTIME_TOKEN is not configured.")
    timeout = _bounded_timeout(
        payload.get("timeout"),
        _env_int("DOXIE_WEB_PROXY_TIMEOUT", DOXIE_WEB_PROXY_DEFAULT_TIMEOUT_SECONDS),
    )
    try:
        result = _post_json(
            url,
            payload,
            token=token,
            timeout=timeout,
        )
    except Exception as exc:
        return tool_error(str(exc))
    return json.dumps(result, ensure_ascii=False)


def serper_search_tool(args: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query is required.")
    payload = {
        "query": query,
        "location": args.get("location"),
        "gl": args.get("gl"),
        "hl": args.get("hl"),
        "page": args.get("page", 1),
        "num": args.get("num", 10),
        "time_range": args.get("time_range"),
    }
    return _proxy_result("DOXIE_SERPER_PROXY_URL", payload)


def jina_web_parser_tool(args: dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return tool_error("url is required.")
    payload = {
        "url": url,
        "output_format": args.get("output_format", "markdown"),
        "include_links": args.get("include_links", True),
        "include_images": args.get("include_images", True),
        "timeout": args.get("timeout", DOXIE_WEB_PROXY_DEFAULT_TIMEOUT_SECONDS),
    }
    return _proxy_result("DOXIE_WEB_PARSE_PROXY_URL", payload)


registry.register(
    name="serper_search_tool",
    toolset="doxie_web",
    schema=SERPER_SEARCH_SCHEMA,
    handler=lambda args, **kw: serper_search_tool(args),
    emoji="🔎",
    max_result_size_chars=100_000,
)

registry.register(
    name="jina_web_parser_tool",
    toolset="doxie_web",
    schema=JINA_WEB_PARSER_SCHEMA,
    handler=lambda args, **kw: jina_web_parser_tool(args),
    emoji="🌐",
    max_result_size_chars=120_000,
)
