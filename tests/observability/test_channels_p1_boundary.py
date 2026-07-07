from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_EXCLUDED_DIRS = {
    ".venv",
    "__pycache__",
    ".import_linter_cache",
    "tests",
    "docs",
}

_MOVED_PLATFORM_MODULES = (
    "base",
    "helpers",
    "_http_client_limits",
    "telegram",
    "telegram_network",
    "discord",
    "slack",
    "feishu",
    "feishu_comment",
    "feishu_comment_rules",
    "qqbot",
    "weixin",
    "wecom",
    "wecom_callback",
    "wecom_crypto",
    "whatsapp",
    "signal",
    "signal_rate_limit",
    "sms",
    "email",
    "dingtalk",
    "homeassistant",
    "mattermost",
    "matrix",
    "bluebubbles",
    "webhook",
    "msgraph_webhook",
    "api_server",
    "yuanbao",
    "yuanbao_media",
    "yuanbao_proto",
    "yuanbao_sticker",
)

_MOVED_PLATFORM_PATTERN = "|".join(re.escape(module) for module in _MOVED_PLATFORM_MODULES)
_OLD_PLATFORM_IMPORT = re.compile(
    r"^\s*(?:from\s+gateway\.platforms\.(?:"
    + _MOVED_PLATFORM_PATTERN
    + r")\b|import\s+gateway\.platforms\.(?:"
    + _MOVED_PLATFORM_PATTERN
    + r")\b|from\s+gateway\.platforms\s+import\s+(?:"
    + _MOVED_PLATFORM_PATTERN
    + r"|YuanbaoAdapter)\b)"
)
_GATEWAY_IMPORT = re.compile(r"^\s*(?:from\s+gateway\b|import\s+gateway\.)")


def _production_python_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if set(rel.parts) & _EXCLUDED_DIRS:
            continue
        files.append(path)
    return files


def test_moved_platform_contracts_are_owned_by_channels_package() -> None:
    """P1 slice guard.

    The platform base and shared helper contracts have moved to
    ``channels.platforms``. No production module should import the removed
    legacy owners.
    """
    offenders: list[str] = []
    for path in _production_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _OLD_PLATFORM_IMPORT.match(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    if offenders:
        raise AssertionError(
            "Production code must import moved platform contracts from "
            "channels.platforms, not gateway.platforms:\n  "
            + "\n  ".join(offenders)
        )


def test_legacy_platform_contract_files_are_removed() -> None:
    legacy_dir = REPO_ROOT / "gateway" / "platforms"
    if legacy_dir.exists():
        raise AssertionError(
            "P1 migrated every platform owner, so gateway/platforms must not "
            "remain as a package or compatibility shim"
        )

    offenders = [
        f"gateway/platforms/{module}"
        for module in _MOVED_PLATFORM_MODULES
        if (
            (REPO_ROOT / "gateway" / "platforms" / f"{module}.py").exists()
            or (REPO_ROOT / "gateway" / "platforms" / module).exists()
        )
    ]

    if offenders:
        raise AssertionError(
            "P1 moved platform contract files must not leave gateway shims:\n  "
            + "\n  ".join(offenders)
        )


def test_legacy_slash_command_shadow_owner_is_removed() -> None:
    legacy_file = REPO_ROOT / "gateway" / "slash_commands.py"
    if legacy_file.exists():
        raise AssertionError(
            "gateway/slash_commands.py is a shadow mixin with no production "
            "entry-point imports; P1 must not keep or recreate it"
        )


def test_channels_package_does_not_import_legacy_gateway_modules() -> None:
    offenders: list[str] = []
    channels_root = REPO_ROOT / "channels"
    for path in channels_root.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _GATEWAY_IMPORT.match(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    if offenders:
        raise AssertionError(
            "P1 channels must not depend on legacy gateway modules:\n  "
            + "\n  ".join(offenders)
        )


def test_legacy_channel_support_owners_are_removed() -> None:
    legacy_paths = [
        "gateway/platform_registry.py",
        "gateway/session_context.py",
        "gateway/status.py",
        "gateway/sticker_cache.py",
        "gateway/whatsapp_identity.py",
    ]
    offenders = [path for path in legacy_paths if (REPO_ROOT / path).exists()]
    if offenders:
        raise AssertionError(
            "P1 moved channel support owners must not leave gateway shims:\n  "
            + "\n  ".join(offenders)
        )
