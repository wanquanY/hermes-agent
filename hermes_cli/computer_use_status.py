"""Status helpers for the macOS Computer Use cua-driver backend."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


CUA_RELEASE_API_URL = "https://api.github.com/repos/trycua/cua/releases?per_page=30"
CUA_INSTALL_COMMAND = "hermes computer-use install"
CUA_UPGRADE_COMMAND = "hermes computer-use install --upgrade"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _version_from_text(value: str) -> str:
    match = _VERSION_RE.search(str(value or ""))
    return match.group(0) if match else ""


def _version_tuple(value: str) -> Optional[Tuple[int, int, int]]:
    match = _VERSION_RE.search(str(value or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _is_update_available(current: str, latest: str) -> Optional[bool]:
    if not latest:
        return None
    if not current:
        return True
    current_tuple = _version_tuple(current)
    latest_tuple = _version_tuple(latest)
    if not current_tuple or not latest_tuple:
        return latest != current
    return latest_tuple > current_tuple


def _asset_matches_machine(name: str, machine: str) -> bool:
    normalized = name.lower()
    if "universal" in normalized:
        return True
    if machine == "arm64":
        return "arm64" in normalized or "aarch64" in normalized
    if machine in {"x86_64", "amd64"}:
        return "x86_64" in normalized or "amd64" in normalized
    return machine.lower() in normalized


def _asset_matches_platform(name: str) -> bool:
    normalized = name.lower()
    if sys.platform == "darwin":
        return "darwin" in normalized or "macos" in normalized
    if sys.platform.startswith("linux"):
        return "linux" in normalized
    if sys.platform == "win32":
        return "windows" in normalized or "win32" in normalized
    return sys.platform.lower() in normalized


def _is_cua_driver_asset(name: str) -> bool:
    normalized = name.lower()
    return "cua-driver" in normalized


def _is_cua_driver_release(release: Dict[str, Any]) -> bool:
    tag = str(release.get("tag_name") or "").lower()
    name = str(release.get("name") or "").lower()
    if tag.startswith("cua-driver") or name.startswith("cua-driver"):
        return True
    assets = release.get("assets")
    if not isinstance(assets, list):
        return False
    return any(
        isinstance(item, dict)
        and _is_cua_driver_asset(str(item.get("name") or ""))
        for item in assets
    )


def _latest_release_asset_compatible(release: Dict[str, Any]) -> Optional[bool]:
    if _best_cua_driver_asset(release) is not None:
        return True
    assets = release.get("assets")
    if not isinstance(assets, list) or not assets:
        return None
    machine = platform.machine()
    names = [
        str(item.get("name") or "")
        for item in assets
        if isinstance(item, dict) and item.get("name")
    ]
    if not names:
        return None
    driver_assets = [name for name in names if _is_cua_driver_asset(name)]
    if not driver_assets:
        return False
    return any(
        _asset_matches_platform(name) and _asset_matches_machine(name, machine)
        for name in driver_assets
    )


def _asset_score(name: str, machine: str) -> int:
    normalized = name.lower()
    if not _is_cua_driver_asset(normalized):
        return 0
    if not _asset_matches_platform(normalized):
        return 0
    if "universal-binary" in normalized:
        return 120
    if "universal" in normalized:
        return 110
    if not _asset_matches_machine(normalized, machine):
        return 0
    if "binary" in normalized:
        return 100
    return 90


def _best_cua_driver_asset(release: Dict[str, Any]) -> Optional[Dict[str, str]]:
    assets = release.get("assets")
    if not isinstance(assets, list) or not assets:
        return None
    machine = platform.machine()
    candidates = []
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        score = _asset_score(name, machine)
        if score <= 0:
            continue
        candidates.append({
            "name": name,
            "url": str(item.get("browser_download_url") or ""),
            "score": score,
        })
    candidates.sort(key=lambda item: (-int(item["score"]), item["name"]))
    if not candidates:
        return None
    best = candidates[0]
    return {
        "name": str(best["name"]),
        "url": str(best["url"]),
    }


def _release_status_payload(release: Dict[str, Any]) -> Dict[str, Any]:
    tag = str(release.get("tag_name") or "")
    asset = _best_cua_driver_asset(release) or {}
    return {
        "tag": tag,
        "version": _version_from_text(tag),
        "url": str(release.get("html_url") or ""),
        "asset_compatible": _latest_release_asset_compatible(release),
        "asset_name": asset.get("name", ""),
        "asset_url": asset.get("url", ""),
    }


def fetch_latest_cua_driver_release() -> Dict[str, Any]:
    req = urllib.request.Request(
        CUA_RELEASE_API_URL,
        headers={"Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    releases = data if isinstance(data, list) else [data]
    first_driver_release: Optional[Dict[str, Any]] = None
    for release in releases:
        if not isinstance(release, dict) or not _is_cua_driver_release(release):
            continue
        payload = _release_status_payload(release)
        if first_driver_release is None:
            first_driver_release = payload
        if payload.get("asset_compatible") is not False:
            return payload
    if first_driver_release is not None:
        return first_driver_release
    raise RuntimeError("no cua-driver release found")


def _default_cua_driver_path() -> Path:
    return Path.home() / ".local" / "bin" / "cua-driver"


def _resolved_cua_driver_path() -> str:
    path = shutil.which("cua-driver")
    if path:
        return path
    local_path = _default_cua_driver_path()
    if local_path.exists() and os.access(local_path, os.X_OK):
        return str(local_path)
    return ""


def _installed_cua_driver_version(binary_path: str = "") -> str:
    try:
        result = subprocess.run(
            [binary_path or "cua-driver", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    output = "\n".join([result.stdout or "", result.stderr or ""]).strip()
    return _version_from_text(output) or output


def get_cua_driver_status(*, check_latest: bool = False) -> Dict[str, Any]:
    """Return structured status for the cua-driver backend.

    The install and upgrade operation remains owned by
    ``hermes_cli.tools_config.install_cua_driver``. This helper only reports
    local state and, when requested, compares against the latest upstream CUA
    release metadata.
    """
    path = _resolved_cua_driver_path()
    supported = sys.platform == "darwin"
    current_version = _installed_cua_driver_version(path) if path else ""
    status: Dict[str, Any] = {
        "tool": "computer_use",
        "backend": "cua-driver",
        "platform": sys.platform,
        "machine": platform.machine(),
        "supported": supported,
        "installed": bool(path),
        "path": path,
        "current_version": current_version,
        "latest_version": "",
        "latest_tag": "",
        "latest_url": "",
        "latest_compatible": None,
        "update_available": None,
        "checked_latest": bool(check_latest),
        "checked_at": int(time.time()),
        "install_command": CUA_INSTALL_COMMAND,
        "upgrade_command": CUA_UPGRADE_COMMAND,
        "error": "",
    }
    if check_latest:
        try:
            latest = fetch_latest_cua_driver_release()
            status.update({
                "latest_version": latest.get("version") or "",
                "latest_tag": latest.get("tag") or "",
                "latest_url": latest.get("url") or "",
                "latest_asset_name": latest.get("asset_name") or "",
                "latest_asset_url": latest.get("asset_url") or "",
                "latest_compatible": latest.get("asset_compatible"),
            })
            compatible = status["latest_compatible"]
            status["update_available"] = (
                False
                if compatible is False
                else _is_update_available(current_version, status["latest_version"])
            )
        except Exception as exc:
            status["error"] = str(exc)
    return status


def format_cua_driver_status(status: Dict[str, Any]) -> str:
    lines = []
    if not status.get("supported"):
        lines.append("cua-driver: unsupported on this platform")
        return "\n".join(lines)

    if status.get("installed"):
        version = status.get("current_version") or "unknown version"
        lines.append(f"cua-driver: installed at {status.get('path')} ({version})")
    else:
        lines.append("cua-driver: not installed")
        lines.append(f"  Run: {status.get('install_command') or CUA_INSTALL_COMMAND}")

    if status.get("checked_latest"):
        latest = status.get("latest_version") or status.get("latest_tag") or "unknown"
        if status.get("latest_compatible") is False:
            lines.append(f"  Latest release is not compatible with {status.get('machine') or 'this machine'}: {latest}")
        elif status.get("error"):
            lines.append(f"  Latest check failed: {status['error']}")
        elif status.get("update_available"):
            current = status.get("current_version") or "not installed"
            lines.append(f"  Update available: {current} -> {latest}")
        elif status.get("latest_version") or status.get("latest_tag"):
            lines.append(f"  Up to date: {latest}")

    if status.get("installed"):
        lines.append(f"  Refresh to latest: {status.get('upgrade_command') or CUA_UPGRADE_COMMAND}")
    return "\n".join(lines)


def print_cua_driver_status(status: Dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(status, ensure_ascii=False, sort_keys=True))
        return
    print(format_cua_driver_status(status))
