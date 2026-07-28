"""Process-local derived model discovery cache.

Discovered provider models are not connection truth and must never be written
to the owner-scoped connection YAML.  The control plane can nevertheless keep
the latest successful validation result so ``model.options`` immediately
projects it to Desktop until the connection changes or the process restarts.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Iterable

_LOCK = threading.RLock()
_MODELS: dict[tuple[str, str, int], tuple[str, ...]] = {}
_VALIDATIONS: dict[tuple[str, str, int], dict[str, object]] = {}


def store_discovered_models(
    *,
    owner_id: str,
    connection_id: str,
    connection_revision: int,
    model_ids: Iterable[str],
    status: str = "ready",
    limited: bool = False,
    reachable: bool | None = True,
) -> None:
    normalized = tuple(
        dict.fromkeys(
            str(model_id or "").strip()
            for model_id in model_ids
            if str(model_id or "").strip()
        )
    )
    key = (str(owner_id), str(connection_id), int(connection_revision))
    with _LOCK:
        clear_discovered_models(
            owner_id=owner_id,
            connection_id=connection_id,
        )
        _MODELS[key] = normalized
        _VALIDATIONS[key] = {
            "status": str(status or "ready"),
            "limited": bool(limited),
            "reachable": reachable,
            "validated_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }


def discovered_models(
    *,
    owner_id: str,
    connection_id: str,
    connection_revision: int,
) -> tuple[str, ...]:
    key = (str(owner_id), str(connection_id), int(connection_revision))
    with _LOCK:
        return _MODELS.get(key, ())


def validation_observation(
    *,
    owner_id: str,
    connection_id: str,
    connection_revision: int,
) -> dict[str, object] | None:
    key = (str(owner_id), str(connection_id), int(connection_revision))
    with _LOCK:
        observation = _VALIDATIONS.get(key)
        return dict(observation) if observation is not None else None


def clear_discovered_models(*, owner_id: str, connection_id: str) -> None:
    owner = str(owner_id)
    connection = str(connection_id)
    with _LOCK:
        for key in [
            key
            for key in _MODELS
            if key[0] == owner and key[1] == connection
        ]:
            _MODELS.pop(key, None)
            _VALIDATIONS.pop(key, None)


__all__ = [
    "clear_discovered_models",
    "discovered_models",
    "store_discovered_models",
    "validation_observation",
]
