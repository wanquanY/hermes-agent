"""Dovie desktop-native presentation tool registration."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from agent.runtime_cwd import resolve_agent_cwd
from tools.dovie_media_tools import dovie_image_generate
from tools.presentation import generate_presentation, regenerate_presentation_slide
from tools.presentation.editable_service import (
    build_editable_presentation,
    inspect_reference_presentation,
)
from tools.presentation.slidespec_contract import SLIDESPEC_JSON_SCHEMA
from tools.registry import registry, tool_error


DOVIE_PRESENTATION_BUILD_SCHEMA = {
    "name": "dovie_presentation_build",
    "description": (
        "Default tool for normal PowerPoint, PPT, PPTX, slide-deck, and "
        "presentation requests. Build a locally editable PowerPoint from a "
        "strict SlideSpec. "
        "Text, shapes, local images, charts, tables, speaker notes, and sources "
        "remain native PowerPoint objects; no generated JavaScript is executed. "
        "Load the deck-builder skill before authoring. For image-led layouts, "
        "create or obtain meaningful assets first, save them locally, and pass "
        "their workspace paths. After building, repair every diagnostic and use "
        "vision_analyze on every preview page before delivery. "
        "Use this tool unless the user explicitly requests flattened, image-only, "
        "or non-editable slides."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Presentation title used for the default output filename.",
                "minLength": 1,
                "maxLength": 200,
            },
            "slidespec": {
                **deepcopy(SLIDESPEC_JSON_SCHEMA),
                "description": (
                    "Strict slidespec/1 document using exactly the element variants "
                    "declared here. Coordinates are normalized to 0..1. For a "
                    "rectangle use type='shape', kind='rect', and fill/fill_token. "
                    "Do not invent Figma-style rectangle, fills, or typography fields."
                ),
            },
            "output_path": {
                "type": "string",
                "description": (
                    "Optional workspace-relative .pptx path. "
                    "Defaults to presentations/<title>.pptx."
                ),
            },
            "reference_deck_path": {
                "type": "string",
                "description": (
                    "Optional local workspace or attachment .pptx used as the "
                    "visual reference. Its page ratio, Office theme colors, and "
                    "theme fonts are safely extracted and applied before rendering. "
                    "Inspect the supplied deck first and preserve its visual language."
                ),
            },
            "render_preview": {
                "type": "boolean",
                "description": (
                    "Render PDF and PNG previews when LibreOffice and Poppler are "
                    "available. The editable PPTX is still created when unavailable."
                ),
                "default": True,
            },
            "overwrite": {
                "type": "boolean",
                "description": (
                    "Replace an existing output file. Defaults to false and creates "
                    "a unique filename."
                ),
                "default": False,
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 10,
                "maximum": 600,
                "default": 180,
            },
        },
        "required": ["title", "slidespec"],
        "additionalProperties": False,
    },
}

DOVIE_PRESENTATION_INSPECT_SCHEMA = {
    "name": "dovie_presentation_inspect",
    "description": (
        "Inspect a supplied or existing PPTX before authoring. Safely extracts "
        "its page ratio, Office theme colors, theme fonts, and layout names, "
        "and renders local page previews for vision_analyze. Use this first "
        "for every reference-deck, template-following, or existing-deck request."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "presentation_path": {
                "type": "string",
                "description": (
                    "Local workspace or Dovie attachment path to an existing .pptx."
                ),
            },
            "render_preview": {
                "type": "boolean",
                "description": (
                    "Render every page to a local PNG for visual inspection."
                ),
                "default": True,
            },
        },
        "required": ["presentation_path"],
        "additionalProperties": False,
    },
}


DOVIE_PRESENTATION_GENERATE_SCHEMA = {
    "name": "dovie_presentation_generate",
    "description": (
        "Legacy flattened image-backed PowerPoint generator. Every slide becomes "
        "one non-editable full-slide image. Never use this for a normal PowerPoint, "
        "PPT, PPTX, slide-deck, or presentation request. Use it only when the user "
        "explicitly requests image-only, flattened, or non-editable slides."
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
                "description": (
                    "Optional provider-native image quality override. Omit unless the "
                    "user explicitly requests one; the Dovie Admin model configuration "
                    "supplies the supported default."
                ),
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
        "Legacy image-only operation: regenerate exactly one slide in an existing "
        "Dovie image-backed PowerPoint. Do not use it for editable decks. "
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
                "description": (
                    "Optional provider-native image quality override. Omit to reuse the "
                    "deck override or the Dovie Admin model default."
                ),
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


def dovie_presentation_build(args: dict[str, Any]) -> str:
    try:
        result = build_editable_presentation(
            args,
            workspace_root=str(resolve_agent_cwd()),
        )
    except InterruptedError:
        return tool_error(
            "editable presentation build interrupted",
            status="cancelled",
        )
    except (TypeError, ValueError) as exc:
        return tool_error(str(exc), status="failed")
    except Exception as exc:
        return tool_error(
            f"editable presentation build failed: {exc}",
            status="failed",
        )
    return json.dumps(result, ensure_ascii=False)

def dovie_presentation_inspect(args: dict[str, Any]) -> str:
    try:
        result = inspect_reference_presentation(
            args,
            workspace_root=str(resolve_agent_cwd()),
        )
    except InterruptedError:
        return tool_error(
            "presentation reference inspection interrupted",
            status="cancelled",
        )
    except (TypeError, ValueError) as exc:
        return tool_error(str(exc), status="failed")
    except Exception as exc:
        return tool_error(
            f"presentation reference inspection failed: {exc}",
            status="failed",
        )
    return json.dumps(result, ensure_ascii=False)


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
    name="dovie_presentation_inspect",
    toolset="dovie_presentation",
    schema=DOVIE_PRESENTATION_INSPECT_SCHEMA,
    handler=lambda args, **kw: dovie_presentation_inspect(args),
    emoji="🔎",
    max_result_size_chars=80_000,
)

registry.register(
    name="dovie_presentation_build",
    toolset="dovie_presentation",
    schema=DOVIE_PRESENTATION_BUILD_SCHEMA,
    handler=lambda args, **kw: dovie_presentation_build(args),
    emoji="📊",
    max_result_size_chars=80_000,
)

registry.register(
    name="dovie_presentation_generate",
    toolset="dovie_presentation_legacy",
    schema=DOVIE_PRESENTATION_GENERATE_SCHEMA,
    handler=lambda args, **kw: dovie_presentation_generate(args),
    emoji="📊",
    max_result_size_chars=80_000,
)

registry.register(
    name="dovie_presentation_regenerate_slide",
    toolset="dovie_presentation_legacy",
    schema=DOVIE_PRESENTATION_REGENERATE_SLIDE_SCHEMA,
    handler=lambda args, **kw: dovie_presentation_regenerate_slide(args),
    emoji="🖼️",
    max_result_size_chars=80_000,
)
