# ruff: noqa: F401,F403,F405,F821,ARG001
"""Control-plane RPCs for native Dovie presentation revision."""

from __future__ import annotations

import os

from tools.dovie_media_tools import dovie_image_generate
from tools.presentation import regenerate_presentation_slide
from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


@method("presentation.slide.regenerate")
def _regenerate_slide(rid, params: dict) -> dict:
    workspace_root = str(
        params.get("workspace_path")
        or params.get("workspacePath")
        or params.get("workspace_root")
        or params.get("workspaceRoot")
        or ""
    ).strip()
    if not workspace_root:
        return _err(rid, 4006, "workspace_path required")
    if not os.path.isdir(workspace_root):
        return _err(rid, 4004, "workspace_path must be an existing directory")
    request = {
        "presentation_path": (
            params.get("presentation_path")
            or params.get("presentationPath")
            or params.get("path")
        ),
        "page_number": params.get("page_number") or params.get("pageNumber"),
        "prompt": params.get("prompt"),
        "title": params.get("title"),
        "model": params.get("model"),
        "quality": params.get("quality"),
        "reference_image_url": (
            params.get("reference_image_url")
            or params.get("referenceImageUrl")
        ),
        "use_current_slide_as_reference": params.get(
            "use_current_slide_as_reference",
            params.get("useCurrentSlideAsReference", True),
        ),
    }
    try:
        result = regenerate_presentation_slide(
            request,
            image_generator=dovie_image_generate,
            workspace_root=workspace_root,
        )
    except InterruptedError:
        return _err(rid, 4990, "presentation slide regeneration interrupted")
    except (TypeError, ValueError) as exc:
        return _err(rid, 4004, str(exc))
    except Exception as exc:
        return _err(rid, 5029, f"presentation slide regeneration failed: {exc}")
    return _ok(rid, result)
