from __future__ import annotations

from tui_gateway import server
from tui_gateway.methods import presentation as presentation_methods


def test_presentation_slide_regenerate_rpc_uses_explicit_workspace(
    tmp_path,
    monkeypatch,
):
    presentation_path = tmp_path / "review.pptx"
    presentation_path.write_bytes(b"pptx")
    calls = []

    def regenerate(args, *, image_generator, workspace_root):
        calls.append(
            {
                "args": args,
                "image_generator": image_generator,
                "workspace_root": workspace_root,
            }
        )
        return {
            "dovie_event": "presentation_slide_regenerated",
            "status": "completed",
            "output_path": str(presentation_path),
            "page_number": 2,
            "prompt": "Revised comparison",
            "revision": 1,
        }

    monkeypatch.setattr(
        presentation_methods,
        "regenerate_presentation_slide",
        regenerate,
    )
    response = server._methods["presentation.slide.regenerate"](
        "request-1",
        {
            "workspace_path": str(tmp_path),
            "presentation_path": str(presentation_path),
            "page_number": 2,
            "prompt": "Revised comparison",
        },
    )

    assert response["result"]["status"] == "completed"
    assert calls[0]["workspace_root"] == str(tmp_path)
    assert calls[0]["args"] == {
        "presentation_path": str(presentation_path),
        "page_number": 2,
        "prompt": "Revised comparison",
        "title": None,
        "model": None,
        "quality": None,
        "reference_image_url": None,
        "use_current_slide_as_reference": True,
    }
    assert callable(calls[0]["image_generator"])


def test_presentation_slide_regenerate_rpc_requires_existing_workspace():
    response = server._methods["presentation.slide.regenerate"](
        "request-2",
        {
            "workspace_path": "/path/that/does/not/exist",
            "presentation_path": "/path/that/does/not/exist/review.pptx",
            "page_number": 1,
            "prompt": "Revised",
        },
    )

    assert response["error"]["code"] == 4004
