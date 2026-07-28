"""Matrix outbound text and HTML formatting helpers."""

from __future__ import annotations

import re
from html import escape as _html_escape
from typing import Any, Dict, Optional, Set

from channels.platforms.matrix_support import _OUTBOUND_MENTION_RE


class MatrixFormattingMixin:
    def _build_text_message_content(self, text: str, msgtype: str = "m.text") -> Dict[str, Any]:
        """Build Matrix text content with HTML and outbound mention metadata."""
        msg_content: Dict[str, Any] = {"msgtype": msgtype, "body": text}
        mention_user_ids = self._extract_outbound_mentions(text)
        if mention_user_ids:
            msg_content["m.mentions"] = {"user_ids": mention_user_ids}

        html_source = self._inject_outbound_mention_links(text)
        html = self._markdown_to_html(html_source)
        if html and html != text:
            msg_content["format"] = "org.matrix.custom.html"
            msg_content["formatted_body"] = html

        return msg_content

    def _extract_outbound_mentions(self, text: str) -> list[str]:
        """Return unique Matrix user IDs mentioned in outbound text."""
        protected, _ = self._protect_outbound_mention_regions(text)
        seen: Set[str] = set()
        mentions: list[str] = []
        for match in _OUTBOUND_MENTION_RE.finditer(protected):
            user_id = match.group(1)
            if user_id not in seen:
                seen.add(user_id)
                mentions.append(user_id)
        return mentions

    def _inject_outbound_mention_links(self, text: str) -> str:
        """Wrap outbound Matrix mentions in markdown links outside code spans."""
        if not text:
            return text

        protected, placeholders = self._protect_outbound_mention_regions(text)

        linked = _OUTBOUND_MENTION_RE.sub(
            lambda match: f"[{match.group(1)}](https://matrix.to/#/{match.group(1)})",
            protected,
        )

        for idx, original in enumerate(placeholders):
            linked = linked.replace(f"\x00MENTION_PROTECTED{idx}\x00", original)

        return linked

    def _protect_outbound_mention_regions(self, text: str) -> tuple[str, list[str]]:
        """Protect markdown regions where outbound mentions should stay literal."""
        placeholders: list[str] = []

        def _protect(fragment: str) -> str:
            idx = len(placeholders)
            placeholders.append(fragment)
            return f"\x00MENTION_PROTECTED{idx}\x00"

        protected = re.sub(
            r"```[\s\S]*?```",
            lambda match: _protect(match.group(0)),
            text or "",
        )
        protected = re.sub(
            r"`[^`\n]+`",
            lambda match: _protect(match.group(0)),
            protected,
        )
        protected = re.sub(
            r"\[[^\]]+\]\([^)]+\)",
            lambda match: _protect(match.group(0)),
            protected,
        )

        return protected, placeholders

    def _is_bot_mentioned(
        self,
        body: str,
        formatted_body: Optional[str] = None,
        mention_user_ids: Optional[list] = None,
    ) -> bool:
        """Return True if the bot is mentioned in the message.

        Per MSC3952, ``m.mentions.user_ids`` is the authoritative mention
        signal in the Matrix spec.  When the sender's client populates that
        field with the bot's user-id, we trust it — even when the visible
        body text does not contain an explicit ``@bot`` string (some clients
        only render mention "pills" in ``formatted_body`` or use display
        names).
        """
        # m.mentions.user_ids — authoritative per MSC3952 / Matrix v1.7.
        if mention_user_ids and self._user_id and self._user_id in mention_user_ids:
            return True
        if not body and not formatted_body:
            return False
        if self._user_id and self._user_id in body:
            return True
        if self._user_id and ":" in self._user_id:
            localpart = self._user_id.split(":")[0].lstrip("@")
            if localpart and re.search(
                r"\b" + re.escape(localpart) + r"\b", body, re.IGNORECASE
            ):
                return True
        if formatted_body and self._user_id:
            if f"matrix.to/#/{self._user_id}" in formatted_body:
                return True
        return False

    def _strip_mention(self, body: str) -> str:
        """Remove explicit bot mentions from message body.

        Important: only strip explicit mention tokens (``@user:server`` or
        ``@localpart``). Do NOT strip bare words matching the bot localpart,
        otherwise normal phrases like "Hermes Agent" become "Agent".
        """
        if not body:
            return ""

        # Strip explicit full MXID mentions.
        if self._user_id:
            body = body.replace(self._user_id, "")

        # Strip explicit @localpart mentions only (not bare localpart words).
        if self._user_id and ":" in self._user_id:
            localpart = self._user_id.split(":")[0].lstrip("@")
            if localpart:
                body = re.sub(
                    r'(?<![\w])@' + re.escape(localpart) + r'\b',
                    '',
                    body,
                    flags=re.IGNORECASE,
                )

        # Normalize spacing after mention removal.
        body = re.sub(r'[ \t]{2,}', ' ', body)
        body = re.sub(r'\s+([,.;:!?])', r'\1', body)
        return body.strip()

    async def _get_display_name(self, room_id: str, user_id: str) -> str:
        """Get a user's display name in a room, falling back to user_id."""
        state_store = (
            getattr(self._client, "state_store", None) if self._client else None
        )
        if state_store:
            try:
                member = await state_store.get_member(room_id, user_id)
                if member and getattr(member, "displayname", None):
                    return member.displayname
            except Exception:
                pass
        # Strip the @...:server format to just the localpart.
        if user_id.startswith("@") and ":" in user_id:
            return user_id[1:].split(":")[0]
        return user_id

    def _mxc_to_http(self, mxc_url: str) -> str:
        """Convert mxc://server/media_id to an HTTP download URL."""
        if not mxc_url.startswith("mxc://"):
            return mxc_url
        parts = mxc_url[6:]  # strip mxc://
        return f"{self._homeserver}/_matrix/client/v1/media/download/{parts}"

    def _markdown_to_html(self, text: str) -> str:
        """Convert Markdown to Matrix-compatible HTML (org.matrix.custom.html).

        Uses the ``markdown`` library when available (installed with the
        ``matrix`` extra).  Falls back to a comprehensive regex converter
        that handles fenced code blocks, inline code, headers, bold,
        italic, strikethrough, links, blockquotes, lists, and horizontal
        rules — everything the Matrix HTML spec allows.
        """
        try:
            import markdown as _md

            md = _md.Markdown(
                extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
            )
            if "html_block" in md.preprocessors:
                md.preprocessors.deregister("html_block")

            html = md.convert(text)
            md.reset()

            if html.count("<p>") == 1:
                html = html.replace("<p>", "").replace("</p>", "")
            return html
        except ImportError:
            pass

        return self._markdown_to_html_fallback(text)

    # ------------------------------------------------------------------
    # Regex-based Markdown -> HTML (no extra dependencies)
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_link_url(url: str) -> str:
        """Sanitize a URL for use in an href attribute."""
        stripped = url.strip()
        scheme = stripped.split(":", 1)[0].lower().strip() if ":" in stripped else ""
        if scheme in {"javascript", "data", "vbscript"}:
            return ""
        return stripped.replace('"', "&quot;")

    @staticmethod
    def _markdown_to_html_fallback(text: str) -> str:
        """Comprehensive regex Markdown-to-HTML for Matrix."""
        placeholders: list = []

        def _protect_html(html_fragment: str) -> str:
            idx = len(placeholders)
            placeholders.append(html_fragment)
            return f"\x00PROTECTED{idx}\x00"

        # Fenced code blocks: ```lang\n...\n```
        result = re.sub(
            r"```(\w*)\n(.*?)```",
            lambda m: _protect_html(
                f'<pre><code class="language-{_html_escape(m.group(1))}">'
                f"{_html_escape(m.group(2))}</code></pre>"
                if m.group(1)
                else f"<pre><code>{_html_escape(m.group(2))}</code></pre>"
            ),
            text,
            flags=re.DOTALL,
        )

        # Inline code: `code`
        result = re.sub(
            r"`([^`\n]+)`",
            lambda m: _protect_html(f"<code>{_html_escape(m.group(1))}</code>"),
            result,
        )

        # Extract and protect markdown links before escaping.
        result = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            lambda m: _protect_html(
                '<a href="{}">{}</a>'.format(
                    MatrixFormattingMixin._sanitize_link_url(m.group(2)),
                    _html_escape(m.group(1)),
                )
            ),
            result,
        )

        # HTML-escape remaining text.
        parts = re.split(r"(\x00PROTECTED\d+\x00)", result)
        for idx, part in enumerate(parts):
            if not part.startswith("\x00PROTECTED"):
                parts[idx] = _html_escape(part)
        result = "".join(parts)

        # Block-level transforms (line-oriented).
        lines = result.split("\n")
        out_lines: list = []
        i = 0
        while i < len(lines):
            line = lines[i]

            # Horizontal rule
            if re.match(r"^[\s]*([-*_])\s*\1\s*\1[\s\-*_]*$", line):
                out_lines.append("<hr>")
                i += 1
                continue

            # Headers
            hdr = re.match(r"^(#{1,6})\s+(.+)$", line)
            if hdr:
                level = len(hdr.group(1))
                out_lines.append(f"<h{level}>{hdr.group(2).strip()}</h{level}>")
                i += 1
                continue

            # Blockquote
            if (
                line.startswith("&gt; ")
                or line == "&gt;"
                or line.startswith("> ")
                or line == ">"
            ):
                bq_lines = []
                while i < len(lines) and (
                    lines[i].startswith("&gt; ")
                    or lines[i] == "&gt;"
                    or lines[i].startswith("> ")
                    or lines[i] == ">"
                ):
                    ln = lines[i]
                    if ln.startswith("&gt; "):
                        bq_lines.append(ln[5:])
                    elif ln.startswith("> "):
                        bq_lines.append(ln[2:])
                    else:
                        bq_lines.append("")
                    i += 1
                out_lines.append(f"<blockquote>{'<br>'.join(bq_lines)}</blockquote>")
                continue

            # Unordered list
            ul_match = re.match(r"^[\s]*[-*+]\s+(.+)$", line)
            if ul_match:
                items = []
                while i < len(lines) and re.match(r"^[\s]*[-*+]\s+(.+)$", lines[i]):
                    items.append(re.match(r"^[\s]*[-*+]\s+(.+)$", lines[i]).group(1))
                    i += 1
                li = "".join(f"<li>{item}</li>" for item in items)
                out_lines.append(f"<ul>{li}</ul>")
                continue

            # Ordered list
            ol_match = re.match(r"^[\s]*\d+[.)]\s+(.+)$", line)
            if ol_match:
                items = []
                while i < len(lines) and re.match(r"^[\s]*\d+[.)]\s+(.+)$", lines[i]):
                    items.append(re.match(r"^[\s]*\d+[.)]\s+(.+)$", lines[i]).group(1))
                    i += 1
                li = "".join(f"<li>{item}</li>" for item in items)
                out_lines.append(f"<ol>{li}</ol>")
                continue

            out_lines.append(line)
            i += 1

        result = "\n".join(out_lines)

        # Inline transforms.
        result = re.sub(
            r"\*\*(.+?)\*\*", r"<strong>\1</strong>", result, flags=re.DOTALL
        )
        result = re.sub(r"__(.+?)__", r"<strong>\1</strong>", result, flags=re.DOTALL)
        result = re.sub(r"\*(.+?)\*", r"<em>\1</em>", result, flags=re.DOTALL)
        result = re.sub(
            r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", result, flags=re.DOTALL
        )
        result = re.sub(r"~~(.+?)~~", r"<del>\1</del>", result, flags=re.DOTALL)
        result = re.sub(r"\n", "<br>\n", result)
        result = re.sub(
            r"<br>\n(</?(?:pre|blockquote|h[1-6]|ul|ol|li|hr))", r"\n\1", result
        )
        result = re.sub(r"(</(?:pre|blockquote|h[1-6]|ul|ol|li)>)<br>", r"\1", result)

        # Restore protected regions.
        for idx, original in enumerate(placeholders):
            result = result.replace(f"\x00PROTECTED{idx}\x00", original)

        return result
