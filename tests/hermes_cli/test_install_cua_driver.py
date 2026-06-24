"""Tests for ``install_cua_driver`` upgrade semantics and architecture pre-check.

Hermes owns the cua-driver install/upgrade path by selecting the latest
compatible upstream release asset and copying the signed binary into a writable
local command path. ``install_cua_driver(upgrade=True)`` must:

* Be macOS-only — no-op silently on Linux/Windows so ``hermes update`` can
  call it unconditionally without warning every non-macOS user.
* Refresh the release asset even when the binary is already on PATH (this is
  the fix for the "we only pulled cua-driver once on enable" complaint).
* Preserve original ``upgrade=False`` behaviour for the toolset-enable flow:
  skip if installed, install otherwise, warn on non-macOS.
* Select an architecture-compatible asset before downloading to avoid raw 404
  errors on Intel macOS when the upstream release lacks x86_64 assets.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


class TestInstallCuaDriverUpgrade:
    def test_upgrade_on_non_macos_is_silent_noop(self):
        from hermes_cli import tools_config

        with patch.object(tools_config, "_print_warning") as warn, \
             patch("platform.system", return_value="Linux"):
            assert tools_config.install_cua_driver(upgrade=True) is False
            warn.assert_not_called()

    def test_non_upgrade_on_non_macos_warns(self):
        from hermes_cli import tools_config

        with patch.object(tools_config, "_print_warning") as warn, \
             patch("platform.system", return_value="Linux"):
            assert tools_config.install_cua_driver(upgrade=False) is False
            warn.assert_called()

    def test_upgrade_on_macos_with_binary_refreshes_release_asset(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                                                 if n == "cua-driver" else None), \
             patch.object(tools_config, "_install_cua_driver_release_asset",
                          return_value=True) as installer, \
             patch("subprocess.run"):
            assert tools_config.install_cua_driver(upgrade=True) is True
            installer.assert_called_once()
            kwargs = installer.call_args.kwargs
            assert kwargs.get("verbose") is False

    def test_upgrade_on_macos_without_binary_refreshes_release_asset(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which", return_value=None), \
             patch.object(tools_config, "_install_cua_driver_release_asset",
                          return_value=True) as installer:
            assert tools_config.install_cua_driver(upgrade=True) is True
            installer.assert_called_once()

    def test_non_upgrade_on_macos_with_binary_skips_install(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                                                 if n == "cua-driver" else None), \
             patch.object(tools_config, "_install_cua_driver_release_asset") as installer, \
             patch("subprocess.run"):
            assert tools_config.install_cua_driver(upgrade=False) is True
            installer.assert_not_called()

    def test_non_upgrade_on_macos_without_binary_installs_release_asset(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which", return_value=None), \
             patch.object(tools_config, "_install_cua_driver_release_asset",
                          return_value=True) as installer:
            assert tools_config.install_cua_driver(upgrade=False) is True
            installer.assert_called_once()
            kwargs = installer.call_args.kwargs
            assert kwargs.get("verbose") is True


class TestInstallCuaDriverReleaseAsset:
    def test_copy_replaces_local_symlink_without_following_app_bundle_target(self, tmp_path):
        from hermes_cli import tools_config

        source = tmp_path / "new-cua-driver"
        source.write_text("new binary", encoding="utf-8")
        app_bundle_binary = tmp_path / "CuaDriver.app" / "Contents" / "MacOS" / "cua-driver"
        app_bundle_binary.parent.mkdir(parents=True)
        app_bundle_binary.write_text("old app bundle binary", encoding="utf-8")
        link = tmp_path / ".local" / "bin" / "cua-driver"
        link.parent.mkdir(parents=True)
        link.symlink_to(app_bundle_binary)

        tools_config._copy_cua_driver_binary(source, link)

        assert not link.is_symlink()
        assert link.read_text(encoding="utf-8") == "new binary"
        assert app_bundle_binary.read_text(encoding="utf-8") == "old app bundle binary"


class TestCheckCuaDriverAssetForArch:
    def test_arm64_always_returns_true(self):
        from hermes_cli import tools_config

        with patch("platform.machine", return_value="arm64"):
            assert tools_config._check_cua_driver_asset_for_arch() is True

    def test_x86_64_with_asset_returns_true(self):
        from hermes_cli import tools_config

        release = {
            "tag_name": "cua-driver-v0.1.6",
            "assets": [
                {"name": "cua-driver-0.1.6-darwin-arm64.tar.gz"},
                {"name": "cua-driver-0.1.6-darwin-x86_64.tar.gz"},
            ],
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(release).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("platform.machine", return_value="x86_64"), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            assert tools_config._check_cua_driver_asset_for_arch() is True

    def test_x86_64_without_asset_returns_false(self):
        from hermes_cli import tools_config

        release = {
            "tag_name": "cua-driver-v0.1.6",
            "assets": [
                {"name": "cua-driver-0.1.6-darwin-arm64.tar.gz"},
                {"name": "cua-driver.tar.gz"},
            ],
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(release).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("platform.machine", return_value="x86_64"), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(tools_config, "_print_warning") as warn, \
             patch.object(tools_config, "_print_info"):
            assert tools_config._check_cua_driver_asset_for_arch() is False
            warn.assert_called_once()
            assert "no Intel" in warn.call_args[0][0].lower() or "x86_64" in warn.call_args[0][0]

    def test_x86_64_api_failure_returns_true(self):
        """Network failure should fail open — let the installer handle it."""
        from hermes_cli import tools_config

        with patch("platform.machine", return_value="x86_64"), \
             patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            assert tools_config._check_cua_driver_asset_for_arch() is True

    def test_fresh_install_x86_64_no_asset_skips_installer(self):
        """When the latest release has no Intel asset, skip the installer."""
        from hermes_cli import tools_config

        release = {
            "tag_name": "cua-driver-v0.1.6",
            "assets": [{"name": "cua-driver-0.1.6-darwin-arm64.tar.gz"}],
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(release).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch("platform.machine", return_value="x86_64"), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(tools_config, "_print_warning"), \
             patch.object(tools_config, "_print_info"), \
             patch.object(tools_config, "_run_cua_driver_installer") as runner:
            assert tools_config.install_cua_driver(upgrade=False) is False
            runner.assert_not_called()

    def test_upgrade_x86_64_no_asset_returns_existing_status(self):
        """On upgrade with no Intel asset, return whether binary existed."""
        from hermes_cli import tools_config

        release = {
            "tag_name": "cua-driver-v0.1.6",
            "assets": [{"name": "cua-driver-0.1.6-darwin-arm64.tar.gz"}],
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(release).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        # With binary installed — returns True (binary exists)
        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                                                 if n in ("cua-driver", "curl") else None), \
             patch("platform.machine", return_value="x86_64"), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(tools_config, "_print_warning"), \
             patch.object(tools_config, "_print_info"), \
             patch.object(tools_config, "_run_cua_driver_installer") as runner:
            assert tools_config.install_cua_driver(upgrade=True) is True
            runner.assert_not_called()

        # Without binary — returns False
        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch("platform.machine", return_value="x86_64"), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(tools_config, "_print_warning"), \
             patch.object(tools_config, "_print_info"), \
             patch.object(tools_config, "_run_cua_driver_installer") as runner:
            assert tools_config.install_cua_driver(upgrade=True) is False
            runner.assert_not_called()
