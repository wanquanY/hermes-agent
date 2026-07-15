"""Shared inbound media cache and delivery-path helpers for channel adapters."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

from hermes_constants import get_hermes_dir, get_hermes_home

logger = logging.getLogger(__name__)

SUPPORTED_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
    ".json": "application/json",
}


def _base_public_attr(name: str, fallback=None):
    module = sys.modules.get("channels.platforms.base")
    return getattr(module, name, fallback) if module is not None else fallback


def safe_url_for_log(url: str, max_len: int = 80) -> str:
    """Return a URL string safe for logs (no query/fragment/userinfo)."""
    if max_len <= 0:
        return ""

    if url is None:
        return ""

    raw = str(url)
    if not raw:
        return ""

    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw[:max_len]

    if parsed.scheme and parsed.netloc:
        # Strip potential embedded credentials (user:pass@host).
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        base = f"{parsed.scheme}://{netloc}"
        path = parsed.path or ""
        if path and path != "/":
            basename = path.rsplit("/", 1)[-1]
            safe = f"{base}/.../{basename}" if basename else f"{base}/..."
        else:
            safe = base
    else:
        safe = raw

    if len(safe) <= max_len:
        return safe
    if max_len <= 3:
        return "." * max_len
    return f"{safe[:max_len - 3]}..."


async def _ssrf_redirect_guard(response):
    """Re-validate each redirect target to prevent redirect-based SSRF.

    Without this, an attacker can host a public URL that 302-redirects to
    http://169.254.169.254/ and bypass the pre-flight is_safe_url() check.

    Must be async because httpx.AsyncClient awaits response event hooks.
    """
    if response.is_redirect and response.next_request:
        redirect_url = str(response.next_request.url)
        from tools.url_safety import is_safe_url
        if not is_safe_url(redirect_url):
            raise ValueError(
                f"Blocked redirect to private/internal address: {safe_url_for_log(redirect_url)}"
            )


# ---------------------------------------------------------------------------
# Image cache utilities
#
# When users send images on messaging platforms, we download them to a local
# cache directory so they can be analyzed by the vision tool (which accepts
# local file paths). This avoids issues with ephemeral platform URLs
# (e.g. Telegram file URLs expire after ~1 hour).
# ---------------------------------------------------------------------------

# Default location: {HERMES_HOME}/cache/images/ (legacy: image_cache/)
IMAGE_CACHE_DIR = get_hermes_dir("cache/images", "image_cache")

# ---------------------------------------------------------------------------
# Inbound media size cap (#13145)
#
# Inbound image / audio / video payloads are buffered fully into process
# memory before being written to the cache directory. With no cap, a single
# large upload (Discord Nitro allows 500 MB) — or a remote URL in an inbound
# message payload pointing at an arbitrarily large file — can spike RAM and
# OOM-kill the gateway. The ``cache_*_from_bytes`` helpers (the shared funnel
# every platform reaches eventually) and the ``cache_*_from_url`` downloaders
# enforce this cap, so the protection holds regardless of which platform
# adapter or code path produced the bytes.
#
# Configurable via ``gateway.max_inbound_media_bytes`` in config.yaml.
# ``0`` disables the cap. Default 128 MiB — generous enough for ordinary
# photos/voice notes/short clips while still bounding a hostile upload.
# ---------------------------------------------------------------------------
DEFAULT_INBOUND_MEDIA_MAX_BYTES = 128 * 1024 * 1024


def get_inbound_media_max_bytes() -> int:
    """Return the max inbound image/audio/video bytes allowed in memory.

    Reads ``gateway.max_inbound_media_bytes`` from config.yaml. ``0`` (or a
    negative / unparseable value) disables the cap. Non-fatal if config is
    unreadable — falls back to the default.
    """
    try:
        from hermes_cli.config import load_config as _load_config
        cfg = _load_config()
    except Exception:
        return DEFAULT_INBOUND_MEDIA_MAX_BYTES
    gw = cfg.get("gateway", {}) if isinstance(cfg, dict) else {}
    if not isinstance(gw, dict) or "max_inbound_media_bytes" not in gw:
        return DEFAULT_INBOUND_MEDIA_MAX_BYTES
    try:
        return int(gw["max_inbound_media_bytes"])
    except (TypeError, ValueError):
        return DEFAULT_INBOUND_MEDIA_MAX_BYTES


def validate_inbound_media_size(
    size: int,
    *,
    media_type: str = "media",
    max_bytes: Optional[int] = None,
) -> None:
    """Raise ``ValueError`` if an inbound media payload exceeds the cap.

    A ``max_bytes`` of ``0`` (or the configured cap resolving to ``0``)
    disables the check entirely. Passing ``max_bytes`` lets callers resolve
    the limit once and reuse it across an incremental read.
    """
    cap_fn = _base_public_attr("get_inbound_media_max_bytes", get_inbound_media_max_bytes)
    limit = cap_fn() if max_bytes is None else max_bytes
    if limit and size > limit:
        raise ValueError(
            f"Inbound {media_type} payload is too large "
            f"({size} bytes > {limit} bytes)"
        )


async def _read_httpx_body_with_limit(response, *, media_type: str) -> bytes:
    """Read an httpx streaming response body without exceeding the media cap.

    Rejects early on an oversized ``Content-Length`` header, then re-checks
    the running total as chunks arrive so a lying/absent header can't smuggle
    an unbounded body past the cap.
    """
    cap_fn = _base_public_attr("get_inbound_media_max_bytes", get_inbound_media_max_bytes)
    max_bytes = cap_fn()
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            logger.debug(
                "Ignoring invalid Content-Length for inbound %s: %r",
                media_type, content_length,
            )
        else:
            validate_inbound_media_size(
                declared_size, media_type=media_type, max_bytes=max_bytes,
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        validate_inbound_media_size(total, media_type=media_type, max_bytes=max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


def get_image_cache_dir() -> Path:
    """Return the image cache directory, creating it if it doesn't exist."""
    cache_dir = Path(_base_public_attr("IMAGE_CACHE_DIR", IMAGE_CACHE_DIR))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _looks_like_image(data: bytes) -> bool:
    """Return True if *data* starts with a known image magic-byte sequence."""
    if len(data) < 4:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return True
    if data[:2] == b"BM":
        return True
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return True
    return False


def cache_image_from_bytes(data: bytes, ext: str = ".jpg") -> str:
    """
    Save raw image bytes to the cache and return the absolute file path.

    Args:
        data: Raw image bytes.
        ext:  File extension including the dot (e.g. ".jpg", ".png").

    Returns:
        Absolute path to the cached image file as a string.

    Raises:
        ValueError: If *data* does not look like a valid image (e.g. an HTML
            error page returned by the upstream server).
    """
    validate_inbound_media_size(len(data), media_type="image")
    if not _looks_like_image(data):
        snippet = data[:80].decode("utf-8", errors="replace")
        raise ValueError(
            f"Refusing to cache non-image data as {ext} "
            f"(starts with: {snippet!r})"
        )
    cache_dir = get_image_cache_dir()
    filename = f"img_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


async def cache_image_from_url(url: str, ext: str = ".jpg", retries: int = 2) -> str:
    """
    Download an image from a URL and save it to the local cache.

    Retries on transient failures (timeouts, 429, 5xx) with exponential
    backoff so a single slow CDN response doesn't lose the media.

    Args:
        url: The HTTP/HTTPS URL to download from.
        ext: File extension including the dot (e.g. ".jpg", ".png").
        retries: Number of retry attempts on transient failures.

    Returns:
        Absolute path to the cached image file as a string.

    Raises:
        ValueError: If the URL targets a private/internal network (SSRF protection).
    """
    from tools.url_safety import is_safe_url
    if not is_safe_url(url):
        raise ValueError(f"Blocked unsafe URL (SSRF protection): {safe_url_for_log(url)}")

    import httpx
    _log = logging.getLogger(__name__)

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        event_hooks={"response": [_ssrf_redirect_guard]},
    ) as client:
        for attempt in range(retries + 1):
            try:
                async with client.stream(
                    "GET",
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
                        "Accept": "image/*,*/*;q=0.8",
                    },
                ) as response:
                    response.raise_for_status()
                    content = await _read_httpx_body_with_limit(
                        response, media_type="image",
                    )
                return cache_image_from_bytes(content, ext)
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 429:
                    raise
                if attempt < retries:
                    wait = 1.5 * (attempt + 1)
                    _log.debug(
                        "Media cache retry %d/%d for %s (%.1fs): %s",
                        attempt + 1,
                        retries,
                        safe_url_for_log(url),
                        wait,
                        exc,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise


def cleanup_image_cache(max_age_hours: int = 24) -> int:
    """
    Delete cached images older than *max_age_hours*.

    Returns the number of files removed.
    """
    import time

    cache_dir = get_image_cache_dir()
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# Audio cache utilities
#
# Same pattern as image cache -- voice messages from platforms are downloaded
# here so the STT tool (OpenAI Whisper) can transcribe them from local files.
# ---------------------------------------------------------------------------

AUDIO_CACHE_DIR = get_hermes_dir("cache/audio", "audio_cache")


def get_audio_cache_dir() -> Path:
    """Return the audio cache directory, creating it if it doesn't exist."""
    cache_dir = Path(_base_public_attr("AUDIO_CACHE_DIR", AUDIO_CACHE_DIR))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def cache_audio_from_bytes(data: bytes, ext: str = ".ogg") -> str:
    """
    Save raw audio bytes to the cache and return the absolute file path.

    Args:
        data: Raw audio bytes.
        ext:  File extension including the dot (e.g. ".ogg", ".mp3").

    Returns:
        Absolute path to the cached audio file as a string.
    """
    validate_inbound_media_size(len(data), media_type="audio")
    cache_dir = get_audio_cache_dir()
    filename = f"audio_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


async def cache_audio_from_url(url: str, ext: str = ".ogg", retries: int = 2) -> str:
    """
    Download an audio file from a URL and save it to the local cache.

    Retries on transient failures (timeouts, 429, 5xx) with exponential
    backoff so a single slow CDN response doesn't lose the media.

    Args:
        url: The HTTP/HTTPS URL to download from.
        ext: File extension including the dot (e.g. ".ogg", ".mp3").
        retries: Number of retry attempts on transient failures.

    Returns:
        Absolute path to the cached audio file as a string.

    Raises:
        ValueError: If the URL targets a private/internal network (SSRF protection).
    """
    from tools.url_safety import is_safe_url
    if not is_safe_url(url):
        raise ValueError(f"Blocked unsafe URL (SSRF protection): {safe_url_for_log(url)}")

    import httpx
    _log = logging.getLogger(__name__)

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        event_hooks={"response": [_ssrf_redirect_guard]},
    ) as client:
        for attempt in range(retries + 1):
            try:
                async with client.stream(
                    "GET",
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
                        "Accept": "audio/*,*/*;q=0.8",
                    },
                ) as response:
                    response.raise_for_status()
                    content = await _read_httpx_body_with_limit(
                        response, media_type="audio",
                    )
                return cache_audio_from_bytes(content, ext)
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 429:
                    raise
                if attempt < retries:
                    wait = 1.5 * (attempt + 1)
                    _log.debug(
                        "Audio cache retry %d/%d for %s (%.1fs): %s",
                        attempt + 1,
                        retries,
                        safe_url_for_log(url),
                        wait,
                        exc,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise


# ---------------------------------------------------------------------------
# Video cache utilities
#
# Same pattern as image/audio cache -- videos from platforms are downloaded
# here so the agent can reference them by local file path.
# ---------------------------------------------------------------------------

VIDEO_CACHE_DIR = get_hermes_dir("cache/videos", "video_cache")

SUPPORTED_VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
}


def get_video_cache_dir() -> Path:
    """Return the video cache directory, creating it if it doesn't exist."""
    cache_dir = Path(_base_public_attr("VIDEO_CACHE_DIR", VIDEO_CACHE_DIR))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def cache_video_from_bytes(data: bytes, ext: str = ".mp4") -> str:
    """Save raw video bytes to the cache and return the absolute file path."""
    validate_inbound_media_size(len(data), media_type="video")
    cache_dir = get_video_cache_dir()
    filename = f"video_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


# ---------------------------------------------------------------------------
# Document cache utilities
#
# Same pattern as image/audio cache -- documents from platforms are downloaded
# here so the agent can reference them by local file path.
# ---------------------------------------------------------------------------

DOCUMENT_CACHE_DIR = get_hermes_dir("cache/documents", "document_cache")
SCREENSHOT_CACHE_DIR = get_hermes_dir("cache/screenshots", "browser_screenshots")
_HERMES_HOME = get_hermes_home()
MEDIA_DELIVERY_ALLOW_DIRS_ENV = "HERMES_MEDIA_ALLOW_DIRS"
MEDIA_DELIVERY_SAFE_ROOTS = (
    IMAGE_CACHE_DIR,
    AUDIO_CACHE_DIR,
    VIDEO_CACHE_DIR,
    DOCUMENT_CACHE_DIR,
    SCREENSHOT_CACHE_DIR,
    _HERMES_HOME / "image_cache",
    _HERMES_HOME / "audio_cache",
    _HERMES_HOME / "video_cache",
    _HERMES_HOME / "document_cache",
    _HERMES_HOME / "browser_screenshots",
)


def _media_delivery_allowed_roots() -> List[Path]:
    """Return roots from which model-emitted local media may be delivered."""
    safe_roots = _base_public_attr("MEDIA_DELIVERY_SAFE_ROOTS", MEDIA_DELIVERY_SAFE_ROOTS)
    roots = [Path(root) for root in safe_roots]
    extra_roots = os.environ.get(MEDIA_DELIVERY_ALLOW_DIRS_ENV, "")
    for chunk in extra_roots.split(os.pathsep):
        for raw_root in chunk.split(","):
            raw_root = raw_root.strip()
            if not raw_root:
                continue
            root = Path(os.path.expanduser(raw_root))
            if root.is_absolute():
                roots.append(root)
    return roots


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_media_delivery_path(path: str) -> Optional[str]:
    """Return a safe absolute file path for native media delivery, else None.

    MEDIA tags and bare local paths in model output are untrusted text. Only
    existing regular files under Hermes-managed media caches, or roots the
    operator explicitly allowlists, may be uploaded as native attachments.
    Symlinks are resolved before the containment check.
    """
    if not path:
        return None

    candidate = str(path).strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "`\"'":
        candidate = candidate[1:-1].strip()
    candidate = candidate.lstrip("`\"'").rstrip("`\"',.;:)}]")
    if not candidate:
        return None

    expanded = Path(os.path.expanduser(candidate))
    if not expanded.is_absolute():
        return None

    try:
        resolved = expanded.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None

    if not resolved.is_file():
        return None

    for root in _media_delivery_allowed_roots():
        try:
            resolved_root = root.expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if _path_is_within(resolved, resolved_root):
            return str(resolved)

    return None


SUPPORTED_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".log": "text/plain",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".zip": "application/zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ts": "text/plain",
    ".py": "text/plain",
    ".sh": "text/plain",
}


# ---------------------------------------------------------------------------
# Image document types
#
# Image extensions that platforms may deliver as "documents" rather than
# native photo attachments (Telegram users uploading via the file picker,
# clients that wrap stickers/screenshots as files, etc.). When we see one
# of these, we route the bytes through the image cache and the normal
# vision/photo handling path instead of rejecting them as unsupported
# documents.
# ---------------------------------------------------------------------------

SUPPORTED_IMAGE_DOCUMENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@dataclass(frozen=True)
class CachedMedia:
    path: str
    media_type: str
    kind: str

    def context_note(self) -> str:
        return f"[Cached {self.kind} attachment: {self.path}]"


def get_document_cache_dir() -> Path:
    """Return the document cache directory, creating it if it doesn't exist."""
    cache_dir = Path(_base_public_attr("DOCUMENT_CACHE_DIR", DOCUMENT_CACHE_DIR))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def cache_document_from_bytes(data: bytes, filename: str) -> str:
    """
    Save raw document bytes to the cache and return the absolute file path.

    The cached filename preserves the original human-readable name with a
    unique prefix: ``doc_{uuid12}_{original_filename}``.

    Args:
        data: Raw document bytes.
        filename: Original filename (e.g. "report.pdf").

    Returns:
        Absolute path to the cached document file as a string.

    Raises:
        ValueError: If the sanitized path escapes the cache directory.
    """
    cache_dir = get_document_cache_dir()
    # Sanitize: strip directory components, null bytes, and control characters
    safe_name = Path(filename).name if filename else "document"
    safe_name = safe_name.replace("\x00", "").strip()
    if not safe_name or safe_name in {".", ".."}:
        safe_name = "document"
    cached_name = f"doc_{uuid.uuid4().hex[:12]}_{safe_name}"
    filepath = cache_dir / cached_name
    # Final safety check: ensure path stays inside cache dir
    if not filepath.resolve().is_relative_to(cache_dir.resolve()):
        raise ValueError(f"Path traversal rejected: {filename!r}")
    filepath.write_bytes(data)
    return str(filepath)


def cache_media_bytes(
    data: bytes,
    *,
    filename: str | None = None,
    mime_type: str | None = None,
    default_kind: str = "document",
) -> CachedMedia | None:
    """Cache inbound media bytes using the canonical media cache for its kind."""
    safe_filename = Path(filename or "").name
    ext = Path(safe_filename).suffix.lower()
    media_type = (mime_type or "").strip().lower()
    kind = (default_kind or "").strip().lower()

    if media_type.startswith("image/") or kind == "image" or ext in SUPPORTED_IMAGE_DOCUMENT_TYPES:
        image_ext = ext if ext in SUPPORTED_IMAGE_DOCUMENT_TYPES else ".jpg"
        path = cache_image_from_bytes(data, image_ext)
        return CachedMedia(
            path=path,
            media_type=media_type or SUPPORTED_IMAGE_DOCUMENT_TYPES.get(image_ext, "image/jpeg"),
            kind="image",
        )

    if media_type.startswith("audio/") or kind in {"audio", "voice"}:
        audio_ext = ext if ext in _AUDIO_EXTS else ".ogg"
        path = cache_audio_from_bytes(data, audio_ext)
        return CachedMedia(
            path=path,
            media_type=media_type or f"audio/{audio_ext.lstrip('.')}",
            kind="audio",
        )

    if media_type.startswith("video/") or kind == "video" or ext in SUPPORTED_VIDEO_TYPES:
        video_ext = ext if ext in SUPPORTED_VIDEO_TYPES else ".mp4"
        path = cache_video_from_bytes(data, video_ext)
        return CachedMedia(
            path=path,
            media_type=media_type or SUPPORTED_VIDEO_TYPES.get(video_ext, "video/mp4"),
            kind="video",
        )

    document_ext = ext or ".bin"
    document_mime = media_type or SUPPORTED_DOCUMENT_TYPES.get(document_ext, "application/octet-stream")
    document_name = safe_filename or f"attachment{document_ext}"
    path = cache_document_from_bytes(data, document_name)
    return CachedMedia(path=path, media_type=document_mime, kind="document")


def cleanup_document_cache(max_age_hours: int = 24) -> int:
    """
    Delete cached documents older than *max_age_hours*.

    Returns the number of files removed.
    """
    import time

    cache_dir = get_document_cache_dir()
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


