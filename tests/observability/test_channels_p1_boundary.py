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
    legacy_files = [
        REPO_ROOT / "gateway" / "slash_commands.py",
        REPO_ROOT / "gateway" / "slash_access.py",
    ]
    offenders = [path.relative_to(REPO_ROOT).as_posix() for path in legacy_files if path.exists()]
    if offenders:
        raise AssertionError(
            "P1 slash command owners live in channels/slash_commands. "
            "Legacy gateway slash owners must not remain:\n  "
            + "\n  ".join(offenders)
        )


def test_gateway_runner_does_not_own_slash_command_runtime() -> None:
    run_py = REPO_ROOT / "gateway" / "run.py"
    if not run_py.exists():
        return
    text = run_py.read_text(encoding="utf-8")
    forbidden = [
        "def _check_slash_access(",
        "def _handle_whoami_command(",
        "def _handle_kanban_command(",
        "def _maybe_confirm_destructive_slash(",
        "def _request_slash_confirm(",
    ]
    offenders = [pattern for pattern in forbidden if pattern in text]
    if offenders:
        raise AssertionError(
            "GatewayRunner must delegate slash command runtime ownership to "
            "channels/slash_commands, not define these owners:\n  "
            + "\n  ".join(offenders)
        )


def test_slash_command_target_owner_exists() -> None:
    owner = REPO_ROOT / "channels" / "slash_commands"
    required = ["__init__.py", "access.py", "handlers.py", "confirmation.py"]
    missing = [name for name in required if not (owner / name).exists()]
    if missing:
        raise AssertionError(
            "P1 slash command target owner is incomplete:\n  "
            + "\n  ".join(f"channels/slash_commands/{name}" for name in missing)
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
