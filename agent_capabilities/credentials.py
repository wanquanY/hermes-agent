"""In-memory, conversation-bound capability credential registry."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class CapabilityCredential:
    capability: str
    token: str
    api_origin: str
    conversation_id: str
    execution_participant_id: str
    expires_at: float

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:12]

    @property
    def expired(self) -> bool:
        return self.expires_at <= time.time()


class CapabilityCredentialRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_conversation: dict[tuple[str, str], CapabilityCredential] = {}

    def configure(
        self,
        *,
        capability: str,
        token: str,
        api_origin: str,
        conversation_id: str,
        execution_participant_id: str,
        expires_at: float,
    ) -> CapabilityCredential:
        normalized_origin = str(api_origin or "").strip().rstrip("/")
        parsed = urlsplit(normalized_origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("capability api origin is invalid")
        values = {
            "capability": str(capability or "").strip(),
            "token": str(token or "").strip(),
            "conversation_id": str(conversation_id or "").strip(),
            "execution_participant_id": str(execution_participant_id or "").strip(),
        }
        if any(not value for value in values.values()):
            raise ValueError("capability credential context is incomplete")
        if expires_at <= time.time():
            raise ValueError("capability credential is already expired")
        credential = CapabilityCredential(
            api_origin=normalized_origin,
            expires_at=float(expires_at),
            **values,
        )
        with self._lock:
            self._by_conversation[(credential.capability, credential.conversation_id)] = credential
        return credential

    def resolve(self, *, capability: str, conversation_id: str) -> CapabilityCredential:
        key = (str(capability or "").strip(), str(conversation_id or "").strip())
        with self._lock:
            credential = self._by_conversation.get(key)
            if credential is None:
                raise RuntimeError("capability credential is not configured for this conversation")
            if credential.expired:
                self._by_conversation.pop(key, None)
                raise RuntimeError("capability credential has expired")
            return credential

    def clear(self, *, capability: str, conversation_id: str) -> bool:
        key = (str(capability or "").strip(), str(conversation_id or "").strip())
        with self._lock:
            return self._by_conversation.pop(key, None) is not None

    def clear_all(self) -> None:
        with self._lock:
            self._by_conversation.clear()

    def has_active(self, capability: str) -> bool:
        now = time.time()
        normalized = str(capability or "").strip()
        with self._lock:
            expired = [
                key
                for key, credential in self._by_conversation.items()
                if credential.expires_at <= now
            ]
            for key in expired:
                self._by_conversation.pop(key, None)
            return any(key[0] == normalized for key in self._by_conversation)


capability_credentials = CapabilityCredentialRegistry()
