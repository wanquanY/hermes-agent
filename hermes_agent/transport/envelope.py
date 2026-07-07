"""Frame envelope types shared by every transport (spec §3 L5)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FrameKind(str, Enum):
    """Wire frame taxonomy — all frames carry a ``type`` matching this enum."""

    HANDSHAKE = "handshake"
    REQUEST = "request"
    RESPONSE = "response"
    ERROR = "error"


@dataclass(frozen=True)
class InboundFrame:
    """Frame received from the peer."""

    kind: FrameKind
    id: str = ""
    method: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundFrame:
    """Frame the router intends to emit back to the peer."""

    kind: FrameKind
    id: str = ""
    result: Any = None
    error: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None

    def to_wire(self) -> dict[str, Any]:
        """Serialize to the wire dict shape."""
        base: dict[str, Any] = {"type": self.kind.value}
        if self.id:
            base["id"] = self.id
        if self.result is not None:
            base["result"] = self.result
        if self.error is not None:
            base["error"] = self.error
        if self.payload is not None:
            # Handshake / event frames use a payload block.
            base.update(self.payload)
        return base


class FrameEnvelope:
    """Static helpers for parsing / serializing frames."""

    @staticmethod
    def parse(raw: dict[str, Any] | str) -> InboundFrame:
        """Turn either a wire dict or a JSON string into an ``InboundFrame``."""
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON frame: {exc}") from exc
        else:
            data = dict(raw)

        if not isinstance(data, dict):
            raise ValueError("frame must be an object")

        type_raw = str(data.get("type") or "").strip().lower()
        # Handshake replies from the server are OutboundFrames; from the client
        # they can be inbound (rare — usually just a greeting acknowledgement).
        if type_raw == FrameKind.HANDSHAKE.value:
            return InboundFrame(
                kind=FrameKind.HANDSHAKE,
                id=str(data.get("id") or ""),
                raw=data,
            )

        # If method is present, treat as request regardless of explicit type.
        method = str(data.get("method") or "").strip()
        params = data.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        if method or type_raw == FrameKind.REQUEST.value:
            return InboundFrame(
                kind=FrameKind.REQUEST,
                id=str(data.get("id") or ""),
                method=method,
                params=params,
                raw=data,
            )

        # Everything else is opaque — return a REQUEST with no method so the
        # router can turn it into a MALFORMED_FRAME error envelope.
        return InboundFrame(
            kind=FrameKind.REQUEST,
            id=str(data.get("id") or ""),
            method="",
            params=params,
            raw=data,
        )

    @staticmethod
    def to_json(frame: OutboundFrame) -> str:
        return json.dumps(frame.to_wire(), ensure_ascii=False, sort_keys=True)
