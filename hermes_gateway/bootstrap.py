"""Gateway bootstrap and process-environment helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def ensure_windows_gateway_venv_imports() -> None:
    """Make detached Windows gateway runs see the Hermes venv packages."""
    if sys.platform != "win32":
        return
    import site

    project_root = Path(__file__).resolve().parent.parent
    candidates: list[Path] = []
    if os.environ.get("VIRTUAL_ENV"):
        candidates.append(Path(os.environ["VIRTUAL_ENV"]))
    candidates.append(project_root / "venv")
    seen: set[str] = set()
    for venv_dir in candidates:
        try:
            resolved_venv = venv_dir.resolve()
        except OSError:
            resolved_venv = venv_dir
        venv_key = str(resolved_venv).lower()
        if venv_key in seen:
            continue
        seen.add(venv_key)
        site_packages = resolved_venv / "Lib" / "site-packages"
        if not site_packages.exists():
            continue
        project_entry = str(project_root)
        site_entry = str(site_packages)
        if project_entry not in sys.path:
            sys.path.insert(0, project_entry)
        site.addsitedir(site_entry)
        if site_entry in sys.path:
            sys.path.remove(site_entry)
        insert_at = 1 if sys.path and sys.path[0] == project_entry else 0
        sys.path.insert(insert_at, site_entry)
        os.environ["VIRTUAL_ENV"] = str(resolved_venv)
        pythonpath = [project_entry, site_entry]
        if os.environ.get("PYTHONPATH"):
            pythonpath.append(os.environ["PYTHONPATH"])
        os.environ["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(pythonpath))
        return


def float_env(name: str, default: float) -> float:
    """Read an env var as float, falling back to ``default`` on typos/empty."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def ensure_ssl_certs() -> None:
    """Set SSL_CERT_FILE if the system doesn't expose CA certs to Python."""
    if "SSL_CERT_FILE" in os.environ:
        return

    import ssl

    paths = ssl.get_default_verify_paths()
    for candidate in (paths.cafile, paths.openssl_cafile):
        if candidate and os.path.exists(candidate):
            os.environ["SSL_CERT_FILE"] = candidate
            return

    try:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
        return
    except ImportError:
        pass

    for candidate in (
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
        "/etc/ssl/ca-bundle.pem",
        "/etc/ssl/cert.pem",
        "/etc/pki/tls/cert.pem",
        "/usr/local/etc/openssl@1.1/cert.pem",
        "/opt/homebrew/etc/openssl@1.1/cert.pem",
    ):
        if os.path.exists(candidate):
            os.environ["SSL_CERT_FILE"] = candidate
            return


def home_target_env_var(platform_name: str) -> str:
    """Return the configured home-target env var for a platform."""
    from cron.scheduler import _resolve_home_env_var

    resolved = _resolve_home_env_var(platform_name)
    if resolved:
        return resolved
    return f"{platform_name.upper()}_HOME_CHANNEL"


def home_thread_env_var(platform_name: str) -> str:
    """Return the optional thread/topic env var for a platform home target."""
    return f"{home_target_env_var(platform_name)}_THREAD_ID"


def restart_notification_pending(hermes_home: Path) -> bool:
    """Return True when a /restart completion marker is waiting to be delivered."""
    return (hermes_home / ".restart_notify.json").exists()


def reload_runtime_env_preserving_config_authority(
    hermes_home: Path,
    *,
    project_env: Path | None = None,
) -> None:
    """Reload .env for fresh credentials without letting stale .env override config."""
    from hermes_cli.env_loader import load_hermes_dotenv

    if project_env is None:
        project_env = Path(__file__).resolve().parent.parent / ".env"
    load_hermes_dotenv(hermes_home=hermes_home, project_env=project_env)

    config_path = hermes_home / "config.yaml"
    if not config_path.exists():
        return
    try:
        import yaml as _yaml
        from hermes_cli.config import _expand_env_vars

        with open(config_path, encoding="utf-8") as f:
            cfg = _yaml.safe_load(f) or {}
        cfg = _expand_env_vars(cfg)
    except Exception:
        return

    agent_cfg = cfg.get("agent", {})
    if isinstance(agent_cfg, dict) and "max_turns" in agent_cfg:
        os.environ["HERMES_MAX_ITERATIONS"] = str(agent_cfg["max_turns"])


def resolve_hermes_bin() -> Optional[list[str]]:
    """Resolve the Hermes update command as argv parts."""
    import shutil

    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        return [hermes_bin]

    try:
        import importlib.util

        if importlib.util.find_spec("hermes_cli") is not None:
            return [sys.executable, "-m", "hermes_cli.main"]
    except Exception:
        pass

    return None
