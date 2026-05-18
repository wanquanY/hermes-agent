from __future__ import annotations

import copy
import threading
from pathlib import Path

_cfg_lock = threading.Lock()
_cfg_cache: dict | None = None
_cfg_mtime: float | None = None
_cfg_path: Path | None = None


def load_cfg(hermes_home: Path) -> dict:
    global _cfg_cache, _cfg_mtime, _cfg_path
    try:
        import yaml

        path = hermes_home / "config.yaml"
        mtime = path.stat().st_mtime if path.exists() else None
        with _cfg_lock:
            if _cfg_cache is not None and _cfg_mtime == mtime and _cfg_path == path:
                return copy.deepcopy(_cfg_cache)
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        with _cfg_lock:
            _cfg_cache = copy.deepcopy(data)
            _cfg_mtime = mtime
            _cfg_path = path
        return data
    except Exception:
        return {}


def save_cfg(hermes_home: Path, cfg: dict) -> None:
    global _cfg_cache, _cfg_mtime, _cfg_path
    import yaml

    path = hermes_home / "config.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)
    with _cfg_lock:
        _cfg_cache = copy.deepcopy(cfg)
        _cfg_path = path
        try:
            _cfg_mtime = path.stat().st_mtime
        except Exception:
            _cfg_mtime = None
