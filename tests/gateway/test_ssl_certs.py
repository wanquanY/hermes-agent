"""Tests for SSL certificate auto-detection in the gateway bootstrap owner."""

import os
from unittest.mock import patch, MagicMock

from hermes_gateway.bootstrap import ensure_ssl_certs


class TestEnsureSslCerts:
    def test_respects_existing_env_var(self):
        with patch.dict(os.environ, {"SSL_CERT_FILE": "/custom/ca.pem"}):
            ensure_ssl_certs()
            assert os.environ["SSL_CERT_FILE"] == "/custom/ca.pem"

    def test_sets_from_ssl_default_paths(self, tmp_path):
        cert = tmp_path / "ca.crt"
        cert.write_text("FAKE CERT")

        mock_paths = MagicMock()
        mock_paths.cafile = str(cert)
        mock_paths.openssl_cafile = None

        env = {k: v for k, v in os.environ.items() if k != "SSL_CERT_FILE"}
        with patch.dict(os.environ, env, clear=True), \
             patch("ssl.get_default_verify_paths", return_value=mock_paths):
            ensure_ssl_certs()
            assert os.environ.get("SSL_CERT_FILE") == str(cert)

    def test_no_op_when_nothing_found(self):
        mock_paths = MagicMock()
        mock_paths.cafile = None
        mock_paths.openssl_cafile = None

        env = {k: v for k, v in os.environ.items() if k != "SSL_CERT_FILE"}
        with patch.dict(os.environ, env, clear=True), \
             patch("ssl.get_default_verify_paths", return_value=mock_paths), \
             patch("os.path.exists", return_value=False), \
             patch.dict("sys.modules", {"certifi": None}):
            ensure_ssl_certs()
            assert "SSL_CERT_FILE" not in os.environ
