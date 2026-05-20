import json

import tools.doxie_web_tools as doxie_web_tools
from tools.registry import discover_builtin_tools, registry
from toolsets import get_toolset


def test_serper_search_uses_doxie_proxy(monkeypatch):
    monkeypatch.setenv("DOXIE_SERPER_PROXY_URL", "https://doxie.example/api/v1/llm-proxy/v1/serper-search")
    monkeypatch.setenv("DOXIE_LLM_RUNTIME_TOKEN", "runtime-token")
    calls = []

    def fake_post_json(url, payload, *, token, timeout):
        calls.append((url, payload, token, timeout))
        return {
            "success": True,
            "query": payload["query"],
            "data": {"results": [{"title": "Result", "link": "https://example.com"}]},
        }

    monkeypatch.setattr(doxie_web_tools, "_post_json", fake_post_json)

    result = json.loads(doxie_web_tools.serper_search_tool({"query": "Doxie", "num": 3}))

    assert result["success"] is True
    assert calls == [(
        "https://doxie.example/api/v1/llm-proxy/v1/serper-search",
        {
            "query": "Doxie",
            "location": None,
            "gl": None,
            "hl": None,
            "page": 1,
            "num": 3,
            "time_range": None,
        },
        "runtime-token",
        45,
    )]


def test_jina_parser_uses_doxie_proxy(monkeypatch):
    monkeypatch.setenv("DOXIE_WEB_PARSE_PROXY_URL", "https://doxie.example/api/v1/llm-proxy/v1/web-page-parse")
    monkeypatch.setenv("DOXIE_LLM_RUNTIME_TOKEN", "runtime-token")
    calls = []

    def fake_post_json(url, payload, *, token, timeout):
        calls.append((url, payload, token, timeout))
        return {"success": True, "content": "# Article", "content_type": "markdown"}

    monkeypatch.setattr(doxie_web_tools, "_post_json", fake_post_json)

    result = json.loads(doxie_web_tools.jina_web_parser_tool({"url": "https://example.com/a"}))

    assert result["success"] is True
    assert calls[0][0] == "https://doxie.example/api/v1/llm-proxy/v1/web-page-parse"
    assert calls[0][1]["url"] == "https://example.com/a"
    assert calls[0][2] == "runtime-token"
    assert calls[0][3] == 45


def test_jina_parser_honors_bounded_timeout(monkeypatch):
    monkeypatch.setenv("DOXIE_WEB_PARSE_PROXY_URL", "https://doxie.example/api/v1/llm-proxy/v1/web-page-parse")
    monkeypatch.setenv("DOXIE_LLM_RUNTIME_TOKEN", "runtime-token")
    calls = []

    def fake_post_json(url, payload, *, token, timeout):
        calls.append((url, payload, token, timeout))
        return {"success": True, "content": "# Article", "content_type": "markdown"}

    monkeypatch.setattr(doxie_web_tools, "_post_json", fake_post_json)

    doxie_web_tools.jina_web_parser_tool({"url": "https://example.com/a", "timeout": 120})
    doxie_web_tools.jina_web_parser_tool({"url": "https://example.com/b", "timeout": 2})

    assert calls[0][3] == 60
    assert calls[1][3] == 5


def test_doxie_web_tools_are_registered():
    discover_builtin_tools()

    serper_entry = registry.get_entry("serper_search_tool")
    parser_entry = registry.get_entry("jina_web_parser_tool")

    assert serper_entry is not None
    assert serper_entry.toolset == "doxie_web"
    assert parser_entry is not None
    assert parser_entry.toolset == "doxie_web"
    assert get_toolset("doxie_web")["tools"] == ["jina_web_parser_tool", "serper_search_tool"]


def test_doxie_web_toolset_is_configurable():
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS

    configurable = {name for name, _label, _summary in CONFIGURABLE_TOOLSETS}

    assert "doxie_web" in configurable
