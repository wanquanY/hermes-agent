"""Shared lifecycle operations for local skill packages."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

_INSPECT_MAX_FILES = 200
_INSPECT_MAX_FILE_BYTES = 96_000
_INSPECT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hub",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "__pycache__",
    }
)


def _skill_detail_file(path: Path, skill_dir: Path) -> dict[str, Any] | None:
    if path.is_symlink() or any(part in _INSPECT_EXCLUDED_DIRS for part in path.parts):
        return None
    try:
        resolved = path.resolve()
        resolved.relative_to(skill_dir)
        content = resolved.read_bytes()
    except (OSError, ValueError):
        return None

    truncated = len(content) > _INSPECT_MAX_FILE_BYTES
    preview = content[:_INSPECT_MAX_FILE_BYTES]
    try:
        text = preview.decode("utf-8")
        is_binary = False
    except UnicodeDecodeError:
        text = f"[Binary file: {path.relative_to(skill_dir).as_posix()}, size: {len(content)} bytes]"
        is_binary = True
        truncated = False
    return {
        "path": path.relative_to(skill_dir).as_posix(),
        "content": text,
        "truncated": truncated,
        "is_binary": is_binary,
        "size": len(content),
    }


def _direct_local_skill_record(
    skill_name: str,
    install_path: str,
    skills_root: Path,
) -> dict[str, Any] | None:
    relative_candidates = [
        str(install_path or "").strip(),
        skill_name.replace(":", "/", 1),
    ]
    for raw_relative in relative_candidates:
        if not raw_relative:
            continue
        relative = Path(raw_relative)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        skill_dir = (skills_root / relative).resolve()
        try:
            skill_dir.relative_to(skills_root)
        except ValueError:
            continue
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            continue
        parts = skill_dir.relative_to(skills_root).parts
        return {
            "name": skill_name,
            "description": "",
            "category": "/".join(parts[:-1]) or None,
            "skill_dir": str(skill_dir),
        }
    return None


def inspect_installed_skill(
    skill_name: str,
    install_path: str = "",
) -> dict[str, Any]:
    """Return local skill metadata and bounded package-file previews.

    Unlike the Skills Hub inspection path, this function resolves the skill
    through the same local index used by ``skills.list`` and never contacts a
    remote registry. Reading the package directly also keeps inspection free
    from skill activation, environment capture, and disabled-skill checks.
    """

    normalized_name = str(skill_name or "").strip()
    if not normalized_name:
        raise RuntimeError("skill_name is required")

    from tools.skills_tool import (
        SKILLS_DIR,
        _find_all_skills,
        _parse_frontmatter,
        _parse_tags,
    )

    skills_root = Path(SKILLS_DIR).expanduser().resolve()
    installed = _direct_local_skill_record(
        normalized_name,
        install_path,
        skills_root,
    )
    if installed is None:
        installed = next(
            (
                item
                for item in _find_all_skills(skip_disabled=True)
                if str(item.get("name") or "").strip() == normalized_name
            ),
            None,
        )
    if installed is None:
        raise RuntimeError(f"skill not found in local index: {normalized_name}")

    skill_dir = Path(str(installed.get("skill_dir") or "")).expanduser().resolve()
    if not skill_dir.is_dir():
        raise RuntimeError(f"skill package directory not found: {skill_dir}")
    skill_md = skill_dir / "SKILL.md"
    try:
        if skill_md.is_symlink():
            raise RuntimeError("SKILL.md symlinks are not supported")
        skill_md.resolve().relative_to(skill_dir)
        skill_content = skill_md.read_text(encoding="utf-8")
        frontmatter, _body = _parse_frontmatter(skill_content)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"failed to read local skill package: {exc}") from exc

    candidates = sorted(
        (
            path
            for path in skill_dir.rglob("*")
            if path.is_file()
        ),
        key=lambda path: (
            path.relative_to(skill_dir).as_posix() != "SKILL.md",
            path.relative_to(skill_dir).as_posix(),
        ),
    )
    files = []
    for path in candidates:
        detail = _skill_detail_file(path, skill_dir)
        if detail is None:
            continue
        files.append(detail)
        if len(files) >= _INSPECT_MAX_FILES:
            break

    metadata = frontmatter.get("metadata")
    hermes_metadata = metadata.get("hermes", {}) if isinstance(metadata, dict) else {}
    tags = _parse_tags(hermes_metadata.get("tags") or frontmatter.get("tags", ""))
    plugin_name = str(installed.get("plugin") or "").strip()
    return {
        "name": str(frontmatter.get("name") or installed.get("name") or normalized_name),
        "description": str(
            frontmatter.get("description") or installed.get("description") or ""
        ),
        "source": plugin_name or "local",
        "source_type": "plugin" if plugin_name else "local",
        "identifier": normalized_name if plugin_name else "",
        "category": str(installed.get("category") or "uncategorized"),
        "tags": tags,
        "skill_md_preview": skill_content,
        "files": files,
        "files_truncated": len(candidates) > len(files),
        "skill_dir": str(skill_dir),
    }


def _safe_relative_skill_dir(loaded_skill: dict[str, Any], skill_name: str) -> Path:
    raw_path = str(loaded_skill.get("path") or "").strip()
    if raw_path:
        rel = Path(raw_path)
        if rel.name == "SKILL.md":
            rel = rel.parent
    else:
        rel = Path(skill_name)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        return Path(skill_name)
    return rel


def _replace_directory_atomically(source_dir: Path | None, target_dir: Path, content: str) -> None:
    target_parent = target_dir.parent
    target_parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}.", dir=str(target_parent)))
    backup_dir = target_parent / f".{target_dir.name}.backup.{int(time.time() * 1000)}"
    try:
        if source_dir and source_dir.is_dir():
            shutil.rmtree(temp_dir)
            shutil.copytree(source_dir, temp_dir, symlinks=False)
        else:
            (temp_dir / "SKILL.md").write_text(content, encoding="utf-8")

        from tools.skill_manager_tool import _security_scan_skill

        scan_error = _security_scan_skill(temp_dir)
        if scan_error:
            raise RuntimeError(scan_error)

        if target_dir.exists():
            target_dir.rename(backup_dir)
        temp_dir.rename(target_dir)
        shutil.rmtree(backup_dir, ignore_errors=True)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if backup_dir.exists() and not target_dir.exists():
            backup_dir.rename(target_dir)
        raise


def _clear_and_reload_skills() -> None:
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache

        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        pass
    try:
        from agent.skill_commands import reload_skills

        reload_skills()
    except Exception:
        pass


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _safe_zip_parts(name: str) -> tuple[str, ...]:
    normalized = str(name or "").replace("\\", "/").strip("/")
    parts = tuple(part for part in normalized.split("/") if part and part != ".")
    if not parts or any(part == ".." for part in parts):
        raise RuntimeError(f"unsafe archive path: {name}")
    if parts[0].startswith("__MACOSX"):
        return ()
    return parts


def _skill_archive_root(infos: list[zipfile.ZipInfo]) -> tuple[str, ...]:
    roots: set[tuple[str, ...]] = set()
    for info in infos:
        if info.is_dir():
            continue
        parts = _safe_zip_parts(info.filename)
        if not parts or parts[-1] != "SKILL.md":
            continue
        roots.add(parts[:-1])
    if not roots:
        raise RuntimeError("archive does not contain a SKILL.md")
    if len(roots) > 1:
        raise RuntimeError("archive contains multiple skills; import one skill package at a time")
    return next(iter(roots))


def _archive_skill_name(skill_md: Path, fallback: str, explicit_name: str = "") -> str:
    if explicit_name.strip():
        raw_name = explicit_name.strip()
    else:
        try:
            from tools.skills_tool import _parse_frontmatter

            frontmatter, _body = _parse_frontmatter(skill_md.read_text(encoding="utf-8")[:4000])
            raw_name = str(frontmatter.get("name") or fallback).strip()
        except Exception:
            raw_name = fallback
    from tools.skills_hub import _validate_skill_name

    return _validate_skill_name(raw_name)


def import_skill_archive_to_home(
    archive_path: Path | str,
    *,
    category: str = "",
    name: str = "",
) -> dict[str, str]:
    """Import a local ZIP skill package into the active Hermes home.

    The archive must contain exactly one ``SKILL.md`` package. A wrapping root
    directory is allowed and stripped during import. Paths are validated before
    writing, symlinks are rejected, and the same local skill security scan used
    by other lifecycle operations runs before the package is installed.
    """

    source = Path(str(archive_path)).expanduser().resolve()
    if not source.is_file():
        raise RuntimeError(f"skill archive not found: {source}")
    if source.suffix.lower() != ".zip":
        raise RuntimeError("only .zip skill archives are supported")

    from hermes_constants import get_hermes_home, get_skills_dir
    from tools.skills_guard import content_hash
    from tools.skills_hub import _validate_category_name, append_audit_log

    temp_root = Path(tempfile.mkdtemp(prefix="skill-import."))
    extracted_dir = temp_root / "skill"
    extracted_dir.mkdir(parents=True)
    try:
        with zipfile.ZipFile(source) as zf:
            infos = [info for info in zf.infolist() if not info.is_dir()]
            if not infos:
                raise RuntimeError("archive is empty")
            root = _skill_archive_root(infos)
            for info in infos:
                if _is_zip_symlink(info):
                    raise RuntimeError(f"archive contains unsupported symlink: {info.filename}")
                parts = _safe_zip_parts(info.filename)
                if not parts:
                    continue
                if root and parts[: len(root)] != root:
                    continue
                rel_parts = parts[len(root):]
                if not rel_parts:
                    continue
                target = extracted_dir.joinpath(*rel_parts)
                target_resolved = target.resolve()
                try:
                    target_resolved.relative_to(extracted_dir.resolve())
                except ValueError as exc:
                    raise RuntimeError(f"archive path escapes skill package: {info.filename}") from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))

        skill_md = extracted_dir / "SKILL.md"
        if not skill_md.is_file():
            raise RuntimeError("archive does not contain SKILL.md at the skill package root")

        fallback_name = root[-1] if root else source.stem
        skill_name = _archive_skill_name(skill_md, fallback_name, explicit_name=name)
        safe_category = _validate_category_name(category) if category else ""
        skills_dir = get_skills_dir().resolve()
        target_dir = (skills_dir / safe_category / skill_name).resolve() if safe_category else (skills_dir / skill_name).resolve()
        try:
            target_dir.relative_to(skills_dir)
        except ValueError as exc:
            raise RuntimeError("resolved skill directory escaped the skills root") from exc

        _replace_directory_atomically(extracted_dir, target_dir, skill_md.read_text(encoding="utf-8"))
        append_audit_log("IMPORT", skill_name, "local-archive", "local", "scanned", content_hash(target_dir))
        _clear_and_reload_skills()

        return {
            "name": skill_name,
            "sourceArchive": str(source),
            "targetSkillDir": str(target_dir),
            "targetHermesHome": str(get_hermes_home().resolve()),
            "installPath": str(target_dir.relative_to(skills_dir)),
        }
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def copy_installed_skill_to_home(skill_name: str, target_home: Path | str) -> dict[str, str]:
    """Copy an installed local skill package into another Hermes home.

    This preserves supporting files, validates the resulting package with the
    same security scan used by ``skill_manage``, atomically replaces the target
    package directory, and refreshes skill caches for the target home.
    """

    normalized_name = str(skill_name or "").strip()
    if not normalized_name:
        raise RuntimeError("skill_name is required")

    from hermes_constants import get_skills_dir, reset_hermes_home_override, set_hermes_home_override
    from tools.skills_tool import skill_view

    source_raw = json.loads(skill_view(normalized_name, preprocess=False))
    if not source_raw.get("success"):
        raise RuntimeError(str(source_raw.get("error") or f"skill not found: {normalized_name}"))

    resolved_name = str(source_raw.get("name") or normalized_name).strip()
    if not resolved_name:
        raise RuntimeError("source skill has no name")
    source_dir_raw = source_raw.get("skill_dir")
    source_dir = Path(str(source_dir_raw)).expanduser().resolve() if source_dir_raw else None
    content = str(source_raw.get("content") or "")
    if not content.strip():
        raise RuntimeError(f"source skill has empty SKILL.md: {resolved_name}")

    target_home_path = Path(str(target_home)).expanduser().resolve()
    token = set_hermes_home_override(target_home_path)
    try:
        target_skills_dir = get_skills_dir().resolve()
        rel_dir = _safe_relative_skill_dir(source_raw, resolved_name)
        target_dir = (target_skills_dir / rel_dir).resolve()
        try:
            target_dir.relative_to(target_skills_dir)
        except ValueError as exc:
            raise RuntimeError("resolved target skill directory escaped the target skills root") from exc
        _replace_directory_atomically(source_dir, target_dir, content)
        _clear_and_reload_skills()
    finally:
        reset_hermes_home_override(token)

    return {
        "name": resolved_name,
        "sourceSkillDir": str(source_dir) if source_dir else "",
        "targetSkillDir": str(target_dir),
        "targetHermesHome": str(target_home_path),
    }
