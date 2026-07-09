"""Gateway model switching command and session overrides."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from agent.i18n import t
from channels.platforms.base import MessageEvent
from hermes_gateway.agent_cache import agent_cache_for
from utils import base_url_host_matches

logger = logging.getLogger(__name__)


def _gateway_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _load_gateway_config() -> dict:
    from hermes_agent.gateway.runtime_config import load_gateway_runtime_config

    return load_gateway_runtime_config(_gateway_home())


def _persist_model_switch(config_path: Path, result: object) -> None:
    import yaml

    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}
    existing_model_cfg = cfg.get("model")
    if isinstance(existing_model_cfg, dict):
        model_cfg = existing_model_cfg
    else:
        model_cfg = {}
        if isinstance(existing_model_cfg, str) and existing_model_cfg.strip():
            model_cfg["default"] = existing_model_cfg.strip()
        cfg["model"] = model_cfg
    model_cfg["default"] = result.new_model
    model_cfg["provider"] = result.target_provider
    if result.base_url:
        model_cfg["base_url"] = result.base_url
    else:
        model_cfg.pop("base_url", None)
    model_cfg.pop("api_key", None)
    model_cfg.pop("api_mode", None)
    from hermes_cli.config import save_config

    save_config(cfg)


class GatewayModelCommandService:
    def __init__(self, runner):
        self._runner = runner

    async def handle_model_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /model command — switch model for this session.

        Supports:
          /model                              — interactive picker (Telegram/Discord) or text list
          /model <name>                       — switch for this session only
          /model <name> --global              — switch and persist to config.yaml
          /model <name> --provider <provider> — switch provider + model
          /model --provider <provider>        — switch to provider, auto-detect model
        """
        from hermes_cli.model_switch import (
            switch_model as _switch_model, parse_model_flags,
            resolve_persist_behavior,
            list_authenticated_providers,
            list_picker_providers,
        )
        from hermes_cli.providers import get_label

        raw_args = event.get_command_args().strip()

        # Parse --provider, --global, and --refresh flags
        model_input, explicit_provider, is_global, force_refresh, is_session = parse_model_flags(raw_args)
        persist_global = resolve_persist_behavior(is_global, is_session)

        # --refresh: bust the disk cache so the picker shows live data.
        if force_refresh:
            try:
                from hermes_cli.models import clear_provider_models_cache
                clear_provider_models_cache()
            except Exception:
                logger.debug("Could not clear provider models cache for /model --refresh", exc_info=True)

        # Read current model/provider from config
        current_model = ""
        current_provider = "openrouter"
        current_base_url = ""
        current_api_key = ""
        user_provs = None
        custom_provs = None
        config_path = _gateway_home() / "config.yaml"
        try:
            cfg = _load_gateway_config()
            if cfg:
                model_cfg = cfg.get("model", {})
                if isinstance(model_cfg, dict):
                    current_model = model_cfg.get("default", "")
                    current_provider = model_cfg.get("provider", current_provider)
                    current_base_url = model_cfg.get("base_url", "")
                user_provs = cfg.get("providers")
                try:
                    from hermes_cli.config import get_compatible_custom_providers
                    custom_provs = get_compatible_custom_providers(cfg)
                except Exception as exc:
                    logger.debug("Could not resolve compatible custom providers: %s", exc)
                    custom_provs = cfg.get("custom_providers")
        except Exception as exc:
            logger.debug("Could not load gateway model config for /model: %s", exc)

        # Check for session override
        source = event.source
        session_key = self._runner._session_key_for_source(source)
        override = self._runner._session_model_overrides.get(session_key, {})
        if override:
            current_model = override.get("model", current_model)
            current_provider = override.get("provider", current_provider)
            current_base_url = override.get("base_url", current_base_url)
            current_api_key = override.get("api_key", current_api_key)

        # No args: show interactive picker (Telegram/Discord) or text list
        if not model_input and not explicit_provider:
            # Try interactive picker if the platform supports it
            adapter = self._runner.adapters.get(source.platform)
            has_picker = (
                adapter is not None
                and getattr(type(adapter), "send_model_picker", None) is not None
            )

            if has_picker:
                try:
                    providers = list_picker_providers(
                        current_provider=current_provider,
                        current_base_url=current_base_url,
                        current_model=current_model,
                        user_providers=user_provs,
                        custom_providers=custom_provs,
                        max_models=50,
                    )
                except Exception as exc:
                    logger.debug("Could not build model picker providers: %s", exc)
                    providers = []

                if providers:
                    # Build a callback closure for when the user picks a model.
                    # Captures self + locals needed for the switch logic.
                    _self = self._runner
                    _session_key = session_key
                    _cur_model = current_model
                    _cur_provider = current_provider
                    _cur_base_url = current_base_url
                    _cur_api_key = current_api_key

                    async def _on_model_selected(
                        _chat_id: str, model_id: str, provider_slug: str
                    ) -> str:
                        """Perform the model switch and return confirmation text."""
                        result = _switch_model(
                            raw_input=model_id,
                            current_provider=_cur_provider,
                            current_model=_cur_model,
                            current_base_url=_cur_base_url,
                            current_api_key=_cur_api_key,
                            is_global=persist_global,
                            explicit_provider=provider_slug,
                            user_providers=user_provs,
                            custom_providers=custom_provs,
                        )
                        if not result.success:
                            return t("gateway.model.error_prefix", error=result.error_message)

                        # Update cached agent in-place
                        cached_entry = None
                        _cache_lock = getattr(_self, "_agent_cache_lock", None)
                        _cache = getattr(_self, "_agent_cache", None)
                        if _cache_lock and _cache is not None:
                            with _cache_lock:
                                cached_entry = _cache.get(_session_key)
                        if cached_entry and cached_entry[0] is not None:
                            try:
                                cached_entry[0].switch_model(
                                    new_model=result.new_model,
                                    new_provider=result.target_provider,
                                    api_key=result.api_key,
                                    base_url=result.base_url,
                                    api_mode=result.api_mode,
                                )
                            except Exception as exc:
                                logger.warning("Picker model switch failed for cached agent: %s", exc)

                        # Store model note + session override
                        if not hasattr(_self, "_pending_model_notes"):
                            _self._pending_model_notes = {}
                        _self._pending_model_notes[_session_key] = (
                            f"[Note: model was just switched from {_cur_model} to {result.new_model} "
                            f"via {result.provider_label or result.target_provider}. "
                            f"Adjust your self-identification accordingly.]"
                        )
                        _self._session_model_overrides[_session_key] = {
                            "model": result.new_model,
                            "provider": result.target_provider,
                            "api_key": result.api_key,
                            "base_url": result.base_url,
                            "api_mode": result.api_mode,
                        }

                        # Evict cached agent so the next turn creates a fresh
                        # agent from the override rather than relying on the
                        # stale cache signature to trigger a rebuild.
                        agent_cache_for(_self).evict_cached_agent(_session_key)

                        if persist_global:
                            try:
                                _persist_model_switch(config_path, result)
                            except Exception as e:
                                logger.warning("Failed to persist picker model switch: %s", e)

                        # Build confirmation text
                        plabel = result.provider_label or result.target_provider
                        lines = [t("gateway.model.switched", model=result.new_model)]
                        lines.append(t("gateway.model.provider_label", provider=plabel))
                        mi = result.model_info
                        from hermes_cli.model_switch import resolve_display_context_length
                        _sw_config_ctx = None
                        try:
                            _sw_cfg = _load_gateway_config()
                            _sw_model_cfg = _sw_cfg.get("model", {})
                            if isinstance(_sw_model_cfg, dict):
                                _sw_raw = _sw_model_cfg.get("context_length")
                                if _sw_raw is not None:
                                    _sw_config_ctx = int(_sw_raw)
                        except Exception as exc:
                            logger.debug("Could not resolve picker model context length: %s", exc)
                        ctx = resolve_display_context_length(
                            result.new_model,
                            result.target_provider,
                            base_url=result.base_url or current_base_url or "",
                            api_key=result.api_key or current_api_key or "",
                            model_info=mi,
                            custom_providers=custom_provs,
                            config_context_length=_sw_config_ctx,
                        )
                        if ctx:
                            lines.append(t("gateway.model.context_label", tokens=f"{ctx:,}"))
                        if mi:
                            if mi.max_output:
                                lines.append(t("gateway.model.max_output_label", tokens=f"{mi.max_output:,}"))
                            if mi.has_cost_data():
                                lines.append(t("gateway.model.cost_label", cost=mi.format_cost()))
                            lines.append(t("gateway.model.capabilities_label", capabilities=mi.format_capabilities()))
                        if persist_global:
                            lines.append(t("gateway.model.saved_global"))
                        else:
                            lines.append(t("gateway.model.session_only_hint"))
                        return "\n".join(lines)

                    metadata = self._runner._thread_metadata_for_source(
                        source,
                        self._runner._reply_anchor_for_event(event),
                    )
                    result = await adapter.send_model_picker(
                        chat_id=source.chat_id,
                        providers=providers,
                        current_model=current_model,
                        current_provider=current_provider,
                        session_key=session_key,
                        on_model_selected=_on_model_selected,
                        metadata=metadata,
                    )
                    if result.success:
                        return None  # Picker sent — adapter handles the response

            # Fallback: text list (for platforms without picker or if picker failed)
            provider_label = get_label(current_provider)
            lines = [t("gateway.model.current_label", model=current_model or "unknown", provider=provider_label), ""]

            try:
                providers = list_authenticated_providers(
                    current_provider=current_provider,
                    current_base_url=current_base_url,
                    current_model=current_model,
                    user_providers=user_provs,
                    custom_providers=custom_provs,
                    max_models=5,
                )
                for p in providers:
                    tag = t("gateway.model.current_tag") if p["is_current"] else ""
                    lines.append(f"**{p['name']}** `--provider {p['slug']}`{tag}:")
                    if p["models"]:
                        model_strs = ", ".join(f"`{m}`" for m in p["models"])
                        extra = t("gateway.model.more_models_suffix", count=p["total_models"] - len(p["models"])) if p["total_models"] > len(p["models"]) else ""
                        lines.append(f"  {model_strs}{extra}")
                    elif p.get("api_url"):
                        lines.append(f"  `{p['api_url']}`")
                    lines.append("")
            except Exception as exc:
                logger.debug("Could not list authenticated providers for /model: %s", exc)

            lines.append(t("gateway.model.usage_switch_model"))
            lines.append(t("gateway.model.usage_switch_provider"))
            lines.append(t("gateway.model.usage_persist"))
            return "\n".join(lines)

        def _apply_resolved_switch(result: object, *, persist_global: bool) -> str:
            # If there's a cached agent, update it in-place
            cached_entry = None
            _cache_lock = getattr(self._runner, "_agent_cache_lock", None)
            _cache = getattr(self._runner, "_agent_cache", None)
            if _cache_lock and _cache is not None:
                with _cache_lock:
                    cached_entry = _cache.get(session_key)

            if cached_entry and cached_entry[0] is not None:
                try:
                    cached_entry[0].switch_model(
                        new_model=result.new_model,
                        new_provider=result.target_provider,
                        api_key=result.api_key,
                        base_url=result.base_url,
                        api_mode=result.api_mode,
                    )
                except Exception as exc:
                    logger.warning("In-place model switch failed for cached agent: %s", exc)

            # Store a note to prepend to the next user message so the model
            # knows about the switch (avoids system messages mid-history).
            if not hasattr(self._runner, "_pending_model_notes"):
                self._runner._pending_model_notes = {}
            self._runner._pending_model_notes[session_key] = (
                f"[Note: model was just switched from {current_model} to {result.new_model} "
                f"via {result.provider_label or result.target_provider}. "
                f"Adjust your self-identification accordingly.]"
            )

            # Store session override so next agent creation uses the new model
            self._runner._session_model_overrides[session_key] = {
                "model": result.new_model,
                "provider": result.target_provider,
                "api_key": result.api_key,
                "base_url": result.base_url,
                "api_mode": result.api_mode,
            }

            # Evict cached agent so the next turn creates a fresh agent from the
            # override rather than relying on cache signature mismatch detection.
            agent_cache_for(self._runner).evict_cached_agent(session_key)

            # Persist to config if --global
            if persist_global:
                try:
                    _persist_model_switch(config_path, result)
                except Exception as e:
                    logger.warning("Failed to persist model switch: %s", e)

            # Build confirmation message with full metadata
            provider_label = result.provider_label or result.target_provider
            lines = [t("gateway.model.switched", model=result.new_model)]
            lines.append(t("gateway.model.provider_label", provider=provider_label))

            # Context: always resolve via the provider-aware chain so Codex OAuth,
            # Copilot, and Nous-enforced caps win over the raw models.dev entry.
            mi = result.model_info
            from hermes_cli.model_switch import resolve_display_context_length
            _sw2_config_ctx = None
            try:
                _sw2_cfg = _load_gateway_config()
                _sw2_model_cfg = _sw2_cfg.get("model", {})
                if isinstance(_sw2_model_cfg, dict):
                    _sw2_raw = _sw2_model_cfg.get("context_length")
                    if _sw2_raw is not None:
                        _sw2_config_ctx = int(_sw2_raw)
            except Exception as exc:
                logger.debug("Could not resolve switched model context length: %s", exc)
            ctx = resolve_display_context_length(
                result.new_model,
                result.target_provider,
                base_url=result.base_url or current_base_url or "",
                api_key=result.api_key or current_api_key or "",
                model_info=mi,
                custom_providers=custom_provs,
                config_context_length=_sw2_config_ctx,
            )
            if ctx:
                lines.append(t("gateway.model.context_label", tokens=f"{ctx:,}"))
            if mi:
                if mi.max_output:
                    lines.append(t("gateway.model.max_output_label", tokens=f"{mi.max_output:,}"))
                if mi.has_cost_data():
                    lines.append(t("gateway.model.cost_label", cost=mi.format_cost()))
                lines.append(t("gateway.model.capabilities_label", capabilities=mi.format_capabilities()))

            # Cache notice
            cache_enabled = (
                (base_url_host_matches(result.base_url or "", "openrouter.ai") and "claude" in result.new_model.lower())
                or result.api_mode == "anthropic_messages"
            )
            if cache_enabled:
                lines.append(t("gateway.model.prompt_caching_enabled"))

            if result.warning_message:
                lines.append(t("gateway.model.warning_prefix", warning=result.warning_message))

            if persist_global:
                lines.append(t("gateway.model.saved_global"))
            else:
                lines.append(t("gateway.model.session_only_hint"))

            return "\n".join(lines)

        # Perform the switch
        result = _switch_model(
            raw_input=model_input,
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=current_base_url,
            current_api_key=current_api_key,
            is_global=persist_global,
            explicit_provider=explicit_provider,
            user_providers=user_provs,
            custom_providers=custom_provs,
        )

        if not result.success:
            return t("gateway.model.error_prefix", error=result.error_message)

        try:
            from hermes_cli.model_cost_guard import expensive_model_warning

            expensive_warning = expensive_model_warning(
                result.new_model,
                provider=result.target_provider,
                base_url=result.base_url,
                api_key=result.api_key,
                model_info=result.model_info,
            )
        except Exception:
            logger.debug("Could not evaluate expensive model warning for /model", exc_info=True)
            expensive_warning = None

        if expensive_warning is not None:
            async def _on_confirm(choice: str) -> str:
                if choice == "cancel":
                    return "Model switch cancelled."
                return _apply_resolved_switch(result, persist_global=persist_global)

            request_confirm = getattr(self._runner, "_request_slash_confirm", None)
            if request_confirm is not None:
                return await request_confirm(
                    command="model",
                    title="/model",
                    message=expensive_warning.message,
                    handler=_on_confirm,
                )

            from channels.slash_commands import request_slash_confirm

            return await request_slash_confirm(
                runtime=self._runner._slash_confirmation_runtime(),
                event=event,
                command="model",
                title="/model",
                message=expensive_warning.message,
                handler=_on_confirm,
            )

        return _apply_resolved_switch(result, persist_global=persist_global)


    def apply_session_model_override(
        self, session_key: str, model: str, runtime_kwargs: dict
    ) -> tuple:
        """Apply /model session overrides if present, returning (model, runtime_kwargs).

        The gateway /model command stores per-session overrides in
        ``_session_model_overrides``.  These must take precedence over
        config.yaml defaults so the switched model is actually used for
        subsequent messages.  Fields with ``None`` values are skipped so
        partial overrides don't clobber valid config defaults.
        """
        override = self._runner._session_model_overrides.get(session_key)
        if not override:
            return model, runtime_kwargs
        model = override.get("model", model)
        for key in ("provider", "api_key", "base_url", "api_mode"):
            val = override.get(key)
            if val is not None:
                runtime_kwargs[key] = val
        return model, runtime_kwargs

    def is_intentional_model_switch(self, session_key: str, agent_model: str) -> bool:
        """Return True if *agent_model* matches an active /model session override."""
        override = self._runner._session_model_overrides.get(session_key)
        return override is not None and override.get("model") == agent_model


def model_command_for(runner) -> GatewayModelCommandService:
    service = getattr(runner, "model_command", None)
    if isinstance(service, GatewayModelCommandService):
        return service
    service = GatewayModelCommandService(runner)
    runner.model_command = service
    return service
