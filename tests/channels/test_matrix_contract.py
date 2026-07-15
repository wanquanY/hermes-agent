from __future__ import annotations

import os
from unittest.mock import patch

from channels.platforms.matrix import MAX_MESSAGE_LENGTH
from channels.platforms.matrix import MatrixAdapter
from channels.platforms.matrix import _looks_like_matrix_image_filename
from channels.platforms.matrix import check_matrix_requirements
from channels.config import PlatformConfig


def test_channels_matrix_exports_adapter_contract() -> None:
    assert MatrixAdapter.__name__ == "MatrixAdapter"
    assert MAX_MESSAGE_LENGTH == 4000
    assert isinstance(check_matrix_requirements(), bool)


def test_channels_matrix_image_filename_detection_contract() -> None:
    assert _looks_like_matrix_image_filename("photo.jpg") is True
    assert _looks_like_matrix_image_filename("photo.txt") is False
    assert _looks_like_matrix_image_filename("nested/photo.jpg") is False


def test_channels_matrix_adapter_reads_config_contract() -> None:
    with patch.dict(os.environ, {}, clear=False):
        adapter = MatrixAdapter(
            PlatformConfig(
                enabled=True,
                token="syt_test_token",
                extra={
                    "homeserver": "https://matrix.example.org",
                    "user_id": "@bot:example.org",
                },
            )
        )

    assert adapter._homeserver == "https://matrix.example.org"
    assert adapter._user_id == "@bot:example.org"
    assert adapter._access_token == "syt_test_token"
