import json
import zipfile

import dovie_extension.document_parse_tool as document_parse_tool
from dovie_extension.document_parse_tool import parse_document_tool
from dovie_extension.prompt_attachments import (
    enrich_prompt_with_document_attachments,
    format_document_attachment_context,
)
from tui_gateway.services.prompt_attachments import submitted_image_paths
from tools.registry import registry


def test_parse_document_reads_plain_text(tmp_path):
    document = tmp_path / "note.txt"
    document.write_text("hello document\nsecond line\n", encoding="utf-8")

    result = json.loads(parse_document_tool(path=str(document), max_chars=1000))

    assert result["success"] is True
    assert result["parser"] == "direct_text"
    assert result["content"] == "hello document\nsecond line\n"
    assert result["metadata"]["lines"] == 2


def test_parse_document_extracts_pptx_text_with_local_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("DOVIE_MINERU_PROXY_URL", raising=False)
    document = tmp_path / "slides.pptx"
    slide_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody>
    <a:p><a:r><a:t>Deck title</a:t></a:r></a:p>
    <a:p><a:r><a:t>Slide body</a:t></a:r></a:p>
  </p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>"""
    with zipfile.ZipFile(document, "w") as zf:
        zf.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("ppt/slides/slide1.xml", slide_xml)

    result = json.loads(parse_document_tool(path=str(document), max_chars=1000))

    assert result["success"] is True
    assert result["parser"] == "pptx_xml"
    assert "Deck title" in result["content"]
    assert "Slide body" in result["content"]
    assert result["metadata"]["mineru_fallback_reason"] == "Dovie MinerU proxy is not configured"


def test_parse_document_uploads_local_pdf_to_dovie_mineru_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("DOVIE_MINERU_PROXY_URL", "https://dovie.example/api/v1/llm-proxy/v1/document-parse")
    monkeypatch.setenv("DOVIE_LLM_RUNTIME_TOKEN", "runtime-token")
    document = tmp_path / "brief.pdf"
    document.write_bytes(b"%PDF-1.4\nlocal pdf\n")

    calls = []

    def fake_post_multipart(url, *, token, fields, files, timeout):
        calls.append((url, token, fields, files))
        assert url == "https://dovie.example/api/v1/llm-proxy/v1/document-parse"
        assert token == "runtime-token"
        assert fields["file_name"] == "brief.pdf"
        assert fields["file_type"] == "pdf"
        assert files["file"][0] == "brief.pdf"
        assert files["file"][1] == b"%PDF-1.4\nlocal pdf\n"
        return {
            "success": True,
            "content": "# Parsed PDF\n\nMinerU content",
            "parser": "mineru",
            "metadata": {"source": "dovie_llm_proxy"},
        }

    monkeypatch.setattr(document_parse_tool, "_http_post_multipart", fake_post_multipart)

    result = json.loads(parse_document_tool(path=str(document), max_chars=1000, allow_local_fallback=False))

    assert result["success"] is True
    assert result["parser"] == "mineru"
    assert result["content"] == "# Parsed PDF\n\nMinerU content"
    assert result["metadata"]["source"] == "mineru"
    assert result["metadata"]["local_upload"] is True
    assert len(calls) == 1


def test_parse_document_registered_as_file_tool():
    entry = registry.get_entry("parse_document")

    assert entry is not None
    assert entry.toolset == "file"


def test_document_attachment_context_instructs_model_to_call_parse_document():
    context = format_document_attachment_context([
        {
            "fileName": "paper.pdf",
            "path": "/tmp/paper.pdf",
            "mimeType": "application/pdf",
        }
    ])

    assert "parse_document" in context
    assert "path='/tmp/paper.pdf'" in context
    assert "file_type='pdf'" in context


def test_document_attachment_prompt_enrichment_ignores_images():
    prompt = enrich_prompt_with_document_attachments(
        "总结附件",
        [
            {"fileName": "photo.png", "path": "/tmp/photo.png", "mimeType": "image/png"},
            {"fileName": "paper.pdf", "path": "/tmp/paper.pdf", "mimeType": "application/pdf"},
        ],
    )

    assert prompt.endswith("总结附件")
    assert "paper.pdf" in prompt
    assert "photo.png" not in prompt


def test_dovie_local_image_attachment_routes_to_vision_input(tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    paths = submitted_image_paths({
        "attachments": [
            {
                "name": "photo.png",
                "path": str(image),
                "mimeType": "image/png",
                "kind": "image",
            },
            {
                "name": "paper.pdf",
                "path": str(tmp_path / "paper.pdf"),
                "mimeType": "application/pdf",
                "kind": "file",
            },
        ],
    })

    assert paths == [str(image)]
