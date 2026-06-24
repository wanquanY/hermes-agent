"""Structured status checks for the cua-driver Computer Use backend."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock


def _mock_response(payload):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__ = MagicMock(return_value=False)
    return response


def _mock_release(tag: str, assets: list[str]):
    return _mock_response({
        "tag_name": tag,
        "html_url": f"https://github.com/trycua/cua/releases/tag/{tag}",
        "assets": [{"name": name} for name in assets],
    })


def _mock_releases(*items):
    payload = []
    for tag, assets in items:
        payload.append({
            "tag_name": tag,
            "name": tag,
            "html_url": f"https://github.com/trycua/cua/releases/tag/{tag}",
            "assets": [{"name": name} for name in assets],
        })
    return _mock_response(payload)


def test_cua_driver_status_reports_available_update(monkeypatch):
    from hermes_cli import computer_use_status as status_mod

    monkeypatch.setattr(status_mod.sys, "platform", "darwin")
    monkeypatch.setattr(status_mod.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(status_mod.shutil, "which", lambda name: "/usr/local/bin/cua-driver" if name == "cua-driver" else None)
    monkeypatch.setattr(
        status_mod.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="cua-driver 0.1.6\n", stderr=""),
    )
    monkeypatch.setattr(
        status_mod.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _mock_release("cua-driver-v0.1.7", ["cua-driver-0.1.7-darwin-arm64.tar.gz"]),
    )

    result = status_mod.get_cua_driver_status(check_latest=True)

    assert result["installed"] is True
    assert result["current_version"] == "0.1.6"
    assert result["latest_version"] == "0.1.7"
    assert result["latest_compatible"] is True
    assert result["update_available"] is True


def test_cua_driver_status_uses_latest_driver_release_not_repo_latest(monkeypatch):
    from hermes_cli import computer_use_status as status_mod

    monkeypatch.setattr(status_mod.sys, "platform", "darwin")
    monkeypatch.setattr(status_mod.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(status_mod.shutil, "which", lambda name: "/usr/local/bin/cua-driver" if name == "cua-driver" else None)
    monkeypatch.setattr(
        status_mod.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="cua-driver 0.6.5\n", stderr=""),
    )
    monkeypatch.setattr(
        status_mod.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _mock_releases(
            ("agent-v0.8.3", []),
            ("cua-driver-rs-v0.6.6", ["cua-driver-rs-0.6.6-darwin-arm64.tar.gz"]),
        ),
    )

    result = status_mod.get_cua_driver_status(check_latest=True)

    assert result["latest_tag"] == "cua-driver-rs-v0.6.6"
    assert result["latest_version"] == "0.6.6"
    assert result["update_available"] is True


def test_cua_driver_status_selects_best_release_asset(monkeypatch):
    from hermes_cli import computer_use_status as status_mod

    monkeypatch.setattr(status_mod.sys, "platform", "darwin")
    monkeypatch.setattr(status_mod.platform, "machine", lambda: "arm64")
    release = {
        "tag_name": "cua-driver-rs-v0.6.6",
        "html_url": "https://github.com/trycua/cua/releases/tag/cua-driver-rs-v0.6.6",
        "assets": [
            {
                "name": "cua-driver-rs-0.6.6-darwin-arm64.tar.gz",
                "browser_download_url": "https://example.invalid/arm64.tar.gz",
            },
            {
                "name": "cua-driver-rs-0.6.6-darwin-universal-binary.tar.gz",
                "browser_download_url": "https://example.invalid/universal.tar.gz",
            },
        ],
    }

    payload = status_mod._release_status_payload(release)

    assert payload["asset_compatible"] is True
    assert payload["asset_name"] == "cua-driver-rs-0.6.6-darwin-universal-binary.tar.gz"
    assert payload["asset_url"] == "https://example.invalid/universal.tar.gz"


def test_cua_driver_status_falls_back_to_local_bin_path(monkeypatch, tmp_path):
    from hermes_cli import computer_use_status as status_mod

    local_bin = tmp_path / ".local" / "bin" / "cua-driver"
    local_bin.parent.mkdir(parents=True)
    local_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    local_bin.chmod(0o755)
    observed_args = []

    def fake_run(args, **kwargs):
        observed_args.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="cua-driver 0.6.6\n", stderr="")

    monkeypatch.setattr(status_mod.sys, "platform", "darwin")
    monkeypatch.setattr(status_mod.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(status_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(status_mod, "_default_cua_driver_path", lambda: local_bin)
    monkeypatch.setattr(status_mod.subprocess, "run", fake_run)

    result = status_mod.get_cua_driver_status(check_latest=False)

    assert result["installed"] is True
    assert result["path"] == str(local_bin)
    assert result["current_version"] == "0.6.6"
    assert observed_args == [[str(local_bin), "--version"]]


def test_cua_driver_status_blocks_incompatible_latest_asset(monkeypatch):
    from hermes_cli import computer_use_status as status_mod

    monkeypatch.setattr(status_mod.sys, "platform", "darwin")
    monkeypatch.setattr(status_mod.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(status_mod.shutil, "which", lambda name: "/usr/local/bin/cua-driver" if name == "cua-driver" else None)
    monkeypatch.setattr(
        status_mod.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="cua-driver 0.1.6\n", stderr=""),
    )
    monkeypatch.setattr(
        status_mod.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _mock_release("cua-driver-v0.1.7", ["cua-driver-0.1.7-darwin-arm64.tar.gz"]),
    )

    result = status_mod.get_cua_driver_status(check_latest=True)

    assert result["latest_compatible"] is False
    assert result["update_available"] is False


def test_cua_driver_status_json_printer(capsys):
    from hermes_cli.computer_use_status import print_cua_driver_status

    print_cua_driver_status({
        "tool": "computer_use",
        "backend": "cua-driver",
        "installed": False,
    }, as_json=True)

    assert json.loads(capsys.readouterr().out)["backend"] == "cua-driver"
