from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}


def split_path_input(raw: str) -> tuple[str, str]:
    raw = str(raw or "").strip()
    if not raw:
        return "", ""
    if raw[0] in {'"', "'"}:
        quote = raw[0]
        pos = 1
        while pos < len(raw):
            ch = raw[pos]
            if ch == "\\" and pos + 1 < len(raw):
                pos += 2
                continue
            if ch == quote:
                return raw[1:pos], raw[pos + 1 :].strip()
            pos += 1
        return raw[1:], ""

    pos = 0
    while pos < len(raw):
        ch = raw[pos]
        if ch == "\\" and pos + 1 < len(raw) and raw[pos + 1] == " ":
            pos += 2
        elif ch == " ":
            break
        else:
            pos += 1
    return raw[:pos].replace("\\ ", " "), raw[pos:].strip()


def resolve_attachment_path(raw_path: str) -> Path | None:
    token = str(raw_path or "").strip()
    if not token:
        return None
    if (token.startswith('"') and token.endswith('"')) or (
        token.startswith("'") and token.endswith("'")
    ):
        token = token[1:-1].strip()
    token = token.replace("\\ ", " ")
    if token.startswith("file://"):
        try:
            parsed = urlparse(token)
            if parsed.scheme == "file":
                token = unquote(parsed.path or "")
                if parsed.netloc and os.name == "nt":
                    token = f"//{parsed.netloc}{token}"
        except Exception:
            pass
    expanded = os.path.expandvars(os.path.expanduser(token))
    if os.name != "nt":
        normalized = expanded.replace("\\", "/")
        if (
            len(normalized) >= 3
            and normalized[1] == ":"
            and normalized[2] == "/"
            and normalized[0].isalpha()
        ):
            expanded = f"/mnt/{normalized[0].lower()}/{normalized[3:]}"
    path = Path(expanded)
    if not path.is_absolute():
        path = Path(os.getenv("TERMINAL_CWD", os.getcwd())) / path
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    try:
        if not resolved.exists() or not resolved.is_file():
            return None
    except OSError:
        return None
    return resolved


def detect_file_drop(user_input: str) -> dict | None:
    if not isinstance(user_input, str):
        return None
    stripped = user_input.strip()
    if not stripped:
        return None
    starts_like_path = (
        stripped.startswith("/")
        or stripped.startswith("~")
        or stripped.startswith("./")
        or stripped.startswith("../")
        or stripped.startswith("file://")
        or (
            len(stripped) >= 3
            and stripped[1] == ":"
            and stripped[2] in {"\\", "/"}
            and stripped[0].isalpha()
        )
        or stripped.startswith('"/')
        or stripped.startswith('"~')
        or stripped.startswith("'/")
        or stripped.startswith("'~")
        or stripped.startswith('"./')
        or stripped.startswith('"../')
        or stripped.startswith("'./")
        or stripped.startswith("'../")
        or (
            len(stripped) >= 4
            and stripped[0] in {"'", '"'}
            and stripped[2] == ":"
            and stripped[3] in {"\\", "/"}
            and stripped[1].isalpha()
        )
    )
    if not starts_like_path:
        return None

    direct_path = resolve_attachment_path(stripped)
    if direct_path is not None:
        return {
            "path": direct_path,
            "is_image": direct_path.suffix.lower() in IMAGE_EXTENSIONS,
            "remainder": "",
        }

    first_token, remainder = split_path_input(stripped)
    drop_path = resolve_attachment_path(first_token)
    if drop_path is None and " " in stripped and stripped[0] not in {"'", '"'}:
        for pos in reversed([idx for idx, ch in enumerate(stripped) if ch == " "]):
            candidate = stripped[:pos].rstrip()
            resolved = resolve_attachment_path(candidate)
            if resolved is not None:
                drop_path = resolved
                remainder = stripped[pos + 1 :].strip()
                break
    if drop_path is None:
        return None
    return {
        "path": drop_path,
        "is_image": drop_path.suffix.lower() in IMAGE_EXTENSIONS,
        "remainder": remainder,
    }
