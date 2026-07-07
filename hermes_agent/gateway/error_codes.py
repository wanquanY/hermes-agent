"""Central error code registry (spec §J9)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorCode(str, Enum):
    """Every ``_err(rid, code, ...)`` must draw ``code`` from this enum.

    Numeric magic codes (``4001``, ``5023``) scattered across the codebase are
    banned. Range convention:

    * ``4000-4999`` client-side / contract violation (bad params, unknown method)
    * ``5000-5999`` internal / backend failure (storage, seq allocator, worker)
    * ``6000-6999`` capability negotiation / handshake
    """

    # 4xxx — client / contract
    UNKNOWN_METHOD = "4001"
    INVALID_PARAMS = "4002"
    PERMISSION_DENIED = "4003"
    METHOD_DISABLED = "4004"
    RATE_LIMITED = "4005"
    MALFORMED_FRAME = "4006"
    UNSUPPORTED_CAPABILITY = "4007"

    # 5xxx — internal
    STORAGE_BUSY = "5001"
    SEQ_ALLOCATOR_BUSY = "5002"
    RUN_STATE_CONFLICT = "5003"
    RUN_NOT_FOUND = "5004"
    SESSION_NOT_FOUND = "5005"
    MESSAGE_NOT_FOUND = "5008"
    WORKER_UNAVAILABLE = "5006"
    UPSTREAM_FAILURE = "5007"
    INVARIANT_VIOLATED = "5099"

    # 6xxx — handshake / capability
    CONTRACT_VERSION_MISMATCH = "6001"
    HANDSHAKE_TIMEOUT = "6002"


@dataclass(frozen=True)
class GatewayError:
    """Structured error envelope for ``_err(...)`` return."""

    code: ErrorCode
    message: str
    details: dict | None = None


class MethodError(Exception):
    """Raised inside a gateway handler to report a spec-classified error.

    ``dispatch`` catches this and returns the canonical ``err()`` envelope
    with the correct ``ErrorCode`` on the wire. Handlers use this instead
    of returning the ``err()`` dict directly, because the dispatch pipeline
    already wraps handler return values in a ``{"result": ...}`` envelope.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        if not isinstance(code, ErrorCode):
            raise TypeError(
                f"MethodError code must be ErrorCode, got {type(code).__name__}"
            )
        self.code = code
        self.message = message
        self.details = details or None


def err(request_id: str, code: ErrorCode, message: str, **details) -> dict:
    """Canonical error frame builder.

    Signature enforces the ``ErrorCode`` type on ``code`` — callers can no
    longer pass a magic string / number.
    """
    if not isinstance(code, ErrorCode):
        raise TypeError(
            f"code must be an ErrorCode enum, got {type(code).__name__}={code!r}"
        )
    return {
        "id": str(request_id or ""),
        "error": {
            "code": code.value,
            "message": str(message),
            "details": details or None,
        },
    }
