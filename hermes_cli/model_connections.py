"""User model-connection overlays for managed desktop runtimes.

Hermes remains the owner of provider definitions and runtime interpretation.
This repository stores only user-owned connection overrides: credential
references, custom endpoint settings, manual model metadata, model picker
preferences, enablement, and revision/tombstone state. Secret bytes are
deliberately rejected.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse

import yaml

from hermes_constants import get_hermes_home
from hermes_cli.model_provider_identity import is_dovie_cloud_connection

SCHEMA_VERSION = 1
OWNER_ENV = "DOVIE_LOCAL_OWNER_ID"
CONTROL_HOME_ENV = "DOVIE_HERMES_CONTROL_HOME"
_OWNER_RE = re.compile(r"^(?:owner|guest)_[A-Za-z0-9_-]{8,128}$")
_CONNECTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_API_MODES = frozenset({
    "chat_completions",
    "codex_responses",
    "anthropic_messages",
    "bedrock_converse",
    "codex_app_server",
})
_SECRET_KEYS = frozenset({
    "api",
    "api_key",
    "authorization",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "token",
})
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_MAX_OPERATIONS = 128


class ModelConnectionError(ValueError):
    """Stable model-connection domain failure."""

    def __init__(
        self, code: str, message: str, *, details: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _assert_user_manageable_connection(
    connection_id: Any,
    provider_id: Any = None,
) -> None:
    if is_dovie_cloud_connection(connection_id, provider_id):
        raise ModelConnectionError(
            "MODEL_CONNECTION_PLATFORM_MANAGED",
            "Dovie Cloud is managed by the platform and is not a user connection",
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_local_owner_id(value: str) -> str:
    owner_id = str(value or "").strip()
    if not _OWNER_RE.fullmatch(owner_id):
        raise ModelConnectionError(
            "MODEL_OWNER_SCOPE_MISMATCH",
            "invalid or missing local owner context",
        )
    return owner_id


def current_local_owner_id() -> str:
    return validate_local_owner_id(os.environ.get(OWNER_ENV, ""))


def control_plane_home() -> Path:
    configured = str(os.environ.get(CONTROL_HOME_ENV) or "").strip()
    return Path(configured).expanduser().resolve() if configured else get_hermes_home()


def model_connections_path(control_home: Path, owner_id: str) -> Path:
    safe_owner = validate_local_owner_id(owner_id)
    root = Path(control_home).expanduser().resolve() / "model-connections"
    target = root / f"{safe_owner}.yaml"
    if target.parent != root:
        raise ModelConnectionError(
            "MODEL_OWNER_SCOPE_MISMATCH",
            "local owner path escaped model connection root",
        )
    return target


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Cross-process advisory lock with a process-local RLock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    local_lock = _thread_lock(path)
    with local_lock:
        with lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _empty_document(owner_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "owner_id": owner_id,
        "revision": 0,
        "updated_at": utc_now_iso(),
        "connections": {},
        "profile_defaults": {},
        "tombstones": {},
        "operations": {},
    }


def _read_document(path: Path, owner_id: str) -> dict[str, Any]:
    if not path.exists():
        return _empty_document(owner_id)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ModelConnectionError(
            "MODEL_CONNECTION_STORE_INVALID",
            f"model connection store could not be read: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise ModelConnectionError(
            "MODEL_CONNECTION_STORE_INVALID",
            "model connection store must contain an object",
        )
    if int(raw.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ModelConnectionError(
            "MODEL_CONNECTION_SCHEMA_UNSUPPORTED",
            "unsupported model connection store schema",
        )
    if str(raw.get("owner_id") or "") != owner_id:
        raise ModelConnectionError(
            "MODEL_OWNER_SCOPE_MISMATCH",
            "model connection store belongs to another local owner",
        )
    document = _empty_document(owner_id)
    document.update(raw)
    for key in ("connections", "profile_defaults", "tombstones", "operations"):
        if not isinstance(document.get(key), dict):
            raise ModelConnectionError(
                "MODEL_CONNECTION_STORE_INVALID",
                f"model connection store field {key} must be an object",
            )
    document["revision"] = int(document.get("revision") or 0)
    return document


def _write_document(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.replace(temp_name, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def _assert_no_secret_fields(value: Any, path: str = "connection") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if key in _SECRET_KEYS or key.endswith("_secret"):
                raise ModelConnectionError(
                    "MODEL_CONNECTION_SECRET_FORBIDDEN",
                    f"{path}.{raw_key} is a secret field and must use credential_ref",
                )
            _assert_no_secret_fields(child, f"{path}.{raw_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secret_fields(child, f"{path}[{index}]")


def _text(value: Any, *, field: str, maximum: int, required: bool = True) -> str:
    normalized = str(value or "").strip()
    if required and not normalized:
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            f"{field} is required",
            details={"field": field},
        )
    if len(normalized) > maximum or any(ord(ch) < 32 for ch in normalized):
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            f"{field} is invalid",
            details={"field": field},
        )
    return normalized


def _normalize_model(model_id: str, raw: Any) -> dict[str, Any]:
    normalized_id = _text(model_id, field="model_id", maximum=512)
    entry = dict(raw) if isinstance(raw, Mapping) else {}
    entry.pop("id", None)
    display_name = _text(
        entry.get("display_name") or entry.get("name") or normalized_id,
        field="display_name",
        maximum=256,
    )
    normalized: dict[str, Any] = {
        **entry,
        "display_name": display_name,
        "origin": str(entry.get("origin") or "manual"),
    }
    if entry.get("context_window") is not None:
        try:
            context_window = int(entry["context_window"])
        except (TypeError, ValueError) as exc:
            raise ModelConnectionError(
                "MODEL_CONNECTION_INVALID",
                "context_window must be a positive integer",
                details={"field": "context_window"},
            ) from exc
        if context_window <= 0:
            raise ModelConnectionError(
                "MODEL_CONNECTION_INVALID",
                "context_window must be a positive integer",
                details={"field": "context_window"},
            )
        normalized["context_window"] = context_window
    for field in ("vision_enabled", "reasoning_enabled"):
        if field not in entry:
            continue
        if not isinstance(entry[field], bool):
            raise ModelConnectionError(
                "MODEL_CONNECTION_INVALID",
                f"{field} must be a boolean",
                details={"field": field},
            )
        normalized[field] = entry[field]

    from hermes_constants import VALID_REASONING_EFFORTS

    allowed_efforts = {"none", "enabled", *VALID_REASONING_EFFORTS}
    supported_reasoning_efforts: list[str] = []
    if entry.get("reasoning_efforts") is not None:
        raw_efforts = entry["reasoning_efforts"]
        if not isinstance(raw_efforts, list):
            raise ModelConnectionError(
                "MODEL_CONNECTION_INVALID",
                "reasoning_efforts must be an array",
                details={"field": "reasoning_efforts"},
            )
        for raw_effort in raw_efforts:
            effort = str(raw_effort or "").strip().lower()
            if effort not in allowed_efforts:
                raise ModelConnectionError(
                    "MODEL_CONNECTION_INVALID",
                    "reasoning_efforts contains an unsupported value",
                    details={
                        "field": "reasoning_efforts",
                        "value": effort,
                    },
                )
            if effort not in supported_reasoning_efforts:
                supported_reasoning_efforts.append(effort)
        normalized["reasoning_efforts"] = supported_reasoning_efforts

    default_reasoning_effort = (
        str(entry.get("default_reasoning_effort") or "").strip().lower()
    )
    if default_reasoning_effort:
        if default_reasoning_effort not in allowed_efforts:
            raise ModelConnectionError(
                "MODEL_CONNECTION_INVALID",
                "default_reasoning_effort is unsupported",
                details={"field": "default_reasoning_effort"},
            )
        if (
            supported_reasoning_efforts
            and default_reasoning_effort not in supported_reasoning_efforts
        ):
            raise ModelConnectionError(
                "MODEL_CONNECTION_INVALID",
                "default_reasoning_effort must be one of reasoning_efforts",
                details={"field": "default_reasoning_effort"},
            )
        normalized["default_reasoning_effort"] = default_reasoning_effort

    if normalized.get("reasoning_enabled") is not True and (
        supported_reasoning_efforts or default_reasoning_effort
    ):
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            "reasoning controls require reasoning_enabled=true",
            details={"field": "reasoning_enabled"},
        )

    reasoning_format = _text(
        entry.get("reasoning_format"),
        field="reasoning_format",
        maximum=64,
        required=False,
    )
    if reasoning_format:
        normalized["reasoning_format"] = reasoning_format
    return {normalized_id: normalized}


def _normalize_manual_models(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, list):
        items = (
            (
                str(item.get("id") or "").strip(),
                item,
            )
            for item in value
            if isinstance(item, Mapping)
        )
    else:
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            "manual_models must be an object or array",
        )
    for model_id, raw in items:
        normalized.update(_normalize_model(str(model_id), raw))
    return normalized


def _normalize_model_preferences(value: Any) -> dict[str, dict[str, bool]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            "model_preferences must be an object",
        )
    normalized: dict[str, dict[str, bool]] = {}
    for model_id, raw in value.items():
        normalized_id = _text(str(model_id), field="model_id", maximum=512)
        if not isinstance(raw, Mapping):
            raise ModelConnectionError(
                "MODEL_CONNECTION_INVALID",
                "model preference must be an object",
                details={"field": f"model_preferences.{normalized_id}"},
            )
        normalized[normalized_id] = {
            "picker_visible": raw.get("picker_visible") is not False,
        }
    return normalized


def _validate_base_url(
    value: Any,
    *,
    required: bool,
    allow_insecure_http: bool,
) -> str:
    base_url = _text(value, field="base_url", maximum=2048, required=required)
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            "base_url must be an absolute HTTP(S) URL",
            details={"field": "base_url"},
        )
    if parsed.username or parsed.password:
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            "base_url must not contain user information",
            details={"field": "base_url"},
        )
    loopback = parsed.hostname.lower().rstrip(".") in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if parsed.scheme == "http" and not loopback and not allow_insecure_http:
        raise ModelConnectionError(
            "MODEL_CONNECTION_INSECURE_ENDPOINT",
            "remote HTTP endpoints require explicit allow_insecure_http",
            details={"field": "base_url"},
        )
    return base_url.rstrip("/")


def _normalize_connection(
    draft: Mapping[str, Any],
    *,
    existing: Mapping[str, Any] | None,
    owner_id: str,
    revision: int,
    timestamp: str,
) -> dict[str, Any]:
    _assert_no_secret_fields(draft)
    connection_id = _text(
        draft.get("connection_id"),
        field="connection_id",
        maximum=160,
    )
    if not _CONNECTION_ID_RE.fullmatch(connection_id):
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            "connection_id contains unsupported characters",
            details={"field": "connection_id"},
        )
    provider_id = _text(
        draft.get("provider_id"),
        field="provider_id",
        maximum=128,
    )
    if not _PROVIDER_ID_RE.fullmatch(provider_id):
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            "provider_id contains unsupported characters",
            details={"field": "provider_id"},
        )
    _assert_user_manageable_connection(connection_id, provider_id)

    kind = str(draft.get("kind") or "").strip().lower()
    if not kind:
        kind = "builtin" if connection_id.startswith("builtin:") else "custom_endpoint"
    if kind not in {"builtin", "custom_endpoint"}:
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            "kind must be builtin or custom_endpoint",
            details={"field": "kind"},
        )
    if kind == "builtin" and connection_id != f"builtin:{provider_id}":
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            "builtin connection_id must match provider_id",
            details={"field": "connection_id"},
        )
    if kind == "custom_endpoint" and not connection_id.startswith("custom:"):
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            "custom endpoint connection_id must start with custom:",
            details={"field": "connection_id"},
        )

    api_mode = str(draft.get("api_mode") or "").strip().lower()
    if api_mode and api_mode not in _SAFE_API_MODES:
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            "unsupported api_mode",
            details={"field": "api_mode"},
        )
    base_url = _validate_base_url(
        draft.get("base_url"),
        required=kind == "custom_endpoint",
        allow_insecure_http=bool(draft.get("allow_insecure_http")),
    )
    credential_ref = _text(
        draft.get("credential_ref"),
        field="credential_ref",
        maximum=256,
        required=False,
    )
    if credential_ref and not credential_ref.startswith(
        "dovie-secure://model-credentials/"
    ):
        raise ModelConnectionError(
            "MODEL_CONNECTION_INVALID",
            "credential_ref is outside the managed desktop namespace",
            details={"field": "credential_ref"},
        )

    previous = dict(existing or {})
    if "picker_visibility_default" in draft:
        picker_visibility_default = draft["picker_visibility_default"]
        if not isinstance(picker_visibility_default, bool):
            raise ModelConnectionError(
                "MODEL_CONNECTION_INVALID",
                "picker_visibility_default must be a boolean",
                details={"field": "picker_visibility_default"},
            )
    elif "picker_visibility_default" in previous:
        picker_visibility_default = previous["picker_visibility_default"]
    else:
        # Connections created by the user are opt-in in the Desktop model
        # picker. Schema-v1 records created before this field existed migrate
        # to the same rule at normalization time.
        picker_visibility_default = False
    normalized = {
        **previous,
        **dict(draft),
        "connection_id": connection_id,
        "provider_id": provider_id,
        "kind": kind,
        "name": _text(
            draft.get("name") or previous.get("name") or provider_id,
            field="name",
            maximum=256,
        ),
        "base_url": base_url or None,
        "api_mode": api_mode or None,
        "credential_ref": credential_ref or None,
        "manual_models": _normalize_manual_models(
            draft.get("manual_models", draft.get("models"))
        ),
        "model_preferences": _normalize_model_preferences(
            draft.get("model_preferences", previous.get("model_preferences"))
        ),
        "picker_visibility_default": picker_visibility_default,
        "discover_models": bool(draft.get("discover_models", True)),
        "enabled": bool(draft.get("enabled", True)),
        "scope": {"kind": "device_user", "owner_id": owner_id},
        "revision": revision,
        "created_at": str(previous.get("created_at") or timestamp),
        "updated_at": timestamp,
    }
    previous_routing_revision = int(
        previous.get("routing_revision") or previous.get("revision") or 0
    )
    routing_fields = (
        "provider_id",
        "kind",
        "base_url",
        "api_mode",
        "manual_models",
        "enabled",
    )
    routing_changed = not previous or any(
        previous.get(field) != normalized.get(field) for field in routing_fields
    )
    normalized["routing_revision"] = (
        previous_routing_revision + 1 if routing_changed else previous_routing_revision
    )
    for key in ("expected_revision", "operation_id", "allow_insecure_http", "models"):
        normalized.pop(key, None)
    return normalized


def _request_fingerprint(operation: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"operation": operation, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ModelConnectionRepository:
    """Atomic owner-scoped repository for managed connection overlays."""

    def __init__(self, *, control_home: Path, owner_id: str):
        self.owner_id = validate_local_owner_id(owner_id)
        self.path = model_connections_path(control_home, self.owner_id)

    @classmethod
    def for_runtime(cls) -> "ModelConnectionRepository":
        return cls(
            control_home=control_plane_home(),
            owner_id=current_local_owner_id(),
        )

    def snapshot(self) -> dict[str, Any]:
        with _file_lock(self.path):
            return copy.deepcopy(_read_document(self.path, self.owner_id))

    def list(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        document = self.snapshot()
        rows = [
            copy.deepcopy(value)
            for value in document["connections"].values()
            if isinstance(value, dict)
            and not is_dovie_cloud_connection(
                value.get("connection_id"),
                value.get("provider_id"),
            )
            and (include_disabled or bool(value.get("enabled", True)))
        ]
        return sorted(rows, key=lambda row: str(row.get("connection_id") or ""))

    def get(self, connection_id: str) -> dict[str, Any] | None:
        normalized_id = _text(
            connection_id,
            field="connection_id",
            maximum=160,
        )
        document = self.snapshot()
        value = document["connections"].get(normalized_id)
        if not isinstance(value, dict):
            return None
        if is_dovie_cloud_connection(normalized_id, value.get("provider_id")):
            return None
        return copy.deepcopy(value)

    def profile_default(self, profile_id: str) -> dict[str, Any] | None:
        normalized_id = _text(
            profile_id,
            field="profile_id",
            maximum=160,
        )
        document = self.snapshot()
        value = document["profile_defaults"].get(normalized_id)
        return copy.deepcopy(value) if isinstance(value, dict) else None

    def set_profile_default(
        self,
        profile_id: str,
        selection: Mapping[str, Any],
        *,
        expected_revision: int | None,
        operation_id: str,
    ) -> dict[str, Any]:
        from hermes_cli.model_routes import normalize_model_selection

        normalized_profile_id = _text(
            profile_id,
            field="profile_id",
            maximum=160,
        )
        normalized_selection = normalize_model_selection(selection)
        return self._mutate(
            operation="set_profile_default",
            operation_id=operation_id,
            payload={
                "profile_id": normalized_profile_id,
                "selection": normalized_selection,
                "expected_revision": expected_revision,
            },
            callback=lambda document, timestamp: self._set_profile_default_document(
                document,
                normalized_profile_id,
                normalized_selection,
                expected_revision=expected_revision,
                timestamp=timestamp,
            ),
        )

    def upsert(
        self,
        draft: Mapping[str, Any],
        *,
        expected_revision: int | None,
        operation_id: str,
    ) -> dict[str, Any]:
        return self._mutate(
            operation="upsert",
            operation_id=operation_id,
            payload={
                "draft": dict(draft),
                "expected_revision": expected_revision,
            },
            callback=lambda document, timestamp: self._upsert_document(
                document,
                draft,
                expected_revision=expected_revision,
                timestamp=timestamp,
            ),
        )

    def retire(
        self,
        connection_id: str,
        *,
        expected_revision: int,
        operation_id: str,
    ) -> dict[str, Any]:
        return self._mutate(
            operation="retire",
            operation_id=operation_id,
            payload={
                "connection_id": connection_id,
                "expected_revision": expected_revision,
            },
            callback=lambda document, timestamp: self._retire_document(
                document,
                connection_id,
                expected_revision=expected_revision,
                timestamp=timestamp,
            ),
        )

    def manual_model_upsert(
        self,
        connection_id: str,
        model: Mapping[str, Any],
        *,
        expected_revision: int,
        operation_id: str,
    ) -> dict[str, Any]:
        return self._mutate(
            operation="manual_model_upsert",
            operation_id=operation_id,
            payload={
                "connection_id": connection_id,
                "model": dict(model),
                "expected_revision": expected_revision,
            },
            callback=lambda document, timestamp: self._manual_model_upsert_document(
                document,
                connection_id,
                model,
                expected_revision=expected_revision,
                timestamp=timestamp,
            ),
        )

    def manual_model_delete(
        self,
        connection_id: str,
        model_id: str,
        *,
        expected_revision: int,
        operation_id: str,
    ) -> dict[str, Any]:
        return self._mutate(
            operation="manual_model_delete",
            operation_id=operation_id,
            payload={
                "connection_id": connection_id,
                "model_id": model_id,
                "expected_revision": expected_revision,
            },
            callback=lambda document, timestamp: self._manual_model_delete_document(
                document,
                connection_id,
                model_id,
                expected_revision=expected_revision,
                timestamp=timestamp,
            ),
        )

    def set_model_picker_visibility(
        self,
        connection_id: str,
        model_id: str,
        visible: bool,
        *,
        expected_revision: int,
        operation_id: str,
    ) -> dict[str, Any]:
        return self._mutate(
            operation="set_model_picker_visibility",
            operation_id=operation_id,
            payload={
                "connection_id": connection_id,
                "model_id": model_id,
                "visible": bool(visible),
                "expected_revision": expected_revision,
            },
            callback=lambda document, timestamp: (
                self._set_model_picker_visibility_document(
                    document,
                    connection_id,
                    model_id,
                    bool(visible),
                    expected_revision=expected_revision,
                    timestamp=timestamp,
                )
            ),
        )

    def _mutate(
        self,
        *,
        operation: str,
        operation_id: str,
        payload: Mapping[str, Any],
        callback,
    ) -> dict[str, Any]:
        normalized_operation_id = _text(
            operation_id,
            field="operation_id",
            maximum=160,
        )
        fingerprint = _request_fingerprint(operation, payload)
        with _file_lock(self.path):
            document = _read_document(self.path, self.owner_id)
            previous = document["operations"].get(normalized_operation_id)
            if isinstance(previous, dict):
                if previous.get("fingerprint") != fingerprint:
                    raise ModelConnectionError(
                        "MODEL_OPERATION_ID_REUSED",
                        "operation_id was already used for a different request",
                    )
                result = previous.get("result")
                return copy.deepcopy(result) if isinstance(result, dict) else {}

            timestamp = utc_now_iso()
            result = callback(document, timestamp)
            document["revision"] = int(document.get("revision") or 0) + 1
            document["updated_at"] = timestamp
            document["operations"][normalized_operation_id] = {
                "fingerprint": fingerprint,
                "operation": operation,
                "result": copy.deepcopy(result),
                "completed_at": timestamp,
            }
            while len(document["operations"]) > _MAX_OPERATIONS:
                oldest_key = next(iter(document["operations"]))
                document["operations"].pop(oldest_key, None)
            _write_document(self.path, document)
            return copy.deepcopy(result)

    def _upsert_document(
        self,
        document: dict[str, Any],
        draft: Mapping[str, Any],
        *,
        expected_revision: int | None,
        timestamp: str,
    ) -> dict[str, Any]:
        connection_id = _text(
            draft.get("connection_id"),
            field="connection_id",
            maximum=160,
        )
        existing = document["connections"].get(connection_id)
        current_revision = (
            int(existing.get("revision") or 0) if isinstance(existing, dict) else 0
        )
        expected = int(expected_revision or 0)
        if expected != current_revision:
            raise ModelConnectionError(
                "MODEL_CONNECTION_REVISION_CONFLICT",
                "connection revision changed",
                details={
                    "connection_id": connection_id,
                    "expected_revision": expected,
                    "actual_revision": current_revision,
                },
            )
        normalized = _normalize_connection(
            draft,
            existing=existing if isinstance(existing, dict) else None,
            owner_id=self.owner_id,
            revision=current_revision + 1,
            timestamp=timestamp,
        )
        document["connections"][connection_id] = normalized
        document["tombstones"].pop(connection_id, None)
        return normalized

    def _set_profile_default_document(
        self,
        document: dict[str, Any],
        profile_id: str,
        selection: Mapping[str, Any],
        *,
        expected_revision: int | None,
        timestamp: str,
    ) -> dict[str, Any]:
        existing = document["profile_defaults"].get(profile_id)
        current_revision = (
            int(existing.get("revision") or 0) if isinstance(existing, dict) else 0
        )
        expected = int(expected_revision or 0)
        if expected != current_revision:
            raise ModelConnectionError(
                "MODEL_PROFILE_DEFAULT_REVISION_CONFLICT",
                "profile default model revision changed",
                details={
                    "profile_id": profile_id,
                    "expected_revision": expected,
                    "actual_revision": current_revision,
                },
            )
        normalized = {
            "profile_id": profile_id,
            "selection": copy.deepcopy(dict(selection)),
            "revision": current_revision + 1,
            "created_at": str((existing or {}).get("created_at") or timestamp),
            "updated_at": timestamp,
        }
        document["profile_defaults"][profile_id] = normalized
        return normalized

    def _retire_document(
        self,
        document: dict[str, Any],
        connection_id: str,
        *,
        expected_revision: int,
        timestamp: str,
    ) -> dict[str, Any]:
        normalized_id = _text(
            connection_id,
            field="connection_id",
            maximum=160,
        )
        existing = document["connections"].get(normalized_id)
        if not isinstance(existing, dict):
            tombstone = document["tombstones"].get(normalized_id)
            if isinstance(tombstone, dict):
                return tombstone
            raise ModelConnectionError(
                "MODEL_CONNECTION_NOT_FOUND",
                "model connection was not found",
            )
        _assert_user_manageable_connection(
            normalized_id,
            existing.get("provider_id"),
        )
        current_revision = int(existing.get("revision") or 0)
        if int(expected_revision) != current_revision:
            raise ModelConnectionError(
                "MODEL_CONNECTION_REVISION_CONFLICT",
                "connection revision changed",
                details={
                    "connection_id": normalized_id,
                    "expected_revision": int(expected_revision),
                    "actual_revision": current_revision,
                },
            )
        if str(existing.get("kind") or "") == "builtin":
            disconnected = {
                **existing,
                "credential_ref": None,
                "manual_models": {},
                "enabled": True,
                "revision": current_revision + 1,
                "routing_revision": int(
                    existing.get("routing_revision") or existing.get("revision") or 0
                )
                + 1,
                "updated_at": timestamp,
            }
            document["connections"][normalized_id] = disconnected
            return disconnected
        document["connections"].pop(normalized_id, None)
        tombstone = {
            "connection_id": normalized_id,
            "provider_id": str(existing.get("provider_id") or ""),
            "name": str(existing.get("name") or normalized_id),
            "retired_revision": current_revision + 1,
            "retired_at": timestamp,
        }
        document["tombstones"][normalized_id] = tombstone
        return tombstone

    def _require_connection_revision(
        self,
        document: dict[str, Any],
        connection_id: str,
        expected_revision: int,
    ) -> tuple[str, dict[str, Any]]:
        normalized_id = _text(
            connection_id,
            field="connection_id",
            maximum=160,
        )
        existing = document["connections"].get(normalized_id)
        if not isinstance(existing, dict):
            raise ModelConnectionError(
                "MODEL_CONNECTION_NOT_FOUND",
                "model connection was not found",
            )
        _assert_user_manageable_connection(
            normalized_id,
            existing.get("provider_id"),
        )
        actual = int(existing.get("revision") or 0)
        if int(expected_revision) != actual:
            raise ModelConnectionError(
                "MODEL_CONNECTION_REVISION_CONFLICT",
                "connection revision changed",
                details={
                    "connection_id": normalized_id,
                    "expected_revision": int(expected_revision),
                    "actual_revision": actual,
                },
            )
        return normalized_id, existing

    def _manual_model_upsert_document(
        self,
        document: dict[str, Any],
        connection_id: str,
        model: Mapping[str, Any],
        *,
        expected_revision: int,
        timestamp: str,
    ) -> dict[str, Any]:
        normalized_id, existing = self._require_connection_revision(
            document,
            connection_id,
            expected_revision,
        )
        model_id = _text(model.get("id"), field="model_id", maximum=512)
        manual_models = dict(existing.get("manual_models") or {})
        manual_models.update(_normalize_model(model_id, model))
        model_preferences = _normalize_model_preferences(
            existing.get("model_preferences")
        )
        if model_id not in model_preferences:
            model_preferences[model_id] = {"picker_visible": False}
        updated = {
            **existing,
            "manual_models": manual_models,
            "model_preferences": model_preferences,
            "revision": int(existing["revision"]) + 1,
            "routing_revision": int(
                existing.get("routing_revision") or existing.get("revision") or 0
            )
            + 1,
            "updated_at": timestamp,
        }
        document["connections"][normalized_id] = updated
        return updated

    def _manual_model_delete_document(
        self,
        document: dict[str, Any],
        connection_id: str,
        model_id: str,
        *,
        expected_revision: int,
        timestamp: str,
    ) -> dict[str, Any]:
        normalized_id, existing = self._require_connection_revision(
            document,
            connection_id,
            expected_revision,
        )
        normalized_model_id = _text(model_id, field="model_id", maximum=512)
        manual_models = dict(existing.get("manual_models") or {})
        if normalized_model_id not in manual_models:
            raise ModelConnectionError(
                "MODEL_NOT_AVAILABLE",
                "manual model was not found",
            )
        manual_models.pop(normalized_model_id)
        updated = {
            **existing,
            "manual_models": manual_models,
            "revision": int(existing["revision"]) + 1,
            "routing_revision": int(
                existing.get("routing_revision") or existing.get("revision") or 0
            )
            + 1,
            "updated_at": timestamp,
        }
        document["connections"][normalized_id] = updated
        return updated

    def _set_model_picker_visibility_document(
        self,
        document: dict[str, Any],
        connection_id: str,
        model_id: str,
        visible: bool,
        *,
        expected_revision: int,
        timestamp: str,
    ) -> dict[str, Any]:
        normalized_id, existing = self._require_connection_revision(
            document,
            connection_id,
            expected_revision,
        )
        normalized_model_id = _text(model_id, field="model_id", maximum=512)
        model_preferences = _normalize_model_preferences(
            existing.get("model_preferences")
        )
        # Always persist the explicit user choice. New user connections are
        # hidden by default while legacy connections may still default to
        # visible, so absence can no longer represent one universal value.
        model_preferences[normalized_model_id] = {
            "picker_visible": bool(visible),
        }
        updated = {
            **existing,
            "model_preferences": model_preferences,
            "revision": int(existing["revision"]) + 1,
            # Picker visibility is catalog presentation state. It must not
            # invalidate a persisted runtime route or a historical selection.
            "updated_at": timestamp,
        }
        document["connections"][normalized_id] = updated
        return updated


__all__ = [
    "CONTROL_HOME_ENV",
    "ModelConnectionError",
    "ModelConnectionRepository",
    "OWNER_ENV",
    "SCHEMA_VERSION",
    "control_plane_home",
    "current_local_owner_id",
    "model_connections_path",
    "validate_local_owner_id",
]
