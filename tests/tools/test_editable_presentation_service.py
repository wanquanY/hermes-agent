import json
import sys
import zipfile
from pathlib import Path

import pytest

from tools.presentation.editable_service import (
    PRESENTATION_MIME_TYPE,
    build_editable_presentation,
    normalize_slidespec,
)


def _sample_spec(image_path: Path | None = None) -> dict:
    elements = [
        {
            "id": "title",
            "type": "text",
            "box": [0.08, 0.08, 0.84, 0.12],
            "text": "Quarterly review",
            "style": "title",
        },
        {
            "id": "metric",
            "type": "chart",
            "box": [0.08, 0.26, 0.52, 0.54],
            "kind": "bar",
            "data": {
                "labels": ["Q1", "Q2"],
                "series": [{"name": "Revenue", "values": [12, 18]}],
            },
        },
        {
            "id": "summary",
            "type": "table",
            "box": [0.64, 0.26, 0.28, 0.54],
            "rows": [["Metric", "Value"], ["Growth", "50%"]],
        },
    ]
    if image_path is not None:
        elements.append(
            {
                "id": "logo",
                "type": "image",
                "box": [0.84, 0.02, 0.08, 0.05],
                "src": str(image_path),
                "fit": "contain",
            }
        )
    return {
        "version": "slidespec/1",
        "title": "Quarterly review",
        "page_size": "16:9",
        "theme": {"preset": "executive-blue"},
        "pages": [
            {
                "id": "cover",
                "speaker_notes": "Lead with the growth story.",
                "sources": [
                    {
                        "label": "Board metrics",
                        "uri": "workspace://metrics.csv",
                        "locator": "Q1:Q2",
                    }
                ],
                "elements": elements,
            }
        ],
    }


def _write_fake_renderer(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "payload = json.load(sys.stdin)",
                "output = pathlib.Path(payload['output_path'])",
                "output.write_bytes(b'fake-pptx')",
                "spec_path = output.with_suffix('.slidespec.json')",
                "spec_path.write_text(json.dumps(payload['spec']), encoding='utf-8')",
                "preview = {'status': 'skipped', 'reason': 'render_preview=false', 'pdf_path': None, 'page_images': []}",
                "if payload.get('render_preview', True):",
                "    preview_root = output.with_name(f'.{output.stem}.preview')",
                "    preview_root.mkdir(parents=True, exist_ok=True)",
                "    page_path = preview_root / 'page-1.png'",
                "    page_path.write_bytes(b'fake-png')",
                "    preview = {'status': 'completed', 'renderer': 'dovie-artifact-runtime', 'pdf_path': None, 'page_images': [str(page_path)]}",
                "print(json.dumps({",
                "  'status': 'completed',",
                "  'kind': 'dovie.editable_presentation',",
                "  'schema_version': 'slidespec/1',",
                "  'title': payload['spec'].get('title', ''),",
                "  'page_count': len(payload['spec']['pages']),",
                "  'output_path': str(output),",
                "  'slidespec_path': str(spec_path),",
                "  'diagnostics': [],",
                "  'preview': preview,",
                "}))",
            ]
        ),
        encoding="utf-8",
    )

def _write_reference_deck(path: Path) -> None:
    presentation = """<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldSz cx="12192000" cy="6858000"/>
</p:presentation>"""
    theme = """<?xml version="1.0" encoding="UTF-8"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <a:themeElements>
    <a:clrScheme name="Reference">
      <a:dk1><a:srgbClr val="101820"/></a:dk1>
      <a:lt1><a:srgbClr val="FFF8EE"/></a:lt1>
      <a:dk2><a:srgbClr val="4D5966"/></a:dk2>
      <a:lt2><a:srgbClr val="F4EBDD"/></a:lt2>
      <a:accent1><a:srgbClr val="FF5A36"/></a:accent1>
      <a:accent2><a:srgbClr val="2563EB"/></a:accent2>
      <a:accent3><a:srgbClr val="C2412D"/></a:accent3>
      <a:accent4><a:srgbClr val="D9CDBD"/></a:accent4>
    </a:clrScheme>
    <a:fontScheme name="Reference Fonts">
      <a:majorFont><a:latin typeface="Aptos Display"/><a:ea typeface="Source Han Sans SC"/></a:majorFont>
      <a:minorFont><a:latin typeface="Aptos"/><a:ea typeface="Source Han Sans SC"/></a:minorFont>
    </a:fontScheme>
  </a:themeElements>
</a:theme>"""
    layout = """<?xml version="1.0" encoding="UTF-8"?>
<p:sldLayout xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld name="Title and visual"/>
</p:sldLayout>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/theme/theme1.xml", theme)
        archive.writestr("ppt/slideLayouts/slideLayout1.xml", layout)


def test_normalize_slidespec_resolves_local_assets(tmp_path):
    image_path = tmp_path / "logo.png"
    image_path.write_bytes(b"png")

    normalized = normalize_slidespec(
        _sample_spec(image_path),
        workspace_root=str(tmp_path),
    )

    image = normalized["pages"][0]["elements"][-1]
    assert image["src"] == str(image_path.resolve())


def test_build_editable_presentation_applies_reference_deck_theme(
    monkeypatch,
    tmp_path,
):
    renderer = tmp_path / "fake_renderer.py"
    _write_fake_renderer(renderer)
    reference_deck = tmp_path / "reference.pptx"
    _write_reference_deck(reference_deck)
    monkeypatch.setenv("DOVIE_NODE_BINARY", sys.executable)
    monkeypatch.setenv("DOVIE_DECK_RENDERER_PATH", str(renderer))

    result = build_editable_presentation(
        {
            "title": "Reference-led review",
            "slidespec": _sample_spec(),
            "reference_deck_path": str(reference_deck),
            "render_preview": False,
        },
        workspace_root=str(tmp_path),
        interrupted=lambda: False,
    )

    slidespec = json.loads(Path(result["slidespec_path"]).read_text(encoding="utf-8"))
    assert slidespec["theme"]["palette"]["accent"] == "FF5A36"
    assert slidespec["theme"]["palette"]["background"] == "FFF8EE"
    assert slidespec["theme"]["font_family"] == "Aptos"
    assert slidespec["theme"]["cjk_font_family"] == "Source Han Sans SC"
    assert result["reference_deck"]["layout_names"] == ["Title and visual"]
    assert result["reference_deck"]["mode"] == "theme-and-page-style"


def test_normalize_slidespec_accepts_semantic_editorial_layout(tmp_path):
    image_path = tmp_path / "hero.png"
    image_path.write_bytes(b"png")
    spec = {
        "version": "slidespec/1",
        "title": "Dovie editorial review",
        "page_size": "16:9",
        "theme": {"preset": "editorial-art"},
        "pages": [
            {
                "id": "cover",
                "layout": "editorial-cover",
                "content": {
                    "eyebrow": "JULY / PRODUCT REVIEW",
                    "title": "从工具到创作系统",
                    "subtitle": "原生可编辑、视觉可信、可持续迭代",
                    "image_src": str(image_path),
                    "image_alt": "Abstract installation artwork",
                    "image_fit": "cover",
                },
            },
            {
                "id": "metrics",
                "layout": "editorial-metrics",
                "content": {
                    "title": "关键结果",
                    "metrics": [
                        {"value": "15", "label": "页", "detail": "艺术化叙事"},
                        {"value": "100%", "label": "可编辑", "detail": "原生对象"},
                    ],
                },
            },
        ],
    }

    normalized = normalize_slidespec(spec, workspace_root=str(tmp_path))

    assert normalized["theme"]["preset"] == "editorial-art"
    assert normalized["pages"][0]["layout"] == "editorial-cover"
    assert normalized["pages"][0]["content"]["image_src"] == str(image_path.resolve())
    assert normalized["pages"][1]["content"]["metrics"][1]["value"] == "100%"


def test_normalize_slidespec_rejects_stretch_on_semantic_image(tmp_path):
    image_path = tmp_path / "hero.png"
    image_path.write_bytes(b"png")
    spec = {
        "version": "slidespec/1",
        "title": "No distorted evidence",
        "pages": [
            {
                "id": "split",
                "layout": "editorial-split",
                "content": {
                    "title": "Preserve image geometry",
                    "image_src": str(image_path),
                    "image_fit": "stretch",
                },
            }
        ],
    }

    with pytest.raises(ValueError, match="stretch is forbidden"):
        normalize_slidespec(spec, workspace_root=str(tmp_path))


def test_normalize_slidespec_accepts_dovie_grid_templates_and_items(tmp_path):
    spec = {
        "version": "slidespec/1",
        "title": "Dovie Grid review",
        "page_size": "16:9",
        "theme": {"preset": "dovie-grid"},
        "pages": [
            {
                "id": "agenda",
                "layout": "grid-agenda",
                "content": {
                    "eyebrow": "EXECUTIVE REVIEW",
                    "title": "Today’s decisions",
                    "items": [
                        {
                            "label": "01",
                            "title": "Align the narrative",
                            "body": "One audience-facing message per slide.",
                            "detail": "Decision",
                        },
                        {
                            "label": "02",
                            "value": "3×",
                            "title": "Show the evidence",
                            "body": "Prefer charts and meaningful visuals.",
                        },
                    ],
                },
            },
            {
                "id": "closing",
                "layout": "grid-closing",
                "content": {
                    "title": "Build the next version",
                    "subtitle": "Editable, sourced, and visually verified.",
                },
            },
        ],
    }

    normalized = normalize_slidespec(spec, workspace_root=str(tmp_path))

    assert normalized["theme"]["preset"] == "dovie-grid"
    assert normalized["pages"][0]["layout"] == "grid-agenda"
    assert normalized["pages"][0]["content"]["items"][1]["value"] == "3×"
    assert normalized["pages"][1]["layout"] == "grid-closing"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda spec: spec["pages"][0]["elements"][0].update(
                {"box": [0.9, 0.1, 0.2, 0.2]}
            ),
            "normalized 0..1",
        ),
        (
            lambda spec: spec.update({"theme": {"preset": "unknown"}}),
            "theme.preset",
        ),
        (
            lambda spec: spec["pages"][0]["elements"][2].update(
                {"rows": [["A", "B"], ["C"]]}
            ),
            "consistent non-zero",
        ),
        (
            lambda spec: spec["pages"][0]["elements"][0].update(
                {"font_weigth": 700}
            ),
            "unsupported fields: font_weigth",
        ),
        (
            lambda spec: spec["pages"][0]["elements"][0].update(
                {"typography": {"size": 24}}
            ),
            "flatten typography into style, font_size",
        ),
        (
            lambda spec: spec["pages"][0]["elements"].insert(
                0,
                {
                    "type": "rectangle",
                    "box": [0, 0, 1, 1],
                },
            ),
            "use type='shape' with kind='rect'",
        ),
        (
            lambda spec: spec["pages"][0]["elements"].insert(
                0,
                {
                    "type": "shape",
                    "box": [0, 0, 1, 1],
                    "fills": ["#FFFFFF"],
                },
            ),
            "replace fills with one fill or fill_token",
        ),
        (
            lambda spec: spec["pages"][0]["elements"].append(
                {
                    "id": "distorted-image",
                    "type": "image",
                    "box": [0.1, 0.1, 0.4, 0.4],
                    "src": "image.png",
                    "fit": "stretch",
                }
            ),
            "stretch is forbidden",
        ),
    ],
)
def test_normalize_slidespec_rejects_invalid_contract(tmp_path, mutate, message):
    spec = _sample_spec()
    mutate(spec)

    with pytest.raises(ValueError, match=message):
        normalize_slidespec(spec, workspace_root=str(tmp_path))


def test_normalize_slidespec_rejects_asset_outside_allowed_roots(tmp_path):
    outside = tmp_path.parent / "outside-logo.png"
    outside.write_bytes(b"png")

    with pytest.raises(ValueError, match="escapes"):
        normalize_slidespec(
            _sample_spec(outside),
            workspace_root=str(tmp_path),
        )


def test_build_editable_presentation_invokes_controlled_renderer(
    monkeypatch,
    tmp_path,
):
    renderer = tmp_path / "fake_renderer.py"
    _write_fake_renderer(renderer)
    monkeypatch.setenv("DOVIE_NODE_BINARY", sys.executable)
    monkeypatch.setenv("DOVIE_DECK_RENDERER_PATH", str(renderer))

    result = build_editable_presentation(
        {
            "title": "Quarterly review",
            "slidespec": _sample_spec(),
            "output_path": "deliverables/review.pptx",
            "render_preview": False,
        },
        workspace_root=str(tmp_path),
        interrupted=lambda: False,
    )

    output = tmp_path / "deliverables" / "review.pptx"
    assert output.read_bytes() == b"fake-pptx"
    assert result["status"] == "completed"
    assert result["dovie_event"] == "presentation_build_completed"
    assert result["mime_type"] == PRESENTATION_MIME_TYPE
    assert result["preview"]["status"] == "skipped"
    assert result["quality"] == {
        "status": "passed",
        "structural_status": "passed",
        "visual_status": "skipped",
        "issue_count": 0,
        "requirements": (
            "Repair every structural issue, attach every rendered page image "
            "as a native multimodal input at readable size, and rebuild until "
            "clean. A vision-capable current authoring model must inspect the "
            "raw pixels itself; auxiliary vision analysis is the fallback only "
            "for a non-vision current model."
        ),
    }
    assert result["artifacts"] == [
        {
            "path": str(output),
            "title": "Quarterly review",
            "mime_type": PRESENTATION_MIME_TYPE,
            "operation": "created",
        }
    ]


def test_build_editable_presentation_uses_artifact_runtime_preview(
    monkeypatch,
    tmp_path,
):
    renderer = tmp_path / "fake_renderer.py"
    _write_fake_renderer(renderer)
    monkeypatch.setenv("DOVIE_NODE_BINARY", sys.executable)
    monkeypatch.setenv("DOVIE_DECK_RENDERER_PATH", str(renderer))

    result = build_editable_presentation(
        {
            "title": "Artifact preview",
            "slidespec": _sample_spec(),
            "output_path": "artifact-preview.pptx",
        },
        workspace_root=str(tmp_path),
        interrupted=lambda: False,
    )

    assert result["preview"]["status"] == "completed"
    assert result["preview"]["renderer"] == "dovie-artifact-runtime"
    assert result["preview"]["pdf_path"] is None
    assert Path(result["preview"]["page_images"][0]).read_bytes() == b"fake-png"
    assert result["quality"]["visual_status"] == "awaiting_agent_review"


def test_build_editable_presentation_returns_unique_path_by_default(
    monkeypatch,
    tmp_path,
):
    renderer = tmp_path / "fake_renderer.py"
    _write_fake_renderer(renderer)
    monkeypatch.setenv("DOVIE_NODE_BINARY", sys.executable)
    monkeypatch.setenv("DOVIE_DECK_RENDERER_PATH", str(renderer))
    existing = tmp_path / "review.pptx"
    existing.write_bytes(b"existing")

    result = build_editable_presentation(
        {
            "title": "Review",
            "slidespec": _sample_spec(),
            "output_path": "review.pptx",
            "render_preview": False,
        },
        workspace_root=str(tmp_path),
        interrupted=lambda: False,
    )

    assert result["output_path"] == str(tmp_path / "review-2.pptx")
    assert existing.read_bytes() == b"existing"
