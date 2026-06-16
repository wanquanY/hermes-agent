from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agent.doxie_diagnostics import emit_doxie_diagnostic

DAY_SECONDS = 24 * 60 * 60
DEFAULT_GROWTH_RANGE_DAYS = 30
GROWTH_PRESET_DAYS = {
    "week": 7,
    "month": 30,
}
INTERNAL_SESSION_SOURCES = {"tool", "cron"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _diagnose_growth(stage: str, **fields: Any) -> None:
    emit_doxie_diagnostic("[profile-growth-summary]", {"stage": stage, **fields})


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        parsed = float(value)
        if parsed > 100_000_000_000:
            return parsed / 1000
        return parsed if parsed > 0 else 0.0
    raw = _text(value)
    if not raw:
        return 0.0
    try:
        parsed = float(raw)
        if parsed > 100_000_000_000:
            return parsed / 1000
        return parsed if parsed > 0 else 0.0
    except ValueError:
        pass
    try:
        parsed_dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        return parsed_dt.timestamp()
    except ValueError:
        return 0.0


def _start_of_local_day(value: float | None = None) -> float:
    date = datetime.fromtimestamp(value or datetime.now().timestamp()).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return date.timestamp()


def _parse_local_date_start(value: Any) -> float:
    raw = _text(value)
    if not raw:
        return 0.0
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if match:
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        ).timestamp()
    parsed = _timestamp(raw)
    return _start_of_local_day(parsed) if parsed > 0 else 0.0


def _local_date_key(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d")


def _iso_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
        if value > 0
        else ""
    )


def _latest_iso_before_end(end_ts: float, *values: Any) -> str:
    latest = max(
        (
            parsed
            for parsed in (_timestamp(value) for value in values)
            if parsed > 0 and _start_of_local_day(parsed) <= end_ts
        ),
        default=0.0,
    )
    return _iso_timestamp(latest)


def _safe_path(value: Any) -> Path | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def _unique_paths(values: Iterable[Any]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = _safe_path(value)
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _read_text(path: Path, max_bytes: int = 96 * 1024) -> str:
    try:
        if not path.is_file():
            return ""
        with path.open("rb") as handle:
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _count_markdown_knowledge_items(content: str) -> int:
    count = 0
    for line in str(content or "").splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        if re.match(r"^#+\s+", trimmed):
            count += 1
        elif re.match(r"^[-*]\s+", trimmed):
            count += 1
        elif re.match(r"^\d+[.)]\s+", trimmed):
            count += 1
        elif len(trimmed) >= 8:
            count += 1
    return count


def _markdown_highlights(content: str, limit: int = 3) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for line in str(content or "").splitlines():
        trimmed = re.sub(r"^\d+[.)]\s*", "", re.sub(r"^[-*]\s*", "", re.sub(r"^#+\s*", "", line.strip()))).strip()
        if len(trimmed) < 3 or trimmed in seen:
            continue
        seen.add(trimmed)
        result.append(trimmed)
        if len(result) >= limit:
            break
    return result


@dataclass(frozen=True)
class FileStat:
    path: Path
    mtime: float


@dataclass(frozen=True)
class MemoryDocument:
    home: Path
    path: Path
    content: str
    mtime: float


@dataclass(frozen=True)
class SkillStat:
    name: str
    path: Path
    home: Path
    mtime: float


@dataclass(frozen=True)
class SessionStat:
    id: str
    name: str
    source: str
    mtime: float


def _file_stat(path: Path) -> FileStat | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return FileStat(path=path, mtime=float(stat.st_mtime or 0))


def _select_memory_document(home_paths: list[Path], file_name: str) -> MemoryDocument | None:
    docs: list[MemoryDocument] = []
    for home in home_paths:
        path = home / "memories" / file_name
        stat = _file_stat(path)
        if stat is None:
            continue
        docs.append(MemoryDocument(home=home, path=path, content=_read_text(path), mtime=stat.mtime))
    with_content = [doc for doc in docs if doc.content.strip()]
    candidates = with_content or docs
    return sorted(candidates, key=lambda item: item.mtime, reverse=True)[0] if candidates else None


def _list_installed_skills(home: Path | None) -> list[SkillStat]:
    if home is None:
        return []
    skills_root = home / "skills"
    if not skills_root.is_dir():
        return []
    skills: list[SkillStat] = []
    try:
        skill_files = sorted(skills_root.rglob("SKILL.md"))
    except OSError:
        return []
    for skill_file in skill_files:
        if not skill_file.is_file():
            continue
        skill_dir = skill_file.parent
        try:
            stat = skill_dir.stat()
            rel = skill_dir.relative_to(skills_root)
        except OSError:
            continue
        skills.append(SkillStat(name=str(rel), path=skill_dir, home=home, mtime=float(stat.st_mtime or 0)))
    return skills


def _connect_readonly(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _list_profile_sessions(home_paths: list[Path]) -> list[SessionStat]:
    sessions: list[SessionStat] = []
    for home in home_paths:
        db_path = home / "state.db"
        conn = _connect_readonly(db_path)
        if conn is None:
            continue
        try:
            rows = conn.execute(
                """
                WITH RECURSIVE roots AS (
                    SELECT s.*
                    FROM sessions s
                    WHERE COALESCE(s.transient, 0) = 0
                      AND LOWER(COALESCE(s.source, '')) NOT IN ('tool', 'cron')
                      AND (COALESCE(s.message_count, 0) > 0 OR COALESCE(s.title, '') <> '')
                      AND (
                        s.parent_session_id IS NULL
                        OR EXISTS (
                          SELECT 1 FROM sessions p
                          WHERE p.id = s.parent_session_id
                            AND p.end_reason = 'branched'
                            AND s.started_at >= p.ended_at
                        )
                      )
                ),
                chain(root_id, cur_id) AS (
                    SELECT id, id FROM roots
                    UNION ALL
                    SELECT c.root_id, child.id
                    FROM chain c
                    JOIN sessions parent ON parent.id = c.cur_id
                    JOIN sessions child ON child.parent_session_id = c.cur_id
                    WHERE parent.end_reason = 'compression'
                      AND child.started_at >= parent.ended_at
                ),
                chain_max AS (
                    SELECT
                        root_id,
                        MAX(COALESCE(
                            (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = cur_id),
                            (SELECT ss.last_active FROM sessions ss WHERE ss.id = cur_id),
                            (SELECT ss.started_at FROM sessions ss WHERE ss.id = cur_id)
                        )) AS effective_last_active
                    FROM chain
                    GROUP BY root_id
                )
                SELECT roots.id, roots.title, roots.source, roots.started_at,
                       COALESCE(chain_max.effective_last_active, roots.last_active, roots.started_at) AS last_active
                FROM roots
                LEFT JOIN chain_max ON chain_max.root_id = roots.id
                """,
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for row in rows:
            source = _text(row["source"]).lower()
            if source in INTERNAL_SESSION_SOURCES:
                continue
            session_id = _text(row["id"])
            if not session_id:
                continue
            sessions.append(
                SessionStat(
                    id=session_id,
                    name=_text(row["title"]) or session_id,
                    source="state.db",
                    mtime=_number(row["last_active"] or row["started_at"]),
                )
            )
    by_id: dict[str, SessionStat] = {}
    for session in sessions:
        existing = by_id.get(session.id)
        if existing is None or session.mtime > existing.mtime:
            by_id[session.id] = session
    return list(by_id.values())


def _empty_delta() -> dict[str, int]:
    return {"memoryItems": 0, "skillCount": 0, "sessionCount": 0}


def _growth_score(memory_items: int, skill_count: int, session_count: int) -> int:
    return round(memory_items * 4 + skill_count * 10 + min(session_count, 80) * 1.5)


def _resolve_growth_range(range_options: dict[str, Any], activity_times: list[float]) -> dict[str, float]:
    today_start = _start_of_local_day()
    requested_end = _parse_local_date_start(range_options.get("endDate") or range_options.get("end_date"))
    end_ts = min(requested_end, today_start) if requested_end > 0 else today_start
    preset = _text(range_options.get("rangePreset") or range_options.get("range_preset")).lower()
    if preset == "custom":
        requested_start = _parse_local_date_start(range_options.get("startDate") or range_options.get("start_date"))
        start_ts = min(requested_start, today_start) if requested_start > 0 else end_ts - (GROWTH_PRESET_DAYS["week"] - 1) * DAY_SECONDS
    elif preset == "all":
        earliest = min((_start_of_local_day(value) for value in activity_times if value > 0), default=end_ts)
        start_ts = min(earliest, end_ts)
    else:
        days = GROWTH_PRESET_DAYS.get(preset, DEFAULT_GROWTH_RANGE_DAYS)
        start_ts = end_ts - (days - 1) * DAY_SECONDS
    if start_ts > end_ts:
        start_ts, end_ts = end_ts, start_ts
    return {"start": start_ts, "end": end_ts}


def _day_in_range(value: Any, growth_range: dict[str, float]) -> bool:
    parsed = _timestamp(value)
    if parsed <= 0:
        return False
    day = _start_of_local_day(parsed)
    return growth_range["start"] <= day <= growth_range["end"]


def _build_daily_growth_series(
    *,
    memory_items: int,
    user_memory_items: int,
    memory_mtime: float,
    user_mtime: float,
    skills: list[SkillStat],
    sessions: list[SessionStat],
    growth_range: dict[str, float],
) -> list[dict[str, Any]]:
    days = max(1, int((growth_range["end"] - growth_range["start"]) // DAY_SECONDS) + 1)
    baseline = _empty_delta()
    deltas_by_date: dict[str, dict[str, int]] = {}

    def apply_delta(key: str, count: int, mtime: float) -> None:
        if count <= 0 or mtime <= 0:
            return
        day = _start_of_local_day(mtime)
        if day > growth_range["end"]:
            return
        if day < growth_range["start"]:
            baseline[key] += count
            return
        date_key = _local_date_key(day)
        delta = deltas_by_date.setdefault(date_key, _empty_delta())
        delta[key] += count

    apply_delta("memoryItems", memory_items, memory_mtime)
    apply_delta("memoryItems", user_memory_items, user_mtime)
    for skill in skills:
        apply_delta("skillCount", 1, skill.mtime)
    for session in sessions:
        apply_delta("sessionCount", 1, session.mtime)

    running = dict(baseline)
    series: list[dict[str, Any]] = []
    for index in range(days):
        day = growth_range["start"] + index * DAY_SECONDS
        date_key = _local_date_key(day)
        delta = deltas_by_date.get(date_key) or _empty_delta()
        running["memoryItems"] += delta["memoryItems"]
        running["skillCount"] += delta["skillCount"]
        running["sessionCount"] += delta["sessionCount"]
        series.append(
            {
                "date": date_key,
                "memoryItems": running["memoryItems"],
                "skillCount": running["skillCount"],
                "sessionCount": running["sessionCount"],
                "memoryDelta": delta["memoryItems"],
                "skillDelta": delta["skillCount"],
                "sessionDelta": delta["sessionCount"],
                "score": _growth_score(running["memoryItems"], running["skillCount"], running["sessionCount"]),
            }
        )
    return series


def summarize_agent_profile_growth(
    profile: dict[str, Any],
    *,
    version: dict[str, Any] | None = None,
    range_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    range_options = range_options or {}
    version = version or {}
    profile_id = _text(profile.get("id") or profile.get("agentProfileId") or profile.get("agent_profile_id"))
    version_id = _text(version.get("id") or version.get("versionId") or version.get("version_id"))
    home_paths = _unique_paths(
        [
            version.get("runtimeHomePath") or version.get("runtime_home_path"),
            profile.get("runtimeHomePath") or profile.get("runtime_home_path"),
            profile.get("hermesHomePath") or profile.get("hermes_home_path"),
        ]
    )
    primary_home = home_paths[0] if home_paths else None
    project_doc = _select_memory_document(home_paths, "MEMORY.md")
    user_doc = _select_memory_document(home_paths, "USER.md")
    skills = _list_installed_skills(primary_home)
    sessions = _list_profile_sessions(home_paths)

    memory_items = _count_markdown_knowledge_items(project_doc.content if project_doc else "")
    user_memory_items = _count_markdown_knowledge_items(user_doc.content if user_doc else "")
    activity_times = [
        _timestamp(profile.get("createdAt") or profile.get("created_at")),
        _timestamp(profile.get("updatedAt") or profile.get("updated_at")),
        project_doc.mtime if project_doc else 0.0,
        user_doc.mtime if user_doc else 0.0,
        *(skill.mtime for skill in skills),
        *(session.mtime for session in sessions),
    ]
    growth_range = _resolve_growth_range(range_options, activity_times)
    daily_growth = _build_daily_growth_series(
        memory_items=memory_items,
        user_memory_items=user_memory_items,
        memory_mtime=project_doc.mtime if project_doc else 0.0,
        user_mtime=user_doc.mtime if user_doc else 0.0,
        skills=skills,
        sessions=sessions,
        growth_range=growth_range,
    )
    final = daily_growth[-1] if daily_growth else _empty_delta()
    end_ts = growth_range["end"]
    latest_skill_at = _latest_iso_before_end(end_ts, *(skill.mtime for skill in skills))
    latest_session_at = _latest_iso_before_end(end_ts, *(session.mtime for session in sessions))
    latest_memory_at = _latest_iso_before_end(
        end_ts,
        project_doc.mtime if project_doc else 0.0,
        user_doc.mtime if user_doc else 0.0,
    )
    project_memory_items = memory_items if project_doc and _start_of_local_day(project_doc.mtime) <= end_ts else 0
    visible_user_memory_items = user_memory_items if user_doc and _start_of_local_day(user_doc.mtime) <= end_ts else 0

    def rel(path: Path) -> str:
        if primary_home is None:
            return str(path)
        try:
            return str(path.relative_to(primary_home))
        except ValueError:
            return str(path)

    recent_events: list[dict[str, Any]] = []
    if project_doc is not None:
        recent_events.append(
            {
                "id": "memory-project",
                "type": "memory",
                "title": f"沉淀 {memory_items} 条项目记忆" if memory_items else "项目记忆更新",
                "description": "、".join(_markdown_highlights(project_doc.content)) or "MEMORY.md 中已有可用于后续任务的长期上下文",
                "at": _iso_timestamp(project_doc.mtime),
                "source": rel(project_doc.path),
            }
        )
    if user_doc is not None:
        recent_events.append(
            {
                "id": "memory-user",
                "type": "memory",
                "title": f"形成 {user_memory_items} 条用户画像" if user_memory_items else "用户画像更新",
                "description": "、".join(_markdown_highlights(user_doc.content)) or "USER.md 中已有该分身理解到的用户偏好",
                "at": _iso_timestamp(user_doc.mtime),
                "source": rel(user_doc.path),
            }
        )
    for skill in sorted(skills, key=lambda item: item.mtime, reverse=True)[:3]:
        recent_events.append(
            {
                "id": f"skill-{skill.name}",
                "type": "skill",
                "title": f"解锁技能 {skill.name}",
                "description": f"该分身已可调用 {skill.name} 处理匹配任务",
                "at": _iso_timestamp(skill.mtime),
                "source": rel(skill.path),
            }
        )
    for session in sorted(sessions, key=lambda item: item.mtime, reverse=True)[:2]:
        recent_events.append(
            {
                "id": f"session-{session.id}",
                "type": "session",
                "title": "项目熟悉度提升",
                "description": f"累计 {len(sessions)} 次会话沉淀，最近会话 {session.name}",
                "at": _iso_timestamp(session.mtime),
                "source": session.source,
            }
        )
    recent_events = sorted(
        [event for event in recent_events if event.get("at") and _day_in_range(event.get("at"), growth_range)],
        key=lambda item: _timestamp(item.get("at")),
        reverse=True,
    )[:6]

    summary = {
        "memoryItems": int(final.get("memoryItems", 0) or 0),
        "projectMemoryItems": project_memory_items,
        "userMemoryItems": visible_user_memory_items,
        "skillCount": int(final.get("skillCount", 0) or 0),
        "sessionCount": int(final.get("sessionCount", 0) or 0),
        "latestMemoryAt": latest_memory_at,
        "latestSkillAt": latest_skill_at,
        "latestSessionAt": latest_session_at,
        "latestActivityAt": _latest_iso_before_end(
            end_ts,
            latest_memory_at,
            latest_skill_at,
            latest_session_at,
            profile.get("updatedAt") or profile.get("updated_at"),
            profile.get("createdAt") or profile.get("created_at"),
        ),
        "dailyGrowth": daily_growth,
        "recentEvents": recent_events,
    }
    _diagnose_growth(
        "read_model_summary",
        profile_id=profile_id,
        version_id=version_id,
        range_preset=_text(range_options.get("rangePreset") or range_options.get("range_preset")),
        start_date=_text(range_options.get("startDate") or range_options.get("start_date")),
        end_date=_text(range_options.get("endDate") or range_options.get("end_date")),
        home_count=len(home_paths),
        home_paths=[str(path) for path in home_paths],
        primary_home=str(primary_home) if primary_home else "",
        project_memory_found=project_doc is not None,
        user_memory_found=user_doc is not None,
        project_memory_path=str(project_doc.path) if project_doc else "",
        user_memory_path=str(user_doc.path) if user_doc else "",
        project_memory_items=project_memory_items,
        user_memory_items=visible_user_memory_items,
        skill_count=summary["skillCount"],
        session_count=summary["sessionCount"],
        daily_point_count=len(daily_growth),
        recent_event_count=len(recent_events),
        latest_activity_at=summary["latestActivityAt"],
    )
    return summary
