"""Slack Block Kit text extraction helpers."""

from __future__ import annotations

import json


def _extract_text_from_slack_blocks(blocks: list) -> str:
    """Extract readable text from Slack Block Kit blocks, including quoted/forwarded content.

    Slack's modern WYSIWYG composer sends messages with a ``blocks`` array
    containing ``rich_text`` elements. When a user forwards or quotes another
    message, the quoted content appears as nested ``rich_text_quote`` elements
    that are *not* included in the plain ``text`` field of the event.

    This helper walks the rich-text tree recursively and returns readable lines,
    preserving quotes, list items, and preformatted blocks so the agent can see
    forwarded/quoted content instead of only the lossy plain-text field.
    """
    if not blocks:
        return ""

    parts: list[str] = []

    def _render_inline_elements(elements: list) -> str:
        """Render inline elements (text, link, channel, user, emoji, etc.)."""
        pieces: list[str] = []
        for el in elements:
            el_type = el.get("type", "")
            if el_type == "text":
                pieces.append(el.get("text", ""))
            elif el_type == "link":
                url = el.get("url", "")
                text = el.get("text", "") or url
                pieces.append(f"{text} ({url})")
            elif el_type == "channel":
                pieces.append(f"<#{el.get('channel_id', '')}>")
            elif el_type == "user":
                pieces.append(f"<@{el.get('user_id', '')}>")
            elif el_type == "usergroup":
                pieces.append(f"<!subteam^{el.get('usergroup_id', '')}>")
            elif el_type == "emoji":
                pieces.append(f":{el.get('name', '')}:")
            elif el_type == "broadcast":
                pieces.append(f"<!{el.get('range', 'here')}>")
            elif el_type == "date":
                pieces.append(el.get("fallback", ""))
        return "".join(pieces)

    def _append_line(text: str, quote_depth: int = 0, bullet: str = "") -> None:
        if not text or not text.strip():
            return
        prefix = ((">" * quote_depth) + " ") if quote_depth else ""
        parts.append(f"{prefix}{bullet}{text}".rstrip())

    def _walk_elements(elements: list, quote_depth: int = 0, bullet: str = "") -> None:
        for elem in elements:
            elem_type = elem.get("type", "")

            if elem_type == "rich_text_section":
                _append_line(
                    _render_inline_elements(elem.get("elements", [])),
                    quote_depth=quote_depth,
                    bullet=bullet,
                )
            elif elem_type == "rich_text_quote":
                _walk_elements(elem.get("elements", []), quote_depth=quote_depth + 1)
            elif elem_type == "rich_text_list":
                list_style = elem.get("style")
                for idx, item in enumerate(elem.get("elements", [])):
                    item_bullet = "• " if list_style == "bullet" else f"{idx + 1}. "
                    _walk_elements([item], quote_depth=quote_depth, bullet=item_bullet)
            elif elem_type == "rich_text_preformatted":
                code_lines: list[str] = []
                for child in elem.get("elements", []):
                    child_type = child.get("type", "")
                    if child_type == "rich_text_section":
                        rendered = _render_inline_elements(child.get("elements", []))
                    else:
                        rendered = _render_inline_elements([child])
                    if rendered:
                        code_lines.append(rendered)
                code_text = "\n".join(code_lines)
                if code_text:
                    lang = elem.get("language", "")
                    _append_line(f"```{lang}\n{code_text}\n```", quote_depth=quote_depth, bullet=bullet)
            else:
                rendered = _render_inline_elements([elem])
                if rendered:
                    _append_line(rendered, quote_depth=quote_depth, bullet=bullet)

    for block in blocks:
        if (block or {}).get("type") == "rich_text":
            _walk_elements(block.get("elements", []))

    return "\n".join(parts)


def _serialize_slack_blocks_for_agent(blocks: list, max_chars: int = 6000) -> str:
    """Return a compact, redacted JSON view of the current message's Block Kit payload."""
    if not blocks:
        return ""

    if all((block or {}).get("type") == "rich_text" for block in blocks):
        return ""

    scalar_allowlist = {
        "type",
        "block_id",
        "action_id",
        "style",
        "dispatch_action",
        "optional",
        "multiple",
        "emoji",
    }
    recursive_allowlist = {
        "text",
        "title",
        "description",
        "label",
        "placeholder",
        "accessory",
        "fields",
        "elements",
        "options",
        "option_groups",
        "confirm",
        "submit",
        "close",
        "hint",
    }

    def _sanitize(value):
        if isinstance(value, list):
            return [item for item in (_sanitize(v) for v in value) if item not in (None, {}, [], "")]
        if isinstance(value, dict):
            sanitized = {}
            for key, item in value.items():
                if key in scalar_allowlist:
                    sanitized[key] = item
                elif key in recursive_allowlist:
                    cleaned = _sanitize(item)
                    if cleaned not in (None, {}, [], ""):
                        sanitized[key] = cleaned
            return sanitized
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)

    try:
        payload = json.dumps(_sanitize(blocks), ensure_ascii=False, indent=2)
    except Exception:
        payload = repr(blocks)

    if len(payload) > max_chars:
        payload = payload[: max_chars - 18].rstrip() + "\n... [truncated]"

    return f"[Slack Block Kit payload for this message]\n```json\n{payload}\n```"

