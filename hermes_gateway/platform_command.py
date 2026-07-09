"""Gateway /platform command ownership."""

from __future__ import annotations

from channels.platforms.base import MessageEvent
from hermes_gateway.config import Platform


def _resolve_platform(name: str):
    if not name:
        return None
    for platform in Platform.__members__.values():
        if platform.value.lower() == name.lower():
            return platform
    return None


class GatewayPlatformCommandMixin:
    async def _handle_platform_command(self, event: MessageEvent) -> str:
        """Surface and manually control failed or paused gateway adapters."""

        args = (event.get_command_args() or "").strip()
        parts = args.split(maxsplit=1)
        action = (parts[0] if parts else "list").lower()
        target = parts[1].lower() if len(parts) > 1 else ""

        if action == "list":
            lines = ["**Gateway platforms**"]
            connected = sorted(p.value for p in self.adapters.keys())
            if connected:
                lines.append("Connected: " + ", ".join(connected))
            else:
                lines.append("Connected: (none)")
            failed = getattr(self, "_failed_platforms", {}) or {}
            if failed:
                for platform, info in failed.items():
                    if info.get("paused"):
                        reason = info.get("pause_reason") or "paused"
                        lines.append(
                            f"  · {platform.value} — PAUSED ({reason}). "
                            f"Resume with `/platform resume {platform.value}`."
                        )
                    else:
                        attempts = info.get("attempts", 0)
                        lines.append(
                            f"  · {platform.value} — retrying (attempt {attempts})"
                        )
            else:
                lines.append("Failed/paused: (none)")
            return "\n".join(lines)

        if action in {"pause", "resume"}:
            if not target:
                return f"Usage: /platform {action} <name>"
            platform = _resolve_platform(target)
            if platform is None:
                return f"Unknown platform: {target}"
            failed = getattr(self, "_failed_platforms", {}) or {}
            if action == "pause":
                if platform not in failed:
                    return (
                        f"{platform.value} is not in the retry queue "
                        f"(it's either connected or not enabled)."
                    )
                if failed[platform].get("paused"):
                    return f"{platform.value} is already paused."
                self._pause_failed_platform(platform, reason="paused via /platform pause")
                return (
                    f"✓ {platform.value} paused. "
                    f"Resume with `/platform resume {platform.value}` or "
                    f"`hermes gateway restart` to reset."
                )
            if platform not in failed:
                return (
                    f"{platform.value} is not in the retry queue — "
                    f"nothing to resume."
                )
            if not failed[platform].get("paused"):
                return (
                    f"{platform.value} is already retrying — "
                    f"no resume needed."
                )
            self._resume_paused_platform(platform)
            return f"✓ {platform.value} resumed — retrying on next watcher tick."

        return (
            "Usage: /platform <list|pause|resume> [name]\n"
            "  /platform list — show platform status\n"
            "  /platform pause <name> — stop retrying a failing platform\n"
            "  /platform resume <name> — re-queue a paused platform"
        )
