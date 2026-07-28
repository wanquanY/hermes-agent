"""Gateway background-task command ownership."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import List, Optional

from agent.i18n import t
from channels.platforms.base import MessageEvent
from hermes_constants import get_hermes_home
from hermes_agent.gateway.runtime_config import load_gateway_runtime_config
from hermes_gateway.config import Platform
from hermes_gateway.checkpoint_config import checkpoint_agent_kwargs
from hermes_gateway.fast_command import fast_command_for
from hermes_gateway.gateway_runtime_config import runtime_config_for
from hermes_gateway.platform_runtime import platform_runtime_for
from hermes_gateway.profile_runtime import profile_runtime_for

logger = logging.getLogger(__name__)


def _platform_config_key(platform: Platform) -> str:
    return "cli" if platform == Platform.LOCAL else platform.value


def load_gateway_config() -> dict:
    return load_gateway_runtime_config(get_hermes_home())


class GatewayBackgroundTaskMixin:
    async def _handle_background_command(self, event: MessageEvent) -> str:
        """Handle /background <prompt> — run a prompt in a separate background session.

        Spawns a new AIAgent in a background thread with its own session.
        When it completes, sends the result back to the same chat without
        modifying the active session's conversation history.
        """
        prompt = event.get_command_args().strip()
        if not prompt:
            return t("gateway.background.usage")

        source = event.source
        task_id = f"bg_{datetime.now().strftime('%H%M%S')}_{os.urandom(3).hex()}"

        event_message_id = self._reply_anchor_for_event(event)

        # Forward image/audio attachments so the background agent can see them.
        media_urls = list(event.media_urls) if event.media_urls else []
        media_types = list(event.media_types) if event.media_types else []

        # Fire-and-forget the background task
        _task = asyncio.create_task(
            self._run_background_task(
                prompt,
                source,
                task_id,
                event_message_id=event_message_id,
                media_urls=media_urls,
                media_types=media_types,
            )
        )
        self._background_tasks.add(_task)
        _task.add_done_callback(self._background_tasks.discard)

        preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
        return t("gateway.background.started", preview=preview, task_id=task_id)

    async def _run_background_task(
        self,
        prompt: str,
        source: "SessionSource",
        task_id: str,
        event_message_id: Optional[str] = None,
        media_urls: Optional[List[str]] = None,
        media_types: Optional[List[str]] = None,
    ) -> None:
        """Enter the source profile before resolving any runtime dependency."""
        with profile_runtime_for(self).scope_for_source(source):
            await self._run_background_task_scoped(
                prompt,
                source,
                task_id,
                event_message_id=event_message_id,
                media_urls=media_urls,
                media_types=media_types,
            )

    async def _run_background_task_scoped(
        self,
        prompt: str,
        source: "SessionSource",
        task_id: str,
        event_message_id: Optional[str] = None,
        media_urls: Optional[List[str]] = None,
        media_types: Optional[List[str]] = None,
    ) -> None:
        """Execute a background agent task and deliver the result to the chat."""
        from run_agent import AIAgent

        media_urls = media_urls or []
        media_types = media_types or []

        adapter = platform_runtime_for(self).adapter_for_source(source)
        if not adapter:
            logger.warning("No adapter for platform %s in background task %s", source.platform, task_id)
            return

        _thread_metadata = self._thread_metadata_for_source(source, event_message_id)

        try:
            user_config = load_gateway_config()
            runtime_config = runtime_config_for(self)
            model, runtime_kwargs = runtime_config.resolve_session_agent_runtime(
                source=source,
                user_config=user_config,
            )
            if not runtime_kwargs.get("api_key"):
                await adapter.send(
                    source.chat_id,
                    f"❌ Background task {task_id} failed: no provider credentials configured.",
                    metadata=_thread_metadata,
                )
                return

            platform_key = _platform_config_key(source.platform)

            from hermes_cli.tools_config import _get_platform_tools
            enabled_toolsets = sorted(_get_platform_tools(user_config, platform_key))
            agent_cfg = user_config.get("agent") or {}
            disabled_toolsets = agent_cfg.get("disabled_toolsets") or None

            pr = runtime_config.load_provider_routing()
            prefill_messages = runtime_config.load_prefill_messages()
            fallback_model = runtime_config.load_fallback_model()
            max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
            reasoning_config = runtime_config.resolve_session_reasoning_config(
                source=source,
                model=model,
            )
            service_tier = fast_command_for(self).load_service_tier()
            turn_route = runtime_config.resolve_turn_agent_config(
                prompt,
                model,
                runtime_kwargs,
                service_tier=service_tier,
            )

            # Enrich the prompt with image descriptions so the background
            # agent can see user-attached images (same as the main flow).
            enriched_prompt = prompt
            if media_urls:
                image_paths = []
                for i, path in enumerate(media_urls):
                    mtype = media_types[i] if i < len(media_types) else ""
                    if mtype.startswith("image/"):
                        image_paths.append(path)
                if image_paths:
                    try:
                        enriched_prompt = await self._enrich_message_with_vision(
                            prompt, image_paths,
                        )
                    except Exception as e:
                        logger.warning("Background task vision enrichment failed: %s", e)

            def run_sync():
                agent = AIAgent(
                    model=turn_route["model"],
                    **turn_route["runtime"],
                    max_iterations=max_iterations,
                    quiet_mode=True,
                    verbose_logging=False,
                    enabled_toolsets=enabled_toolsets,
                    disabled_toolsets=disabled_toolsets,
                    ephemeral_system_prompt=(
                        runtime_config.load_ephemeral_system_prompt() or None
                    ),
                    prefill_messages=prefill_messages or None,
                    reasoning_config=reasoning_config,
                    service_tier=service_tier,
                    request_overrides=turn_route.get("request_overrides"),
                    providers_allowed=pr.get("only"),
                    providers_ignored=pr.get("ignore"),
                    providers_order=pr.get("order"),
                    provider_sort=pr.get("sort"),
                    provider_require_parameters=pr.get("require_parameters", False),
                    provider_data_collection=pr.get("data_collection"),
                    session_id=task_id,
                    platform=platform_key,
                    user_id=source.user_id,
                    user_name=source.user_name,
                    chat_id=source.chat_id,
                    chat_name=source.chat_name,
                    chat_type=source.chat_type,
                    thread_id=source.thread_id,
                    session_db=self._session_db,
                    fallback_model=fallback_model,
                    **checkpoint_agent_kwargs(user_config),
                )
                try:
                    return agent.run_conversation(
                        user_message=enriched_prompt,
                        task_id=task_id,
                    )
                finally:
                    self._cleanup_agent_resources(agent)

            result = await self._run_in_executor_with_context(run_sync)

            response = result.get("final_response", "") if result else ""
            if not response and result and result.get("error"):
                response = f"Error: {result['error']}"

            # Extract media files from the response
            if response:
                media_files, response = adapter.extract_media(response)
                from channels.platforms.base import BasePlatformAdapter
                media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
                images, text_content = adapter.extract_images(response)

                preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
                header = f'✅ Background task complete\nPrompt: "{preview}"\n\n'

                if text_content:
                    await adapter.send(
                        chat_id=source.chat_id,
                        content=header + text_content,
                        metadata=_thread_metadata,
                    )
                elif not images and not media_files:
                    await adapter.send(
                        chat_id=source.chat_id,
                        content=header + "(No response generated)",
                        metadata=_thread_metadata,
                    )

                # Send extracted images
                for image_url, alt_text in (images or []):
                    try:
                        await adapter.send_image(
                            chat_id=source.chat_id,
                            image_url=image_url,
                            caption=alt_text,
                            metadata=_thread_metadata,
                        )
                    except Exception:
                        logger.debug("Suppressed recoverable gateway exception", exc_info=True)

                # Send media files
                for media_path, _is_voice in (media_files or []):
                    try:
                        await adapter.send_document(
                            chat_id=source.chat_id,
                            file_path=media_path,
                            metadata=_thread_metadata,
                        )
                    except Exception:
                        logger.debug("Suppressed recoverable gateway exception", exc_info=True)
            else:
                preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
                await adapter.send(
                    chat_id=source.chat_id,
                    content=f'✅ Background task complete\nPrompt: "{preview}"\n\n(No response generated)',
                    metadata=_thread_metadata,
                )

        except Exception as e:
            logger.exception("Background task %s failed", task_id)
            try:
                await adapter.send(
                    chat_id=source.chat_id,
                    content=f"❌ Background task {task_id} failed: {e}",
                    metadata=_thread_metadata,
                )
            except Exception:
                logger.debug("Suppressed recoverable gateway exception", exc_info=True)
