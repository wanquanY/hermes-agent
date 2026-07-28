"""Shared lifecycle operations for local skill packages."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any


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
