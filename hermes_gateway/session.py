"""Public gateway session API."""
from __future__ import annotations

from channels.session_identity import (
    SessionContext,
    SessionSource,
    build_session_key,
    is_shared_multi_user_session,
)
from channels.whatsapp_identity import (
    canonical_whatsapp_identifier,
    normalize_whatsapp_identifier,
)
from hermes_gateway.config import GatewayConfig, HomeChannel, Platform, SessionResetPolicy
from hermes_gateway.session_context import (
    _hash_chat_id,
    _hash_id,
    _hash_sender_id,
    build_session_context,
    build_session_context_prompt,
)
from hermes_gateway.session_entry import SessionEntry
from hermes_gateway.session_store import SessionStore

__all__ = [
    "GatewayConfig",
    "HomeChannel",
    "Platform",
    "SessionResetPolicy",
    "SessionContext",
    "SessionSource",
    "build_session_key",
    "is_shared_multi_user_session",
    "canonical_whatsapp_identifier",
    "normalize_whatsapp_identifier",
    "build_session_context",
    "build_session_context_prompt",
    "_hash_id",
    "_hash_sender_id",
    "_hash_chat_id",
    "SessionEntry",
    "SessionStore",
]
