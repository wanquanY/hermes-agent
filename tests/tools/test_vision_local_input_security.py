"""Security regressions for model-supplied local vision inputs."""

import json

import pytest

from tools.vision_tools import (
    _VISION_MAX_DOWNLOAD_BYTES,
    _vision_analyze_native,
    video_analyze_tool,
    vision_analyze_tool,
)


@pytest.mark.asyncio
async def test_vision_blocks_secret_bearing_local_file(tmp_path):
    secret = tmp_path / ".env"
    secret.write_bytes(b"\x89PNG\r\n\x1a\n" + b"secret")

    result = json.loads(await vision_analyze_tool(str(secret), "extract text"))

    assert result["success"] is False
    assert "secret-bearing environment file" in result["error"]


@pytest.mark.asyncio
async def test_native_vision_blocks_secret_bearing_local_file(tmp_path):
    secret = tmp_path / ".env.local"
    secret.write_bytes(b"\x89PNG\r\n\x1a\n" + b"secret")

    result = json.loads(await _vision_analyze_native(str(secret), "extract text"))

    assert result["success"] is False
    assert "secret-bearing environment file" in result["error"]


@pytest.mark.asyncio
async def test_video_blocks_secret_bearing_local_file(tmp_path):
    secret = tmp_path / ".env.production"
    secret.write_bytes(b"credential data")

    result = json.loads(await video_analyze_tool(str(secret), "extract text"))

    assert result["success"] is False
    assert "secret-bearing environment file" in result["error"]


@pytest.mark.asyncio
async def test_local_image_size_is_rejected_before_payload_read(tmp_path):
    image = tmp_path / "huge.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    # Sparse growth keeps the test cheap while exercising the real stat path.
    with image.open("r+b") as handle:
        handle.truncate(_VISION_MAX_DOWNLOAD_BYTES + 1)

    result = json.loads(await vision_analyze_tool(str(image), "describe"))

    assert result["success"] is False
    assert "Image too large" in result["error"]
