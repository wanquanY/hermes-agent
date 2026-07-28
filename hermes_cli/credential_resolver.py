"""Credential resolution boundary for standalone and managed runtimes.

Managed Desktop credentials are opaque references.  Hermes receives secret
bytes only through a short-lived, capability-authenticated localhost broker;
it never reads the desktop database or persists the returned value.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

BROKER_BOOTSTRAP_ENV = "DOVIE_CREDENTIAL_BROKER_BOOTSTRAP_FILE"
MANAGED_CREDENTIAL_PREFIX = "dovie-secure://model-credentials/"
_MAX_FRAME_BYTES = 64 * 1024
_FRAME_HEADER_BYTES = 4


class CredentialResolutionError(RuntimeError):
    """Stable credential-boundary failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CredentialStatus:
    configured: bool
    status: str
    generation: int = 0
    secret_kind: str = "api_key"
    updated_at: str | None = None


@dataclass(frozen=True)
class ResolvedCredential:
    value: str
    generation: int
    lease_id: str
    secret_kind: str = "api_key"
    expires_at: str | None = None


class CredentialResolver(Protocol):
    def status(self, credential_ref: str) -> CredentialStatus: ...

    def resolve(
        self,
        credential_ref: str,
        *,
        purpose: str,
        connection_id: str,
    ) -> ResolvedCredential: ...

    def report_result(
        self,
        lease_id: str,
        *,
        outcome: str,
        error_code: str | None = None,
    ) -> None: ...

    def update_oauth(
        self,
        credential_ref: str,
        *,
        connection_id: str,
        purpose: str,
        expected_generation: int,
        bundle: Mapping[str, Any],
    ) -> CredentialStatus: ...


class StandaloneCredentialResolver:
    """Compatibility resolver for explicit environment references.

    Existing Hermes auth/pool/config resolution remains authoritative for
    standalone provider paths.  This adapter intentionally supports only the
    explicit ``env://NAME`` reference form so opaque Desktop references cannot
    accidentally fall through to process environment lookup.
    """

    _PREFIX = "env://"

    def _env_name(self, credential_ref: str) -> str:
        reference = str(credential_ref or "").strip()
        if not reference.startswith(self._PREFIX):
            raise CredentialResolutionError(
                "CREDENTIAL_REFERENCE_UNSUPPORTED",
                "standalone credential references must use env://NAME",
            )
        name = reference[len(self._PREFIX) :]
        if not name or not name.replace("_", "").isalnum() or name.upper() != name:
            raise CredentialResolutionError(
                "CREDENTIAL_REFERENCE_INVALID",
                "credential environment name is invalid",
            )
        return name

    def status(self, credential_ref: str) -> CredentialStatus:
        configured = bool(os.environ.get(self._env_name(credential_ref), "").strip())
        return CredentialStatus(
            configured=configured,
            status="configured" if configured else "missing",
        )

    def resolve(
        self,
        credential_ref: str,
        *,
        purpose: str,
        connection_id: str,
    ) -> ResolvedCredential:
        del purpose, connection_id
        value = os.environ.get(self._env_name(credential_ref), "").strip()
        if not value:
            raise CredentialResolutionError(
                "CREDENTIAL_REQUIRED",
                "credential is not configured",
            )
        return ResolvedCredential(
            value=value,
            generation=0,
            lease_id=f"standalone:{uuid.uuid4().hex}",
        )

    def report_result(
        self,
        lease_id: str,
        *,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        del lease_id, outcome, error_code

    def update_oauth(
        self,
        credential_ref: str,
        *,
        connection_id: str,
        purpose: str,
        expected_generation: int,
        bundle: Mapping[str, Any],
    ) -> CredentialStatus:
        del credential_ref, connection_id, purpose, expected_generation, bundle
        raise CredentialResolutionError(
            "CREDENTIAL_OAUTH_UPDATE_UNSUPPORTED",
            "standalone credentials cannot be updated through the Desktop broker",
        )


@dataclass(frozen=True)
class _BrokerBootstrap:
    socket_path: str | None
    pipe_path: str | None
    capability: str
    owner_id: str
    runtime_id: str
    expires_at: str | None


class DovieBrokerCredentialResolver:
    """Resolve credentials from Electron's authenticated local broker."""

    def __init__(self, bootstrap_path: Path, *, timeout_seconds: float = 5.0):
        self._bootstrap_path = Path(bootstrap_path).expanduser().resolve()
        self._timeout_seconds = max(0.5, min(float(timeout_seconds), 30.0))
        self._bootstrap_lock = threading.Lock()
        self._bootstrap: _BrokerBootstrap | None = None

    @classmethod
    def from_environment(cls) -> "DovieBrokerCredentialResolver":
        raw_path = str(os.environ.get(BROKER_BOOTSTRAP_ENV) or "").strip()
        if not raw_path:
            raise CredentialResolutionError(
                "CREDENTIAL_BROKER_UNAVAILABLE",
                "managed credential broker bootstrap is missing",
            )
        return cls(Path(raw_path))

    @staticmethod
    def _is_trusted_unix_socket_location(
        endpoint: Path,
        bootstrap_path: Path,
    ) -> bool:
        if endpoint.parent == bootstrap_path.parent:
            # Backward compatibility for older Desktop runtimes that placed
            # the socket beside the private bootstrap file.
            return True
        # Electron deliberately gives Hermes an app-private TMPDIR while the
        # broker must use Node's shorter system temp root to stay below Unix
        # sockaddr limits.  Therefore process-local tempfile roots are not a
        # valid trust anchor.  The namespace shape is checked here; ownership,
        # type, and restrictive permissions are enforced below.
        return (
            endpoint.name == "b.sock"
            and endpoint.parent.name.startswith("dovie-cb-")
        )

    @staticmethod
    def _validate_unix_endpoint_permissions(endpoint: Path) -> None:
        try:
            directory_stat = endpoint.parent.stat()
            endpoint_stat = endpoint.stat()
        except OSError as exc:
            raise CredentialResolutionError(
                "CREDENTIAL_BROKER_UNAVAILABLE",
                "managed credential broker socket is unavailable",
            ) from exc
        if not stat.S_ISDIR(directory_stat.st_mode) or not stat.S_ISSOCK(
            endpoint_stat.st_mode
        ):
            raise CredentialResolutionError(
                "CREDENTIAL_BROKER_UNAVAILABLE",
                "managed credential broker endpoint is not a socket",
            )
        if directory_stat.st_mode & 0o077:
            raise CredentialResolutionError(
                "CREDENTIAL_BROKER_SOCKET_PERMISSIONS",
                "managed credential broker socket directory permissions are too broad",
            )
        if endpoint_stat.st_mode & 0o077:
            raise CredentialResolutionError(
                "CREDENTIAL_BROKER_SOCKET_PERMISSIONS",
                "managed credential broker socket permissions are too broad",
            )
        if hasattr(os, "getuid"):
            current_uid = os.getuid()
            if (
                directory_stat.st_uid != current_uid
                or endpoint_stat.st_uid != current_uid
            ):
                raise CredentialResolutionError(
                    "CREDENTIAL_BROKER_SOCKET_OWNER",
                    "managed credential broker socket owner is invalid",
                )

    def _load_bootstrap(self) -> _BrokerBootstrap:
        with self._bootstrap_lock:
            if self._bootstrap is not None:
                return self._bootstrap
            path = self._bootstrap_path
            try:
                file_stat = path.stat()
            except OSError as exc:
                raise CredentialResolutionError(
                    "CREDENTIAL_BROKER_UNAVAILABLE",
                    "managed credential broker bootstrap could not be read",
                ) from exc
            if not stat.S_ISREG(file_stat.st_mode):
                raise CredentialResolutionError(
                    "CREDENTIAL_BROKER_UNAVAILABLE",
                    "managed credential broker bootstrap is not a file",
                )
            if os.name != "nt" and file_stat.st_mode & 0o077:
                raise CredentialResolutionError(
                    "CREDENTIAL_BROKER_BOOTSTRAP_PERMISSIONS",
                    "managed credential broker bootstrap permissions are too broad",
                )
            if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
                raise CredentialResolutionError(
                    "CREDENTIAL_BROKER_BOOTSTRAP_OWNER",
                    "managed credential broker bootstrap owner is invalid",
                )
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise CredentialResolutionError(
                    "CREDENTIAL_BROKER_UNAVAILABLE",
                    "managed credential broker bootstrap is invalid",
                ) from exc
            if not isinstance(payload, dict):
                raise CredentialResolutionError(
                    "CREDENTIAL_BROKER_UNAVAILABLE",
                    "managed credential broker bootstrap must be an object",
                )
            socket_path = str(payload.get("socket_path") or "").strip()
            pipe_path = str(payload.get("pipe_path") or "").strip()
            capability = str(payload.get("capability") or "").strip()
            owner_id = str(payload.get("owner_id") or "").strip()
            runtime_id = str(payload.get("runtime_id") or "").strip()
            expires_at = str(payload.get("expires_at") or "").strip() or None
            if socket_path:
                if os.name == "nt" or pipe_path:
                    raise CredentialResolutionError(
                        "CREDENTIAL_BROKER_UNAVAILABLE",
                        "managed credential broker endpoint is invalid",
                    )
                endpoint = Path(socket_path).expanduser().resolve()
                if (
                    not endpoint.is_absolute()
                    or not self._is_trusted_unix_socket_location(endpoint, path)
                ):
                    raise CredentialResolutionError(
                        "CREDENTIAL_BROKER_UNAVAILABLE",
                        "managed credential broker socket location is not trusted",
                    )
                self._validate_unix_endpoint_permissions(endpoint)
                socket_path = str(endpoint)
            elif pipe_path:
                if os.name != "nt" or not pipe_path.lower().startswith(
                    "\\\\.\\pipe\\dovie-model-credential-"
                ):
                    raise CredentialResolutionError(
                        "CREDENTIAL_BROKER_UNAVAILABLE",
                        "managed credential broker pipe is invalid",
                    )
            else:
                raise CredentialResolutionError(
                    "CREDENTIAL_BROKER_UNAVAILABLE",
                    "managed credential broker has no local endpoint",
                )
            if len(capability) < 32 or not owner_id or not runtime_id:
                raise CredentialResolutionError(
                    "CREDENTIAL_BROKER_UNAVAILABLE",
                    "managed credential broker capability is incomplete",
                )
            self._bootstrap = _BrokerBootstrap(
                socket_path=socket_path or None,
                pipe_path=pipe_path or None,
                capability=capability,
                owner_id=owner_id,
                runtime_id=runtime_id,
                expires_at=expires_at,
            )
            return self._bootstrap

    def validate_boundary(self, expected_owner_id: str) -> None:
        bootstrap = self._load_bootstrap()
        if bootstrap.owner_id != str(expected_owner_id or "").strip():
            raise CredentialResolutionError(
                "CREDENTIAL_BROKER_OWNER_MISMATCH",
                "managed credential broker owner does not match runtime context",
            )

    @staticmethod
    def _receive_exact(connection: socket.socket, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = connection.recv(length - len(chunks))
            if not chunk:
                raise CredentialResolutionError(
                    "CREDENTIAL_BROKER_PROTOCOL_ERROR",
                    "credential broker closed an incomplete frame",
                )
            chunks.extend(chunk)
        return bytes(chunks)

    def _windows_pipe_exchange(self, pipe_path: str, encoded: bytes) -> bytes:
        if os.name != "nt":
            raise OSError("Windows named pipes are unavailable on this platform")
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        kernel32.WaitNamedPipeW.restype = wintypes.BOOL
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        timeout_ms = max(500, min(int(self._timeout_seconds * 1000), 30_000))
        if not kernel32.WaitNamedPipeW(pipe_path, timeout_ms):
            raise ctypes.WinError(ctypes.get_last_error())
        handle = kernel32.CreateFileW(
            pipe_path,
            0x80000000 | 0x40000000,
            0,
            None,
            3,
            0,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())

        def write_all(payload: bytes) -> None:
            offset = 0
            while offset < len(payload):
                chunk = payload[offset:]
                buffer = ctypes.create_string_buffer(chunk)
                written = wintypes.DWORD()
                if not kernel32.WriteFile(
                    handle,
                    buffer,
                    len(chunk),
                    ctypes.byref(written),
                    None,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if written.value <= 0:
                    raise OSError("credential broker named pipe write was incomplete")
                offset += written.value

        def read_exact(length: int) -> bytes:
            chunks = bytearray()
            while len(chunks) < length:
                remaining = length - len(chunks)
                buffer = ctypes.create_string_buffer(remaining)
                received = wintypes.DWORD()
                if not kernel32.ReadFile(
                    handle,
                    buffer,
                    remaining,
                    ctypes.byref(received),
                    None,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if received.value <= 0:
                    raise OSError("credential broker named pipe frame was incomplete")
                chunks.extend(buffer.raw[: received.value])
            return bytes(chunks)

        try:
            write_all(encoded)
            header = read_exact(_FRAME_HEADER_BYTES)
            response_length = struct.unpack(">I", header)[0]
            if response_length <= 0 or response_length > _MAX_FRAME_BYTES:
                raise CredentialResolutionError(
                    "CREDENTIAL_BROKER_RESPONSE_TOO_LARGE",
                    "credential broker response is too large",
                )
            return read_exact(response_length)
        finally:
            kernel32.CloseHandle(handle)

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        bootstrap = self._load_bootstrap()
        request = {
            "schema_version": 1,
            "request_id": uuid.uuid4().hex,
            "method": method,
            "runtime_id": bootstrap.runtime_id,
            "owner_id": bootstrap.owner_id,
            "capability": bootstrap.capability,
            "params": params,
        }
        body = json.dumps(
            request,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode(
            "utf-8"
        )
        if len(body) > _MAX_FRAME_BYTES:
            raise CredentialResolutionError(
                "CREDENTIAL_BROKER_REQUEST_TOO_LARGE",
                "credential broker request is too large",
            )
        encoded = struct.pack(">I", len(body)) + body
        try:
            if bootstrap.pipe_path:
                response_body = self._windows_pipe_exchange(
                    bootstrap.pipe_path,
                    encoded,
                )
            elif bootstrap.socket_path:
                raw_connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                raw_connection.settimeout(self._timeout_seconds)
                raw_connection.connect(bootstrap.socket_path)
                with raw_connection as connection:
                    connection.settimeout(self._timeout_seconds)
                    connection.sendall(encoded)
                    header = self._receive_exact(connection, _FRAME_HEADER_BYTES)
                    response_length = struct.unpack(">I", header)[0]
                    if response_length <= 0 or response_length > _MAX_FRAME_BYTES:
                        raise CredentialResolutionError(
                            "CREDENTIAL_BROKER_RESPONSE_TOO_LARGE",
                            "credential broker response is too large",
                        )
                    response_body = self._receive_exact(connection, response_length)
            else:
                raise OSError("credential broker bootstrap has no endpoint")
        except OSError as exc:
            raise CredentialResolutionError(
                "CREDENTIAL_BROKER_UNAVAILABLE",
                "managed credential broker is unavailable",
            ) from exc
        try:
            response = json.loads(response_body.decode("utf-8"))
        except Exception as exc:
            raise CredentialResolutionError(
                "CREDENTIAL_BROKER_PROTOCOL_ERROR",
                "credential broker returned an invalid response",
            ) from exc
        if not isinstance(response, dict):
            raise CredentialResolutionError(
                "CREDENTIAL_BROKER_PROTOCOL_ERROR",
                "credential broker response must be an object",
            )
        if response.get("ok") is not True:
            error = response.get("error")
            error = error if isinstance(error, dict) else {}
            raise CredentialResolutionError(
                str(error.get("code") or "CREDENTIAL_BROKER_REJECTED"),
                str(error.get("message") or "credential broker rejected the request"),
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise CredentialResolutionError(
                "CREDENTIAL_BROKER_PROTOCOL_ERROR",
                "credential broker result must be an object",
            )
        return result

    def status(self, credential_ref: str) -> CredentialStatus:
        result = self._request(
            "credential.status",
            {"credential_ref": credential_ref},
        )
        return CredentialStatus(
            configured=bool(result.get("configured")),
            status=str(result.get("status") or "missing"),
            generation=int(result.get("generation") or 0),
            secret_kind=str(result.get("secret_kind") or "api_key"),
            updated_at=str(result.get("updated_at") or "") or None,
        )

    def resolve(
        self,
        credential_ref: str,
        *,
        purpose: str,
        connection_id: str,
    ) -> ResolvedCredential:
        if not str(credential_ref or "").startswith(MANAGED_CREDENTIAL_PREFIX):
            raise CredentialResolutionError(
                "CREDENTIAL_REFERENCE_INVALID",
                "credential reference is outside the managed namespace",
            )
        status = self.status(credential_ref)
        result = self._request(
            "credential.resolve",
            {
                "credential_ref": credential_ref,
                "purpose": purpose,
                "connection_id": connection_id,
                "expected_generation": status.generation,
            },
        )
        value = str(result.get("value") or "")
        lease_id = str(result.get("lease_id") or "")
        if not value or not lease_id:
            raise CredentialResolutionError(
                "CREDENTIAL_BROKER_PROTOCOL_ERROR",
                "credential broker returned an incomplete lease",
            )
        return ResolvedCredential(
            value=value,
            generation=int(result.get("generation") or 0),
            lease_id=lease_id,
            secret_kind=str(result.get("secret_kind") or "api_key"),
            expires_at=str(result.get("expires_at") or "") or None,
        )

    def update_oauth(
        self,
        credential_ref: str,
        *,
        connection_id: str,
        purpose: str,
        expected_generation: int,
        bundle: Mapping[str, Any],
    ) -> CredentialStatus:
        if purpose not in {"oauth_bootstrap", "oauth_refresh"}:
            raise CredentialResolutionError(
                "CREDENTIAL_PURPOSE_FORBIDDEN",
                "OAuth credential update purpose is invalid",
            )
        result = self._request(
            "credential.update_oauth",
            {
                "credential_ref": credential_ref,
                "connection_id": connection_id,
                "purpose": purpose,
                "expected_generation": int(expected_generation),
                "bundle": dict(bundle),
            },
        )
        return CredentialStatus(
            configured=bool(result.get("configured")),
            status=str(result.get("status") or "missing"),
            generation=int(result.get("generation") or 0),
            secret_kind=str(result.get("secret_kind") or "oauth_bundle"),
            updated_at=str(result.get("updated_at") or "") or None,
        )

    def report_result(
        self,
        lease_id: str,
        *,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        self._request(
            "credential.report_result",
            {
                "lease_id": lease_id,
                "outcome": outcome,
                "error_code": error_code,
            },
        )


def runtime_credential_resolver() -> CredentialResolver:
    """Return the resolver appropriate for the current runtime boundary."""

    if str(os.environ.get(BROKER_BOOTSTRAP_ENV) or "").strip():
        return DovieBrokerCredentialResolver.from_environment()
    return StandaloneCredentialResolver()


def credential_status_dict(status: CredentialStatus) -> dict[str, Any]:
    return asdict(status)


__all__ = [
    "BROKER_BOOTSTRAP_ENV",
    "CredentialResolutionError",
    "CredentialResolver",
    "CredentialStatus",
    "DovieBrokerCredentialResolver",
    "MANAGED_CREDENTIAL_PREFIX",
    "ResolvedCredential",
    "StandaloneCredentialResolver",
    "credential_status_dict",
    "runtime_credential_resolver",
]
