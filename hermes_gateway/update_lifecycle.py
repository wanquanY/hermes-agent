"""Gateway update lifecycle ownership."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

from agent.i18n import t
from channels.platforms.base import MessageEvent
from hermes_gateway.bootstrap import resolve_hermes_bin as _resolve_hermes_bin
from hermes_gateway.config import Platform
from hermes_gateway.lifecycle_home import gateway_home

logger = logging.getLogger(__name__)


class GatewayUpdateLifecycleService:
    def __init__(self, runner):
        self._runner = runner

    async def handle_update_command(self, event: MessageEvent) -> str:
        """Handle /update command — update Hermes Agent to the latest version.

        Spawns ``hermes update`` in a detached session (via ``setsid``) so it
        survives the gateway restart that ``hermes update`` may trigger. Marker
        files are written so either the current gateway process or the next one
        can notify the user when the update finishes.
        """
        import json
        import shutil
        import subprocess
        from datetime import datetime
        from hermes_cli.config import is_managed, format_managed_message

        # Block non-messaging platforms (API server, webhooks, ACP)
        platform = event.source.platform
        _allowed = getattr(self._runner, "_UPDATE_ALLOWED_PLATFORMS", frozenset())
        # Plugin platforms with allow_update_command=True are also allowed
        if platform not in _allowed:
            try:
                from channels.platform_registry import platform_registry
                entry = platform_registry.get(platform.value)
                if not entry or not entry.allow_update_command:
                    return t("gateway.update.platform_not_messaging")
            except Exception:
                return t("gateway.update.platform_not_messaging")

        if is_managed():
            return f"✗ {format_managed_message('update Hermes Agent')}"

        project_root = Path(__file__).parent.parent.resolve()
        git_dir = project_root / '.git'

        if not git_dir.exists():
            return t("gateway.update.not_git_repo")

        hermes_cmd = _resolve_hermes_bin()
        if not hermes_cmd:
            return t("gateway.update.hermes_cmd_not_found")

        pending_path = gateway_home() / ".update_pending.json"
        output_path = gateway_home() / ".update_output.txt"
        exit_code_path = gateway_home() / ".update_exit_code"
        session_key = self._runner._session_key_for_source(event.source)
        pending = {
            "platform": event.source.platform.value,
            "chat_id": event.source.chat_id,
            "user_id": event.source.user_id,
            "session_key": session_key,
            "timestamp": datetime.now().isoformat(),
        }
        if event.source.thread_id:
            pending["thread_id"] = event.source.thread_id
        _tmp_pending = pending_path.with_suffix(".tmp")
        _tmp_pending.write_text(json.dumps(pending))
        _tmp_pending.replace(pending_path)
        exit_code_path.unlink(missing_ok=True)

        # Spawn `hermes update --gateway` detached so it survives gateway restart.
        # --gateway enables file-based IPC for interactive prompts (stash
        # restore, config migration) so the gateway can forward them to the
        # user instead of silently skipping them.
        # Use setsid for portable session detach (works under system services
        # where systemd-run --user fails due to missing D-Bus session).
        # PYTHONUNBUFFERED ensures output is flushed line-by-line so the
        # gateway can stream it to the messenger in near-real-time.
        # Spawn `hermes update --gateway` detached so it survives gateway restart.
        # --gateway enables file-based IPC for interactive prompts (stash
        # restore, config migration) so the gateway can forward them to the
        # user instead of silently skipping them.
        # Use setsid for portable session detach (works under system services
        # where systemd-run --user fails due to missing D-Bus session).
        # PYTHONUNBUFFERED ensures output is flushed line-by-line so the
        # gateway can stream it to the messenger in near-real-time.
        #
        # Windows: no bash/setsid chain.  Run `hermes update --gateway`
        # directly via sys.executable; redirect stdout/stderr to the same
        # output files via Popen file handles; write the exit code in a
        # follow-up write.  A tiny Python watcher would be cleaner but
        # we're already inside gateway/run.py's update path which is async,
        # so the simplest correct thing is: launch an inline Python helper
        # that runs the command and writes both outputs.
        try:
            if sys.platform == "win32":
                import textwrap
                from hermes_cli._subprocess_compat import windows_detach_popen_kwargs

                # hermes_cmd is a list of argv parts we can pass directly
                # (no shell-quoting needed).
                helper = textwrap.dedent(
                    """
                    import os, subprocess, sys
                    output_path = sys.argv[1]
                    exit_code_path = sys.argv[2]
                    cmd = sys.argv[3:]
                    env = dict(os.environ)
                    env["PYTHONUNBUFFERED"] = "1"
                    with open(output_path, "wb") as f:
                        proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=env)
                        rc = proc.wait()
                    with open(exit_code_path, "w") as f:
                        f.write(str(rc))
                    """
                ).strip()
                subprocess.Popen(
                    [
                        sys.executable, "-c", helper,
                        str(output_path), str(exit_code_path),
                        *hermes_cmd, "update", "--gateway",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **windows_detach_popen_kwargs(),
                )
            else:
                hermes_cmd_str = " ".join(shlex.quote(part) for part in hermes_cmd)
                update_cmd = (
                    f"PYTHONUNBUFFERED=1 {hermes_cmd_str} update --gateway"
                    f" > {shlex.quote(str(output_path))} 2>&1; "
                    # Avoid `status=$?`: `status` is a read-only special parameter
                    # in zsh, and this command string is copied/reused in macOS/zsh
                    # operator wrappers. Keep the template zsh-safe even though this
                    # specific subprocess currently runs under bash.
                    f"rc=$?; printf '%s' \"$rc\" > {shlex.quote(str(exit_code_path))}"
                )
                setsid_bin = shutil.which("setsid")
                if setsid_bin:
                    # Preferred: setsid creates a new session, fully detached
                    subprocess.Popen(
                        [setsid_bin, "bash", "-c", update_cmd],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                else:
                    # Fallback: start_new_session=True calls os.setsid() in child
                    subprocess.Popen(
                        ["bash", "-c", update_cmd],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
        except Exception as e:
            pending_path.unlink(missing_ok=True)
            exit_code_path.unlink(missing_ok=True)
            return t("gateway.update.start_failed", error=e)

        self.schedule_update_notification_watch()
        return t("gateway.update.starting")
    def schedule_update_notification_watch(self) -> None:
        """Ensure a background task is watching for update completion."""
        existing_task = getattr(self, "_update_notification_task", None)
        if existing_task and not existing_task.done():
            return

        try:
            self._runner._update_notification_task = asyncio.create_task(
                self.watch_update_progress()
            )
        except RuntimeError:
            logger.debug("Skipping update notification watcher: no running event loop")
    async def watch_update_progress(
        self,
        poll_interval: float = 2.0,
        stream_interval: float = 4.0,
        timeout: float = 1800.0,
    ) -> None:
        """Watch ``hermes update --gateway``, streaming output + forwarding prompts.

        Polls ``.update_output.txt`` for new content and sends chunks to the
        user periodically.  Detects ``.update_prompt.json`` (written by the
        update process when it needs user input) and forwards the prompt to
        the messenger.  The user's next message is intercepted by
        ``_handle_message`` and written to ``.update_response``.
        """
        pending_path = gateway_home() / ".update_pending.json"
        claimed_path = gateway_home() / ".update_pending.claimed.json"
        output_path = gateway_home() / ".update_output.txt"
        exit_code_path = gateway_home() / ".update_exit_code"
        prompt_path = gateway_home() / ".update_prompt.json"

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        # Resolve the adapter and chat_id for sending messages
        adapter = None
        chat_id = None
        session_key = None
        metadata = None
        for path in (claimed_path, pending_path):
            if path.exists():
                try:
                    pending = json.loads(path.read_text())
                    platform_str = pending.get("platform")
                    chat_id = pending.get("chat_id")
                    session_key = pending.get("session_key")
                    thread_id = pending.get("thread_id")
                    metadata = {"thread_id": thread_id} if thread_id else None
                    if platform_str and chat_id:
                        platform = Platform(platform_str)
                        adapter = self._runner.adapters.get(platform)
                        # Fallback session key if not stored (old pending files)
                        if not session_key:
                            session_key = f"{platform_str}:{chat_id}"
                    break
                except Exception:
                    pass

        if not adapter or not chat_id:
            logger.warning("Update watcher: cannot resolve adapter/chat_id, falling back to completion-only")
            # Fall back to old behavior: wait for exit code and send final notification
            while (pending_path.exists() or claimed_path.exists()) and loop.time() < deadline:
                if exit_code_path.exists():
                    await self.send_update_notification()
                    return
                await asyncio.sleep(poll_interval)
            if (pending_path.exists() or claimed_path.exists()) and not exit_code_path.exists():
                exit_code_path.write_text("124")
                await self.send_update_notification()
            return

        def _strip_ansi(text: str) -> str:
            return re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)

        bytes_sent = 0
        last_stream_time = loop.time()
        buffer = ""

        async def _flush_buffer() -> None:
            """Send buffered output to the user."""
            nonlocal buffer, last_stream_time
            if not buffer.strip():
                buffer = ""
                return
            # Chunk to fit message limits (Telegram: 4096, others: generous)
            clean = _strip_ansi(buffer).strip()
            buffer = ""
            last_stream_time = loop.time()
            if not clean:
                return
            # Split into chunks if too long
            max_chunk = 3500
            chunks = [clean[i:i + max_chunk] for i in range(0, len(clean), max_chunk)]
            for chunk in chunks:
                try:
                    await adapter.send(chat_id, f"```\n{chunk}\n```", metadata=metadata)
                except Exception as e:
                    logger.debug("Update stream send failed: %s", e)

        while loop.time() < deadline:
            # Check for completion
            if exit_code_path.exists():
                # Read any remaining output
                if output_path.exists():
                    try:
                        content = output_path.read_text()
                        if len(content) > bytes_sent:
                            buffer += content[bytes_sent:]
                            bytes_sent = len(content)
                    except OSError:
                        pass
                await _flush_buffer()

                # Send final status
                try:
                    exit_code_raw = exit_code_path.read_text().strip() or "1"
                    exit_code = int(exit_code_raw)
                    if exit_code == 0:
                        await adapter.send(chat_id, "✅ Hermes update finished.", metadata=metadata)
                    else:
                        await adapter.send(
                            chat_id,
                            "❌ Hermes update failed (exit code {}).".format(exit_code),
                            metadata=metadata,
                        )
                    logger.info("Update finished (exit=%s), notified %s", exit_code, session_key)
                except Exception as e:
                    logger.warning("Update final notification failed: %s", e)

                # Cleanup
                for p in (pending_path, claimed_path, output_path,
                          exit_code_path, prompt_path):
                    p.unlink(missing_ok=True)
                (gateway_home() / ".update_response").unlink(missing_ok=True)
                self._runner._update_prompt_pending.pop(session_key, None)
                return

            # Check for new output
            if output_path.exists():
                try:
                    content = output_path.read_text()
                    if len(content) > bytes_sent:
                        buffer += content[bytes_sent:]
                        bytes_sent = len(content)
                except OSError:
                    pass

            # Flush buffer periodically
            if buffer.strip() and (loop.time() - last_stream_time) >= stream_interval:
                await _flush_buffer()

            # Check for prompts — only forward if we haven't already sent
            # one that's still awaiting a response.  Without this guard the
            # watcher would re-read the same .update_prompt.json every poll
            # cycle and spam the user with duplicate prompt messages.
            if (prompt_path.exists() and session_key
                    and not self._runner._update_prompt_pending.get(session_key)):
                try:
                    prompt_data = json.loads(prompt_path.read_text())
                    prompt_text = prompt_data.get("prompt", "")
                    default = prompt_data.get("default", "")
                    if prompt_text:
                        # Flush any buffered output first so the user sees
                        # context before the prompt
                        await _flush_buffer()
                        # Try platform-native buttons first (Discord, Telegram)
                        sent_buttons = False
                        if getattr(type(adapter), "send_update_prompt", None) is not None:
                            try:
                                await adapter.send_update_prompt(
                                    chat_id=chat_id,
                                    prompt=prompt_text,
                                    default=default,
                                    session_key=session_key,
                                    metadata=metadata,
                                )
                                sent_buttons = True
                            except Exception as btn_err:
                                logger.debug("Button-based update prompt failed: %s", btn_err)
                        if not sent_buttons:
                            default_hint = f" (default: {default})" if default else ""
                            await adapter.send(
                                chat_id,
                                f"⚕ **Update needs your input:**\n\n"
                                f"{prompt_text}{default_hint}\n\n"
                                f"Reply `/approve` (yes) or `/deny` (no), "
                                f"or type your answer directly.",
                                metadata=metadata,
                            )
                        # Keep the prompt marker on disk until the user
                        # answers. If the gateway restarts mid-prompt, the
                        # next watcher can recover by re-forwarding it from
                        # disk. Duplicate sends in the same process are
                        # still suppressed by _update_prompt_pending.
                        self._runner._update_prompt_pending[session_key] = True
                        # .update_response to continue — it doesn't re-check
                        logger.info("Forwarded update prompt to %s: %s", session_key, prompt_text[:80])
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug("Failed to read update prompt: %s", e)

            await asyncio.sleep(poll_interval)

        # Timeout
        if not exit_code_path.exists():
            logger.warning("Update watcher timed out after %.0fs", timeout)
            exit_code_path.write_text("124")
            await _flush_buffer()
            try:
                await adapter.send(
                    chat_id,
                    "❌ Hermes update timed out after 30 minutes.",
                    metadata=metadata,
                )
            except Exception:
                pass
            for p in (pending_path, claimed_path, output_path,
                      exit_code_path, prompt_path):
                p.unlink(missing_ok=True)
            (gateway_home() / ".update_response").unlink(missing_ok=True)
            self._runner._update_prompt_pending.pop(session_key, None)
    async def send_update_notification(self) -> bool:
        """If an update finished, notify the user.

        Returns False when the update is still running so a caller can retry
        later. Returns True after a definitive send/skip decision.

        This is the legacy notification path used when the streaming watcher
        cannot resolve the adapter (e.g. after a gateway restart where the
        platform hasn't reconnected yet).
        """
        pending_path = gateway_home() / ".update_pending.json"
        claimed_path = gateway_home() / ".update_pending.claimed.json"
        output_path = gateway_home() / ".update_output.txt"
        exit_code_path = gateway_home() / ".update_exit_code"

        if not pending_path.exists() and not claimed_path.exists():
            return False

        cleanup = True
        active_pending_path = claimed_path
        try:
            if pending_path.exists():
                try:
                    pending_path.replace(claimed_path)
                except FileNotFoundError:
                    if not claimed_path.exists():
                        return True
            elif not claimed_path.exists():
                return True

            pending = json.loads(claimed_path.read_text())
            platform_str = pending.get("platform")
            chat_id = pending.get("chat_id")
            thread_id = pending.get("thread_id")

            if not exit_code_path.exists():
                logger.info("Update notification deferred: update still running")
                cleanup = False
                active_pending_path = pending_path
                claimed_path.replace(pending_path)
                return False

            exit_code_raw = exit_code_path.read_text().strip() or "1"
            exit_code = int(exit_code_raw)

            # Read the captured update output
            output = ""
            if output_path.exists():
                output = output_path.read_text()

            # Resolve adapter
            platform = Platform(platform_str)
            adapter = self._runner.adapters.get(platform)

            if adapter and chat_id:
                metadata = {"thread_id": thread_id} if thread_id else None
                # Strip ANSI escape codes for clean display
                output = re.sub(r'\x1b\[[0-9;]*m', '', output).strip()
                if output:
                    if len(output) > 3500:
                        output = "…" + output[-3500:]
                    if exit_code == 0:
                        msg = f"✅ Hermes update finished.\n\n```\n{output}\n```"
                    else:
                        msg = f"❌ Hermes update failed.\n\n```\n{output}\n```"
                elif exit_code == 0:
                    msg = "✅ Hermes update finished successfully."
                else:
                    msg = "❌ Hermes update failed. Check the gateway logs or run `hermes update` manually for details."
                await adapter.send(chat_id, msg, metadata=metadata)
                logger.info(
                    "Sent post-update notification to %s:%s (exit=%s)",
                    platform_str,
                    chat_id,
                    exit_code,
                )
        except Exception as e:
            logger.warning("Post-update notification failed: %s", e)
        finally:
            if cleanup:
                active_pending_path.unlink(missing_ok=True)
                claimed_path.unlink(missing_ok=True)
                output_path.unlink(missing_ok=True)
                exit_code_path.unlink(missing_ok=True)

        return True


def update_lifecycle_for(runner) -> GatewayUpdateLifecycleService:
    service = getattr(runner, "update_lifecycle", None)
    if isinstance(service, GatewayUpdateLifecycleService):
        return service
    service = GatewayUpdateLifecycleService(runner)
    runner.update_lifecycle = service
    return service
