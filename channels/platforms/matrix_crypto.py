"""Matrix E2EE key verification helpers."""

from __future__ import annotations

import logging
from typing import Any, Optional

from channels.platforms.matrix_support import _CRYPTO_DB_PATH

logger = logging.getLogger(__name__)


class MatrixCryptoMixin:
    @staticmethod
    def _extract_server_ed25519(device_keys_obj: Any) -> Optional[str]:
        """Extract the ed25519 identity key from a DeviceKeys object."""
        for kid, kval in (getattr(device_keys_obj, "keys", {}) or {}).items():
            if str(kid).startswith("ed25519:"):
                return str(kval)
        return None

    async def _reverify_keys_after_upload(
        self, client: Any, local_ed25519: str
    ) -> bool:
        """Re-query the server after share_keys() and verify our ed25519 key matches."""
        try:
            resp = await client.query_keys({client.mxid: [client.device_id]})
            dk = getattr(resp, "device_keys", {}) or {}
            ud = dk.get(str(client.mxid)) or {}
            dev = ud.get(str(client.device_id))
            if dev:
                server_ed = self._extract_server_ed25519(dev)
                if server_ed != local_ed25519:
                    logger.error(
                        "Matrix: device %s has immutable identity keys that "
                        "don't match this installation. Generate a new access "
                        "token with a fresh device.",
                        client.device_id,
                    )
                    return False
        except Exception as exc:
            logger.error("Matrix: post-upload key verification failed: %s", exc, exc_info=True)
            return False
        return True

    async def _verify_device_keys_on_server(self, client: Any, olm: Any) -> bool:
        """Verify our device keys are on the homeserver after loading crypto state.

        Returns True if keys are valid or were successfully re-uploaded.
        Returns False if verification fails (caller should refuse E2EE).
        """
        try:
            resp = await client.query_keys({client.mxid: [client.device_id]})
        except Exception as exc:
            logger.error(
                "Matrix: cannot verify device keys on server: %s — refusing E2EE",
                exc,
                exc_info=True,
            )
            return False

        device_keys_map = getattr(resp, "device_keys", {}) or {}
        our_user_devices = device_keys_map.get(str(client.mxid)) or {}
        our_keys = our_user_devices.get(str(client.device_id))
        local_ed25519 = olm.account.identity_keys.get("ed25519")

        if not our_keys:
            logger.warning("Matrix: device keys missing from server — re-uploading")
            olm.account.shared = False
            try:
                await olm.share_keys()
            except Exception as exc:
                logger.error("Matrix: failed to re-upload device keys: %s", exc, exc_info=True)
                return False
            return await self._reverify_keys_after_upload(client, local_ed25519)

        server_ed25519 = self._extract_server_ed25519(our_keys)

        if server_ed25519 != local_ed25519:
            if olm.account.shared:
                logger.error(
                    "Matrix: server has different identity keys for device %s — "
                    "local crypto state is stale. Delete %s and restart.",
                    client.device_id,
                    _CRYPTO_DB_PATH,
                )
                return False

            logger.warning(
                "Matrix: server has stale keys for device %s — attempting re-upload",
                client.device_id,
            )
            try:
                await client.api.request(
                    client.api.Method.DELETE
                    if hasattr(client.api, "Method")
                    else "DELETE",
                    f"/_matrix/client/v3/devices/{client.device_id}",
                )
                logger.info(
                    "Matrix: deleted stale device %s from server", client.device_id
                )
            except Exception:
                pass
            try:
                await olm.share_keys()
            except Exception as exc:
                logger.error(
                    "Matrix: cannot upload device keys for %s: %s. "
                    "Try generating a new access token to get a fresh device.",
                    client.device_id,
                    exc,
                    exc_info=True,
                )
                return False
            return await self._reverify_keys_after_upload(client, local_ed25519)

        return True


