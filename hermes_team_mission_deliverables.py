from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from typing import Any, Dict, List

from hermes_team_mission_artifact_refs import dedupe_artifact_refs


DELIVERABLE_VISIBILITY_HANDOFF = "handoff"
DELIVERABLE_SOURCE_AUTHORITATIVE = "authoritative"
DELIVERABLE_SOURCE_DERIVED_DEGRADED = "derived_degraded"
DELIVERABLE_SOURCE_LEGACY_IMPORTED = "legacy_imported"
DELIVERABLE_SOURCE_MISSING = "missing"


def text(value: Any) -> str:
    return str(value or "").strip()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def stable_id(prefix: str, *parts: Any) -> str:
    payload = "\x1f".join(text(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _record(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _records(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _bounded_summary(value: Any, fallback: str = "", *, limit: int = 1600) -> str:
    candidate = text(value) or text(fallback)
    if not candidate:
        return ""
    candidate = " ".join(candidate.replace("\r", "\n").split())
    if len(candidate) <= limit:
        return candidate
    return candidate[: max(0, limit - 3)].rstrip() + "..."


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.75
    return max(0.0, min(parsed, 1.0))


def _normalize_status(value: Any) -> str:
    normalized = text(value).lower()
    if normalized in {"completed", "complete", "success", "passed", "pass"}:
        return "completed"
    if normalized in {"blocked", "needs_input", "needs-input"}:
        return "blocked"
    if normalized in {"failed", "failure", "error"}:
        return "failed"
    if normalized in {"partial", "partially_completed", "partially-completed"}:
        return "partial"
    return normalized or "completed"


def _json_candidate(value: str) -> Dict[str, Any]:
    raw = text(value)
    if not raw:
        return {}
    candidates = [raw]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.append(fenced.group(1))
    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        candidates.append(raw[first:last + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _artifact_refs_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    artifacts = payload.get("artifact_refs") or payload.get("artifactRefs") or payload.get("artifacts") or payload.get("deliverables")
    if not isinstance(artifacts, list):
        return []
    refs: list[dict[str, Any]] = []
    for item in artifacts:
        if isinstance(item, dict):
            uri = text(item.get("uri") or item.get("path") or item.get("id"))
            if uri:
                refs.append(dict(item))
        elif isinstance(item, str) and item.strip():
            refs.append({"path": item.strip(), "kind": "file"})
    return dedupe_artifact_refs(refs)


def _artifact_refs_from_text(value: str) -> List[Dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for match in re.finditer(r"(?P<path>(?:/[\w .:@%+=,~\\-]+)+\.[A-Za-z0-9]{1,12})", value or ""):
        refs.append({"path": match.group("path").strip(), "kind": "file"})
    return dedupe_artifact_refs(refs)


def derived_degraded_deliverable_from_text(
    value: str,
    *,
    node_id: str,
    status: str = "completed",
    result: str = "",
    source: str = DELIVERABLE_SOURCE_DERIVED_DEGRADED,
) -> Dict[str, Any]:
    """Deterministically derive a degraded handoff from terminal visible text.

    This is a fallback for missing explicit ``team_mission_submit_deliverable``.
    It never returns an authoritative source and only fires when there is
    recoverable structured evidence or artifact evidence, not arbitrary prose.
    """
    raw_text = text(value)
    if not raw_text:
        return {}
    payload = _json_candidate(raw_text)
    artifact_refs = _artifact_refs_from_payload(payload) if payload else []
    if not artifact_refs:
        artifact_refs = _artifact_refs_from_text(raw_text)
    has_structured_evidence = bool(payload and (
        payload.get("summary")
        or payload.get("finalSummary")
        or payload.get("result")
        or payload.get("status")
        or payload.get("verification")
        or payload.get("deliverables")
        or payload.get("artifact_refs")
        or payload.get("artifactRefs")
        or payload.get("artifacts")
    ))
    has_artifact_evidence = bool(artifact_refs)
    if not has_structured_evidence and not has_artifact_evidence:
        return {}
    if not payload:
        payload = {
            "node_id": node_id,
            "status": status,
            "result": result or "PARTIAL",
            "text_excerpt": _bounded_summary(raw_text, limit=1200),
        }
    normalized_source = text(payload.get("source"))
    if normalized_source not in {DELIVERABLE_SOURCE_DERIVED_DEGRADED, DELIVERABLE_SOURCE_LEGACY_IMPORTED}:
        normalized_source = text(source) or DELIVERABLE_SOURCE_DERIVED_DEGRADED
    payload.setdefault("node_id", node_id)
    payload.setdefault("status", status)
    if result:
        payload.setdefault("result", result)
    summary = _bounded_summary(
        payload.get("summary") or payload.get("finalSummary") or payload.get("description"),
        fallback=raw_text,
    )
    next_context = _record(payload.get("next_context") or payload.get("nextContext"))
    return {
        "status": _normalize_status(payload.get("status") or status),
        "result": text(payload.get("result") or result or ("PASS" if status == "completed" else "")),
        "summary": summary,
        "payload": payload,
        "artifact_refs": artifact_refs,
        "next_context": next_context,
        "source": normalized_source,
        "confidence": 0.7 if normalized_source == DELIVERABLE_SOURCE_LEGACY_IMPORTED else (0.58 if has_structured_evidence else 0.42),
        "visibility": DELIVERABLE_VISIBILITY_HANDOFF,
    }


def row_to_deliverable(row: sqlite3.Row | None) -> Dict[str, Any]:
    if row is None:
        return {}
    deliverable_id = text(_row_value(row, "deliverable_id"))
    mission_id = text(_row_value(row, "mission_id"))
    node_id = text(_row_value(row, "node_id"))
    run_id = text(_row_value(row, "run_id"))
    task_id = text(_row_value(row, "task_id"))
    payload = _json_loads(_row_value(row, "payload_json", ""), {})
    artifact_refs = dedupe_artifact_refs(_records(_json_loads(_row_value(row, "artifact_refs_json", ""), [])))
    next_context = _record(_json_loads(_row_value(row, "next_context_json", ""), {}))
    output_contract = _record(_json_loads(_row_value(row, "output_contract_json", ""), {}))
    return {
        "deliverable_id": deliverable_id,
        "deliverableId": deliverable_id,
        "mission_id": mission_id,
        "missionId": mission_id,
        "node_id": node_id,
        "nodeId": node_id,
        "run_id": run_id,
        "runId": run_id,
        "task_id": task_id,
        "taskId": task_id,
        "status": text(_row_value(row, "status")),
        "result": text(_row_value(row, "result")),
        "summary": text(_row_value(row, "summary")),
        "payload": payload if isinstance(payload, dict) else {},
        "artifact_refs": artifact_refs,
        "artifactRefs": artifact_refs,
        "next_context": next_context,
        "nextContext": next_context,
        "output_contract": output_contract,
        "outputContract": output_contract,
        "source": text(_row_value(row, "source")),
        "confidence": float(_row_value(row, "confidence", 0) or 0),
        "visibility": text(_row_value(row, "visibility")),
        "created_at": float(_row_value(row, "created_at", 0) or 0),
        "createdAt": float(_row_value(row, "created_at", 0) or 0),
        "updated_at": float(_row_value(row, "updated_at", 0) or 0),
        "updatedAt": float(_row_value(row, "updated_at", 0) or 0),
    }


def upsert_team_mission_deliverable(
    db: Any,
    *,
    deliverable_id: str = "",
    mission_id: str,
    node_id: str,
    run_id: str,
    task_id: str = "",
    status: str = "completed",
    result: str = "",
    summary: str = "",
    payload: Dict[str, Any] | None = None,
    artifact_refs: List[Dict[str, Any]] | None = None,
    next_context: Dict[str, Any] | None = None,
    output_contract: Dict[str, Any] | None = None,
    source: str = DELIVERABLE_SOURCE_AUTHORITATIVE,
    confidence: float = 0.9,
    visibility: str = DELIVERABLE_VISIBILITY_HANDOFF,
    created_at: float | None = None,
    updated_at: float | None = None,
) -> Dict[str, Any]:
    mission_id = text(mission_id)
    node_id = text(node_id)
    run_id = text(run_id)
    if not mission_id or not node_id or not run_id:
        return {}
    payload = _record(payload)
    artifacts = dedupe_artifact_refs(_records(artifact_refs or payload.get("artifact_refs") or payload.get("artifactRefs")))
    next_context = _record(next_context or payload.get("next_context") or payload.get("nextContext"))
    output_contract = _record(output_contract)
    normalized_status = _normalize_status(status or payload.get("status"))
    normalized_result = text(result or payload.get("result"))
    normalized_source = text(source) or DELIVERABLE_SOURCE_AUTHORITATIVE
    normalized_visibility = text(visibility) or DELIVERABLE_VISIBILITY_HANDOFF
    normalized_summary = _bounded_summary(
        summary or payload.get("summary") or payload.get("finalSummary"),
        fallback=f"{node_id}: {normalized_status}",
    )
    if not normalized_summary:
        return {}
    normalized_id = text(deliverable_id) or stable_id(
        "deliverable",
        mission_id,
        node_id,
        run_id,
        text(task_id),
        normalized_source,
    )
    now = time.time()
    created = float(created_at or now)
    updated = float(updated_at or now)

    def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
        existing = conn.execute(
            "SELECT created_at FROM team_mission_deliverables WHERE deliverable_id = ?",
            (normalized_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO team_mission_deliverables (
                deliverable_id, mission_id, node_id, run_id, task_id,
                status, result, summary, payload_json, artifact_refs_json,
                next_context_json, output_contract_json, source, confidence,
                visibility, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(deliverable_id) DO UPDATE SET
                mission_id = excluded.mission_id,
                node_id = excluded.node_id,
                run_id = excluded.run_id,
                task_id = excluded.task_id,
                status = excluded.status,
                result = excluded.result,
                summary = excluded.summary,
                payload_json = excluded.payload_json,
                artifact_refs_json = excluded.artifact_refs_json,
                next_context_json = excluded.next_context_json,
                output_contract_json = excluded.output_contract_json,
                source = excluded.source,
                confidence = excluded.confidence,
                visibility = excluded.visibility,
                updated_at = excluded.updated_at
            """,
            (
                normalized_id,
                mission_id,
                node_id,
                run_id,
                text(task_id),
                normalized_status,
                normalized_result,
                normalized_summary,
                _json_dumps(payload),
                _json_dumps(artifacts),
                _json_dumps(next_context),
                _json_dumps(output_contract),
                normalized_source,
                _confidence(confidence),
                normalized_visibility,
                float(_row_value(existing, "created_at", created) or created),
                updated,
            ),
        )
        row = conn.execute(
            "SELECT * FROM team_mission_deliverables WHERE deliverable_id = ?",
            (normalized_id,),
        ).fetchone()
        node_row = conn.execute(
            "SELECT metadata_json FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
            (mission_id, node_id),
        ).fetchone()
        node_metadata = _record(_json_loads(_row_value(node_row, "metadata_json", ""), {}))
        node_metadata.update({
            "last_deliverable_id": normalized_id,
            "last_deliverable_status": normalized_status,
            "last_deliverable_result": normalized_result,
            "last_deliverable_source": normalized_source,
            "last_deliverable_run_id": run_id,
        })
        conn.execute(
            """
            UPDATE team_mission_nodes
               SET metadata_json = ?,
                   updated_at = ?
             WHERE mission_id = ? AND node_id = ?
            """,
            (_json_dumps(node_metadata), updated, mission_id, node_id),
        )
        return row_to_deliverable(row)

    return db._execute_write(_do)


def list_team_mission_deliverables(
    db: Any,
    *,
    mission_id: str = "",
    node_id: str = "",
    run_id: str = "",
    task_id: str = "",
    statuses: List[str] | None = None,
    sources: List[str] | None = None,
    visibility: List[str] | None = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if text(mission_id):
        clauses.append("mission_id = ?")
        params.append(text(mission_id))
    if text(node_id):
        clauses.append("node_id = ?")
        params.append(text(node_id))
    if text(run_id):
        clauses.append("run_id = ?")
        params.append(text(run_id))
    if text(task_id):
        clauses.append("task_id = ?")
        params.append(text(task_id))
    normalized_statuses = [text(item) for item in (statuses or []) if text(item)]
    if normalized_statuses:
        clauses.append(f"status IN ({','.join('?' for _ in normalized_statuses)})")
        params.extend(normalized_statuses)
    normalized_sources = [text(item) for item in (sources or []) if text(item)]
    if normalized_sources:
        clauses.append(f"source IN ({','.join('?' for _ in normalized_sources)})")
        params.extend(normalized_sources)
    normalized_visibility = [text(item) for item in (visibility or []) if text(item)]
    if normalized_visibility:
        clauses.append(f"visibility IN ({','.join('?' for _ in normalized_visibility)})")
        params.extend(normalized_visibility)
    where = " AND ".join(clauses) if clauses else "1 = 1"
    params.append(max(1, min(int(limit or 200), 1000)))
    with db._lock:
        rows = db._conn.execute(
            f"""
            SELECT *
            FROM team_mission_deliverables
            WHERE {where}
            ORDER BY updated_at DESC, created_at DESC, deliverable_id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [item for item in (row_to_deliverable(row) for row in rows) if item]


def get_team_mission_deliverable(db: Any, deliverable_id: str) -> Dict[str, Any]:
    deliverable_id = text(deliverable_id)
    if not deliverable_id:
        return {}
    with db._lock:
        row = db._conn.execute(
            "SELECT * FROM team_mission_deliverables WHERE deliverable_id = ?",
            (deliverable_id,),
        ).fetchone()
    return row_to_deliverable(row)


def latest_team_mission_deliverable_for_run(db: Any, run_id: str) -> Dict[str, Any]:
    items = list_team_mission_deliverables(db, run_id=run_id, limit=1)
    return items[0] if items else {}


def team_mission_run_has_deliverable(db: Any, run_id: str) -> bool:
    return bool(latest_team_mission_deliverable_for_run(db, run_id))
