def test_enrich_with_attached_images_builds_visible_tool_context(monkeypatch, tmp_path):
    from tools import vision_tools
    from tui_gateway.services.media import enrich_with_attached_images

    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    async def fake_vision_analyze_tool(image_url, user_prompt, model=None):
        raise AssertionError("prompt image context must not call vision_analyze synchronously")

    monkeypatch.setattr(vision_tools, "vision_analyze_tool", fake_vision_analyze_tool)

    enriched = enrich_with_attached_images("这个图片内容是什么？", [str(image_path)])

    assert "vision_analyze" in enriched
    assert repr(str(image_path)) in enriched
    assert f"Path: {image_path}" in enriched
    assert "这个图片内容是什么？" in enriched
    assert "Do not infer visual contents" in enriched


def test_enrich_with_attached_images_deduplicates_refs(monkeypatch, tmp_path):
    from tools import vision_tools
    from tui_gateway.services.media import enrich_with_attached_images

    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    async def fake_vision_analyze_tool(image_url, user_prompt, model=None):
        raise AssertionError("prompt image context must not call vision_analyze synchronously")

    monkeypatch.setattr(vision_tools, "vision_analyze_tool", fake_vision_analyze_tool)

    enriched = enrich_with_attached_images("分析一下", [str(image_path), str(image_path)])

    assert enriched.count(f"Path: {image_path}") == 1
    assert enriched.count("Required tool call: vision_analyze") == 1


def test_enrich_with_attached_images_includes_remote_url_tool_context(monkeypatch):
    from tools import vision_tools
    from tui_gateway.services.media import enrich_with_attached_images

    async def fake_vision_analyze_tool(image_url, user_prompt, model=None):
        raise AssertionError("prompt image context must not call vision_analyze synchronously")

    monkeypatch.setattr(vision_tools, "vision_analyze_tool", fake_vision_analyze_tool)

    enriched = enrich_with_attached_images("分析一下", ["https://example.com/chart.png"])

    assert "URL: https://example.com/chart.png" in enriched
    assert "vision_analyze(image_url='https://example.com/chart.png'" in enriched
    assert "分析一下" in enriched


def test_build_image_aware_run_message_routes_text_url_natively(monkeypatch):
    from agent import auxiliary_client, image_routing
    from hermes_cli import config
    from tui_gateway.services.prompt_image_routing import build_image_aware_run_message

    monkeypatch.setattr(auxiliary_client, "_read_main_provider", lambda: "openai")
    monkeypatch.setattr(auxiliary_client, "_read_main_model", lambda: "gpt-test")
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(image_routing, "_lookup_supports_vision", lambda provider, model, cfg: True)
    logs = []

    def log_prompt_stage(session, sid, stage, **fields):
        logs.append((stage, fields))

    message = build_image_aware_run_message(
        prompt="分析 https://example.com/chart.png",
        prompt_text="分析 https://example.com/chart.png",
        submitted_images=[],
        session={},
        sid="runtime-1",
        run_id="run-1",
        turn_id="turn-1",
        log_prompt_stage=log_prompt_stage,
    )

    assert isinstance(message, list)
    assert message[0]["type"] == "text"
    assert "[Image attached: https://example.com/chart.png]" in message[0]["text"]
    assert message[1] == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/chart.png"},
    }
    assert (
        "native-image-build-end",
        {"run_id": "run-1", "turn_id": "turn-1", "skipped_count": 0, "part_count": 2},
    ) in logs
