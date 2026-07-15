"""Matrix room operation helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from channels.platforms.base import SendResult
from channels.platforms.matrix_support import (
    EventID,
    EventType,
    PresenceState,
    RoomCreatePreset,
    RoomID,
    UserID,
)

logger = logging.getLogger(__name__)


class MatrixRoomOpsMixin:
    def _background_read_receipt(self, room_id: str, event_id: str) -> None:
        """Fire-and-forget read receipt with error logging."""

        async def _send() -> None:
            try:
                await self.send_read_receipt(room_id, event_id)
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug("Matrix: background read receipt failed: %s", exc)

        asyncio.ensure_future(_send())

    async def send_read_receipt(self, room_id: str, event_id: str) -> bool:
        """Send a read receipt (m.read) for an event."""
        if not self._client:
            return False
        try:
            room = RoomID(room_id)
            event = EventID(event_id)
            if hasattr(self._client, "set_fully_read_marker"):
                await self._client.set_fully_read_marker(room, event, event)
            elif hasattr(self._client, "send_receipt"):
                await self._client.send_receipt(room, event)
            elif hasattr(self._client, "set_read_markers"):
                await self._client.set_read_markers(
                    room,
                    fully_read_event=event,
                    read_receipt=event,
                )
            else:
                logger.debug("Matrix: client has no read receipt method")
                return False
            logger.debug("Matrix: sent read receipt for %s in %s", event_id, room_id)
            return True
        except Exception as exc:
            logger.debug("Matrix: read receipt failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Message redaction
    # ------------------------------------------------------------------

    async def redact_message(
        self,
        room_id: str,
        event_id: str,
        reason: str = "",
    ) -> bool:
        """Redact (delete) a message or event from a room."""
        if not self._client:
            return False
        try:
            await self._client.redact(
                RoomID(room_id),
                EventID(event_id),
                reason=reason or None,
            )
            logger.info("Matrix: redacted %s in %s", event_id, room_id)
            return True
        except Exception as exc:
            logger.warning("Matrix: redact error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Room creation & management
    # ------------------------------------------------------------------

    async def create_room(
        self,
        name: str = "",
        topic: str = "",
        invite: Optional[list] = None,
        is_direct: bool = False,
        preset: str = "private_chat",
    ) -> Optional[str]:
        """Create a new Matrix room."""
        if not self._client:
            return None
        try:
            preset_enum = {
                "private_chat": RoomCreatePreset.PRIVATE,
                "public_chat": RoomCreatePreset.PUBLIC,
                "trusted_private_chat": RoomCreatePreset.TRUSTED_PRIVATE,
            }.get(preset, RoomCreatePreset.PRIVATE)
            invitees = [UserID(u) for u in (invite or [])]
            room_id = await self._client.create_room(
                name=name or None,
                topic=topic or None,
                invitees=invitees,
                is_direct=is_direct,
                preset=preset_enum,
            )
            room_id_str = str(room_id)
            self._joined_rooms.add(room_id_str)
            logger.info("Matrix: created room %s (%s)", room_id_str, name or "unnamed")
            return room_id_str
        except Exception as exc:
            logger.warning("Matrix: create_room error: %s", exc)
            return None

    async def invite_user(self, room_id: str, user_id: str) -> bool:
        """Invite a user to a room."""
        if not self._client:
            return False
        try:
            await self._client.invite_user(RoomID(room_id), UserID(user_id))
            logger.info("Matrix: invited %s to %s", user_id, room_id)
            return True
        except Exception as exc:
            logger.warning("Matrix: invite error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Presence
    # ------------------------------------------------------------------

    _VALID_PRESENCE_STATES = frozenset(("online", "offline", "unavailable"))

    async def set_presence(self, state: str = "online", status_msg: str = "") -> bool:
        """Set the bot's presence status."""
        if not self._client:
            return False
        if state not in self._VALID_PRESENCE_STATES:
            logger.warning("Matrix: invalid presence state %r", state)
            return False
        try:
            presence_map = {
                "online": PresenceState.ONLINE,
                "offline": PresenceState.OFFLINE,
                "unavailable": PresenceState.UNAVAILABLE,
            }
            await self._client.set_presence(
                presence=presence_map[state],
                status=status_msg or None,
            )
            logger.debug("Matrix: presence set to %s", state)
            return True
        except Exception as exc:
            logger.debug("Matrix: set_presence failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Emote & notice message types
    # ------------------------------------------------------------------

    async def _send_simple_message(
        self,
        chat_id: str,
        text: str,
        msgtype: str,
    ) -> SendResult:
        """Send a simple message (emote, notice) with optional HTML formatting."""
        if not self._client or not text:
            return SendResult(success=False, error="No client or empty text")

        msg_content = self._build_text_message_content(text, msgtype=msgtype)

        try:
            event_id = await self._client.send_message_event(
                RoomID(chat_id),
                EventType.ROOM_MESSAGE,
                msg_content,
            )
            return SendResult(success=True, message_id=str(event_id))
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _is_dm_room(self, room_id: str) -> bool:
        """Check if a room is a DM."""
        if self._dm_rooms.get(room_id, False):
            return True
        # Fallback: check member count via state store.
        state_store = (
            getattr(self._client, "state_store", None) if self._client else None
        )
        if state_store:
            try:
                members = await state_store.get_members(room_id)
                if members and len(members) == 2:
                    return True
            except Exception:
                pass
        return False

    async def _refresh_dm_cache(self) -> None:
        """Refresh the DM room cache from m.direct account data."""
        if not self._client:
            return

        dm_data: Optional[Dict] = None

        try:
            resp = await self._client.get_account_data("m.direct")
            if hasattr(resp, "content"):
                dm_data = resp.content
            elif isinstance(resp, dict):
                dm_data = resp
        except Exception as exc:
            logger.debug("Matrix: get_account_data('m.direct') failed: %s", exc)

        if dm_data is None:
            return

        dm_room_ids: Set[str] = set()
        for user_id, rooms in dm_data.items():
            if isinstance(rooms, list):
                dm_room_ids.update(str(r) for r in rooms)

        self._dm_rooms = {rid: (rid in dm_room_ids) for rid in self._joined_rooms}

    # ------------------------------------------------------------------
    # Mention detection helpers
    # ------------------------------------------------------------------
