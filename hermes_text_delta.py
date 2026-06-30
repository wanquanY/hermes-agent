from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class TextDeltaProjection:
    text: str
    diagnostic_code: str = ""
    diagnostic_fields: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str:
    return str(value or "").strip()


def utf16_code_unit_length(value: str) -> int:
    return len(str(value or "").encode("utf-16-le")) // 2


def _utf16_index_for_offset(value: str, offset: int) -> int | None:
    if offset < 0:
        return None
    if offset == 0:
        return 0
    units = 0
    for index, char in enumerate(value):
        units += utf16_code_unit_length(char)
        if units == offset:
            return index + 1
        if units > offset:
            return None
    return len(value) if units == offset else None


def _longest_suffix_prefix_overlap(existing_text: str, incoming_text: str) -> int:
    max_overlap = min(len(existing_text), len(incoming_text))
    for size in range(max_overlap, 0, -1):
        if existing_text.endswith(incoming_text[:size]):
            return size
    return 0


def project_text_delta(
    current_text: str,
    incoming_text: str,
    payload: Mapping[str, Any] | None = None,
) -> TextDeltaProjection:
    current = str(current_text or "")
    incoming = str(incoming_text or "")
    if not incoming:
        return TextDeltaProjection(current)

    payload = payload if isinstance(payload, Mapping) else {}
    mode = _text(payload.get("mode")).lower()
    if mode == "replace":
        return TextDeltaProjection(incoming)
    if mode in {"snapshot", "cumulative"}:
        if incoming == current:
            return TextDeltaProjection(current)
        if incoming.startswith(current):
            return TextDeltaProjection(incoming)
        if current.startswith(incoming):
            return TextDeltaProjection(current)
        return TextDeltaProjection(
            current + incoming,
            diagnostic_code="delta-offset-diverged",
            diagnostic_fields={
                "mode": mode,
                "content_length_utf16": utf16_code_unit_length(current),
            },
        )

    offset = payload.get("offset")
    if offset is not None:
        try:
            parsed_offset = int(offset)
        except (TypeError, ValueError):
            return TextDeltaProjection(
                current + incoming,
                diagnostic_code="delta-offset-invalid",
                diagnostic_fields={"offset": offset},
            )
        current_units = utf16_code_unit_length(current)
        incoming_units = utf16_code_unit_length(incoming)
        if parsed_offset == current_units:
            return TextDeltaProjection(current + incoming)
        start_index = _utf16_index_for_offset(current, parsed_offset)
        end_index = _utf16_index_for_offset(current, parsed_offset + incoming_units)
        if start_index is not None and end_index is not None:
            already_projected = current[start_index:end_index]
            if already_projected == incoming:
                return TextDeltaProjection(current)
            if incoming.startswith(current[start_index:]):
                return TextDeltaProjection(current[:start_index] + incoming)
        if parsed_offset == 0:
            if incoming.startswith(current):
                return TextDeltaProjection(incoming)
            if current.startswith(incoming):
                return TextDeltaProjection(current)
        return TextDeltaProjection(
            current + incoming,
            diagnostic_code="delta-offset-diverged",
            diagnostic_fields={
                "offset": parsed_offset,
                "content_length_utf16": current_units,
            },
        )

    if incoming == current:
        return TextDeltaProjection(current)
    if incoming.startswith(current):
        return TextDeltaProjection(incoming)
    overlap = _longest_suffix_prefix_overlap(current, incoming)
    if overlap > 0:
        return TextDeltaProjection(current + incoming[overlap:])
    return TextDeltaProjection(current + incoming)
