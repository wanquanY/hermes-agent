"""Behavior tests for the canonical urllib redirect credential policy."""

from __future__ import annotations

import http.server
import threading
import urllib.request
from contextlib import contextmanager
from urllib.error import HTTPError

import pytest

from hermes_cli.urllib_security import (
    SafeCredentialRedirectHandler,
    open_credentialed_url,
    url_origin,
)


@contextmanager
def _server(handler_type):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _TargetHandler(http.server.BaseHTTPRequestHandler):
    seen: list[dict[str, str]] = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append({key.lower(): value for key, value in self.headers.items()})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):
        return


def _source_handler(cross_target: str):
    class _SourceHandler(http.server.BaseHTTPRequestHandler):
        seen: list[dict[str, str]] = []

        def do_GET(self):  # noqa: N802
            if self.path == "/same":
                self.send_response(302)
                self.send_header("Location", "/final")
                self.end_headers()
                return
            if self.path == "/cross":
                self.send_response(302)
                self.send_header("Location", cross_target)
                self.end_headers()
                return
            if self.path == "/loop":
                self.send_response(302)
                self.send_header("Location", "/loop")
                self.end_headers()
                return
            type(self).seen.append({key.lower(): value for key, value in self.headers.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            return

    return _SourceHandler


def _request(url: str) -> urllib.request.Request:
    request = urllib.request.Request(url)
    request.add_header("Authorization", "Bearer provider-secret")
    request.add_header("X-Custom-Provider-Secret", "custom-secret")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "hermes-test")
    return request


@pytest.fixture(autouse=True)
def _restore_installed_opener():
    previous = getattr(urllib.request, "_opener", None)
    yield
    urllib.request._opener = previous


def test_origin_normalizes_default_port_and_trailing_dot():
    assert url_origin("HTTPS://Example.COM./models") == ("https", "example.com", 443)
    assert url_origin("https://example.com:443/models") == ("https", "example.com", 443)


def test_origin_rejects_malformed_port():
    with pytest.raises(ValueError):
        url_origin("https://example.com:not-a-port/models")


def test_same_origin_redirect_preserves_custom_credentials():
    _TargetHandler.seen.clear()
    with _server(_TargetHandler) as target:
        target_url = f"http://127.0.0.1:{target.server_port}/unused"
        Source = _source_handler(target_url)
        with _server(Source) as source:
            with open_credentialed_url(
                _request(f"http://127.0.0.1:{source.server_port}/same"),
                timeout=2,
            ) as response:
                assert response.read() == b"ok"
        headers = Source.seen[-1]
    assert headers["authorization"] == "Bearer provider-secret"
    assert headers["x-custom-provider-secret"] == "custom-secret"


def test_cross_origin_port_redirect_keeps_only_safe_headers():
    _TargetHandler.seen.clear()
    with _server(_TargetHandler) as target:
        target_url = f"http://127.0.0.1:{target.server_port}/final"
        Source = _source_handler(target_url)
        with _server(Source) as source:
            with open_credentialed_url(
                _request(f"http://127.0.0.1:{source.server_port}/cross"),
                timeout=2,
            ) as response:
                assert response.read() == b"ok"
        headers = _TargetHandler.seen[-1]
    assert "authorization" not in headers
    assert "x-custom-provider-secret" not in headers
    assert headers["accept"] == "application/json"
    assert headers["user-agent"] == "hermes-test"


def test_scheme_downgrade_is_cross_origin_and_strips_credentials():
    request = _request("https://provider.example/models")
    redirected = SafeCredentialRedirectHandler(request.full_url).redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "http://provider.example/models-v2",
    )

    assert redirected is not None
    headers = {key.lower(): value for key, value in redirected.header_items()}
    assert "authorization" not in headers
    assert "x-custom-provider-secret" not in headers
    assert headers["accept"] == "application/json"
    assert headers["user-agent"] == "hermes-test"


def test_redirect_loop_fails_closed():
    _TargetHandler.seen.clear()
    with _server(_TargetHandler) as target:
        target_url = f"http://127.0.0.1:{target.server_port}/unused"
        Source = _source_handler(target_url)
        with _server(Source) as source:
            with pytest.raises(HTTPError, match="redirect error"):
                open_credentialed_url(
                    _request(f"http://127.0.0.1:{source.server_port}/loop"),
                    timeout=2,
                )


def test_final_sanitizer_removes_secret_added_by_installed_request_hook():
    class _LateAuthHook(urllib.request.BaseHandler):
        handler_order = 999_999

        def http_request(self, request):
            request.add_header("X-Installed-Hook-Secret", "late-secret")
            return request

        https_request = http_request

    urllib.request.install_opener(urllib.request.build_opener(_LateAuthHook()))
    _TargetHandler.seen.clear()
    with _server(_TargetHandler) as target:
        target_url = f"http://127.0.0.1:{target.server_port}/final"
        Source = _source_handler(target_url)
        with _server(Source) as source:
            with open_credentialed_url(
                _request(f"http://127.0.0.1:{source.server_port}/cross"),
                timeout=2,
            ) as response:
                assert response.read() == b"ok"
        headers = _TargetHandler.seen[-1]
    assert "x-installed-hook-secret" not in headers
