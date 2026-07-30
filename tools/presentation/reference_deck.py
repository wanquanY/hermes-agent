"""Safe OOXML inspection for reference-driven editable presentations."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


MAX_REFERENCE_DECK_BYTES = 100 * 1024 * 1024
MAX_REFERENCE_ENTRY_BYTES = 8 * 1024 * 1024
MAX_REFERENCE_ENTRIES = 4_000

DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
RELATIONSHIP_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
NS = {
    "a": DRAWING_NS,
    "p": PRESENTATION_NS,
    "r": RELATIONSHIP_NS,
}


@dataclass(frozen=True)
class ReferenceDeckStyle:
    path: str
    page_size: str
    width_emu: int
    height_emu: int
    palette: dict[str, str]
    font_family: str
    cjk_font_family: str
    layout_names: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "page_size": self.page_size,
            "width_emu": self.width_emu,
            "height_emu": self.height_emu,
            "palette": dict(self.palette),
            "font_family": self.font_family,
            "cjk_font_family": self.cjk_font_family,
            "layout_names": list(self.layout_names),
        }


def _safe_entry(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ValueError(f"reference deck is missing required OOXML part: {name}") from exc
    if info.file_size > MAX_REFERENCE_ENTRY_BYTES:
        raise ValueError(f"reference deck OOXML part is too large: {name}")
    return archive.read(info)


def _hex_color(node: ElementTree.Element | None) -> str | None:
    if node is None:
        return None
    for local_name in ("srgbClr", "sysClr"):
        child = node.find(f"a:{local_name}", NS)
        if child is None:
            continue
        value = child.get("val") if local_name == "srgbClr" else child.get("lastClr")
        normalized = str(value or "").strip().upper()
        if len(normalized) == 6 and all(character in "0123456789ABCDEF" for character in normalized):
            return normalized
    return None


def _theme_values(theme_xml: bytes) -> tuple[dict[str, str], str, str]:
    root = ElementTree.fromstring(theme_xml)
    color_scheme = root.find(".//a:themeElements/a:clrScheme", NS)
    colors: dict[str, str] = {}
    if color_scheme is not None:
        for child in color_scheme:
            key = child.tag.rsplit("}", 1)[-1]
            color = _hex_color(child)
            if color:
                colors[key] = color

    font_scheme = root.find(".//a:themeElements/a:fontScheme", NS)
    major_latin = (
        font_scheme.find("a:majorFont/a:latin", NS)
        if font_scheme is not None
        else None
    )
    minor_latin = (
        font_scheme.find("a:minorFont/a:latin", NS)
        if font_scheme is not None
        else None
    )
    major_east_asia = (
        font_scheme.find("a:majorFont/a:ea", NS)
        if font_scheme is not None
        else None
    )
    minor_east_asia = (
        font_scheme.find("a:minorFont/a:ea", NS)
        if font_scheme is not None
        else None
    )
    font_family = str(
        (minor_latin.get("typeface") if minor_latin is not None else "")
        or (major_latin.get("typeface") if major_latin is not None else "")
        or "Aptos"
    ).strip()
    cjk_font_family = str(
        (minor_east_asia.get("typeface") if minor_east_asia is not None else "")
        or (major_east_asia.get("typeface") if major_east_asia is not None else "")
        or "PingFang SC"
    ).strip()
    return colors, font_family, cjk_font_family


def _page_size(presentation_xml: bytes) -> tuple[str, int, int]:
    root = ElementTree.fromstring(presentation_xml)
    size = root.find("p:sldSz", NS)
    if size is None:
        return "16:9", 12_192_000, 6_858_000
    width = int(size.get("cx") or 12_192_000)
    height = int(size.get("cy") or 6_858_000)
    ratio = width / max(height, 1)
    candidates = {
        "16:9": 16 / 9,
        "4:3": 4 / 3,
        "1:1": 1.0,
    }
    page_size = min(candidates, key=lambda candidate: abs(candidates[candidate] - ratio))
    return page_size, width, height


def _layout_names(archive: zipfile.ZipFile) -> tuple[str, ...]:
    names: list[str] = []
    for info in sorted(
        (
            item
            for item in archive.infolist()
            if item.filename.startswith("ppt/slideLayouts/slideLayout")
            and item.filename.endswith(".xml")
        ),
        key=lambda item: item.filename,
    ):
        if info.file_size > MAX_REFERENCE_ENTRY_BYTES:
            continue
        root = ElementTree.fromstring(archive.read(info))
        common = root.find("p:cSld", NS)
        name = str(common.get("name") if common is not None else "").strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _semantic_palette(colors: dict[str, str]) -> dict[str, str]:
    return {
        "background": colors.get("lt1", "FFFFFF"),
        "surface": colors.get("lt2", colors.get("lt1", "FFFFFF")),
        "text": colors.get("dk1", "171717"),
        "muted": colors.get("dk2", "667085"),
        "accent": colors.get("accent1", "2E63FF"),
        "accent2": colors.get("accent2", "0F8F79"),
        "danger": colors.get("accent3", "C2412D"),
        "border": colors.get("accent4", colors.get("lt2", "D9DBE1")),
        "darkBackground": colors.get("dk1", "171717"),
        "darkSurface": colors.get("dk2", "242424"),
        "darkText": colors.get("lt1", "F7F7F5"),
    }


def inspect_reference_deck(path_value: str | Path) -> ReferenceDeckStyle:
    path = Path(path_value).resolve()
    if not path.is_file() or path.suffix.lower() != ".pptx":
        raise ValueError("reference deck must be an existing .pptx file")
    if path.stat().st_size > MAX_REFERENCE_DECK_BYTES:
        raise ValueError(
            f"reference deck exceeds {MAX_REFERENCE_DECK_BYTES} bytes"
        )
    try:
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > MAX_REFERENCE_ENTRIES:
                raise ValueError("reference deck contains too many OOXML parts")
            presentation_xml = _safe_entry(archive, "ppt/presentation.xml")
            theme_names = sorted(
                item.filename
                for item in archive.infolist()
                if item.filename.startswith("ppt/theme/theme")
                and item.filename.endswith(".xml")
            )
            if not theme_names:
                raise ValueError("reference deck does not contain an Office theme")
            theme_xml = _safe_entry(archive, theme_names[0])
            colors, font_family, cjk_font_family = _theme_values(theme_xml)
            page_size, width, height = _page_size(presentation_xml)
            return ReferenceDeckStyle(
                path=str(path),
                page_size=page_size,
                width_emu=width,
                height_emu=height,
                palette=_semantic_palette(colors),
                font_family=font_family,
                cjk_font_family=cjk_font_family,
                layout_names=_layout_names(archive),
            )
    except zipfile.BadZipFile as exc:
        raise ValueError("reference deck is not a valid PPTX package") from exc


def apply_reference_style(
    slidespec: dict[str, Any],
    style: ReferenceDeckStyle,
) -> dict[str, Any]:
    result = dict(slidespec)
    explicit_theme = (
        dict(result.get("theme"))
        if isinstance(result.get("theme"), dict)
        else {}
    )
    explicit_palette = (
        dict(explicit_theme.get("palette"))
        if isinstance(explicit_theme.get("palette"), dict)
        else {}
    )
    result["page_size"] = str(result.get("page_size") or style.page_size)
    result["theme"] = {
        "preset": "dovie-grid",
        **explicit_theme,
        "font_family": str(
            explicit_theme.get("font_family") or style.font_family
        ),
        "cjk_font_family": str(
            explicit_theme.get("cjk_font_family") or style.cjk_font_family
        ),
        "palette": {
            **style.palette,
            **explicit_palette,
        },
    }
    return result


__all__ = [
    "ReferenceDeckStyle",
    "apply_reference_style",
    "inspect_reference_deck",
]
