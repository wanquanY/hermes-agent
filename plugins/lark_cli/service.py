"""Hermes-facing configuration, authorization, and execution workflows."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

from plugins.lark_cli.runtime import LarkCliResult, LarkCliRuntime

ALLOWED_SERVICE_COMMANDS = frozenset(
    {
        "api",
        "approval",
        "apps",
        "attendance",
        "base",
        "calendar",
        "contact",
        "doc",
        "drive",
        "im",
        "mail",
        "markdown",
        "minutes",
        "note",
        "okr",
        "schema",
        "sheets",
        "slides",
        "task",
        "vc",
        "whiteboard",
        "wiki",
    }
)
RESERVED_ARGUMENTS = frozenset({"--yes", "-y", "--force"})
FLOW_TTL_LIMIT_SECONDS = 15 * 60
_AUTHORIZATION_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_REDACTED = "[REDACTED]"


def _approval_result(
    *,
    tool_name: str,
    description: str,
    target: str,
    rule_key: str,
) -> dict[str, Any]:
    from tools.approval_gate import run_approval_gate

    return run_approval_gate(
        pattern_keys=[f"plugin_rule:{rule_key}"],
        permanent_keys=set(),
        description=description,
        display_target=target,
        subject=f"tool '{tool_name}'",
        persist_decision=False,
        fail_closed_when_no_human=True,
        one_operation_only=True,
        no_human_block_message=(
            f"BLOCKED: {description} requires human approval, but no approval "
            "responder is attached."
        ),
    )


def _blocked_approval_response(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "status": decision.get("status", decision.get("outcome", "blocked")),
        "approval_required": True,
        **{
            key: value
            for key, value in decision.items()
            if key not in {"approved"}
        },
    }


def _payload_data(result: LarkCliResult) -> dict[str, Any]:
    payload = result.payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    return {}


def _safe_command_display(command: str, arguments: Sequence[str]) -> str:
    values = ["lark-cli", command, *arguments]
    return " ".join(json.dumps(value, ensure_ascii=False) for value in values)


def _redact_secret(value: Any, secret: str) -> Any:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, _REDACTED)
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    if isinstance(value, dict):
        return {
            key: _redact_secret(item, secret)
            for key, item in value.items()
        }
    return value


class LarkCliService:
    """Owns all policy around official CLI operations."""

    def __init__(self, runtime: LarkCliRuntime | None = None) -> None:
        self.runtime = runtime or LarkCliRuntime()

    @property
    def flow_root(self) -> Path:
        return self.runtime.state_root / "auth-flows"

    def status(self, *, verify: bool = True) -> dict[str, Any]:
        probe = self.runtime.probe()
        response: dict[str, Any] = {"ok": probe.available and probe.compatible}
        response["cli"] = probe.to_dict()
        if not probe.available or not probe.compatible:
            return response

        config = self.runtime.run(["config", "show"])
        response["binding"] = config.to_dict()
        if not config.ok:
            response["ok"] = False
            response["next_action"] = "bind"
            return response

        auth_args = ["auth", "status", "--json"]
        if verify:
            auth_args.append("--verify")
        auth = self.runtime.run(auth_args)
        response["auth"] = auth.to_dict()
        auth_data = _payload_data(auth)
        identity = str(auth_data.get("identity") or "")
        verified = auth_data.get("verified")
        response["ok"] = (
            auth.ok
            and identity not in {"", "none"}
            and (not verify or verified is not False)
        )
        if auth.ok and identity in {"", "none"}:
            response["next_action"] = "authorize_user_or_use_bot"
        elif auth.ok and verify and verified is False:
            response["next_action"] = "reauthorize"
        return response

    def bind(self, *, identity: str = "bot-only") -> dict[str, Any]:
        if identity not in {"bot-only", "user-default"}:
            return {
                "ok": False,
                "error": "identity must be bot-only or user-default",
            }
        risk = (
            "Bind the official Lark CLI to the active Hermes Feishu app using "
            f"the {identity} identity policy."
        )
        if identity == "user-default":
            risk += (
                " This permits the agent to act as the authorized user and "
                "access personal resources granted during OAuth."
            )
        decision = _approval_result(
            tool_name="lark_cli_auth",
            description=risk,
            target=f"lark-cli config bind --source hermes --identity {identity}",
            rule_key=f"lark-cli.bind.{identity}",
        )
        if not decision.get("approved"):
            return _blocked_approval_response(decision)

        args = ["config", "bind", "--source", "hermes", "--identity", identity]
        if identity == "user-default":
            # The human approval above covers the exact identity escalation
            # that official lark-cli protects with --force.
            args.append("--force")
        result = self.runtime.run(args)
        return result.to_dict()

    def start_login(
        self,
        *,
        domains: Iterable[str] = (),
        scopes: Iterable[str] = (),
        excludes: Iterable[str] = (),
        recommend: bool = False,
    ) -> dict[str, Any]:
        normalized_domains = self._clean_values(domains, "domain")
        normalized_scopes = self._clean_values(scopes, "scope")
        normalized_excludes = self._clean_values(excludes, "exclude")
        args = ["auth", "login", "--no-wait", "--json"]
        for domain in normalized_domains:
            args.extend(["--domain", domain])
        if normalized_scopes:
            args.extend(["--scope", " ".join(normalized_scopes)])
        for excluded in normalized_excludes:
            args.extend(["--exclude", excluded])
        if recommend or (not normalized_domains and not normalized_scopes):
            args.append("--recommend")

        result = self.runtime.run(args)
        if not result.ok:
            return result.to_dict()
        data = _payload_data(result)
        verification_url = str(data.get("verification_url", ""))
        device_code = str(data.get("device_code", ""))
        expires_in = int(data.get("expires_in") or 600)
        if not verification_url or not device_code:
            return {
                "ok": False,
                "error": "lark-cli did not return a device authorization flow",
                "details": result.to_dict(),
            }

        authorization_id = uuid.uuid4().hex
        self.flow_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.flow_root.chmod(0o700)
        except OSError:
            pass
        flow_dir = self.flow_root / authorization_id
        flow_dir.mkdir(mode=0o700)
        qr_result = self.runtime.run(
            ["auth", "qrcode", verification_url, "--output", "authorization.png"],
            cwd=flow_dir,
        )
        if not qr_result.ok:
            self._remove_flow_dir(flow_dir)
            return {
                "ok": False,
                "error": "failed to generate the required authorization QR code",
                "details": qr_result.to_dict(),
            }
        qr_path = flow_dir / "authorization.png"
        if not qr_path.is_file():
            self._remove_flow_dir(flow_dir)
            return {
                "ok": False,
                "error": "lark-cli reported success without creating the QR image",
            }
        try:
            qr_path.chmod(0o600)
        except OSError:
            pass

        ttl = max(1, min(expires_in, FLOW_TTL_LIMIT_SECONDS))
        state = {
            "device_code": device_code,
            "created_at": int(time.time()),
            "expires_at": int(time.time()) + ttl,
        }
        state_path = flow_dir / "flow.json"
        with state_path.open("x", encoding="utf-8") as state_file:
            json.dump(state, state_file)
        try:
            state_path.chmod(0o600)
        except OSError:
            pass
        return {
            "ok": True,
            "status": "authorization_required",
            "authorization_id": authorization_id,
            "verification_url": verification_url,
            "qr_code_path": str(qr_path.resolve()),
            "expires_in": ttl,
            "instructions": (
                "Show the verification URL first and the QR image directly below it. "
                "End this turn and ask the user to confirm after authorization. "
                "Only then call complete with this authorization_id."
            ),
        }

    def complete_login(self, *, authorization_id: str) -> dict[str, Any]:
        flow_dir, state = self._read_flow(authorization_id)
        if not state:
            return {
                "ok": False,
                "error": "authorization flow is missing, expired, or already used",
                "next_action": "start",
            }
        if int(state.get("expires_at", 0)) <= int(time.time()):
            self._remove_flow_dir(flow_dir)
            return {
                "ok": False,
                "error": "authorization flow expired",
                "next_action": "start",
            }

        device_code = str(state.get("device_code", ""))
        try:
            result = self.runtime.run(
                ["auth", "login", "--device-code", device_code, "--json"],
                timeout_seconds=630,
            )
            return _redact_secret(result.to_dict(), device_code)
        finally:
            self._remove_flow_dir(flow_dir)

    def logout(self) -> dict[str, Any]:
        decision = _approval_result(
            tool_name="lark_cli_auth",
            description=(
                "Log out the current Lark CLI user and remove the locally stored "
                "user authorization token."
            ),
            target="lark-cli auth logout --json",
            rule_key="lark-cli.auth.logout",
        )
        if not decision.get("approved"):
            return _blocked_approval_response(decision)
        return self.runtime.run(["auth", "logout", "--json"]).to_dict()

    def check_scopes(self, scopes: Iterable[str]) -> dict[str, Any]:
        normalized = self._clean_values(scopes, "scope")
        if not normalized:
            return {"ok": False, "error": "at least one scope is required"}
        return self.runtime.run(
            ["auth", "check", "--scope", " ".join(normalized), "--json"]
        ).to_dict()

    def run_business_command(
        self,
        *,
        command: str,
        arguments: Sequence[str] = (),
        identity: str = "auto",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        normalized_command = str(command or "").strip().lower()
        if normalized_command not in ALLOWED_SERVICE_COMMANDS:
            return {
                "ok": False,
                "error": (
                    f"unsupported lark-cli business command: {normalized_command or '<empty>'}"
                ),
                "allowed_commands": sorted(ALLOWED_SERVICE_COMMANDS),
            }
        if identity not in {"auto", "bot", "user"}:
            return {"ok": False, "error": "identity must be auto, bot, or user"}
        try:
            normalized_args = list(self._clean_arguments(arguments))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        for argument in normalized_args:
            flag = argument.split("=", 1)[0]
            if flag in RESERVED_ARGUMENTS:
                return {
                    "ok": False,
                    "error": f"{flag} is managed by the plugin approval boundary",
                }
            if flag in {"--as", "--profile"}:
                return {
                    "ok": False,
                    "error": f"{flag} must be selected through the structured tool fields",
                }

        args = [normalized_command, *normalized_args]
        if identity != "auto":
            args.extend(["--as", identity])
        if dry_run:
            args.append("--dry-run")
        result = self.runtime.run(args)
        if not result.confirmation_required:
            return result.to_dict()

        display = _safe_command_display(normalized_command, normalized_args)
        digest = hashlib.sha256(display.encode("utf-8")).hexdigest()[:16]
        decision = _approval_result(
            tool_name="lark_cli_run",
            description=(
                "The official Lark CLI classified this business operation as "
                "a high-risk write and requires explicit confirmation."
            ),
            target=display,
            rule_key=f"lark-cli.high-risk.{digest}",
        )
        if not decision.get("approved"):
            response = _blocked_approval_response(decision)
            response["cli_result"] = result.to_dict()
            return response

        confirmed_args = [*args, "--yes"]
        return self.runtime.run(confirmed_args).to_dict()

    def list_skills(self, path: str = "") -> dict[str, Any]:
        args = ["skills", "list"]
        if path:
            args.append(self._safe_skill_path(path))
        args.append("--json")
        return self.runtime.run(args).to_dict()

    def read_skill(self, name: str, path: str = "") -> dict[str, Any]:
        skill_name = self._safe_skill_path(name)
        args = ["skills", "read", skill_name]
        if path:
            args.append(self._safe_skill_path(path))
        args.append("--json")
        return self.runtime.run(args).to_dict()

    @staticmethod
    def _clean_values(values: Iterable[str], label: str) -> tuple[str, ...]:
        normalized = []
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            if "\x00" in text or len(text) > 512:
                raise ValueError(f"invalid {label}")
            normalized.append(text)
        return tuple(dict.fromkeys(normalized))

    @staticmethod
    def _clean_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
        if len(arguments) > 128:
            raise ValueError("too many lark-cli arguments")
        normalized = []
        total = 0
        for argument in arguments:
            if not isinstance(argument, str) or "\x00" in argument:
                raise ValueError("lark-cli arguments must be strings without NUL bytes")
            total += len(argument.encode("utf-8"))
            if total > 128 * 1024:
                raise ValueError("lark-cli argument payload is too large")
            normalized.append(argument)
        return tuple(normalized)

    @staticmethod
    def _safe_skill_path(value: str) -> str:
        text = str(value or "").strip().replace("\\", "/")
        if (
            not text
            or text.startswith("/")
            or any(part in {"", ".", ".."} for part in text.split("/"))
            or "\x00" in text
        ):
            raise ValueError("skill path must be a safe relative path")
        return text

    def _read_flow(self, authorization_id: str) -> tuple[Path, dict[str, Any] | None]:
        if not _AUTHORIZATION_ID_RE.fullmatch(str(authorization_id or "")):
            return self.flow_root / "invalid", None
        flow_dir = self.flow_root / authorization_id
        if not self._is_owned_flow_dir(flow_dir):
            return flow_dir, None
        try:
            state = json.loads((flow_dir / "flow.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return flow_dir, None
        return flow_dir, state if isinstance(state, dict) else None

    def _remove_flow_dir(self, flow_dir: Path) -> None:
        if not self._is_owned_flow_dir(flow_dir):
            return
        for child in flow_dir.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
        try:
            flow_dir.rmdir()
        except OSError:
            pass

    def _is_owned_flow_dir(self, flow_dir: Path) -> bool:
        try:
            return (
                flow_dir.is_dir()
                and not flow_dir.is_symlink()
                and flow_dir.resolve().parent == self.flow_root.resolve()
            )
        except OSError:
            return False
