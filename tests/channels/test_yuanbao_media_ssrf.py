"""SSRF contracts for Yuanbao model/inbound media downloads."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from channels.platforms import yuanbao_media


@pytest.mark.asyncio
async def test_initial_unsafe_url_is_blocked_before_client_creation(monkeypatch):
    async def unsafe(_url: str) -> bool:
        return False

    monkeypatch.setattr("tools.url_safety.async_is_safe_url", unsafe)

    def unexpected_client(*_args, **_kwargs):
        raise AssertionError("httpx client must not be created for an unsafe URL")

    monkeypatch.setattr(yuanbao_media.httpx, "AsyncClient", unexpected_client)
    with pytest.raises(ValueError, match="Blocked unsafe URL"):
        await yuanbao_media.download_url("http://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_location_redirect_is_checked_when_next_request_is_missing(monkeypatch):
    checked: list[str] = []

    async def policy(url: str) -> bool:
        checked.append(url)
        return "169.254.169.254" not in url

    monkeypatch.setattr("tools.url_safety.async_is_safe_url", policy)

    class FakeClient:
        def __init__(self, *_args, **kwargs):
            self.hook = kwargs["event_hooks"]["response"][0]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def head(self, url):
            response = SimpleNamespace(
                is_redirect=True,
                url=url,
                headers={"location": "http://169.254.169.254/latest/meta-data/"},
                next_request=None,
            )
            await self.hook(response)
            raise AssertionError("redirect guard should have rejected the response")

    monkeypatch.setattr(yuanbao_media.httpx, "AsyncClient", FakeClient)
    with pytest.raises(ValueError, match="Blocked redirect"):
        await yuanbao_media.download_url("https://public.example/media.png")

    assert checked == [
        "https://public.example/media.png",
        "http://169.254.169.254/latest/meta-data/",
    ]
