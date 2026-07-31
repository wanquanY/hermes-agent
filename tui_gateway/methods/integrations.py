# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import threading

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())
_SKILL_HOME_LOCK = threading.RLock()


# ── Methods: browser / plugins / cron / skills ───────────────────────


def _list_configurable_toolsets(params: dict) -> list[dict]:
    """Return toolsets that the gateway can persist with ``tools.configure``.

    ``toolsets.get_all_toolsets()`` intentionally includes every live registry
    toolset, including conditionally registered implementation groups such as
    ``browser-cdp``. Those groups can be real at runtime, but they are not
    necessarily user-configurable. The TUI/client settings surface must use the
    same source of truth as ``tools.configure`` so every listed row can be
    saved.
    """
    from hermes_cli.tools_config import (
        _DEFAULT_OFF_TOOLSETS,
        _get_effective_configurable_toolsets,
        _toolset_allowed_for_platform,
    )
    from toolsets import get_toolset_info

    session = _sessions.get(params.get("session_id", ""))
    enabled = (
        set(getattr(session["agent"], "enabled_toolsets", []) or [])
        if session
        else set(_load_enabled_toolsets() or [])
    )

    items = []
    for name, _label, summary in _get_effective_configurable_toolsets():
        if not _toolset_allowed_for_platform(name, "cli"):
            continue
        info = get_toolset_info(name) or {}
        tools = list(info.get("resolved_tools") or [])
        items.append(
            {
                "name": name,
                "description": str(info.get("description") or summary or ""),
                "tool_count": int(info.get("tool_count") or len(tools) or 0),
                "enabled": name in enabled if enabled else name not in _DEFAULT_OFF_TOOLSETS,
                "tools": tools,
                "configurable": True,
            }
        )
    return items


def _tool_prepare_import_available(module_name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


def _tool_prepare_agent_browser_cli_available() -> bool:
    try:
        from tools.browser_tool import (
            _find_agent_browser,
            _requires_real_termux_browser_install,
        )

        browser_cmd = _find_agent_browser()
        return not _requires_real_termux_browser_install(browser_cmd)
    except Exception:
        return False


def _tool_prepare_local_browser_available() -> bool:
    try:
        from tools.browser_tool import _chromium_installed, _using_lightpanda_engine

        return _tool_prepare_agent_browser_cli_available() and (
            bool(_chromium_installed()) or bool(_using_lightpanda_engine())
        )
    except Exception:
        return False


def _tool_prepare_post_setup_installed(post_setup_key: str, provider: dict | None = None) -> bool:
    import shutil

    try:
        if post_setup_key == "cua_driver":
            return bool(shutil.which("cua-driver"))
        if post_setup_key == "kittentts":
            return _tool_prepare_import_available("kittentts")
        if post_setup_key == "piper":
            return _tool_prepare_import_available("piper")
        if post_setup_key == "ddgs":
            return _tool_prepare_import_available("ddgs")
        if post_setup_key == "langfuse":
            return _tool_prepare_import_available("langfuse")
        if post_setup_key == "rl_training":
            return _tool_prepare_import_available("tinker_atropos")
        if post_setup_key == "camofox":
            from hermes_cli.tools_config import PROJECT_ROOT

            return (PROJECT_ROOT / "node_modules" / "@askjo" / "camofox-browser").exists()
        if post_setup_key in {"agent_browser", "browserbase"}:
            browser_provider = str((provider or {}).get("browser_provider") or "").strip()
            if browser_provider == "local":
                return _tool_prepare_local_browser_available()
            return _tool_prepare_agent_browser_cli_available()
        if post_setup_key == "spotify":
            # Spotify setup is OAuth, not a package install. Treat it as an
            # interactive configuration flow unless Hermes exposes a browser
            # login bridge for the desktop client.
            return False
    except Exception:
        return False
    return True


def _tool_prepare_post_setup_installable(post_setup_key: str) -> bool:
    return post_setup_key not in {"spotify"}


def _tool_prepare_post_setup_lock():
    global _tool_prepare_post_setup_lock_instance
    try:
        return _tool_prepare_post_setup_lock_instance
    except NameError:
        import threading

        _tool_prepare_post_setup_lock_instance = threading.Lock()
        return _tool_prepare_post_setup_lock_instance


def _tool_prepare_progress(params: dict, name: str, payload: dict) -> None:
    try:
        body = {
            "operation_id": str(params.get("operation_id") or params.get("operationId") or ""),
            "name": name,
            "phase": str(payload.get("phase") or "installing"),
            "message": str(payload.get("message") or ""),
            "level": str(payload.get("level") or "info"),
            "post_setup": str(payload.get("post_setup") or ""),
            "current": int(payload.get("current") or 0),
            "total": int(payload.get("total") or 0),
        }
        _emit("tools.prepare.progress", str(params.get("session_id") or ""), body)
    except Exception:
        pass


def _tool_prepare_wrap_post_setup_output(progress_callback, *, post_setup: str, current: int, total: int):
    from contextlib import contextmanager

    @contextmanager
    def _wrapped():
        if not progress_callback:
            yield
            return
        import hermes_cli.tools_config as tools_config

        originals = {}

        def wrap(attr: str, level: str):
            original = getattr(tools_config, attr, None)
            if not callable(original):
                return
            originals[attr] = original

            def _inner(message="", *args, **kwargs):
                progress_callback(
                    {
                        "phase": "installing",
                        "level": level,
                        "message": str(message or "").strip(),
                        "post_setup": post_setup,
                        "current": current,
                        "total": total,
                    }
                )
                return original(message, *args, **kwargs)

            setattr(tools_config, attr, _inner)

        wrap("_print_info", "info")
        wrap("_print_success", "success")
        wrap("_print_warning", "warning")
        try:
            yield
        finally:
            for attr, original in originals.items():
                setattr(tools_config, attr, original)

    return _wrapped()


def _tool_prepare_missing_env(provider: dict) -> list[dict]:
    try:
        from hermes_cli.tools_config import get_env_value
    except Exception:
        return []
    missing = []
    for var in provider.get("env_vars", []) or []:
        key = str(var.get("key") or "").strip()
        if key and not get_env_value(key):
            missing.append(
                {
                    "key": key,
                    "prompt": str(var.get("prompt") or key),
                    "url": str(var.get("url") or ""),
                    "default": str(var.get("default") or ""),
                }
            )
    return missing


def _tool_prepare_declared_env(provider: dict) -> list[dict]:
    declared = []
    for var in provider.get("env_vars", []) or []:
        key = str(var.get("key") or "").strip()
        if not key:
            continue
        declared.append(
            {
                "key": key,
                "prompt": str(var.get("prompt") or key),
                "url": str(var.get("url") or ""),
                "default": str(var.get("default") or ""),
            }
        )
    return declared


def _tool_prepare_simple_env_status(toolset: str) -> dict | None:
    from hermes_cli.tools_config import TOOLSET_ENV_REQUIREMENTS, get_env_value

    requirements = TOOLSET_ENV_REQUIREMENTS.get(toolset) or []
    if not requirements:
        return None

    missing_env = [
        {
            "key": str(key),
            "prompt": str(key),
            "url": str(url or ""),
            "default": "",
        }
        for key, url in requirements
        if str(key).strip() and not get_env_value(str(key))
    ]
    ready = not missing_env
    return {
        "name": toolset,
        "ready": ready,
        "requires_prepare": not ready,
        "installable": False,
        "requires_configuration": not ready,
        "label": "无需安装" if ready else "需要配置凭据或授权",
        "providers": [
            {
                "name": "Environment",
                "post_setup": "",
                "installed": True,
                "ready": ready,
                "installable": False,
                "requires_configuration": not ready,
                "requires_interactive": False,
                "missing_env": missing_env,
                "env_vars": [
                    {
                        "key": str(key),
                        "prompt": str(key),
                        "url": str(url or ""),
                        "default": "",
                    }
                    for key, url in requirements
                    if str(key).strip()
                ],
            }
        ],
    }


def _tool_prepare_status(name: str, config: dict | None = None) -> dict:
    from hermes_cli.tools_config import TOOL_CATEGORIES, _visible_providers, load_config

    toolset = str(name or "").strip()
    if not toolset:
        return {"name": "", "ready": True, "requires_prepare": False}
    cfg = config or load_config()
    cat = TOOL_CATEGORIES.get(toolset)
    if not cat:
        simple_status = _tool_prepare_simple_env_status(toolset)
        if simple_status:
            return simple_status
        return {
            "name": toolset,
            "ready": True,
            "requires_prepare": False,
            "installable": False,
            "requires_configuration": False,
            "label": "无需安装",
            "providers": [],
        }

    provider_rows = []
    for provider in _visible_providers(cat, cfg):
        post_setup = str(provider.get("post_setup") or "").strip()
        missing_env = _tool_prepare_missing_env(provider)
        installed = _tool_prepare_post_setup_installed(post_setup, provider) if post_setup else True
        requires_configuration = bool(missing_env) or post_setup == "spotify"
        installable = bool(post_setup) and not installed and not missing_env and _tool_prepare_post_setup_installable(post_setup)
        ready = installed and not requires_configuration
        provider_rows.append(
            {
                "name": str(provider.get("name") or ""),
                "post_setup": post_setup,
                "installed": installed,
                "ready": ready,
                "installable": installable,
                "requires_configuration": requires_configuration,
                "requires_interactive": post_setup == "spotify",
                "missing_env": missing_env,
                "env_vars": _tool_prepare_declared_env(provider),
            }
        )

    actionable = [row for row in provider_rows if row["post_setup"] or row["requires_configuration"]]
    if not actionable:
        return {
            "name": toolset,
            "ready": True,
            "requires_prepare": False,
            "installable": False,
            "requires_configuration": False,
            "label": "无需安装",
            "providers": provider_rows,
        }

    ready = any(row["ready"] for row in actionable)
    installable = any(row["installable"] for row in actionable)
    requires_configuration = (
        not ready
        and not installable
        and any(row["requires_configuration"] for row in actionable)
    )
    if ready:
        label = "已准备就绪"
    elif installable:
        label = "需要安装依赖"
    elif requires_configuration:
        label = "需要配置凭据或授权"
    else:
        label = "需要手动准备依赖"
    return {
        "name": toolset,
        "ready": ready,
        "requires_prepare": not ready,
        "installable": installable,
        "requires_configuration": requires_configuration,
        "label": label,
        "providers": provider_rows,
    }


def _tool_prepare_install(name: str, progress_callback=None) -> dict:
    from hermes_cli.tools_config import _run_post_setup

    status = _tool_prepare_status(name)
    if status.get("ready"):
        if progress_callback:
            progress_callback(
                {
                    "phase": "complete",
                    "level": "success",
                    "message": "依赖已准备完成",
                    "current": 1,
                    "total": 1,
                }
            )
        return status
    install_targets = []
    for provider in status.get("providers") or []:
        post_setup = str(provider.get("post_setup") or "").strip()
        if provider.get("installable") and post_setup:
            install_targets.append(post_setup)
    if not install_targets:
        return status
    unique_targets = list(dict.fromkeys(install_targets))
    total = len(unique_targets)
    for index, post_setup in enumerate(unique_targets, start=1):
        if progress_callback:
            progress_callback(
                {
                    "phase": "installing",
                    "message": f"正在安装 {post_setup}",
                    "post_setup": post_setup,
                    "current": index,
                    "total": total,
                }
            )
        with _tool_prepare_post_setup_lock():
            with _tool_prepare_wrap_post_setup_output(
                progress_callback,
                post_setup=post_setup,
                current=index,
                total=total,
            ):
                _run_post_setup(post_setup)
    if progress_callback:
        progress_callback(
            {
                "phase": "complete",
                "level": "success",
                "message": "依赖安装完成",
                "current": total,
                "total": total,
            }
        )
    return _tool_prepare_status(name)


def _tool_prepare_configure(name: str, values: dict) -> dict:
    from hermes_cli.config import save_env_value
    from hermes_cli.tools_config import TOOLSET_ENV_REQUIREMENTS, TOOL_CATEGORIES, _visible_providers, load_config

    toolset = str(name or "").strip()
    if not toolset:
        return {"name": "", "ready": False, "requires_prepare": True, "label": "工具组名称为空"}
    cfg = load_config()
    allowed: dict[str, dict] = {}
    cat = TOOL_CATEGORIES.get(toolset)
    if cat:
        for provider in _visible_providers(cat, cfg):
            for env_var in _tool_prepare_declared_env(provider):
                allowed[env_var["key"]] = env_var
    for key, url in TOOLSET_ENV_REQUIREMENTS.get(toolset) or []:
        normalized = str(key).strip()
        if normalized:
            allowed[normalized] = {
                "key": normalized,
                "prompt": normalized,
                "url": str(url or ""),
                "default": "",
            }

    if not allowed:
        return _tool_prepare_status(toolset, cfg)
    saved = []
    ignored = []
    for raw_key, raw_value in (values or {}).items():
        key = str(raw_key or "").strip()
        if key not in allowed:
            ignored.append(key)
            continue
        value = str(raw_value or "").strip()
        if not value:
            continue
        save_env_value(key, value)
        saved.append(key)

    status = _tool_prepare_status(toolset)
    status["configured"] = saved
    status["ignored"] = ignored
    return status


def _resolve_browser_cdp_url() -> str:
    """Return the configured browser CDP override without network I/O.

    ``/browser status`` must be fast — calling
    ``tools.browser_tool._get_cdp_override`` would invoke
    ``_resolve_cdp_override``, which performs an HTTP probe to
    ``.../json/version`` for discovery-style URLs.  That probe has
    a multi-second timeout and would block the TUI on a slow or
    unreachable host even though status only needs to report whether
    an override is set.

    Mirrors the env/config precedence of ``_get_cdp_override`` (env
    var first, then ``browser.cdp_url`` from config.yaml) without the
    websocket-resolution step, so the answer reflects user intent
    even when the configured host is not currently reachable.  The
    actual WS normalization happens in ``browser_navigate`` on the
    next tool call.
    """
    env_url = os.environ.get("BROWSER_CDP_URL", "").strip()
    if env_url:
        return env_url
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
        browser_cfg = cfg.get("browser", {}) if isinstance(cfg, dict) else {}
        if isinstance(browser_cfg, dict):
            return str(browser_cfg.get("cdp_url", "") or "").strip()
    except Exception:
        pass
    return ""


def _is_default_local_cdp(parsed) -> bool:
    """Match the discovery-style local default; never the concrete WS form.

    A user-supplied ``ws://127.0.0.1:9222/devtools/browser/<id>`` is a
    real, connectable endpoint — collapsing it to bare ``http://...:9222``
    would strip the path and break the connect.
    """
    try:
        port = parsed.port or 80
    except ValueError:
        return False

    discovery_path = parsed.path in {"", "/", "/json", "/json/version"}
    return (
        parsed.scheme in {"http", "ws"}
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and port == 9222
        and discovery_path
    )


def _http_ok(url: str, timeout: float) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def _probe_urls(parsed) -> list[str]:
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    root = f"{scheme}://{parsed.netloc}".rstrip("/")
    return [f"{root}/json/version", f"{root}/json"]


def _normalize_cdp_url(parsed) -> str:
    # Concrete ``/devtools/browser/<id>`` endpoints (Browserbase et al.)
    # are connectable as-is. Discovery-style inputs collapse to bare
    # ``scheme://host:port`` so ``_resolve_cdp_override`` can append
    # ``/json/version`` later without doubling the path.
    if parsed.path.startswith("/devtools/browser/"):
        return parsed.geturl()
    return parsed._replace(path="", params="", query="", fragment="").geturl()


def _failure_messages(url: str, port: int, system: str) -> list[str]:
    from hermes_cli.browser_connect import manual_chrome_debug_command

    command = manual_chrome_debug_command(port, system)
    hint = (
        ["Start a Chromium-family browser with remote debugging, then retry /browser connect:", command]
        if command
        else [
            "No supported Chromium-family browser executable was found in this environment.",
            f"Install one or start a Chromium-family browser with --remote-debugging-port={port}, then retry /browser connect.",
        ]
    )
    return [
        f"Chromium-family browser is not reachable at {url}.",
        *hint,
        "Browser not connected — start a Chromium-family browser with remote debugging and retry /browser connect",
    ]


@method("browser.manage")
def _(rid, params: dict) -> dict:
    action = params.get("action", "status")

    if action == "status":
        url = _resolve_browser_cdp_url()
        return _ok(rid, {"connected": bool(url), "url": url})

    if action == "disconnect":
        return _browser_disconnect(rid)

    if action != "connect":
        return _err(rid, 4015, f"unknown action: {action}")

    return _browser_connect(rid, params)


@method("platforms.manage")
def _(rid, params: dict) -> dict:
    action = str(params.get("action") or "catalog").strip().lower()
    platform = str(params.get("platform") or params.get("platform_id") or "").strip()
    try:
        from tui_gateway.services.platform_connections import (
            approve_pairing,
            channel_directory,
            connect_platform,
            disconnect_platform,
            patch_platform_config,
            platform_accounts,
            platform_catalog,
            platform_pairings,
            platform_schema,
            platform_status,
            poll_qr_login,
            revoke_pairing,
            start_gateway_runtime,
            start_qr_login,
        )

        if action == "catalog":
            return _ok(rid, platform_catalog(_load_cfg))
        if action == "status":
            return _ok(rid, platform_status(_load_cfg))
        if action == "schema":
            return _ok(rid, platform_schema(platform, _load_cfg))
        if action == "patch_config":
            patch = params.get("config") or params.get("patch") or {}
            return _ok(rid, patch_platform_config(platform, patch, _load_cfg, _save_cfg))
        if action == "connect":
            return _ok(rid, connect_platform(platform, _load_cfg, _save_cfg))
        if action in {"gateway.start", "gateway.ensure"}:
            force_restart = bool(params.get("force_restart") or params.get("forceRestart"))
            return _ok(rid, start_gateway_runtime(_load_cfg, force_restart=force_restart))
        if action == "disconnect":
            return _ok(rid, disconnect_platform(platform, _load_cfg, _save_cfg))
        if action == "accounts":
            return _ok(rid, platform_accounts(platform, _load_cfg))
        if action in {"pairings.list", "pairings"}:
            return _ok(rid, platform_pairings(platform or None))
        if action == "pairings.approve":
            return _ok(rid, approve_pairing(platform, str(params.get("code") or "")))
        if action == "pairings.revoke":
            return _ok(rid, revoke_pairing(platform, str(params.get("user_id") or "")))
        if action in {"directory.list", "directory"}:
            return _ok(rid, channel_directory(platform or None))
        if action in {"qr.start", "qr_login.start"}:
            return _ok(rid, start_qr_login(platform))
        if action in {"qr.status", "qr_login.status"}:
            return _ok(rid, poll_qr_login(platform, str(params.get("flow_id") or params.get("flowId") or ""), _load_cfg, _save_cfg))
        return _err(rid, 4020, f"unknown platforms action: {action}")
    except Exception as e:
        return _err(rid, 5036, str(e))


def _browser_connect(rid, params: dict) -> dict:
    import platform

    from hermes_cli.browser_connect import DEFAULT_BROWSER_CDP_URL
    from tools.browser_tool import cleanup_all_browsers
    from urllib.parse import urlparse

    raw_url = params.get("url")
    if raw_url is not None and not isinstance(raw_url, str):
        return _err(
            rid, 4015, f"browser url must be a string, got {type(raw_url).__name__}"
        )
    url = (raw_url or "").strip() or DEFAULT_BROWSER_CDP_URL

    sid = params.get("session_id") or ""
    system = platform.system()
    messages: list[str] = []

    def announce(message: str, *, level: str = "info") -> None:
        messages.append(message)
        # Without a session id the TUI prints `messages` from the
        # response; emitting an event would double-render. Only stream
        # progress when there's a real session to scope it to.
        if sid:
            _emit("browser.progress", sid, {"message": message, "level": level})

    parsed = urlparse(url if "://" in url else f"http://{url}")
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        return _err(rid, 4015, f"unsupported browser url: {url}")
    if not parsed.hostname:
        return _err(rid, 4015, f"missing host in browser url: {url}")
    try:
        port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
    except ValueError:
        return _err(rid, 4015, f"invalid port in browser url: {url}")

    # Always normalize default-local to 127.0.0.1:9222 so downstream
    # comparisons + messaging match what we'll actually persist.
    if _is_default_local_cdp(parsed):
        url = DEFAULT_BROWSER_CDP_URL
        parsed = urlparse(url)
        port = parsed.port or 9222

    try:
        # ws[s]://.../devtools/browser/<id> endpoints (hosted CDP
        # providers) don't serve the HTTP discovery path; just check
        # TCP-level reachability and let browser_navigate handshake.
        if parsed.scheme in {"ws", "wss"} and parsed.path.startswith(
            "/devtools/browser/"
        ):
            import socket

            try:
                with socket.create_connection((parsed.hostname, port), timeout=2.0):
                    pass
            except OSError as e:
                return _err(rid, 5031, f"could not reach browser CDP at {url}: {e}")
        else:
            probes = _probe_urls(parsed)
            ok = any(_http_ok(p, timeout=2.0) for p in probes)

            if not ok and _is_default_local_cdp(parsed):
                from hermes_cli.browser_connect import try_launch_chrome_debug

                announce(
                    "Chromium-family browser isn't running with remote debugging — attempting to launch..."
                )

                if try_launch_chrome_debug(port, system):
                    for _ in range(20):
                        time.sleep(0.5)
                        if any(_http_ok(p, timeout=1.0) for p in probes):
                            ok = True
                            break

                if ok:
                    announce(f"Chromium-family browser launched and listening on port {port}")
                else:
                    for line in _failure_messages(url, port, system)[1:]:
                        announce(line, level="error")
                    return _ok(
                        rid, {"connected": False, "url": url, "messages": messages}
                    )
            elif not ok:
                return _err(rid, 5031, f"could not reach browser CDP at {url}")
            elif _is_default_local_cdp(parsed):
                announce(f"Chromium-family browser is already listening on port {port}")

        normalized = _normalize_cdp_url(parsed)

        # Order matters: reap sessions BEFORE publishing the new env
        # so an in-flight tool call sees the old supervisor closed,
        # then again AFTER so the default task's cached supervisor
        # is drained against the new URL.
        cleanup_all_browsers()
        os.environ["BROWSER_CDP_URL"] = normalized
        cleanup_all_browsers()
    except Exception as e:
        return _err(rid, 5031, str(e))

    payload: dict[str, object] = {"connected": True, "url": normalized}
    if messages:
        payload["messages"] = messages
    return _ok(rid, payload)


def _browser_disconnect(rid) -> dict:
    # Reap, drop the env override, reap again — closes the same swap
    # window covered by ``_browser_connect``.
    def reap() -> None:
        try:
            from tools.browser_tool import cleanup_all_browsers

            cleanup_all_browsers()
        except Exception:
            pass

    reap()
    os.environ.pop("BROWSER_CDP_URL", None)
    reap()
    return _ok(rid, {"connected": False})


def _plugin_market_rows() -> list[dict]:
    """Return every bundled/user plugin with profile-scoped runtime state.

    Discovery and mutations deliberately stay in Hermes. Desktop clients only
    receive a serializable control-plane projection and never inspect plugin
    directories or rewrite profile config themselves.
    """
    from pathlib import Path

    from hermes_cli.config import get_hermes_home
    from hermes_cli.plugins_cmd import (
        _discover_all_plugins,
        _get_disabled_set,
        _get_enabled_set,
        _missing_requires_env_names,
        _read_manifest,
    )

    enabled = _get_enabled_set()
    disabled = _get_disabled_set()
    user_root = (get_hermes_home() / "plugins").resolve()
    rows: list[dict] = []
    for name, version, description, source, directory, canonical_key in _discover_all_plugins():
        path = Path(directory)
        manifest = _read_manifest(path) if path.is_dir() else {}
        try:
            path.resolve().relative_to(user_root)
            user_owned = True
        except ValueError:
            user_owned = False

        requirements = manifest.get("requires_env") or []
        requirement_rows: list[dict] = []
        for requirement in requirements:
            if isinstance(requirement, str):
                requirement_rows.append({"name": requirement})
            elif isinstance(requirement, dict) and requirement.get("name"):
                requirement_rows.append(
                    {
                        "name": str(requirement["name"]),
                        "description": str(requirement.get("description") or ""),
                        "url": str(requirement.get("url") or ""),
                        "secret": bool(requirement.get("secret", False)),
                    }
                )

        runtime_status = (
            "disabled"
            if canonical_key in disabled or name in disabled
            else "enabled"
            if canonical_key in enabled or name in enabled
            else "inactive"
        )
        skill_rows = manifest.get("skills") or []
        skill_names = [
            str(item.get("name") or item.get("path") or "").strip()
            if isinstance(item, dict)
            else str(item).strip()
            for item in skill_rows
        ]
        mcp_rows = manifest.get("mcp_catalog") or []
        mcp_entries = [
            str(item.get("name") or item.get("path") or "").strip()
            if isinstance(item, dict)
            else str(item).strip()
            for item in mcp_rows
        ]
        rows.append(
            {
                # Desktop mutations use the canonical loader key.  The manifest
                # name remains presentation metadata because nested bundled
                # plugins can share a leaf name across categories.
                "name": canonical_key,
                "manifest_name": name,
                "display_name": str(
                    manifest.get("display_name")
                    or manifest.get("label")
                    or manifest.get("title")
                    or name
                ),
                "version": str(version or manifest.get("version") or ""),
                "description": str(description or manifest.get("description") or ""),
                "author": str(manifest.get("author") or ""),
                "source": source,
                "source_url": str(
                    manifest.get("homepage")
                    or manifest.get("repository")
                    or manifest.get("source")
                    or ""
                ),
                "runtime_status": runtime_status,
                "enabled": runtime_status == "enabled",
                "hooks": [str(item) for item in manifest.get("hooks") or []],
                "provides_tools": [str(item) for item in manifest.get("provides_tools") or []],
                "skills": [item for item in skill_names if item],
                "mcp_catalog": [item for item in mcp_entries if item],
                "requires_env": requirement_rows,
                "missing_env": _missing_requires_env_names(manifest),
                "can_remove": source in {"user", "git"} and user_owned,
                "can_update": source in {"user", "git"} and user_owned and (path / ".git").exists(),
            }
        )
    return rows


@method("plugins.list")
def _(rid, params: dict) -> dict:
    try:
        rows = _plugin_market_rows()
        return _ok(
            rid,
            {
                "plugins": rows,
                "summary": {
                    "total": len(rows),
                    "enabled": sum(row["runtime_status"] == "enabled" for row in rows),
                    "available": sum(row["runtime_status"] == "inactive" for row in rows),
                    "needs_configuration": sum(bool(row["missing_env"]) for row in rows),
                },
            },
        )
    except Exception as e:
        return _err(rid, 5032, str(e))


@method("plugins.manage")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.plugins_cmd import (
            dashboard_install_plugin,
            dashboard_remove_user_plugin,
            dashboard_set_agent_plugin_enabled,
            dashboard_update_user_plugin,
        )

        action = str(params.get("action") or "").strip().lower()
        name = str(params.get("name") or "").strip()
        if action == "install":
            identifier = str(params.get("identifier") or "").strip()
            if not identifier:
                return _err(rid, 5033, "plugin identifier required")
            result = dashboard_install_plugin(
                identifier,
                force=bool(params.get("force", False)),
                # Third-party plugins are inert until the user explicitly enables them.
                enable=bool(params.get("enable", False)),
            )
        elif action == "set_enabled":
            if not name:
                return _err(rid, 5033, "plugin name required")
            result = dashboard_set_agent_plugin_enabled(
                name,
                enabled=bool(params.get("enabled", False)),
            )
        elif action == "update":
            if not name:
                return _err(rid, 5033, "plugin name required")
            result = dashboard_update_user_plugin(name)
        elif action == "remove":
            if not name:
                return _err(rid, 5033, "plugin name required")
            result = dashboard_remove_user_plugin(name)
        elif action == "rescan":
            return _ok(rid, {"ok": True, "plugins": _plugin_market_rows()})
        else:
            return _err(rid, 5033, f"unsupported plugin action: {action or '(empty)'}")

        if not result.get("ok"):
            return _err(rid, 5033, str(result.get("error") or "plugin operation failed"))
        result.pop("after_install_path", None)
        return _ok(rid, result)
    except Exception as e:
        return _err(rid, 5033, str(e))


def _start_capability_operation(params: dict) -> dict:
    from tui_gateway.services.capability_runtime import start_capability_operation

    return start_capability_operation(
        params,
        emit=lambda snapshot: _emit("capability.operation.updated", "", snapshot),
        sessions=_sessions,
        enabled_toolsets_loader=_load_enabled_toolsets,
    )


@method("capability.operation.start")
def _(rid, params: dict) -> dict:
    try:
        return _ok(rid, _start_capability_operation(params))
    except Exception as exc:
        return _err(rid, 5037, str(exc))


@method("capability.operation.get")
def _(rid, params: dict) -> dict:
    try:
        from tui_gateway.services.capability_operations import capability_operations

        operation_id = str(params.get("operation_id") or params.get("operationId") or "").strip()
        operation = capability_operations.get(operation_id)
        if operation is None:
            return _err(rid, 5037, f"capability operation not found: {operation_id}")
        return _ok(rid, operation)
    except Exception as exc:
        return _err(rid, 5037, str(exc))


@method("mcp.catalog.list")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli import mcp_catalog
        from hermes_cli.config import get_env_value
        from hermes_cli.mcp_config import _MCP_PRESETS, _get_mcp_servers

        installed = _get_mcp_servers()
        rows: list[dict] = []
        catalog_names: set[str] = set()
        for entry in mcp_catalog.list_catalog():
            catalog_names.add(entry.name)
            rows.append(
                {
                    "name": entry.name,
                    "description": entry.description,
                    "source": entry.source,
                    "kind": "catalog",
                    "transport": entry.transport.type,
                    "auth_type": entry.auth.type,
                    "required_env": [
                        {
                            "name": item.name,
                            "prompt": item.prompt,
                            "required": item.required,
                            "secret": item.secret,
                            "default": item.default,
                        }
                        for item in entry.auth.env
                    ],
                    "missing_env": [
                        item.name
                        for item in entry.auth.env
                        if item.required and not get_env_value(item.name)
                    ],
                    "command": entry.transport.command or "",
                    "args": list(entry.transport.args or []),
                    "url": entry.transport.url or "",
                    "needs_install": entry.install is not None,
                    "default_enabled": entry.tools.default_enabled,
                    "post_install": entry.post_install,
                    "installed": entry.name in installed,
                    "enabled": bool(
                        entry.name in installed
                        and installed[entry.name].get("enabled", True) is not False
                    ),
                }
            )
        for name, preset in sorted(_MCP_PRESETS.items()):
            if name in catalog_names:
                continue
            rows.append(
                {
                    "name": name,
                    "description": f"Hermes built-in {name} MCP preset.",
                    "source": "Hermes built-in preset",
                    "kind": "preset",
                    "transport": "http" if preset.get("url") else "stdio",
                    "auth_type": "none",
                    "required_env": [],
                    "command": str(preset.get("command") or ""),
                    "args": [str(item) for item in preset.get("args") or []],
                    "url": str(preset.get("url") or ""),
                    "needs_install": False,
                    "default_enabled": None,
                    "post_install": "",
                    "installed": name in installed,
                    "enabled": bool(
                        name in installed and installed[name].get("enabled", True) is not False
                    ),
                }
            )
        return _ok(
            rid,
            {
                "entries": rows,
                "diagnostics": [
                    {"name": name, "kind": kind, "message": message}
                    for name, kind, message in mcp_catalog.catalog_diagnostics()
                ],
            },
        )
    except Exception as e:
        return _err(rid, 5034, str(e))


@method("mcp.servers.list")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.mcp_config import _get_mcp_servers
        from tools.mcp_tool import get_mcp_status
        from tui_gateway.services.capability_runtime import mcp_server_row

        servers = _get_mcp_servers()
        runtime_by_name = {
            str(item.get("name") or ""): item
            for item in get_mcp_status()
            if isinstance(item, dict) and item.get("name")
        }
        return _ok(
            rid,
            {
                "servers": [
                    mcp_server_row(name, config, runtime_by_name.get(name))
                    for name, config in sorted(servers.items())
                ]
            },
        )
    except Exception as e:
        return _err(rid, 5035, str(e))


@method("mcp.manage")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli import mcp_catalog
        from hermes_cli.config import get_env_value, load_config, save_config, save_env_value
        from hermes_cli.mcp_config import (
            _get_mcp_servers,
            _remove_mcp_server,
            _save_mcp_server,
        )
        from tui_gateway.services.capability_runtime import probe_mcp_capabilities

        action = str(params.get("action") or "").strip().lower()
        name = str(params.get("name") or "").strip()
        if action == "install":
            entry = mcp_catalog.get_entry(name)
            if entry is None:
                from hermes_cli.mcp_config import _MCP_PRESETS

                preset = _MCP_PRESETS.get(name)
                if preset is None:
                    return _err(rid, 5036, f"unknown MCP catalog entry: {name}")
                if not _save_mcp_server(name, dict(preset)):
                    return _err(rid, 5036, "MCP preset configuration rejected")
                return _ok(rid, {"ok": True, "name": name, "reload_required": True})
            supplied_env = params.get("env") if isinstance(params.get("env"), dict) else {}
            for key, value in supplied_env.items():
                if str(value):
                    save_env_value(str(key), str(value))
            missing = [
                item.name
                for item in entry.auth.env
                if item.required and not get_env_value(item.name)
            ]
            if missing:
                return _ok(rid, {"ok": False, "name": name, "missing_env": missing})
            install_result = mcp_catalog.install_entry(
                entry,
                enable=bool(params.get("enabled", True)),
            )
            return _ok(rid, {**install_result, "reload_required": True})
        if action == "add":
            server = params.get("server") if isinstance(params.get("server"), dict) else {}
            if not name or not server:
                return _err(rid, 5036, "MCP name and server configuration required")
            if name in _get_mcp_servers():
                return _err(rid, 5036, f"MCP server already exists: {name}")
            if not _save_mcp_server(name, dict(server)):
                return _err(rid, 5036, "MCP server configuration rejected")
            return _ok(rid, {"ok": True, "name": name, "reload_required": True})
        if action == "set_enabled":
            config = load_config()
            servers = config.get("mcp_servers")
            if not isinstance(servers, dict) or name not in servers:
                return _err(rid, 5036, f"MCP server not found: {name}")
            servers[name]["enabled"] = bool(params.get("enabled", False))
            save_config(config)
            return _ok(rid, {"ok": True, "name": name, "reload_required": True})
        if action == "set_tools":
            requested_tools = params.get("tools")
            if not isinstance(requested_tools, list):
                return _err(rid, 5036, "MCP tools must be a list")
            config = load_config()
            servers = config.get("mcp_servers")
            if not isinstance(servers, dict) or name not in servers:
                return _err(rid, 5036, f"MCP server not found: {name}")
            tools = servers[name].get("tools")
            if not isinstance(tools, dict):
                tools = {}
                servers[name]["tools"] = tools
            tools["include"] = list(
                dict.fromkeys(
                    str(tool_name).strip()
                    for tool_name in requested_tools
                    if str(tool_name).strip()
                )
            )
            tools.pop("exclude", None)
            save_config(config)
            return _ok(
                rid,
                {
                    "ok": True,
                    "name": name,
                    "enabled_tools": tools["include"],
                    "reload_required": True,
                },
            )
        if action == "remove":
            if not _remove_mcp_server(name):
                return _err(rid, 5036, f"MCP server not found: {name}")
            return _ok(rid, {"ok": True, "name": name, "reload_required": True})
        if action == "test":
            server = _get_mcp_servers().get(name)
            if not server:
                return _err(rid, 5036, f"MCP server not found: {name}")
            return _ok(rid, probe_mcp_capabilities(name, server))
        return _err(rid, 5036, f"unsupported MCP action: {action or '(empty)'}")
    except Exception as e:
        return _err(rid, 5036, str(e))


@method("config.show")
def _(rid, params: dict) -> dict:
    try:
        cfg = _load_cfg()
        model = _resolve_model()
        api_key = os.environ.get("HERMES_API_KEY", "") or cfg.get("api_key", "")
        masked = f"****{api_key[-4:]}" if len(api_key) > 4 else "(not set)"
        base_url = os.environ.get("HERMES_BASE_URL", "") or cfg.get("base_url", "")

        sections = [
            {
                "title": "Model",
                "rows": [
                    ["Model", model],
                    ["Base URL", base_url or "(default)"],
                    ["API Key", masked],
                ],
            },
            {
                "title": "Agent",
                "rows": [
                    ["Max Turns", str(_cfg_max_turns(cfg, 90))],
                    ["Toolsets", ", ".join(cfg.get("enabled_toolsets", [])) or "all"],
                    ["Verbose", str(cfg.get("verbose", False))],
                ],
            },
            {
                "title": "Environment",
                "rows": [
                    ["Working Dir", os.getcwd()],
                    ["Config File", str(Path(_active_hermes_home()) / "config.yaml")],
                ],
            },
        ]
        return _ok(rid, {"sections": sections})
    except Exception as e:
        return _err(rid, 5030, str(e))


@method("tools.list")
def _(rid, params: dict) -> dict:
    try:
        return _ok(rid, {"toolsets": _list_configurable_toolsets(params)})
    except Exception as e:
        return _err(rid, 5031, str(e))


@method("tools.prepare")
def _(rid, params: dict) -> dict:
    action = str(params.get("action") or "status").strip().lower()
    names = [
        str(name).strip()
        for name in params.get("names", []) or []
        if str(name).strip()
    ]
    if action not in {"status", "install", "configure"}:
        return _err(rid, 4017, f"unknown tools.prepare action: {action}")
    if not names:
        return _err(rid, 4018, "names required")
    try:
        values = params.get("values") or {}
        statuses = []
        for name in names:
            if action == "install":
                statuses.append(
                    _tool_prepare_install(
                        name,
                        progress_callback=lambda payload, tool_name=name: _tool_prepare_progress(params, tool_name, payload),
                    )
                )
            elif action == "configure":
                per_tool_values = (
                    values.get(name)
                    if isinstance(values, dict) and isinstance(values.get(name), dict)
                    else values
                )
                statuses.append(_tool_prepare_configure(name, per_tool_values))
            else:
                statuses.append(_tool_prepare_status(name))
        return _ok(
            rid,
            {
                "toolsets": statuses,
                "ready": all(item.get("ready") for item in statuses),
                "prepared": [item.get("name") for item in statuses if item.get("ready")],
            },
        )
    except Exception as e:
        return _err(rid, 5037, str(e))


@method("tools.show")
def _(rid, params: dict) -> dict:
    try:
        from model_tools import get_toolset_for_tool, get_tool_definitions

        session = _sessions.get(params.get("session_id", ""))
        enabled = (
            getattr(session["agent"], "enabled_toolsets", None)
            if session
            else _load_enabled_toolsets()
        )
        tools = get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True)
        sections = {}

        for tool in sorted(tools, key=lambda t: t["function"]["name"]):
            name = tool["function"]["name"]
            desc = str(tool["function"].get("description", "") or "").split("\n")[0]
            if ". " in desc:
                desc = desc[: desc.index(". ") + 1]
            sections.setdefault(get_toolset_for_tool(name) or "unknown", []).append(
                {
                    "name": name,
                    "description": desc,
                }
            )

        return _ok(
            rid,
            {
                "sections": [
                    {"name": name, "tools": rows}
                    for name, rows in sorted(sections.items())
                ],
                "total": len(tools),
            },
        )
    except Exception as e:
        return _err(rid, 5034, str(e))


@method("tools.configure")
def _(rid, params: dict) -> dict:
    action = str(params.get("action", "") or "").strip().lower()
    targets = [
        str(name).strip() for name in params.get("names", []) or [] if str(name).strip()
    ]
    if action not in {"disable", "enable"}:
        return _err(rid, 4017, f"unknown tools action: {action}")
    if not targets:
        return _err(rid, 4018, "names required")

    try:
        from hermes_cli.config import load_config, save_config
        from hermes_cli.tools_config import (
            CONFIGURABLE_TOOLSETS,
            _apply_mcp_change,
            _apply_toolset_change,
            _get_platform_tools,
            _get_plugin_toolset_keys,
        )

        cfg = load_config()
        valid_toolsets = {
            ts_key for ts_key, _, _ in CONFIGURABLE_TOOLSETS
        } | _get_plugin_toolset_keys()
        toolset_targets = [name for name in targets if ":" not in name]
        mcp_targets = [name for name in targets if ":" in name]
        unknown = [name for name in toolset_targets if name not in valid_toolsets]
        toolset_targets = [name for name in toolset_targets if name in valid_toolsets]
        blocked_prepare = []
        if action == "enable":
            prepared_targets = []
            for name in toolset_targets:
                status = _tool_prepare_status(name, cfg)
                if status.get("requires_prepare") and not status.get("ready"):
                    blocked_prepare.append(
                        {
                            "name": name,
                            "label": status.get("label") or "toolset is not prepared",
                            "installable": bool(status.get("installable")),
                            "requires_configuration": bool(status.get("requires_configuration")),
                        }
                    )
                else:
                    prepared_targets.append(name)
            toolset_targets = prepared_targets

        if toolset_targets:
            _apply_toolset_change(cfg, "cli", toolset_targets, action)

        missing_servers = (
            _apply_mcp_change(cfg, mcp_targets, action) if mcp_targets else set()
        )
        save_config(cfg)

        session = _sessions.get(params.get("session_id", ""))
        info = (
            _reset_session_agent(params.get("session_id", ""), session)
            if session
            else None
        )
        enabled = sorted(
            _get_platform_tools(load_config(), "cli", include_default_mcp_servers=False)
        )
        changed = [
            name
            for name in targets
            if name not in unknown
            and name not in {item["name"] for item in blocked_prepare}
            and (":" not in name or name.split(":", 1)[0] not in missing_servers)
        ]

        return _ok(
            rid,
            {
                "changed": changed,
                "enabled_toolsets": enabled,
                "info": info,
                "missing_servers": sorted(missing_servers),
                "blocked_prepare": blocked_prepare,
                "reset": bool(session),
                "unknown": unknown,
            },
        )
    except Exception as e:
        return _err(rid, 5035, str(e))


@method("toolsets.list")
def _(rid, params: dict) -> dict:
    try:
        return _ok(rid, {"toolsets": _list_configurable_toolsets(params)})
    except Exception as e:
        return _err(rid, 5032, str(e))


@method("agents.list")
def _(rid, params: dict) -> dict:
    try:
        from tools.process_registry import process_registry

        procs = process_registry.list_sessions()
        return _ok(
            rid,
            {
                "processes": [
                    {
                        "session_id": p["session_id"],
                        "command": p["command"][:80],
                        "status": p["status"],
                        "uptime": p["uptime_seconds"],
                    }
                    for p in procs
                ]
            },
        )
    except Exception as e:
        return _err(rid, 5033, str(e))


@method("cron.manage")
def _(rid, params: dict) -> dict:
    try:
        from tui_gateway.services.dovie_cron_jobs import manage_cron

        return _ok(rid, manage_cron(params))
    except Exception as e:
        return _err(rid, 5023, str(e))


class _QuietConsole:
    def print(self, *a, **k):
        pass


def _skill_query(params: dict) -> str:
    return str(
        params.get("query")
        or params.get("name")
        or params.get("identifier")
        or ""
    ).strip()


def _clear_skill_prompt_cache() -> None:
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache

        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        pass


def _invalidate_live_agent_skill_prompts() -> int:
    """Force existing sessions to rebuild skill-aware system prompts."""
    try:
        from agent.system_prompt import invalidate_system_prompt
    except Exception:
        return 0

    count = 0
    for session in list(_sessions.values()):
        if not isinstance(session, dict):
            continue
        agent = session.get("agent")
        if agent is None:
            continue
        try:
            invalidate_system_prompt(agent)
            count += 1
        except Exception:
            pass
    return count


def _sync_skill_module_paths_to_active_home() -> None:
    """Keep legacy skill modules aligned with the active Dovie profile home.

    The gateway can serve many Dovie profile/draft scopes in one process. Some
    older skill modules cache paths such as SKILLS_DIR at import time, so an
    already-imported module must be realigned after server.py enters the
    request profile context.
    """
    import sys
    from pathlib import Path

    home = Path(_active_hermes_home()).expanduser().resolve()
    skills_dir = home / "skills"

    module = sys.modules.get("tools.skills_tool")
    if module is not None:
        module.HERMES_HOME = home
        module.SKILLS_DIR = skills_dir

    module = sys.modules.get("tools.skill_manager_tool")
    if module is not None:
        module.HERMES_HOME = home
        module.SKILLS_DIR = skills_dir

    module = sys.modules.get("tools.skills_sync")
    if module is not None:
        module.HERMES_HOME = home
        module.SKILLS_DIR = skills_dir
        module.MANIFEST_FILE = skills_dir / ".bundled_manifest"

    module = sys.modules.get("tools.skills_hub")
    if module is not None:
        hub_dir = skills_dir / ".hub"
        module.HERMES_HOME = home
        module.SKILLS_DIR = skills_dir
        module.HUB_DIR = hub_dir
        module.LOCK_FILE = hub_dir / "lock.json"
        module.QUARANTINE_DIR = hub_dir / "quarantine"
        module.AUDIT_LOG = hub_dir / "audit.log"
        module.TAPS_FILE = hub_dir / "taps.json"
        module.INDEX_CACHE_DIR = hub_dir / "index-cache"


def _list_installed_skill_records(
    *,
    source_filter: str = "all",
    enabled_only: bool = False,
    platform: str | None = None,
) -> dict:
    from pathlib import Path

    _sync_skill_module_paths_to_active_home()

    from agent.skill_utils import get_disabled_skill_names
    from tools.skills_hub import HubLockFile, SKILLS_DIR, ensure_hub_dirs
    from tools.skills_sync import _read_manifest
    from tools.skills_tool import _find_all_skills

    ensure_hub_dirs()
    hub_installed = {entry["name"]: entry for entry in HubLockFile().list_installed()}
    builtin_names = set(_read_manifest())
    disabled_names = get_disabled_skill_names(platform=platform)
    grouped: dict[str, list[str]] = {}
    items: list[dict] = []
    stats = {
        "total_skills": 0,
        "enabled_skills": 0,
        "disabled_skills": 0,
        "skills_by_source": {},
        "skills_by_category": {},
    }

    for skill in sorted(
        _find_all_skills(skip_disabled=True),
        key=lambda row: (row.get("category") or "", row.get("name") or ""),
    ):
        name = str(skill.get("name") or "").strip()
        if not name:
            continue

        category = str(skill.get("category") or "uncategorized")
        hub_entry = hub_installed.get(name)
        if hub_entry:
            source_type = "hub"
            source_display = str(hub_entry.get("source") or "hub")
            trust = str(hub_entry.get("trust_level") or "community")
            identifier = str(hub_entry.get("identifier") or "")
            install_path = str(hub_entry.get("install_path") or "")
        elif name in builtin_names:
            source_type = "builtin"
            source_display = "builtin"
            trust = "builtin"
            identifier = ""
            install_path = ""
        else:
            source_type = "local"
            source_display = "local"
            trust = "local"
            identifier = ""
            install_path = ""

        if source_filter != "all" and source_filter != source_type:
            continue

        enabled = name not in disabled_names
        if enabled_only and not enabled:
            continue

        grouped.setdefault(category, []).append(name)
        skill_dir = Path(str(skill.get("skill_dir"))).resolve() if skill.get("skill_dir") else None
        modified_at = skill_dir.stat().st_mtime if skill_dir and skill_dir.exists() else 0.0
        stats["total_skills"] += 1
        stats["enabled_skills" if enabled else "disabled_skills"] += 1
        stats["skills_by_source"][source_type] = (
            stats["skills_by_source"].get(source_type, 0) + 1
        )
        stats["skills_by_category"][category] = (
            stats["skills_by_category"].get(category, 0) + 1
        )

        items.append(
            {
                "name": name,
                "description": str(skill.get("description") or ""),
                "category": category,
                "source": source_display,
                "source_type": source_type,
                "trust": trust,
                "identifier": identifier,
                "install_path": install_path or (
                    str(skill_dir.relative_to(Path(SKILLS_DIR).resolve()))
                    if skill_dir and skill_dir.is_relative_to(Path(SKILLS_DIR).resolve())
                    else ""
                ),
                "modified_at": modified_at,
                "installed": True,
                "enabled": enabled,
                "status": "enabled" if enabled else "disabled",
                "install_status": "installed",
                "managed": bool(hub_entry),
                "can_uninstall": bool(hub_entry),
                "can_delete": source_type in {"hub", "local"},
                "can_update": bool(hub_entry),
                "can_toggle": True,
            }
        )

    return {"skills": grouped, "items": items, "stats": stats}


def _update_skill_enabled(name: str, enabled: bool, platform: str | None = None) -> dict:
    from hermes_cli.config import load_config, save_config

    if not name:
        raise ValueError("skill name required")

    config = load_config()
    skills_cfg = config.setdefault("skills", {})

    if platform:
        platform_disabled = skills_cfg.setdefault("platform_disabled", {})
        current = platform_disabled.get(platform, skills_cfg.get("disabled", []))
        disabled = {str(item) for item in current or [] if str(item).strip()}
        if enabled:
            disabled.discard(name)
        else:
            disabled.add(name)
        platform_disabled[platform] = sorted(disabled)
    else:
        disabled = {str(item) for item in skills_cfg.get("disabled", []) if str(item).strip()}
        if enabled:
            disabled.discard(name)
        else:
            disabled.add(name)
        skills_cfg["disabled"] = sorted(disabled)

    save_config(config)
    _clear_skill_prompt_cache()
    return {"name": name, "enabled": enabled, "platform": platform}


def _check_skill_requirements(name: str, session_id: str = "") -> dict:
    if not name:
        raise ValueError("skill name required")

    _sync_skill_module_paths_to_active_home()

    from tools.skills_tool import skill_view

    raw = skill_view(name, task_id=session_id or None, preprocess=False)
    parsed = json.loads(raw)
    if not parsed.get("success"):
        return {
            "name": name,
            "satisfied": False,
            "error": parsed.get("error") or "skill not found",
        }

    missing_env = parsed.get("missing_required_environment_variables") or []
    missing_files = parsed.get("missing_credential_files") or []
    missing_commands = parsed.get("missing_required_commands") or []
    return {
        "name": parsed.get("name") or name,
        "satisfied": not (missing_env or missing_files or missing_commands),
        "readiness_status": parsed.get("readiness_status"),
        "setup_needed": bool(parsed.get("setup_needed")),
        "missing_env": missing_env,
        "missing_credential_files": missing_files,
        "missing_commands": missing_commands,
        "setup_help": parsed.get("setup_help"),
        "setup_note": parsed.get("setup_note"),
    }


@method("skills.list")
def _(rid, params: dict) -> dict:
    try:
        with _SKILL_HOME_LOCK:
            return _ok(
                rid,
                _list_installed_skill_records(
                    source_filter=str(params.get("source") or "all"),
                    enabled_only=bool(params.get("enabled_only")),
                    platform=params.get("platform") or None,
                ),
            )
    except Exception as exc:
        return _err(rid, 4020, f"skills.list failed: {exc}")


@method("skills.inspect")
def _(rid, params: dict) -> dict:
    query = _skill_query(params)
    try:
        with _SKILL_HOME_LOCK:
            _sync_skill_module_paths_to_active_home()
            from tools.skill_package_lifecycle import inspect_installed_skill

            return _ok(
                rid,
                {
                    "info": inspect_installed_skill(
                        query,
                        str(params.get("install_path") or ""),
                    )
                },
            )
    except Exception as exc:
        return _err(rid, 4026, f"skills.inspect failed: {exc}")


@method("skills.manage")
def _(rid, params: dict) -> dict:
    action = params.get("action", "list")
    query = _skill_query(params)
    lock_acquired = False
    try:
        _SKILL_HOME_LOCK.acquire()
        lock_acquired = True
        _sync_skill_module_paths_to_active_home()

        if action == "list":
            return _ok(
                rid,
                _list_installed_skill_records(
                    source_filter=str(params.get("source") or "all"),
                    enabled_only=bool(params.get("enabled_only")),
                    platform=params.get("platform") or None,
                ),
            )
        if action == "search":
            from tools.skills_hub import (
                GitHubAuth,
                create_source_router,
                parallel_search_sources,
            )

            raw, _source_counts, _timed_out_ids = parallel_search_sources(
                create_source_router(GitHubAuth()),
                query=query,
                source_filter="all",
                overall_timeout=6,
            )
            raw = raw[:20]
            return _ok(
                rid,
                {
                    "results": [
                        {
                            "name": r.name,
                            "description": r.description,
                            "source": r.source,
                            "identifier": r.identifier,
                            "trust": r.trust_level,
                            "tags": list(r.tags) if r.tags else [],
                        }
                        for r in raw
                    ]
                },
            )
        if action == "install":
            from hermes_cli.skills_hub import do_install

            do_install(
                query,
                category=str(params.get("category") or ""),
                force=bool(params.get("force")),
                name_override=str(params.get("name_override") or params.get("name") or ""),
                skip_confirm=True,
                console=_QuietConsole(),
            )
            _clear_skill_prompt_cache()
            return _ok(rid, {"installed": True, "name": query})
        if action in {"copy_local", "copy_installed"}:
            target_home = str(
                params.get("target_hermes_home")
                or params.get("targetHermesHome")
                or params.get("hermes_home")
                or params.get("hermesHome")
                or ""
            ).strip()
            if not target_home:
                return _err(rid, 4006, "target_hermes_home required")

            from tools.skill_package_lifecycle import copy_installed_skill_to_home

            copied = copy_installed_skill_to_home(query, target_home)
            return _ok(rid, {"copied": True, **copied})
        if action in {"import_archive", "import_local_archive"}:
            archive_path = str(
                params.get("archive_path")
                or params.get("archivePath")
                or params.get("path")
                or ""
            ).strip()
            if not archive_path:
                return _err(rid, 4006, "archive_path required")

            from tools.skill_package_lifecycle import import_skill_archive_to_home

            imported = import_skill_archive_to_home(
                archive_path,
                category=str(params.get("category") or ""),
                name=str(params.get("name") or params.get("name_override") or ""),
            )
            _clear_skill_prompt_cache()
            return _ok(rid, {"imported": True, **imported})
        if action == "uninstall":
            from tools.skills_hub import uninstall_skill

            success, message = uninstall_skill(query)
            if not success:
                return _err(rid, 4021, message)
            _clear_skill_prompt_cache()
            return _ok(rid, {"uninstalled": True, "name": query, "message": message})
        if action == "delete":
            from tools.skills_hub import delete_skill_package

            success, message = delete_skill_package(query)
            if not success:
                return _err(rid, 4025, message)
            _clear_skill_prompt_cache()
            return _ok(rid, {"deleted": True, "name": query, "message": message})
        if action == "update":
            from hermes_cli.skills_hub import _derive_category_from_install_path, do_install
            from tools.skills_hub import HubLockFile, check_for_skill_updates

            lock = HubLockFile()
            updates = [
                entry
                for entry in check_for_skill_updates(name=query or None, lock=lock)
                if entry.get("status") == "update_available"
            ]
            updated: list[dict] = []
            for entry in updates:
                installed = lock.get_installed(entry["name"])
                category = (
                    _derive_category_from_install_path(installed.get("install_path", ""))
                    if installed
                    else ""
                )
                do_install(
                    entry["identifier"],
                    category=category,
                    force=True,
                    skip_confirm=True,
                    console=_QuietConsole(),
                )
                updated.append(
                    {
                        "name": entry.get("name"),
                        "identifier": entry.get("identifier"),
                        "source": entry.get("source"),
                    }
                )
            if updated:
                _clear_skill_prompt_cache()
            return _ok(rid, {"updated": updated, "count": len(updated)})
        if action in {"check", "updates"}:
            from tools.skills_hub import check_for_skill_updates

            checks = []
            for entry in check_for_skill_updates(name=query or None):
                checks.append(
                    {
                        key: value
                        for key, value in entry.items()
                        if key != "bundle"
                    }
                )
            return _ok(rid, {"checks": checks, "count": len(checks)})
        if action in {"requirements", "check_requirements"}:
            return _ok(
                rid,
                {
                    "requirements": _check_skill_requirements(
                        query,
                        session_id=str(params.get("session_id") or ""),
                    )
                },
            )
        if action in {"set_enabled", "enable", "disable"}:
            enabled = action == "enable" or bool(params.get("enabled"))
            return _ok(
                rid,
                _update_skill_enabled(
                    query,
                    enabled,
                    platform=params.get("platform") or None,
                ),
            )
        if action == "browse":
            from hermes_cli.skills_hub import browse_skills

            pg = int(params.get("page", 0) or 0) or (
                int(query) if query.isdigit() else 1
            )
            return _ok(
                rid, browse_skills(page=pg, page_size=int(params.get("page_size", 20)))
            )
        if action == "inspect":
            from hermes_cli.skills_hub import inspect_skill

            return _ok(rid, {"info": inspect_skill(query) or {}})
        return _err(rid, 4017, f"unknown skills action: {action}")
    except Exception as e:
        return _err(rid, 5024, str(e))
    finally:
        if lock_acquired:
            _SKILL_HOME_LOCK.release()


def reload_skill_runtime_state() -> dict:
    """Reload this process's skill registries for the active profile home."""

    with _SKILL_HOME_LOCK:
        _sync_skill_module_paths_to_active_home()
        _clear_skill_prompt_cache()

        from agent.skill_commands import reload_skills

        result = reload_skills()
        invalidated_prompt_sessions = _invalidate_live_agent_skill_prompts()
        added = result.get("added") or []
        removed = result.get("removed") or []
        total = int(result.get("total") or 0)

        lines = ["Reloading skills..."]
        if not added and not removed:
            lines.append("No new skills detected.")
        if added:
            lines.append("Added skills:")
            lines.extend(f"  - {item.get('name', '')}" for item in added)
        if removed:
            lines.append("Removed skills:")
            lines.extend(f"  - {item.get('name', '')}" for item in removed)
        lines.append(f"{total} skill(s) available")
        return {
            "output": "\n".join(lines),
            "result": result,
            "invalidated_prompt_sessions": invalidated_prompt_sessions,
        }


@method("skills.reload")
def _(rid, params: dict) -> dict:
    try:
        return _ok(rid, reload_skill_runtime_state())
    except Exception as e:
        return _err(rid, 5025, str(e))
