import base64
import contextvars
import json
import zipfile
from pathlib import Path

import pytest

from tools.presentation import service as presentation_service
from tools.presentation.service import generate_presentation, regenerate_presentation_slide


PNG_DATA = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG_DATA_URL = f"data:image/png;base64,{base64.b64encode(PNG_DATA).decode('ascii')}"


def test_dovie_presentation_toolset_is_configurable():
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS
    from model_tools import get_tool_definitions
    from tools.dovie_presentation_tools import (
        DOVIE_PRESENTATION_BUILD_SCHEMA,
        DOVIE_PRESENTATION_GENERATE_SCHEMA,
    )
    from tools.presentation.slidespec_contract import SLIDESPEC_JSON_SCHEMA

    configurable = {
        name: (label, summary)
        for name, label, summary in CONFIGURABLE_TOOLSETS
    }

    assert configurable["dovie_presentation"] == (
        "📊 Dovie Editable Presentation",
        "dovie_presentation_inspect, dovie_presentation_build",
    )
    assert configurable["dovie_presentation_legacy"] == (
        "🖼️ Dovie Image-backed Presentation (Legacy)",
        "dovie_presentation_generate, dovie_presentation_regenerate_slide",
    )
    assert "Default tool for normal PowerPoint" in DOVIE_PRESENTATION_BUILD_SCHEMA[
        "description"
    ]
    assert "non-editable full-slide image" in DOVIE_PRESENTATION_GENERATE_SCHEMA[
        "description"
    ]
    editable_names = {
        tool["function"]["name"]
        for tool in get_tool_definitions(
            enabled_toolsets=["dovie_presentation"],
            quiet_mode=True,
        )
    }
    assert editable_names == {
        "dovie_presentation_inspect",
        "dovie_presentation_build",
    }
    legacy_names = {
        tool["function"]["name"]
        for tool in get_tool_definitions(
            enabled_toolsets=["dovie_presentation_legacy"],
            quiet_mode=True,
        )
    }
    assert legacy_names == {
        "dovie_presentation_generate",
        "dovie_presentation_regenerate_slide",
    }
    assert DOVIE_PRESENTATION_BUILD_SCHEMA["parameters"]["properties"]["slidespec"][
        "properties"
    ]["version"] == {"type": "string", "const": "slidespec/1"}
    assert (
        DOVIE_PRESENTATION_BUILD_SCHEMA["parameters"]["properties"]["slidespec"]
        == {
            **SLIDESPEC_JSON_SCHEMA,
            "description": (
                "Strict slidespec/1 document using exactly the element variants "
                "declared here. Coordinates are normalized to 0..1. For a "
                "rectangle use type='shape', kind='rect', and fill/fill_token. "
                "Do not invent Figma-style rectangle, fills, or typography fields."
            ),
        }
    )
    element_variants = DOVIE_PRESENTATION_BUILD_SCHEMA["parameters"]["properties"][
        "slidespec"
    ]["properties"]["pages"]["items"]["properties"]["elements"]["items"]["oneOf"]
    variants_by_type = {
        variant["properties"]["type"]["const"]: variant
        for variant in element_variants
    }
    assert set(variants_by_type) == {"text", "shape", "image", "chart", "table"}
    assert variants_by_type["shape"]["properties"]["kind"]["enum"] == [
        "ellipse",
        "line",
        "rect",
        "roundRect",
    ]
    assert "fill" in variants_by_type["shape"]["properties"]
    assert "fills" not in variants_by_type["shape"]["properties"]
    assert "typography" not in variants_by_type["text"]["properties"]
    quality_schema = DOVIE_PRESENTATION_GENERATE_SCHEMA["parameters"]["properties"][
        "quality"
    ]
    assert "default" not in quality_schema
    assert "1K" not in quality_schema["description"]
    assert "2K" not in quality_schema["description"]


def test_registered_editable_presentation_tool_uses_turn_scoped_workspace(
    monkeypatch,
    tmp_path,
):
    import tools.dovie_presentation_tools as presentation_tools

    captured = {}
    monkeypatch.setattr(
        presentation_tools,
        "resolve_agent_cwd",
        lambda: Path(tmp_path),
    )

    def fake_build(args, *, workspace_root):
        captured["args"] = args
        captured["workspace_root"] = workspace_root
        return {
            "dovie_event": "presentation_build_completed",
            "status": "completed",
        }

    monkeypatch.setattr(
        presentation_tools,
        "build_editable_presentation",
        fake_build,
    )

    result = json.loads(
        presentation_tools.dovie_presentation_build(
            {
                "title": "Editable",
                "slidespec": {
                    "version": "slidespec/1",
                    "pages": [],
                },
            }
        )
    )

    assert result["status"] == "completed"
    assert captured["workspace_root"] == str(tmp_path)


def test_registered_presentation_tool_uses_turn_scoped_workspace(
    monkeypatch,
    tmp_path,
):
    import tools.dovie_presentation_tools as presentation_tools

    captured = {}
    monkeypatch.setattr(
        presentation_tools,
        "resolve_agent_cwd",
        lambda: Path(tmp_path),
    )

    def fake_generate(args, *, image_generator, workspace_root):
        captured["args"] = args
        captured["image_generator"] = image_generator
        captured["workspace_root"] = workspace_root
        return {"status": "completed"}

    monkeypatch.setattr(presentation_tools, "generate_presentation", fake_generate)

    result = json.loads(
        presentation_tools.dovie_presentation_generate(
            {"title": "Scoped", "page_prompts": ["Cover"]}
        )
    )

    assert result == {"status": "completed"}
    assert captured["workspace_root"] == str(tmp_path)


def test_generate_presentation_writes_local_pptx_and_checkpoint(tmp_path):
    calls = []

    def image_generator(args):
        calls.append(args)
        return json.dumps(
            {
                "status": "completed",
                "image_urls": [PNG_DATA_URL],
            }
        )

    result = generate_presentation(
        {
            "title": "Quarterly review",
            "page_titles": ["Cover", "Results"],
            "page_prompts": ["A clean cover slide", "A results dashboard"],
            "aspect_ratio": "16:9",
            "style_prefix": "Editorial presentation.",
            "output_path": "deliverables/review.pptx",
            "max_concurrent": 2,
        },
        image_generator=image_generator,
        workspace_root=str(tmp_path),
        interrupted=lambda: False,
    )

    output = Path(result["output_path"])
    manifest_path = Path(result["manifest_path"])
    assert result["status"] == "completed"
    assert result["dovie_event"] == "presentation_generation_completed"
    assert result["success_count"] == 2
    assert output == tmp_path / "deliverables" / "review.pptx"
    assert output.is_file()
    assert manifest_path.is_file()
    assert result["artifacts"] == [
        {
            "path": str(output),
            "title": "Quarterly review",
            "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "operation": "created",
        }
    ]
    assert len(calls) == 2
    assert all(call["prompt"].startswith("Editorial presentation.") for call in calls)
    assert all("quality" not in call for call in calls)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert [page["status"] for page in manifest["pages"]] == ["completed", "completed"]

    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        embedded = json.loads(archive.read("dovie/presentation.json"))
        assert embedded["title"] == "Quarterly review"
        assert [slide["title"] for slide in embedded["slides"]] == ["Cover", "Results"]
        assert embedded["metadata"]["style_prefix"] == "Editorial presentation."
        assert embedded["metadata"]["revision"] == 0
        assert "ppt/media/image1.png" in archive.namelist()
        assert "ppt/media/image2.png" in archive.namelist()


def test_generate_presentation_propagates_turn_context_to_slide_workers(tmp_path):
    turn_value = contextvars.ContextVar("presentation_turn_value", default="")
    turn_value.set("conversation-workspace")
    observed = []

    result = generate_presentation(
        {
            "title": "Context",
            "page_prompts": ["One", "Two"],
            "max_concurrent": 2,
        },
        image_generator=lambda _args: (
            observed.append(turn_value.get())
            or json.dumps({"status": "completed", "image_urls": [PNG_DATA_URL]})
        ),
        workspace_root=str(tmp_path),
        interrupted=lambda: False,
    )

    assert result["status"] == "completed"
    assert observed == ["conversation-workspace", "conversation-workspace"]


def test_generate_presentation_preserves_failed_checkpoint_without_pptx(tmp_path):
    def image_generator(_args):
        return json.dumps({"error": "provider unavailable"})

    result = generate_presentation(
        {
            "title": "Failure",
            "page_prompts": ["Only slide"],
            "output_path": "failure.pptx",
            "max_retries": 1,
        },
        image_generator=image_generator,
        workspace_root=str(tmp_path),
        interrupted=lambda: False,
    )

    assert result["status"] == "failed"
    assert result["dovie_event"] == "presentation_generation_failed"
    assert result["failed_count"] == 1
    assert not (tmp_path / "failure.pptx").exists()
    checkpoint = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert checkpoint["status"] == "failed"
    assert checkpoint["pages"][0]["error"] == "provider unavailable"


def test_generate_presentation_rejects_output_outside_workspace(tmp_path):
    with pytest.raises(ValueError, match="active workspace"):
        generate_presentation(
            {
                "title": "Unsafe",
                "page_prompts": ["Only slide"],
                "output_path": "../outside.pptx",
            },
            image_generator=lambda _args: "{}",
            workspace_root=str(tmp_path),
            interrupted=lambda: False,
        )


def test_generate_presentation_preserves_checkpoint_when_assembly_fails(
    tmp_path,
    monkeypatch,
):
    def fail_assembly(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(presentation_service, "write_image_presentation", fail_assembly)
    result = generate_presentation(
        {
            "title": "Assembly failure",
            "page_prompts": ["Only slide"],
            "output_path": "assembly-failure.pptx",
            "max_retries": 1,
        },
        image_generator=lambda _args: json.dumps(
            {"status": "completed", "image_urls": [PNG_DATA_URL]}
        ),
        workspace_root=str(tmp_path),
        interrupted=lambda: False,
    )

    assert result["status"] == "failed"
    assert result["dovie_event"] == "presentation_generation_failed"
    assert result["error"] == "presentation assembly failed: disk full"
    assert not (tmp_path / "assembly-failure.pptx").exists()
    checkpoint = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert checkpoint["status"] == "failed"
    assert checkpoint["assembly_error"] == "disk full"


def test_regenerate_presentation_slide_replaces_only_target_page(tmp_path):
    generated = generate_presentation(
        {
            "title": "Editable deck",
            "page_titles": ["Cover", "Details", "Close"],
            "page_prompts": ["Original cover", "Original details", "Original close"],
            "style_prefix": "Consistent editorial style.",
            "output_path": "editable.pptx",
        },
        image_generator=lambda _args: json.dumps(
            {"status": "completed", "image_urls": [PNG_DATA_URL]}
        ),
        workspace_root=str(tmp_path),
        interrupted=lambda: False,
    )
    presentation_path = Path(generated["output_path"])
    with zipfile.ZipFile(presentation_path) as archive:
        original_images = [
            archive.read("ppt/media/image1.png"),
            archive.read("ppt/media/image2.png"),
            archive.read("ppt/media/image3.png"),
        ]

    revised_png_url = (
        "data:image/png;base64,"
        + base64.b64encode(PNG_DATA + b"revised-slide").decode("ascii")
    )
    calls = []
    result = regenerate_presentation_slide(
        {
            "presentation_path": str(presentation_path),
            "page_number": 2,
            "prompt": "Revised details with a clearer comparison",
        },
        image_generator=lambda args: (
            calls.append(args)
            or json.dumps({"status": "completed", "image_urls": [revised_png_url]})
        ),
        workspace_root=str(tmp_path),
        interrupted=lambda: False,
    )

    assert result["status"] == "completed"
    assert result["dovie_event"] == "presentation_slide_regenerated"
    assert result["page_number"] == 2
    assert result["revision"] == 1
    assert result["artifacts"][0]["operation"] == "modified"
    assert calls == [
        {
            "prompt": (
                "Consistent editorial style. "
                "Revised details with a clearer comparison"
            ),
            "aspect_ratio": "16:9",
            "generate_num": 1,
        }
    ]

    with zipfile.ZipFile(presentation_path) as archive:
        assert archive.read("ppt/media/image1.png") == original_images[0]
        assert archive.read("ppt/media/image2.png") != original_images[1]
        assert archive.read("ppt/media/image3.png") == original_images[2]
        embedded = json.loads(archive.read("dovie/presentation.json"))
    assert [slide["prompt"] for slide in embedded["slides"]] == [
        "Original cover",
        "Revised details with a clearer comparison",
        "Original close",
    ]
    assert embedded["metadata"]["revision"] == 1
    assert embedded["metadata"]["last_regenerated_page"] == 2


def test_regenerate_presentation_slide_failure_leaves_original_unchanged(tmp_path):
    generated = generate_presentation(
        {
            "title": "Atomic deck",
            "page_prompts": ["Original"],
            "output_path": "atomic.pptx",
        },
        image_generator=lambda _args: json.dumps(
            {"status": "completed", "image_urls": [PNG_DATA_URL]}
        ),
        workspace_root=str(tmp_path),
        interrupted=lambda: False,
    )
    presentation_path = Path(generated["output_path"])
    original = presentation_path.read_bytes()

    with pytest.raises(RuntimeError, match="provider unavailable"):
        regenerate_presentation_slide(
            {
                "presentation_path": str(presentation_path),
                "page_number": 1,
                "prompt": "Replacement",
            },
            image_generator=lambda _args: json.dumps({"error": "provider unavailable"}),
            workspace_root=str(tmp_path),
            interrupted=lambda: False,
        )

    assert presentation_path.read_bytes() == original
    assert not list(tmp_path.glob(".*.dovie-revision.lock"))


def test_regenerate_presentation_slide_rejects_non_dovie_pptx(tmp_path):
    presentation_path = tmp_path / "foreign.pptx"
    with zipfile.ZipFile(presentation_path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")

    with pytest.raises(ValueError, match="only supported for Dovie"):
        regenerate_presentation_slide(
            {
                "presentation_path": str(presentation_path),
                "page_number": 1,
                "prompt": "Replacement",
            },
            image_generator=lambda _args: "{}",
            workspace_root=str(tmp_path),
            interrupted=lambda: False,
        )
