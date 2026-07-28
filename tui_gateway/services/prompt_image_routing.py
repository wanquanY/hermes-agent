from __future__ import annotations

import sys
from typing import Any, Callable

from tui_gateway.services.media import enrich_with_attached_images, image_refs_for_prompt

LogPromptStage = Callable[..., None]


def build_image_aware_run_message(
    *,
    prompt: Any,
    prompt_text: Any,
    submitted_images: list[str],
    session: dict,
    sid: str,
    run_id: str,
    turn_id: str,
    log_prompt_stage: LogPromptStage,
) -> Any:
    """Return the message passed to the agent after image routing."""
    run_message: Any = prompt
    image_paths, image_urls = image_refs_for_prompt(submitted_images, prompt_text)
    image_refs = [*image_paths, *image_urls]
    if not image_refs:
        return run_message

    try:
        log_prompt_stage(
            session,
            sid,
            "image-routing-decision-start",
            run_id=run_id,
            turn_id=turn_id,
            image_count=len(image_refs),
            image_path_count=len(image_paths),
            image_url_count=len(image_urls),
        )
        from agent.auxiliary_client import _read_main_model, _read_main_provider
        from agent.image_routing import build_native_content_parts, decide_image_input_mode
        from hermes_cli.config import load_config as _tui_load_config

        descriptor = dict(session.get("model_descriptor") or {})
        supports_vision = (
            bool(descriptor.get("vision_enabled"))
            if isinstance(descriptor.get("vision_enabled"), bool)
            else None
        )
        mode = decide_image_input_mode(
            _read_main_provider(),
            _read_main_model(),
            _tui_load_config(),
            supports_vision_override=supports_vision,
        )
        log_prompt_stage(
            session,
            sid,
            "image-routing-decision-end",
            run_id=run_id,
            turn_id=turn_id,
            mode=mode,
        )
    except Exception as exc:
        print(
            f"[tui_gateway] image_routing decision failed, defaulting to text: {exc}",
            file=sys.stderr,
        )
        mode = "text"
        log_prompt_stage(
            session,
            sid,
            "image-routing-decision-error",
            run_id=run_id,
            turn_id=turn_id,
            error=str(exc),
        )

    if mode == "native":
        try:
            log_prompt_stage(
                session,
                sid,
                "native-image-build-start",
                run_id=run_id,
                turn_id=turn_id,
            )
            parts, skipped = build_native_content_parts(
                prompt,
                image_paths,
                image_urls=image_urls,
            )
            if skipped:
                print(
                    f"[tui_gateway] native image attachment skipped {len(skipped)} unreadable path(s)",
                    file=sys.stderr,
                )
            if any(part.get("type") == "image_url" for part in parts):
                run_message = parts
            else:
                run_message = enrich_with_attached_images(prompt, image_refs)
            log_prompt_stage(
                session,
                sid,
                "native-image-build-end",
                run_id=run_id,
                turn_id=turn_id,
                skipped_count=len(skipped or []),
                part_count=len(parts or []),
            )
            return run_message
        except Exception as exc:
            print(
                f"[tui_gateway] native attach failed, falling back to text: {exc}",
                file=sys.stderr,
            )
            log_prompt_stage(
                session,
                sid,
                "native-image-build-error",
                run_id=run_id,
                turn_id=turn_id,
                error=str(exc),
            )

    log_prompt_stage(
        session,
        sid,
        "text-image-enrichment-start",
        run_id=run_id,
        turn_id=turn_id,
    )
    run_message = enrich_with_attached_images(prompt, image_refs)
    log_prompt_stage(
        session,
        sid,
        "text-image-enrichment-end",
        run_id=run_id,
        turn_id=turn_id,
    )
    return run_message
