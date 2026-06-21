import json


def test_enrich_with_attached_images_uses_vision_analyze_tool(monkeypatch, tmp_path):
    from tools import vision_tools
    from tui_gateway.services.media import enrich_with_attached_images

    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    calls = []

    async def fake_vision_analyze_tool(image_url, user_prompt, model=None):
        calls.append({"image_url": image_url, "user_prompt": user_prompt, "model": model})
        return json.dumps({"success": True, "analysis": "A blue dashboard is visible."})

    monkeypatch.setattr(vision_tools, "vision_analyze_tool", fake_vision_analyze_tool)

    enriched = enrich_with_attached_images("这个图片内容是什么？", [str(image_path)])

    assert calls[0]["image_url"] == str(image_path)
    assert "A blue dashboard is visible." in enriched
    assert f"Path: {image_path}" in enriched
    assert "这个图片内容是什么？" in enriched


def test_enrich_with_attached_images_keeps_retry_path_when_vision_fails(monkeypatch, tmp_path):
    from tools import vision_tools
    from tui_gateway.services.media import enrich_with_attached_images

    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    async def fake_vision_analyze_tool(image_url, user_prompt, model=None):
        return json.dumps({
            "success": False,
            "error": "provider unavailable",
            "analysis": "There was a problem with the request.",
        })

    monkeypatch.setattr(vision_tools, "vision_analyze_tool", fake_vision_analyze_tool)

    enriched = enrich_with_attached_images("分析一下", [str(image_path)])

    assert "Image analysis failed: There was a problem with the request. (provider unavailable)" in enriched
    assert f"Path: {image_path}" in enriched
