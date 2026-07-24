"""Dependency-free OOXML writer for image-backed PowerPoint decks."""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from xml.sax.saxutils import escape, quoteattr


PRESENTATION_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)

SLIDE_SIZES = {
    "16:9": (12_192_000, 6_858_000),
    "4:3": (9_144_000, 6_858_000),
    "1:1": (6_858_000, 6_858_000),
}

IMAGE_CONTENT_TYPES = {
    "bmp": "image/bmp",
    "gif": "image/gif",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "webp": "image/webp",
}


@dataclass(frozen=True)
class PresentationSlide:
    page_number: int
    title: str
    prompt: str
    image_path: str
    image_extension: str
    image_url: str | None = None


def _xml(value: object) -> str:
    return escape(str(value or ""), {'"': "&quot;", "'": "&apos;"})


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _content_types(slides: list[PresentationSlide]) -> str:
    image_defaults = "\n".join(
        f'  <Default Extension={quoteattr(extension)} ContentType={quoteattr(IMAGE_CONTENT_TYPES[extension])}/>'
        for extension in sorted({slide.image_extension for slide in slides})
    )
    slide_overrides = "\n".join(
        (
            f'  <Override PartName="/ppt/slides/slide{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        )
        for index in range(1, len(slides) + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
{image_defaults}
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
  <Override PartName="/dovie/presentation.json" ContentType="application/json"/>
{slide_overrides}
</Types>"""


def _root_relationships() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def _core_properties(title: str) -> str:
    created = _utc_timestamp()
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{_xml(title)}</dc:title>
  <dc:creator>Dovie Desktop</dc:creator>
  <cp:lastModifiedBy>Dovie Desktop</cp:lastModifiedBy>
  <cp:revision>1</cp:revision>
  <dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{created}</dcterms:modified>
</cp:coreProperties>"""


def _app_properties(slide_count: int, aspect_ratio: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Dovie Desktop</Application>
  <PresentationFormat>{_xml(aspect_ratio)}</PresentationFormat>
  <Slides>{slide_count}</Slides>
  <Notes>0</Notes>
  <HiddenSlides>0</HiddenSlides>
  <MMClips>0</MMClips>
  <ScaleCrop>false</ScaleCrop>
  <AppVersion>1.0</AppVersion>
</Properties>"""


def _presentation_xml(slide_count: int, width: int, height: int) -> str:
    slide_ids = "\n".join(
        f'    <p:sldId id="{255 + index}" r:id="rId{index + 1}"/>'
        for index in range(1, slide_count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 saveSubsetFonts="1" autoCompressPictures="0">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst>
{slide_ids}
  </p:sldIdLst>
  <p:sldSz cx="{width}" cy="{height}"/>
  <p:notesSz cx="6858000" cy="9144000"/>
  <p:defaultTextStyle><a:defPPr><a:defRPr lang="zh-CN"/></a:defPPr></p:defaultTextStyle>
</p:presentation>"""


def _presentation_relationships(slide_count: int) -> str:
    slide_rels = "\n".join(
        (
            f'  <Relationship Id="rId{index + 1}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
            f'Target="slides/slide{index}.xml"/>'
        )
        for index in range(1, slide_count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>
{slide_rels}
</Relationships>"""


def _slide_master() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
  </p:spTree></p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
  <p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles>
</p:sldMaster>"""


def _slide_master_relationships() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>"""


def _slide_layout() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 type="blank" preserve="1">
  <p:cSld name="Blank"><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
  </p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>"""


def _slide_layout_relationships() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>"""


def _theme() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Dovie">
  <a:themeElements>
    <a:clrScheme name="Dovie">
      <a:dk1><a:srgbClr val="111827"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>
      <a:dk2><a:srgbClr val="334155"/></a:dk2><a:lt2><a:srgbClr val="F8FAFC"/></a:lt2>
      <a:accent1><a:srgbClr val="2563EB"/></a:accent1><a:accent2><a:srgbClr val="7C3AED"/></a:accent2>
      <a:accent3><a:srgbClr val="0F766E"/></a:accent3><a:accent4><a:srgbClr val="EA580C"/></a:accent4>
      <a:accent5><a:srgbClr val="DB2777"/></a:accent5><a:accent6><a:srgbClr val="65A30D"/></a:accent6>
      <a:hlink><a:srgbClr val="2563EB"/></a:hlink><a:folHlink><a:srgbClr val="7C3AED"/></a:folHlink>
    </a:clrScheme>
    <a:fontScheme name="Dovie"><a:majorFont><a:latin typeface="Arial"/><a:ea typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Arial"/><a:ea typeface=""/></a:minorFont></a:fontScheme>
    <a:fmtScheme name="Dovie"><a:fillStyleLst/><a:lnStyleLst/><a:effectStyleLst/><a:bgFillStyleLst/></a:fmtScheme>
  </a:themeElements>
</a:theme>"""


def _slide_xml(slide: PresentationSlide, width: int, height: int) -> str:
    accessible_title = slide.title or f"Slide {slide.page_number}"
    description = slide.prompt or accessible_title
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld name={quoteattr(accessible_title)}><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    <p:pic>
      <p:nvPicPr><p:cNvPr id="2" name={quoteattr(f"Slide {slide.page_number} image")} descr={quoteattr(description)}/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>
      <p:blipFill><a:blip r:embed="rId2"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
      <p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{width}" cy="{height}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
    </p:pic>
    <p:sp>
      <p:nvSpPr><p:cNvPr id="3" name="Accessibility title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1" cy="1"/></a:xfrm><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>
      <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="zh-CN" sz="100"><a:solidFill><a:srgbClr val="FFFFFF"><a:alpha val="0"/></a:srgbClr></a:solidFill></a:rPr><a:t>{_xml(accessible_title)}</a:t></a:r><a:endParaRPr lang="zh-CN"/></a:p></p:txBody>
    </p:sp>
  </p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""


def _slide_relationships(image_name: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/{_xml(image_name)}"/>
</Relationships>"""


def _manifest(
    *,
    title: str,
    aspect_ratio: str,
    slides: list[PresentationSlide],
    metadata: dict[str, Any] | None,
) -> str:
    payload = {
        "schema_version": 1,
        "kind": "dovie.image_presentation",
        "title": title,
        "aspect_ratio": aspect_ratio,
        "slides": [
            {
                "index": index,
                "page_number": slide.page_number,
                "title": slide.title,
                "prompt": slide.prompt,
                "media_path": f"ppt/media/image{index + 1}.{slide.image_extension}",
                "image_url": slide.image_url,
            }
            for index, slide in enumerate(slides)
        ],
        "metadata": dict(metadata or {}),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _write_text(archive: zipfile.ZipFile, name: str, value: str) -> None:
    archive.writestr(name, value.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)


def _normalized_slides(slides: Iterable[PresentationSlide]) -> list[PresentationSlide]:
    normalized = list(slides)
    if not normalized:
        raise ValueError("at least one presentation slide is required")
    for slide in normalized:
        extension = str(slide.image_extension or "").strip().lower().lstrip(".")
        if extension not in IMAGE_CONTENT_TYPES:
            raise ValueError(f"unsupported presentation image extension: {extension or '(empty)'}")
        if not os.path.isfile(slide.image_path):
            raise ValueError(f"presentation image not found: {slide.image_path}")
        if extension != slide.image_extension:
            raise ValueError("presentation image extension must be normalized")
    return normalized


def write_image_presentation(
    output_path: str,
    *,
    title: str,
    aspect_ratio: str,
    slides: Iterable[PresentationSlide],
    metadata: dict[str, Any] | None = None,
    interrupted: Callable[[], bool] | None = None,
) -> str:
    """Write a valid, atomic PPTX whose slides are full-frame raster images."""

    normalized = _normalized_slides(slides)
    if aspect_ratio not in SLIDE_SIZES:
        raise ValueError(f"unsupported presentation aspect ratio: {aspect_ratio}")
    width, height = SLIDE_SIZES[aspect_ratio]
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{destination.stem}.",
        suffix=".pptx.tmp",
        dir=str(destination.parent),
    )
    os.close(descriptor)
    try:
        if interrupted and interrupted():
            raise InterruptedError("presentation generation interrupted")
        with zipfile.ZipFile(temporary_path, "w", allowZip64=True) as archive:
            _write_text(archive, "[Content_Types].xml", _content_types(normalized))
            _write_text(archive, "_rels/.rels", _root_relationships())
            _write_text(archive, "docProps/core.xml", _core_properties(title))
            _write_text(archive, "docProps/app.xml", _app_properties(len(normalized), aspect_ratio))
            _write_text(
                archive,
                "ppt/presentation.xml",
                _presentation_xml(len(normalized), width, height),
            )
            _write_text(
                archive,
                "ppt/_rels/presentation.xml.rels",
                _presentation_relationships(len(normalized)),
            )
            _write_text(archive, "ppt/slideMasters/slideMaster1.xml", _slide_master())
            _write_text(
                archive,
                "ppt/slideMasters/_rels/slideMaster1.xml.rels",
                _slide_master_relationships(),
            )
            _write_text(archive, "ppt/slideLayouts/slideLayout1.xml", _slide_layout())
            _write_text(
                archive,
                "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
                _slide_layout_relationships(),
            )
            _write_text(archive, "ppt/theme/theme1.xml", _theme())
            _write_text(
                archive,
                "dovie/presentation.json",
                _manifest(
                    title=title,
                    aspect_ratio=aspect_ratio,
                    slides=normalized,
                    metadata=metadata,
                ),
            )
            for index, slide in enumerate(normalized, start=1):
                if interrupted and interrupted():
                    raise InterruptedError("presentation generation interrupted")
                image_name = f"image{index}.{slide.image_extension}"
                _write_text(
                    archive,
                    f"ppt/slides/slide{index}.xml",
                    _slide_xml(slide, width, height),
                )
                _write_text(
                    archive,
                    f"ppt/slides/_rels/slide{index}.xml.rels",
                    _slide_relationships(image_name),
                )
                archive.write(
                    slide.image_path,
                    f"ppt/media/{image_name}",
                    compress_type=zipfile.ZIP_STORED,
                )
            if interrupted and interrupted():
                raise InterruptedError("presentation generation interrupted")
        os.replace(temporary_path, destination)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise
    return str(destination)
