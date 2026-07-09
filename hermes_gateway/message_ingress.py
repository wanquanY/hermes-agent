"""Gateway message ingress pre-dispatch controls."""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Literal, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageIngressResult:
    action: Literal["continue", "respond"]
    event: object
    source: object
    session_key: str
    response: Optional[str] = None


class GatewayMessageIngressService:
    def __init__(self, runner):
        self._runner = runner

    async def preprocess(self, event) -> MessageIngressResult:
        runner = self._runner
        source = event.source

        is_internal = bool(getattr(event, "internal", False))

        if not is_internal:
            try:
                from hermes_cli.plugins import invoke_hook as _invoke_hook

                hook_results = _invoke_hook(
                    "pre_gateway_dispatch",
                    event=event,
                    gateway=runner,
                    session_store=runner.session_store,
                )
            except Exception as exc:
                logger.warning("pre_gateway_dispatch invocation failed: %s", exc)
                hook_results = []

            for result in hook_results:
                if not isinstance(result, dict):
                    continue
                action = result.get("action")
                if action == "skip":
                    logger.info(
                        "pre_gateway_dispatch skip: reason=%s platform=%s chat=%s",
                        result.get("reason"),
                        source.platform.value if source.platform else "unknown",
                        source.chat_id or "unknown",
                    )
                    return MessageIngressResult(
                        action="respond",
                        event=event,
                        source=source,
                        session_key="",
                        response=None,
                    )
                if action == "rewrite":
                    new_text = result.get("text")
                    if isinstance(new_text, str):
                        event = dataclasses.replace(event, text=new_text)
                        source = event.source
                    break
                if action == "allow":
                    break

        auth_response = await self._authorize_or_pair(event, source, is_internal)
        if auth_response.action == "respond":
            return auth_response

        session_key = runner._session_key_for_source(source)

        update_response = self._consume_update_prompt_response(event, source, session_key)
        if update_response is not None:
            return MessageIngressResult(
                action="respond",
                event=event,
                source=source,
                session_key=session_key,
                response=update_response,
            )

        clarify_response = self._consume_clarify_response(event, session_key)
        if clarify_response is not None:
            return MessageIngressResult(
                action="respond",
                event=event,
                source=source,
                session_key=session_key,
                response=clarify_response,
            )

        confirm_response = await self._consume_slash_confirm_response(event, session_key)
        if confirm_response is not None:
            return MessageIngressResult(
                action="respond",
                event=event,
                source=source,
                session_key=session_key,
                response=confirm_response,
            )

        return MessageIngressResult(
            action="continue",
            event=event,
            source=source,
            session_key=session_key,
        )

    async def _authorize_or_pair(self, event, source, is_internal: bool) -> MessageIngressResult:
        runner = self._runner
        if is_internal:
            return MessageIngressResult("continue", event, source, "")

        if source.user_id is None:
            if not runner._is_user_authorized(source):
                logger.debug("Ignoring message with no user_id from %s", source.platform.value)
                return MessageIngressResult("respond", event, source, "", None)
            return MessageIngressResult("continue", event, source, "")

        if runner._is_user_authorized(source):
            return MessageIngressResult("continue", event, source, "")

        logger.warning(
            "Unauthorized user: %s (%s) on %s",
            source.user_id,
            source.user_name,
            source.platform.value,
        )
        if (
            source.chat_type == "dm"
            and runner._get_unauthorized_dm_behavior(source.platform) == "pair"
        ):
            platform_name = source.platform.value if source.platform else "unknown"
            if runner.pairing_store._is_rate_limited(platform_name, source.user_id):
                return MessageIngressResult("respond", event, source, "", None)
            code = runner.pairing_store.generate_code(
                platform_name, source.user_id, source.user_name or ""
            )
            adapter = runner.adapters.get(source.platform)
            if code:
                if adapter:
                    await adapter.send(
                        source.chat_id,
                        f"Hi~ I don't recognize you yet!\n\n"
                        f"Here's your pairing code: `{code}`\n\n"
                        f"Ask the bot owner to run:\n"
                        f"`hermes pairing approve {platform_name} {code}`",
                    )
            else:
                if adapter:
                    await adapter.send(
                        source.chat_id,
                        "Too many pairing requests right now~ Please try again later!",
                    )
                runner.pairing_store._record_rate_limit(platform_name, source.user_id)
        return MessageIngressResult("respond", event, source, "", None)

    def _consume_update_prompt_response(self, event, source, session_key: str) -> Optional[str]:
        runner = self._runner
        update_prompts = getattr(runner, "_update_prompt_pending", {})
        if not update_prompts.get(session_key):
            return None

        raw = (event.text or "").strip()
        cmd = event.get_command()
        recognized_cmd = None
        if cmd in {"approve", "yes"}:
            response_text = "y"
        elif cmd in {"deny", "no"}:
            response_text = "n"
        else:
            if cmd:
                try:
                    from hermes_cli.commands import resolve_command as resolve_update_cmd
                except Exception:
                    resolve_update_cmd = None
                if resolve_update_cmd is not None:
                    try:
                        cmd_def = resolve_update_cmd(cmd)
                        recognized_cmd = cmd_def.name if cmd_def else None
                    except Exception:
                        recognized_cmd = None
            response_text = "" if recognized_cmd else raw

        response_path = get_hermes_home() / ".update_response"
        prompt_path = get_hermes_home() / ".update_prompt.json"
        if response_text:
            try:
                tmp = response_path.with_suffix(".tmp")
                tmp.write_text(response_text)
                tmp.replace(response_path)
                prompt_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Failed to write update response: %s", exc)
                return f"✗ Failed to send response to update process: {exc}"
            update_prompts.pop(session_key, None)
            label = response_text if len(response_text) <= 20 else response_text[:20] + "…"
            return f"✓ Sent `{label}` to the update process."

        if recognized_cmd:
            try:
                tmp = response_path.with_suffix(".tmp")
                tmp.write_text("")
                tmp.replace(response_path)
                prompt_path.unlink(missing_ok=True)
                logger.info(
                    "Recognized /%s during pending update prompt for %s; "
                    "cancelled prompt with default and dispatching command",
                    recognized_cmd,
                    session_key,
                )
            except OSError as exc:
                logger.warning(
                    "Failed to write cancel response for pending update prompt: %s",
                    exc,
                )
            update_prompts.pop(session_key, None)
        return None

    def _consume_clarify_response(self, event, session_key: str) -> Optional[str]:
        try:
            from tools import clarify_gateway as clarify_mod

            pending = clarify_mod.get_pending_for_session(session_key)
        except Exception:
            pending = None

        if pending is None:
            return None

        raw_reply = (event.text or "").strip()
        if raw_reply and not raw_reply.startswith("/"):
            resolved = clarify_mod.resolve_gateway_clarify(pending.clarify_id, raw_reply)
            if resolved:
                logger.info(
                    "Gateway intercepted clarify text response (session=%s, id=%s)",
                    session_key,
                    pending.clarify_id,
                )
                return ""
        return None

    async def _consume_slash_confirm_response(self, event, session_key: str) -> Optional[str]:
        from tools import slash_confirm as slash_confirm_mod

        pending_confirm = slash_confirm_mod.get_pending(session_key)
        tool_approval_live = False
        try:
            from tools.approval import has_blocking_approval

            tool_approval_live = has_blocking_approval(session_key)
        except Exception:
            tool_approval_live = False

        if not pending_confirm or tool_approval_live:
            return None

        raw_reply = (event.text or "").strip()
        cmd_reply = event.get_command()
        confirm_choice = None
        if cmd_reply in {"approve", "yes", "ok", "confirm"}:
            confirm_choice = "once"
        elif cmd_reply in {"always", "remember"}:
            confirm_choice = "always"
        elif cmd_reply in {"cancel", "no", "deny", "nevermind"}:
            confirm_choice = "cancel"
        elif raw_reply.lower() in {"approve", "approve once", "once"}:
            confirm_choice = "once"
        elif raw_reply.lower() in {"always", "always approve"}:
            confirm_choice = "always"
        elif raw_reply.lower() in {"cancel", "nevermind", "no"}:
            confirm_choice = "cancel"
        if confirm_choice is not None:
            resolved = await slash_confirm_mod.resolve(
                session_key,
                pending_confirm.get("confirm_id"),
                confirm_choice,
            )
            return resolved or ""

        slash_confirm_mod.clear_if_stale(session_key)
        return None


def message_ingress_for(runner) -> GatewayMessageIngressService:
    service = getattr(runner, "message_ingress", None)
    if isinstance(service, GatewayMessageIngressService):
        return service
    service = GatewayMessageIngressService(runner)
    runner.message_ingress = service
    return service
