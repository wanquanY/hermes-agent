# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services.completions import (
    details_completions,
    path_completion_items,
)

_server = bind_server_globals(globals())


# ── Methods: complete ─────────────────────────────────────────────────


def _gateway_extra_slash_items(text_lower: str, existing: list[dict]) -> list[dict]:
    extras = [
        {
            "text": "/compact",
            "display": "/compact",
            "meta": "Toggle compact display mode",
        },
        {
            "text": "/details",
            "display": "/details",
            "meta": "Control agent detail visibility",
        },
        {
            "text": "/logs",
            "display": "/logs",
            "meta": "Show recent gateway log lines",
        },
        {
            "text": "/mouse",
            "display": "/mouse",
            "meta": "Toggle mouse/wheel tracking [on|off|toggle]",
        },
    ]
    for extra in extras:
        if extra["text"].startswith(text_lower) and not any(
            item["text"] == extra["text"] for item in existing
        ):
            existing.append(extra)
    return existing


def _fallback_slash_items(text: str) -> list[dict]:
    try:
        from hermes_cli.commands import COMMANDS
    except Exception:
        COMMANDS = {}
    word = text[1:].lower()
    items: list[dict] = []
    for cmd, desc in sorted((COMMANDS or {}).items()):
        cmd_name = str(cmd or "").lstrip("/")
        if cmd_name.lower().startswith(word):
            items.append({"text": cmd_name, "display": cmd, "meta": str(desc or "")})
        if len(items) >= 30:
            break
    return _gateway_extra_slash_items(text.lower(), items)

@method("complete.path")
def _(rid, params: dict) -> dict:
    try:
        return _ok(
            rid,
            {"items": path_completion_items(params.get("word", ""), cwd=os.getcwd())},
        )
    except Exception as e:
        return _err(rid, 5021, str(e))


@method("complete.slash")
def _(rid, params: dict) -> dict:
    text = params.get("text", "")
    if not text.startswith("/"):
        return _ok(rid, {"items": []})

    try:
        from hermes_cli.commands import SlashCommandCompleter

        from agent.skill_commands import get_skill_commands

        completer = SlashCommandCompleter(
            skill_commands_provider=lambda: get_skill_commands()
        )
        try:
            from prompt_toolkit.document import Document
            from prompt_toolkit.formatted_text import to_plain_text
        except ImportError:
            details_items = details_completions(text)
            if details_items is not None:
                return _ok(
                    rid,
                    {
                        "items": details_items,
                        "replace_from": text.rfind(" ") + 1 if " " in text else len(text),
                    },
                )
            return _ok(
                rid,
                {
                    "items": _fallback_slash_items(text),
                    "replace_from": text.rfind(" ") + 1 if " " in text else 1,
                },
            )
        doc = Document(text, len(text))
        items = [
            {
                "text": c.text,
                "display": c.display or c.text,
                "meta": to_plain_text(c.display_meta) if c.display_meta else "",
            }
            for c in completer.get_completions(doc, None)
        ][:30]
        text_lower = text.lower()
        _gateway_extra_slash_items(text_lower, items)

        details_items = details_completions(text)
        if details_items is not None:
            return _ok(
                rid,
                {
                    "items": details_items,
                    "replace_from": text.rfind(" ") + 1 if " " in text else len(text),
                },
            )

        return _ok(
            rid,
            {"items": items, "replace_from": text.rfind(" ") + 1 if " " in text else 1},
        )
    except Exception as e:
        return _err(rid, 5020, str(e))
