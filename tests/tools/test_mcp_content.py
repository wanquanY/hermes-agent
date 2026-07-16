from types import SimpleNamespace

from tools import mcp_content
from tools.mcp_content import normalize_call_tool_result, normalize_mcp_content_blocks


def _block(**kwargs):
    return SimpleNamespace(**kwargs)


def test_mixed_rich_content_and_structured_result_is_lossless(monkeypatch):
    monkeypatch.setattr(
        mcp_content,
        "_cache_bytes",
        lambda data, **kwargs: f"MEDIA:/cache/{kwargs['default_kind']}-{len(data)}",
    )
    result = _block(
        isError=False,
        structuredContent={"count": 4},
        content=[
            _block(type="text", text="hello"),
            _block(
                type="resource_link",
                uri="https://example.test/report",
                name="report",
                description="remote report",
                mimeType="text/markdown",
                size=12,
            ),
            _block(
                type="resource",
                resource=_block(
                    uri="file:///tmp/note.txt", mimeType="text/plain", text="note body"
                ),
            ),
            _block(type="audio", data="aGVsbG8=", mimeType="audio/ogg"),
        ],
    )
    normalized = normalize_call_tool_result(result, server_name="docs")
    assert normalized["structuredContent"] == {"count": 4}
    assert "hello" in normalized["result"]
    assert "mcp__docs__read_resource" in normalized["result"]
    assert "URI was not fetched automatically" in normalized["result"]
    assert "note body" in normalized["result"]
    assert "MEDIA:/cache/audio-5" in normalized["result"]


def test_error_path_preserves_embedded_and_structured_evidence(monkeypatch):
    monkeypatch.setattr(
        mcp_content,
        "_cache_bytes",
        lambda data, **kwargs: "MEDIA:/cache/blob.pdf",
    )
    result = _block(
        isError=True,
        structuredContent={"code": "E_BAD"},
        content=[
            _block(type="text", text="failed"),
            _block(
                type="resource",
                resource=_block(
                    uri="file:///tmp/evidence.pdf",
                    mimeType="application/pdf",
                    blob="aGVsbG8=",
                ),
            ),
        ],
    )
    normalized = normalize_call_tool_result(result, server_name="audit")
    assert normalized["structuredContent"] == {"code": "E_BAD"}
    assert "failed" in normalized["error"]
    assert "MEDIA:/cache/blob.pdf" in normalized["error"]


def test_invalid_and_oversized_base64_are_visible_without_crashing():
    parts = normalize_mcp_content_blocks(
        [
            _block(type="image", data="%%%", mimeType="image/png"),
            _block(type="audio", data="aGVsbG8=", mimeType="audio/ogg"),
        ],
        server_name="media",
        max_block_bytes=1,
    )
    assert "invalid base64" in parts[0]
    assert "exceeds 1 bytes" in parts[1]


def test_resource_link_never_performs_network_io():
    parts = normalize_mcp_content_blocks(
        [_block(type="resource_link", uri="https://169.254.169.254/latest/meta-data")],
        server_name="remote",
    )
    assert "169.254.169.254" in parts[0]
    assert "not fetched automatically" in parts[0]
