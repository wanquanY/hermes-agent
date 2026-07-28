"""Secure MEDIA tag inlining for remote API clients."""

from __future__ import annotations

import base64

from channels.platforms.api_server_support import _resolve_media_to_data_urls

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg=="
)


def test_safe_image_is_inlined(tmp_path, monkeypatch):
    image = tmp_path / "shot.png"
    image.write_bytes(_PNG_BYTES)
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))

    result = _resolve_media_to_data_urls(f"Here: MEDIA:{image}")

    assert "data:image/png;base64," in result
    assert "MEDIA:" not in result


def test_multiple_and_quoted_tags_are_inlined(tmp_path, monkeypatch):
    first = tmp_path / "first.png"
    second = tmp_path / "second.jpg"
    first.write_bytes(_PNG_BYTES)
    second.write_bytes(_PNG_BYTES)
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))

    result = _resolve_media_to_data_urls(
        f"`MEDIA:{first}` and MEDIA:\"{second}\""
    )

    assert result.count(";base64,") == 2


def test_relative_traversal_is_never_resolved():
    text = "MEDIA:../../../../etc/passwd.png"
    assert _resolve_media_to_data_urls(text) == text


def test_credential_floor_overrides_operator_allow_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True)
    secret = ssh / "id_rsa.png"
    secret.write_bytes(_PNG_BYTES)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(home))

    text = f"MEDIA:{secret}"
    assert _resolve_media_to_data_urls(text) == text


def test_non_image_and_oversized_image_are_left_untouched(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "notes.txt"
    document.write_text("hello")
    image = tmp_path / "large.png"
    image.write_bytes(b"x" * 16)
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    monkeypatch.setattr(
        "channels.platforms.api_server_support._MEDIA_DATA_URL_MAX_BYTES",
        1,
    )

    document_text = f"MEDIA:{document}"
    image_text = f"MEDIA:{image}"
    assert _resolve_media_to_data_urls(document_text) == document_text
    assert _resolve_media_to_data_urls(image_text) == image_text
