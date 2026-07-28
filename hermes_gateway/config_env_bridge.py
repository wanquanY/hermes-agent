"""Bridge gateway config.yaml values into process environment."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def bridge_gateway_config_env(hermes_home: Path) -> None:
    hermes_home = Path(hermes_home)
    # Bridge config.yaml values into the environment so os.getenv() picks them up.
    # config.yaml is authoritative for terminal settings — overrides .env.
    config_path = hermes_home / 'config.yaml'
    if config_path.exists():
        try:
            import yaml as _yaml
            with open(config_path, encoding="utf-8") as _f:
                _cfg = _yaml.safe_load(_f) or {}
            # Expand ${ENV_VAR} references before bridging to env vars.
            from hermes_cli.config import _expand_env_vars
            _cfg = _expand_env_vars(_cfg)
            # Top-level simple values (fallback only — don't override .env)
            for _key, _val in _cfg.items():
                if isinstance(_val, (str, int, float, bool)) and _key not in os.environ:
                    os.environ[_key] = str(_val)
            # Terminal config is nested — bridge to TERMINAL_* env vars.
            # config.yaml overrides .env for these since it's the documented config path.
            _terminal_cfg = _cfg.get("terminal", {})
            if _terminal_cfg and isinstance(_terminal_cfg, dict):
                _terminal_env_map = {
                    "backend": "TERMINAL_ENV",
                    "cwd": "TERMINAL_CWD",
                    "timeout": "TERMINAL_TIMEOUT",
                    "lifetime_seconds": "TERMINAL_LIFETIME_SECONDS",
                    "docker_image": "TERMINAL_DOCKER_IMAGE",
                    "docker_forward_env": "TERMINAL_DOCKER_FORWARD_ENV",
                    "singularity_image": "TERMINAL_SINGULARITY_IMAGE",
                    "modal_image": "TERMINAL_MODAL_IMAGE",
                    "daytona_image": "TERMINAL_DAYTONA_IMAGE",
                    "vercel_runtime": "TERMINAL_VERCEL_RUNTIME",
                    "ssh_host": "TERMINAL_SSH_HOST",
                    "ssh_user": "TERMINAL_SSH_USER",
                    "ssh_port": "TERMINAL_SSH_PORT",
                    "ssh_key": "TERMINAL_SSH_KEY",
                    "container_cpu": "TERMINAL_CONTAINER_CPU",
                    "container_memory": "TERMINAL_CONTAINER_MEMORY",
                    "container_disk": "TERMINAL_CONTAINER_DISK",
                    "container_persistent": "TERMINAL_CONTAINER_PERSISTENT",
                    "docker_volumes": "TERMINAL_DOCKER_VOLUMES",
                    "docker_env": "TERMINAL_DOCKER_ENV",
                    "docker_mount_cwd_to_workspace": "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE",
                    "docker_run_as_host_user": "TERMINAL_DOCKER_RUN_AS_HOST_USER",
                    "sandbox_dir": "TERMINAL_SANDBOX_DIR",
                    "persistent_shell": "TERMINAL_PERSISTENT_SHELL",
                }
                for _cfg_key, _env_var in _terminal_env_map.items():
                    if _cfg_key in _terminal_cfg:
                        _val = _terminal_cfg[_cfg_key]
                        # Skip cwd placeholder values (".", "auto", "cwd") — the
                        # gateway resolves these to Path.home() later (line ~255).
                        # Writing the raw placeholder here would just be noise.
                        # Only bridge explicit absolute paths from config.yaml.
                        if _cfg_key == "cwd" and str(_val) in {".", "auto", "cwd"}:
                            continue
                        # Expand shell tilde in cwd so subprocess.Popen never
                        # receives a literal "~/" which the kernel rejects.
                        if _cfg_key == "cwd" and isinstance(_val, str):
                            _val = os.path.expanduser(_val)
                        if isinstance(_val, (list, dict)):
                            os.environ[_env_var] = json.dumps(_val)
                        else:
                            os.environ[_env_var] = str(_val)
            # Compression config is read directly from config.yaml by run_agent.py
            # and auxiliary_client.py — no env var bridging needed.
            # Auxiliary model/direct-endpoint overrides (vision, web_extract).
            # Each task has provider/model/base_url/api_key; bridge non-default values to env vars.
            _auxiliary_cfg = _cfg.get("auxiliary", {})
            if _auxiliary_cfg and isinstance(_auxiliary_cfg, dict):
                _aux_task_env = {
                    "vision": {
                        "provider": "AUXILIARY_VISION_PROVIDER",
                        "model": "AUXILIARY_VISION_MODEL",
                        "base_url": "AUXILIARY_VISION_BASE_URL",
                        "api_key": "AUXILIARY_VISION_API_KEY",
                    },
                    "web_extract": {
                        "provider": "AUXILIARY_WEB_EXTRACT_PROVIDER",
                        "model": "AUXILIARY_WEB_EXTRACT_MODEL",
                        "base_url": "AUXILIARY_WEB_EXTRACT_BASE_URL",
                        "api_key": "AUXILIARY_WEB_EXTRACT_API_KEY",
                    },
                    "approval": {
                        "provider": "AUXILIARY_APPROVAL_PROVIDER",
                        "model": "AUXILIARY_APPROVAL_MODEL",
                        "base_url": "AUXILIARY_APPROVAL_BASE_URL",
                        "api_key": "AUXILIARY_APPROVAL_API_KEY",
                    },
                }
                for _task_key, _env_map in _aux_task_env.items():
                    _task_cfg = _auxiliary_cfg.get(_task_key, {})
                    if not isinstance(_task_cfg, dict):
                        continue
                    _prov = str(_task_cfg.get("provider", "")).strip()
                    _model = str(_task_cfg.get("model", "")).strip()
                    _base_url = str(_task_cfg.get("base_url", "")).strip()
                    _api_key = str(_task_cfg.get("api_key", "")).strip()
                    if _prov and _prov != "auto":
                        os.environ[_env_map["provider"]] = _prov
                    if _model:
                        os.environ[_env_map["model"]] = _model
                    if _base_url:
                        os.environ[_env_map["base_url"]] = _base_url
                    if _api_key:
                        os.environ[_env_map["api_key"]] = _api_key
            # config.yaml is the documented, authoritative source for these
            # settings — it unconditionally wins over .env values. Previously
            # the guards below read `if X not in os.environ` and let stale
            # .env entries (e.g. HERMES_MAX_ITERATIONS=60 written by an old
            # `hermes setup` run) silently shadow the user's current config.
            # See PR #18413 / the 60-vs-500 max_turns incident.
            _agent_cfg = _cfg.get("agent", {})
            if _agent_cfg and isinstance(_agent_cfg, dict):
                if "max_turns" in _agent_cfg:
                    os.environ["HERMES_MAX_ITERATIONS"] = str(_agent_cfg["max_turns"])
                if "gateway_timeout" in _agent_cfg:
                    os.environ["HERMES_AGENT_TIMEOUT"] = str(_agent_cfg["gateway_timeout"])
                if "gateway_timeout_warning" in _agent_cfg:
                    os.environ["HERMES_AGENT_TIMEOUT_WARNING"] = str(_agent_cfg["gateway_timeout_warning"])
                if "gateway_notify_interval" in _agent_cfg:
                    os.environ["HERMES_AGENT_NOTIFY_INTERVAL"] = str(_agent_cfg["gateway_notify_interval"])
                if "restart_drain_timeout" in _agent_cfg:
                    os.environ["HERMES_RESTART_DRAIN_TIMEOUT"] = str(_agent_cfg["restart_drain_timeout"])
                if "gateway_auto_continue_freshness" in _agent_cfg:
                    os.environ["HERMES_AUTO_CONTINUE_FRESHNESS"] = str(
                        _agent_cfg["gateway_auto_continue_freshness"]
                    )
            _display_cfg = _cfg.get("display", {})
            if _display_cfg and isinstance(_display_cfg, dict):
                if "busy_input_mode" in _display_cfg:
                    os.environ["HERMES_GATEWAY_BUSY_INPUT_MODE"] = str(_display_cfg["busy_input_mode"])
                if "busy_ack_enabled" in _display_cfg:
                    os.environ["HERMES_GATEWAY_BUSY_ACK_ENABLED"] = str(_display_cfg["busy_ack_enabled"])
            # Timezone: bridge config.yaml → HERMES_TIMEZONE env var.
            _tz_cfg = _cfg.get("timezone", "")
            if _tz_cfg and isinstance(_tz_cfg, str):
                os.environ["HERMES_TIMEZONE"] = _tz_cfg.strip()
            # Security settings
            _security_cfg = _cfg.get("security", {})
            if isinstance(_security_cfg, dict):
                _redact = _security_cfg.get("redact_secrets")
                if _redact is not None:
                    os.environ["HERMES_REDACT_SECRETS"] = str(_redact).lower()
        except Exception as _bridge_err:
            # Previously this was silent (`except Exception: pass`), which
            # hid partial bridge failures and let .env defaults shadow
            # config.yaml values — users observed max_turns=500 in config
            # but a 60-iteration cap in practice. Surface the failure to
            # stderr so operators see it even though `logger` is not yet
            # initialized at module-import time (logger is defined further
            # down this module).
            print(
                f"  Warning: config.yaml → env bridge failed: "
                f"{type(_bridge_err).__name__}: {_bridge_err}",
                file=sys.stderr,
            )
            print(
                "  Gateway will fall back to .env values, which may not match "
                "your current config.yaml. Run `hermes doctor` to investigate.",
                file=sys.stderr,
            )
