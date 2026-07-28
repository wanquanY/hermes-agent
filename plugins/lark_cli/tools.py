"""Structured Hermes tool schemas for the Lark CLI plugin."""

from __future__ import annotations

from typing import Any

from plugins.lark_cli.service import LarkCliService


def _service() -> LarkCliService:
    """Resolve HERMES_HOME at call time for Dovie's profile-scoped runtime."""
    return LarkCliService()

LARK_CLI_STATUS_SCHEMA = {
    "name": "lark_cli_status",
    "description": (
        "Check the official Lark/Feishu CLI version, Hermes credential binding, "
        "and effective bot/user authorization state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "verify": {
                "type": "boolean",
                "description": "Verify the effective token with Lark/Feishu.",
                "default": True,
            }
        },
        "additionalProperties": False,
    },
}

LARK_CLI_AUTH_SCHEMA = {
    "name": "lark_cli_auth",
    "description": (
        "Bind lark-cli to the active Hermes Feishu app or manage two-step user "
        "authorization. Start returns a URL and QR image; complete must only be "
        "called in a later turn after the user confirms authorization."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["bind", "start", "complete", "logout", "check"],
            },
            "identity": {
                "type": "string",
                "enum": ["bot-only", "user-default"],
                "description": (
                    "Binding policy. bot-only is safer; user-default permits "
                    "acting as an authorized user."
                ),
                "default": "bot-only",
            },
            "domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Business domains to authorize, such as calendar or task.",
                "default": [],
            },
            "scopes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact OAuth scopes to authorize or check.",
                "default": [],
            },
            "exclude": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Scopes to exclude from a start request.",
                "default": [],
            },
            "recommend": {
                "type": "boolean",
                "description": "Request only official recommended scopes.",
                "default": False,
            },
            "authorization_id": {
                "type": "string",
                "description": "Opaque flow id returned by action=start.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

LARK_CLI_RUN_SCHEMA = {
    "name": "lark_cli_run",
    "description": (
        "Run an official lark-cli business command using argv (never a shell). "
        "Read the matching official Skill with lark_cli_skill first. High-risk "
        "writes are routed through Hermes approval before --yes is added."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Top-level business domain, for example calendar, im, or doc.",
            },
            "arguments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact argv entries after the business command.",
                "default": [],
            },
            "identity": {
                "type": "string",
                "enum": ["auto", "bot", "user"],
                "default": "auto",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Ask lark-cli to validate and preview without writing.",
                "default": False,
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}

LARK_CLI_SKILL_SCHEMA = {
    "name": "lark_cli_skill",
    "description": (
        "List or read the official AI Skills embedded in the installed lark-cli "
        "binary so command guidance always matches the runtime version."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "read"]},
            "name": {
                "type": "string",
                "description": "Skill name, such as lark-calendar.",
            },
            "path": {
                "type": "string",
                "description": "Optional relative reference path inside a Skill.",
                "default": "",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def handle_status(verify: bool = True, **_: Any) -> dict[str, Any]:
    return _service().status(verify=verify)


def handle_auth(
    action: str,
    identity: str = "bot-only",
    domains: list[str] | None = None,
    scopes: list[str] | None = None,
    exclude: list[str] | None = None,
    recommend: bool = False,
    authorization_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    try:
        if action == "bind":
            return _service().bind(identity=identity)
        if action == "start":
            return _service().start_login(
                domains=domains or (),
                scopes=scopes or (),
                excludes=exclude or (),
                recommend=recommend,
            )
        if action == "complete":
            return _service().complete_login(authorization_id=authorization_id)
        if action == "logout":
            return _service().logout()
        if action == "check":
            return _service().check_scopes(scopes or ())
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": f"unsupported auth action: {action}"}


def handle_run(
    command: str,
    arguments: list[str] | None = None,
    identity: str = "auto",
    dry_run: bool = False,
    **_: Any,
) -> dict[str, Any]:
    return _service().run_business_command(
        command=command,
        arguments=arguments or (),
        identity=identity,
        dry_run=dry_run,
    )


def handle_skill(
    action: str,
    name: str = "",
    path: str = "",
    **_: Any,
) -> dict[str, Any]:
    try:
        if action == "list":
            return _service().list_skills(path=name or path)
        if action == "read":
            if not name:
                return {"ok": False, "error": "name is required for action=read"}
            return _service().read_skill(name, path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": f"unsupported skill action: {action}"}


TOOLS = (
    ("lark_cli_status", LARK_CLI_STATUS_SCHEMA, handle_status, "🪪"),
    ("lark_cli_auth", LARK_CLI_AUTH_SCHEMA, handle_auth, "🔐"),
    ("lark_cli_run", LARK_CLI_RUN_SCHEMA, handle_run, "🪽"),
    ("lark_cli_skill", LARK_CLI_SKILL_SCHEMA, handle_skill, "📚"),
)
