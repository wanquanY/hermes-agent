from __future__ import annotations

import os
import subprocess
import threading
import time

FUZZY_CACHE_TTL_S = 5.0
FUZZY_CACHE_MAX_FILES = 20000
FUZZY_FALLBACK_EXCLUDES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".next",
        ".cache",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

fuzzy_cache_lock = threading.Lock()
fuzzy_cache: dict[str, tuple[float, list[str]]] = {}


def normalize_completion_path(path_part: str) -> str:
    expanded = os.path.expanduser(path_part)
    if os.name != "nt":
        normalized = expanded.replace("\\", "/")
        if (
            len(normalized) >= 3
            and normalized[1] == ":"
            and normalized[2] == "/"
            and normalized[0].isalpha()
        ):
            return f"/mnt/{normalized[0].lower()}/{normalized[3:]}"
    return expanded


def list_repo_files(root: str) -> list[str]:
    """Return file paths relative to ``root`` for fuzzy completion."""
    now = time.monotonic()
    with fuzzy_cache_lock:
        cached = fuzzy_cache.get(root)
        if cached and now - cached[0] < FUZZY_CACHE_TTL_S:
            return cached[1]

    files: list[str] = []
    try:
        top_result = subprocess.run(
            ["git", "-C", root, "rev-parse", "--show-toplevel"],
            capture_output=True,
            timeout=2.0,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if top_result.returncode == 0:
            top = top_result.stdout.decode("utf-8", "replace").strip()
            list_result = subprocess.run(
                [
                    "git",
                    "-C",
                    top,
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                capture_output=True,
                timeout=2.0,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            if list_result.returncode == 0:
                for p in list_result.stdout.decode("utf-8", "replace").split("\0"):
                    if not p:
                        continue
                    rel = os.path.relpath(os.path.join(top, p), root).replace(
                        os.sep, "/"
                    )
                    if rel.startswith("../"):
                        continue
                    files.append(rel)
                    if len(files) >= FUZZY_CACHE_MAX_FILES:
                        break
    except (OSError, subprocess.TimeoutExpired):
        pass

    if not files:
        try:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d not in FUZZY_FALLBACK_EXCLUDES and not d.startswith(".")
                ]
                rel_dir = os.path.relpath(dirpath, root)
                for f in filenames:
                    rel = f if rel_dir == "." else f"{rel_dir}/{f}"
                    files.append(rel.replace(os.sep, "/"))
                    if len(files) >= FUZZY_CACHE_MAX_FILES:
                        break
                if len(files) >= FUZZY_CACHE_MAX_FILES:
                    break
        except OSError:
            pass

    with fuzzy_cache_lock:
        fuzzy_cache[root] = (now, files)

    return files


def fuzzy_basename_rank(name: str, query: str) -> tuple[int, int] | None:
    """Rank ``name`` against ``query``; lower is better."""
    if not query:
        return (3, len(name))

    nl = name.lower()
    ql = query.lower()

    if nl == ql:
        return (0, len(name))

    if nl.startswith(ql):
        return (1, len(name))

    parts: list[str] = []
    buf = ""
    for ch in name:
        if ch in "-_." or (ch.isupper() and buf and not buf[-1].isupper()):
            if buf:
                parts.append(buf)
            buf = ch if ch not in "-_." else ""
        else:
            buf += ch
    if buf:
        parts.append(buf)
    for part in parts:
        if part.lower().startswith(ql):
            return (2, len(name))

    if ql in nl:
        return (3, len(name))

    i = 0
    for ch in nl:
        if ch == ql[i]:
            i += 1
            if i == len(ql):
                return (4, len(name))

    return None


def path_completion_items(word: str, cwd: str | None = None) -> list[dict]:
    if not word:
        return []

    root = cwd or os.getcwd()
    items: list[dict] = []
    is_context = word.startswith("@")
    query = word[1:] if is_context else word

    if is_context and not query:
        return [
            {"text": "@diff", "display": "@diff", "meta": "git diff"},
            {"text": "@staged", "display": "@staged", "meta": "staged diff"},
            {"text": "@file:", "display": "@file:", "meta": "attach file"},
            {"text": "@folder:", "display": "@folder:", "meta": "attach folder"},
            {"text": "@url:", "display": "@url:", "meta": "fetch url"},
            {"text": "@git:", "display": "@git:", "meta": "git log"},
        ]

    if is_context and query in {"file", "folder"}:
        prefix_tag, path_part = query, ""
    elif is_context and query.startswith(("file:", "folder:")):
        prefix_tag, _, tail = query.partition(":")
        path_part = tail
    else:
        prefix_tag = ""
        path_part = query if is_context else query

    if is_context and path_part and "/" not in path_part and prefix_tag != "folder":
        ranked: list[tuple[tuple[int, int], str, str]] = []
        for rel in list_repo_files(root):
            basename = os.path.basename(rel)
            if basename.startswith(".") and not path_part.startswith("."):
                continue
            rank = fuzzy_basename_rank(basename, path_part)
            if rank is None:
                continue
            ranked.append((rank, rel, basename))

        ranked.sort(key=lambda row: (row[0], len(row[1]), row[1]))
        tag = prefix_tag or "file"
        for _, rel, basename in ranked[:30]:
            items.append(
                {
                    "text": f"@{tag}:{rel}",
                    "display": basename,
                    "meta": os.path.dirname(rel),
                }
            )

        return items

    expanded = normalize_completion_path(path_part) if path_part else "."
    if expanded == "." or not expanded:
        search_dir, match = ".", ""
    elif expanded.endswith("/"):
        search_dir, match = expanded, ""
    else:
        search_dir = os.path.dirname(expanded) or "."
        match = os.path.basename(expanded)

    search_dir_abs = search_dir if os.path.isabs(search_dir) else os.path.join(root, search_dir)
    if not os.path.isdir(search_dir_abs):
        return []

    want_dir = prefix_tag == "folder"
    match_lower = match.lower()
    for entry in sorted(os.listdir(search_dir_abs)):
        if match and not entry.lower().startswith(match_lower):
            continue
        if is_context and not prefix_tag and entry.startswith("."):
            continue
        full_abs = os.path.join(search_dir_abs, entry)
        is_dir = os.path.isdir(full_abs)
        if prefix_tag and want_dir != is_dir:
            continue
        rel = os.path.relpath(full_abs, root).replace(os.sep, "/")
        suffix = "/" if is_dir else ""

        if is_context and prefix_tag:
            text = f"@{prefix_tag}:{rel}{suffix}"
        elif is_context:
            kind = "folder" if is_dir else "file"
            text = f"@{kind}:{rel}{suffix}"
        elif word.startswith("~"):
            home_rel = os.path.relpath(full_abs, os.path.expanduser("~")).replace(
                os.sep, "/"
            )
            text = "~/" + home_rel + suffix
        elif word.startswith("./"):
            text = "./" + rel + suffix
        else:
            text = rel + suffix

        items.append(
            {
                "text": text,
                "display": entry + suffix,
                "meta": "dir" if is_dir else "",
            }
        )
        if len(items) >= 30:
            break

    return items


def _details_completion_item(value: str, meta: str = "") -> dict:
    return {"text": value, "display": value, "meta": meta}


def _details_root_completion_item(
    value: str, meta: str, needs_leading_space: bool
) -> dict:
    return _details_completion_item(
        f" {value}" if needs_leading_space else value,
        meta,
    )


def details_completions(text: str) -> list[dict] | None:
    if not text.lower().startswith("/details"):
        return None

    stripped = text.strip()
    if stripped and not "/details".startswith(stripped.lower().split()[0]):
        return None

    body = text[len("/details") :]
    if body.startswith(" "):
        body = body[1:]
    parts = body.split()
    has_trailing_space = text.endswith(" ")
    sections = ("thinking", "tools", "subagents", "activity")
    modes = ("hidden", "collapsed", "expanded")

    if not body or (len(parts) == 0 and has_trailing_space):
        return [
            *[
                _details_root_completion_item(
                    mode, "global mode", not has_trailing_space
                )
                for mode in modes
            ],
            _details_root_completion_item(
                "cycle", "cycle global mode", not has_trailing_space
            ),
            *[
                _details_root_completion_item(
                    section, "section override", not has_trailing_space
                )
                for section in sections
            ],
        ]

    if len(parts) == 1 and not has_trailing_space:
        prefix = parts[0].lower()
        candidates = [*modes, "cycle", *sections]
        return [
            _details_completion_item(
                candidate,
                (
                    "section override"
                    if candidate in sections
                    else "cycle global mode" if candidate == "cycle" else "global mode"
                ),
            )
            for candidate in candidates
            if candidate.startswith(prefix) and candidate != prefix
        ]

    if len(parts) == 1 and has_trailing_space and parts[0].lower() in sections:
        return [
            *[
                _details_completion_item(mode, f"set {parts[0].lower()}")
                for mode in modes
            ],
            _details_completion_item("reset", f"clear {parts[0].lower()} override"),
        ]

    if len(parts) == 2 and not has_trailing_space and parts[0].lower() in sections:
        prefix = parts[1].lower()
        return [
            _details_completion_item(
                candidate,
                (
                    f"clear {parts[0].lower()} override"
                    if candidate == "reset"
                    else f"set {parts[0].lower()}"
                ),
            )
            for candidate in (*modes, "reset")
            if candidate.startswith(prefix) and candidate != prefix
        ]

    return []
