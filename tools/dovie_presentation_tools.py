"""Dovie desktop-native presentation tool registration."""

from __future__ import annotations

import json
from typing import Any

from agent.runtime_cwd import resolve_agent_cwd
from tools.dovie_media_tools import dovie_image_generate
from tools.presentation import generate_presentation, regenerate_presentation_slide
from tools.registry import registry, tool_error


DOVIE_PRESENTATION_GENERATE_SCHEMA = {
    "name": "dovie_presentation_generate",
    "description": (
        "Create a PowerPoint presentation locally in the active workspace. "
        "Each prompt becomes one generated visual slide; the tool checkpoints "
        "page state, writes a previewable .pptx artifact, and needs no cloud PPT export."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Presentation title.",
                "minLength": 1,
                "maxLength": 200,
            },
            "page_prompts": {
                "type": "array",
                "description": "One complete visual-generation prompt per slide, in order.",
                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                "minItems": 1,
                "maxItems": 40,
            },
            "page_titles": {
                "type": "array",
                "description": "Optional accessible title per slide; length must match page_prompts.",
                "items": {"type": "string", "maxLength": 200},
                "maxItems": 40,
            },
            "page_reference_images": {
                "type": "array",
                "description": (
                    "Optional HTTP(S) URL or local workspace/attached-image path per slide; "
                    "use an empty string for no reference."
                ),
                "items": {"type": "string"},
                "maxItems": 40,
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "4:3", "1:1"],
                "default": "16:9",
            },
            "style_prefix": {
                "type": "string",
                "description": "Optional style direction prepended to every slide prompt.",
                "maxLength": 1000,
            },
            "output_path": {
                "type": "string",
                "description": "Optional workspace-relative .pptx path. Defaults to presentations/<title>.pptx.",
            },
            "model": {
                "type": "string",
                "description": "Optional Dovie image model id. Omit for the backend default.",
            },
            "quality": {
                "type": "string",
                "description": "Image quality such as 1K, 2K, or 4K.",
                "default": "2K",
            },
            "max_concurrent": {
                "type": "integer",
                "minimum": 1,
                "maximum": 6,
                "default": 3,
            },
            "max_retries": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "default": 3,
            },
            "overwrite": {
                "type": "boolean",
                "description": "Replace an existing output file. Defaults to false and creates a unique name.",
                "default": False,
            },
        },
        "required": ["title", "page_prompts"],
    },
}

DOVIE_PRESENTATION_REGENERATE_SLIDE_SCHEMA = {
    "name": "dovie_presentation_regenerate_slide",
    "description": (
        "Regenerate exactly one slide in an existing Dovie image-backed PowerPoint. "
        "The remaining slides are preserved and the original .pptx is replaced atomically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "presentation_path": {
                "type": "string",
                "description": "Workspace-relative or absolute path to the Dovie-generated .pptx.",
            },
            "page_number": {
                "type": "integer",
                "description": "One-based slide number to regenerate.",
                "minimum": 1,
            },
            "prompt": {
                "type": "string",
                "description": "Replacement visual-generation prompt for this slide.",
                "minLength": 1,
                "maxLength": 4000,
            },
            "title": {
                "type": "string",
                "description": "Optional replacement accessible title for this slide.",
                "maxLength": 200,
            },
            "model": {
                "type": "string",
                "description": "Optional image model override; defaults to the deck's model.",
            },
            "quality": {
                "type": "string",
                "description": "Optional image quality override; defaults to the deck's quality.",
            },
            "reference_image_url": {
                "type": "string",
                "description": (
                    "Optional HTTP(S) URL or local workspace/attached-image path."
                ),
            },
            "use_current_slide_as_reference": {
                "type": "boolean",
                "description": (
                    "Use the current slide's source URL as an image-to-image reference when available."
                ),
                "default": True,
            },
        },
        "required": ["presentation_path", "page_number", "prompt"],
    },
}


def dovie_presentation_generate(args: dict[str, Any]) -> str:
    try:
        result = generate_presentation(
            args,
            image_generator=dovie_image_generate,
            workspace_root=str(resolve_agent_cwd()),
        )
    except (TypeError, ValueError) as exc:
        return tool_error(str(exc), status="failed")
    except Exception as exc:
        return tool_error(
            f"presentation generation failed: {exc}",
            status="failed",
        )
    return json.dumps(result, ensure_ascii=False)


def dovie_presentation_regenerate_slide(args: dict[str, Any]) -> str:
    try:
        result = regenerate_presentation_slide(
            args,
            image_generator=dovie_image_generate,
            workspace_root=str(resolve_agent_cwd()),
        )
    except InterruptedError:
        return tool_error(
            "presentation slide regeneration interrupted",
            status="cancelled",
        )
    except (TypeError, ValueError) as exc:
        return tool_error(str(exc), status="failed")
    except Exception as exc:
        return tool_error(
            f"presentation slide regeneration failed: {exc}",
            status="failed",
        )
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="dovie_presentation_generate",
    toolset="dovie_presentation",
    schema=DOVIE_PRESENTATION_GENERATE_SCHEMA,
    handler=lambda args, **kw: dovie_presentation_generate(args),
    emoji="📊",
    max_result_size_chars=80_000,
)

registry.register(
    name="dovie_presentation_regenerate_slide",
    toolset="dovie_presentation",
    schema=DOVIE_PRESENTATION_REGENERATE_SLIDE_SCHEMA,
    handler=lambda args, **kw: dovie_presentation_regenerate_slide(args),
    emoji="🖼️",
    max_result_size_chars=80_000,
)
