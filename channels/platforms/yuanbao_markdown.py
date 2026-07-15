"""Yuanbao Markdown processing utilities."""

from __future__ import annotations

import re
from typing import Callable, Optional

class MarkdownProcessor:
    """Encapsulates all Markdown-related utilities for the Yuanbao platform.

    Provides static methods for:
    - Fence detection and streaming merge
    - Table row detection and sanitization
    - Paragraph-boundary splitting
    - Atomic-block extraction and chunk splitting
    - Outer markdown fence stripping
    - Markdown hint prompt generation
    """

    # -- Fence detection ---------------------------------------------------

    @staticmethod
    def has_unclosed_fence(text: str) -> bool:
        """
        Detect whether the text has unclosed code block fences.

        Scan line by line, toggling in/out state when encountering a line starting with ```.
        An odd number of toggles indicates an unclosed fence.

        Args:
            text: Markdown text to check

        Returns:
            Returns True if the text ends with an unclosed fence, otherwise False
        """
        in_fence = False
        for line in text.split('\n'):
            if line.startswith('```'):
                in_fence = not in_fence
        return in_fence

    # -- Table detection ---------------------------------------------------

    @staticmethod
    def ends_with_table_row(text: str) -> bool:
        """
        Detect whether the text ends with a table row (last non-empty line starts and ends with |).

        Args:
            text: Text to check

        Returns:
            Returns True if the last non-empty line is a table row
        """
        trimmed = text.rstrip()
        if not trimmed:
            return False
        last_line = trimmed.split('\n')[-1].strip()
        return last_line.startswith('|') and last_line.endswith('|')

    # -- Paragraph boundary splitting --------------------------------------

    @staticmethod
    def split_at_paragraph_boundary(
        text: str,
        max_chars: int,
        len_fn: Optional[Callable[[str], int]] = None,
    ) -> tuple[str, str]:
        """
        Find the nearest paragraph boundary split point within max_chars, return (head, tail).

        Split priority:
        1. Blank line (paragraph boundary)
        2. Newline after period/question mark/exclamation mark (Chinese and English)
        3. Last newline
        4. Force split at max_chars

        Args:
            text: Text to split
            max_chars: Maximum character count limit
            len_fn: Optional custom length function (e.g. UTF-16 length); defaults to built-in len

        Returns:
            (head, tail) tuple, head is the front part, tail is the back part, satisfying head + tail == text
        """
        _len = len_fn or len
        if _len(text) <= max_chars:
            return text, ''

        # Build a character-index window that fits within max_chars.
        # When len_fn != len we cannot simply slice [:max_chars], so we
        # binary-search for the largest prefix that fits.
        if _len is len:
            window = text[:max_chars]
        else:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _len(text[:mid]) <= max_chars:
                    lo = mid
                else:
                    hi = mid - 1
            window = text[:lo]

        # 1. Prefer the last blank line (\n\n) as paragraph boundary
        pos = window.rfind('\n\n')
        if pos > 0:
            return text[:pos + 2], text[pos + 2:]

        # 2. Then find the last newline after a sentence-ending punctuation
        sentence_end_re = re.compile(r'[。！？.!?]\n')
        best_pos = -1
        for m in sentence_end_re.finditer(window):
            best_pos = m.end()
        if best_pos > 0:
            return text[:best_pos], text[best_pos:]

        # 3. Fallback: find the last newline
        pos = window.rfind('\n')
        if pos > 0:
            return text[:pos + 1], text[pos + 1:]

        # 4. No valid split point found, force split at window boundary
        cut = len(window)
        return text[:cut], text[cut:]

    # -- Atomic block helpers (private) ------------------------------------

    @staticmethod
    def is_fence_atom(text: str) -> bool:
        """Determine whether an atomic block is a code block (starts with ```)."""
        return text.lstrip().startswith('```')

    @staticmethod
    def is_table_atom(text: str) -> bool:
        """Determine whether an atomic block is a table (first line starts with |)."""
        first_line = text.split('\n')[0].strip()
        return first_line.startswith('|') and first_line.endswith('|')

    @staticmethod
    def split_into_atoms(text: str) -> list[str]:
        """
        Split text into a list of "atomic blocks", each being an indivisible logical unit:

        - Code block (fence): from opening ``` to closing ``` (including fence lines)
        - Table: consecutive |...| lines forming a whole segment
        - Normal paragraph: plain text segments separated by blank lines

        Blank lines serve as separators and are not included in any atomic block.

        Args:
            text: Markdown text to split

        Returns:
            List of atomic block strings (all non-empty)
        """
        lines = text.split('\n')
        atoms: list[str] = []

        current_lines: list[str] = []
        in_fence = False

        def _is_table_line(line: str) -> bool:
            stripped = line.strip()
            return stripped.startswith('|') and stripped.endswith('|')

        def _flush_current() -> None:
            if current_lines:
                atom = '\n'.join(current_lines)
                if atom.strip():
                    atoms.append(atom)
                current_lines.clear()

        for line in lines:
            if in_fence:
                current_lines.append(line)
                if line.startswith('```') and len(current_lines) > 1:
                    in_fence = False
                    _flush_current()
            elif line.startswith('```'):
                _flush_current()
                in_fence = True
                current_lines.append(line)
            elif _is_table_line(line):
                if current_lines and not _is_table_line(current_lines[-1]):
                    _flush_current()
                current_lines.append(line)
            elif line.strip() == '':
                _flush_current()
            else:
                if current_lines and _is_table_line(current_lines[-1]):
                    _flush_current()
                current_lines.append(line)

        _flush_current()

        return atoms

    # -- Core: chunk splitting ---------------------------------------------

    @classmethod
    def chunk_markdown_text(
        cls,
        text: str,
        max_chars: int = 4000,
        len_fn: Optional[Callable[[str], int]] = None,
    ) -> list[str]:
        """
        Split Markdown text into multiple chunks by max_chars.

        Guarantees:
        - Each chunk <= max_chars characters (unless a single code block/table itself exceeds the limit)
        - Code blocks (```...```) are not split in the middle
        - Table rows are not split in the middle (tables output as atomic blocks)
        - Split at paragraph boundaries (blank lines, after periods, etc.)
        - Small trailing/leading chunks are merged with neighbours when possible

        Args:
            text: Markdown text to split
            max_chars: Max characters per chunk, default 4000
            len_fn: Optional custom length function (e.g. UTF-16 length); defaults to built-in len

        Returns:
            List of text chunks after splitting (non-empty)
        """
        _len = len_fn or len

        if not text:
            return []

        if _len(text) <= max_chars:
            return [text]

        # Phase 1: Extract atomic blocks
        atoms = cls.split_into_atoms(text)

        # Phase 2: Greedy merge
        chunks: list[str] = []
        indivisible_set: set[int] = set()
        current_parts: list[str] = []
        current_len = 0

        def _flush_parts() -> None:
            if current_parts:
                chunks.append('\n\n'.join(current_parts))

        for atom in atoms:
            atom_len = _len(atom)
            sep_len = 2 if current_parts else 0
            projected_len = current_len + sep_len + atom_len

            if projected_len > max_chars and current_parts:
                _flush_parts()
                current_parts = []
                current_len = 0
                sep_len = 0

            if (not current_parts
                    and atom_len > max_chars
                    and (cls.is_fence_atom(atom) or cls.is_table_atom(atom))):
                indivisible_set.add(len(chunks))
                chunks.append(atom)
                continue

            current_parts.append(atom)
            current_len += sep_len + atom_len

        _flush_parts()

        # Phase 3: Post-processing — split still-oversized chunks at paragraph boundaries
        result: list[str] = []
        for idx, chunk in enumerate(chunks):
            if _len(chunk) <= max_chars:
                result.append(chunk)
                continue

            if idx in indivisible_set:
                result.append(chunk)
                continue

            if cls.has_unclosed_fence(chunk):
                result.append(chunk)
                continue

            remaining = chunk
            while _len(remaining) > max_chars:
                head, remaining = cls.split_at_paragraph_boundary(
                    remaining, max_chars, len_fn=len_fn,
                )
                if not head:
                    head, remaining = remaining[:max_chars], remaining[max_chars:]
                if head:
                    result.append(head)
            if remaining:
                result.append(remaining)

        # Phase 4: Merge small trailing/leading chunks with neighbours
        if len(result) > 1:
            merged: list[str] = [result[0]]
            for chunk in result[1:]:
                prev = merged[-1]
                combined = prev + '\n\n' + chunk
                if _len(combined) <= max_chars:
                    merged[-1] = combined
                else:
                    merged.append(chunk)
            result = merged

        return [c for c in result if c]

    # -- Block separator inference -----------------------------------------

    @classmethod
    def infer_block_separator(cls, prev_chunk: str, next_chunk: str) -> str:
        """
        Infer the separator to use between two split chunks.

        Rules (aligned with TS markdown-stream.ts):
        - Previous chunk ends with code fence or next chunk starts with fence → single newline '\\n'
        - Previous chunk ends with table row and next chunk starts with table row → single newline '\\n' (continued table)
        - Otherwise → double newline '\\n\\n' (paragraph separator)

        Args:
            prev_chunk: Previous chunk
            next_chunk: Next chunk

        Returns:
            '\\n' or '\\n\\n'
        """
        prev_trimmed = prev_chunk.rstrip()
        next_trimmed = next_chunk.lstrip()

        # Previous chunk ends with fence or next chunk starts with fence
        if prev_trimmed.endswith('```') or next_trimmed.startswith('```'):
            return '\n'

        # Table continuation
        if cls.ends_with_table_row(prev_chunk):
            first_line = next_trimmed.split('\n')[0].strip() if next_trimmed else ''
            if first_line.startswith('|') and first_line.endswith('|'):
                return '\n'

        return '\n\n'

    # -- Streaming fence merge ---------------------------------------------

    @classmethod
    def merge_block_streaming_fences(cls, chunks: list[str]) -> list[str]:
        """
        Stream-aware fence-conscious chunk merging.

        When streaming output produces multiple chunks truncated in the middle of a fence,
        attempt to merge adjacent chunks to complete the fence.

        Rules:
        - If chunk i has an unclosed fence and chunk i+1 starts with ```,
            merge i+1 into i (until the fence is closed or no more chunks).
        - Use infer_block_separator to infer the separator during merging.

        Args:
            chunks: Original chunk list

        Returns:
            Merged chunk list (length <= original length)
        """
        if not chunks:
            return []

        result: list[str] = []
        i = 0
        while i < len(chunks):
            current = chunks[i]
            # If current chunk has unclosed fence, try merging subsequent chunks
            while cls.has_unclosed_fence(current) and i + 1 < len(chunks):
                sep = cls.infer_block_separator(current, chunks[i + 1])
                current = current + sep + chunks[i + 1]
                i += 1
            result.append(current)
            i += 1

        return result

    # -- Outer fence stripping ---------------------------------------------

    @staticmethod
    def strip_outer_markdown_fence(text: str) -> str:
        """
        Strip outer Markdown fence.

        When AI reply is entirely wrapped in ```markdown\\n...\\n```, remove the outer fence,
        keeping the content. Only strip when the first line is ```markdown (case-insensitive) and the last line is ```.

        Args:
            text: Text to process

        Returns:
            Text with outer fence stripped (returns original if no match)
        """
        if not text:
            return text

        lines = text.split('\n')
        if len(lines) < 3:
            return text

        first_line = lines[0].strip()
        last_line = lines[-1].strip()

        # First line must be ```markdown (optional language tag md/markdown)
        if not re.match(r'^```(?:markdown|md)?\s*$', first_line, re.IGNORECASE):
            return text

        # Last line must be plain ```
        if last_line != '```':
            return text

        # Strip first and last lines
        inner = '\n'.join(lines[1:-1])
        return inner

    # -- Table sanitization ------------------------------------------------

    @staticmethod
    def sanitize_markdown_table(text: str) -> str:
        """
        Table output sanitization.

        Handle common formatting issues in AI-generated Markdown tables:
        1. Remove extra whitespace before/after table rows
        2. Ensure separator rows (|---|---|) are correctly formatted
        3. Remove empty table rows

        Args:
            text: Markdown text containing tables

        Returns:
            Sanitized text
        """
        if '|' not in text:
            return text

        lines = text.split('\n')
        result_lines: list[str] = []

        for line in lines:
            stripped = line.strip()

            # Table row processing
            if stripped.startswith('|') and stripped.endswith('|'):
                # Separator row normalization: | --- | --- | → |---|---|
                if re.match(r'^\|[\s\-:]+(\|[\s\-:]+)+\|$', stripped):
                    cells = stripped.split('|')
                    normalized = '|'.join(
                        cell.strip() if cell.strip() else cell
                        for cell in cells
                    )
                    result_lines.append(normalized)
                elif stripped == '||' or stripped.replace('|', '').strip() == '':
                    # Empty table row → skip
                    continue
                else:
                    result_lines.append(stripped)
            else:
                result_lines.append(line)

        return '\n'.join(result_lines)

    # -- Markdown hint prompt ----------------------------------------------

    @staticmethod
    def markdown_hint_system_prompt() -> str:
        """
        Markdown rendering hint (appended to system prompt).

        Tell AI that Yuanbao platform supports Markdown rendering, including:
        - Code blocks (```lang)
        - Tables (| col | col |)
        - Bold/italic
        """
        return (
            "The current platform supports Markdown rendering. You can use the following formats:\n"
            "- Code blocks: ```language\\ncode\\n```\n"
            "- Tables: | col1 | col2 |\\n|---|---|\\n| val1 | val2 |\n"
            "- Bold: **text** / Italic: *text*\n"
            "Please use Markdown formatting when appropriate to improve readability."
        )

