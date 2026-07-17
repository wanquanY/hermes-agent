"""Canonical profile-scoped learning graph.

The graph is a read model over the existing memory and skill stores.  It does
not persist a second copy of either domain.  Every consumer (CLI, gateway and
desktop) receives the same nodes, edges and timeline from
``LearningGraphService``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import quote

from hermes_constants import get_hermes_home

GRAPH_SCHEMA_VERSION = 1
MEMORY_FILES = {"memory": "MEMORY.md", "profile": "USER.md"}
_EXCLUDED_SKILL_PARTS = {".archive", ".hub", ".git", "node_modules"}


@dataclass(frozen=True)
class SkillNode:
    name: str
    category: str
    source: str = "profile"
    timestamp: Optional[int] = None
    timestamp_source: str = "skill_mtime"
    use_count: int = 0
    state: str = "active"
    created_by: Optional[str] = None
    pinned: bool = False
    related: list[str] = field(default_factory=list)


def profile_scope_key(home: str | Path) -> str:
    resolved = Path(home).expanduser().resolve()
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]


def canonical_skill_node_id(name: str, scope_key: str = "") -> str:
    encoded = quote(str(name).strip(), safe="._-")
    return f"skill:{scope_key}:{encoded}" if scope_key else f"skill:{encoded}"


def _normalize_memory_content(content: str) -> str:
    return "\n".join(line.rstrip() for line in str(content or "").strip().splitlines())


def memory_revision(content: str) -> str:
    normalized = _normalize_memory_content(content)
    return hashlib.blake2b(normalized.encode("utf-8"), digest_size=12).hexdigest()


def canonical_memory_node_id(
    source: str,
    content: str,
    occurrence: int = 0,
    *,
    scope_key: str = "",
) -> str:
    if source not in MEMORY_FILES:
        raise ValueError(f"unknown memory source: {source!r}")
    suffix = f"{source}:{memory_revision(content)}:{max(0, int(occurrence))}"
    return f"memory:{scope_key}:{suffix}" if scope_key else f"memory:{suffix}"


def _frontmatter(text: str) -> dict[str, Any]:
    try:
        from agent.skill_utils import parse_frontmatter

        metadata, _ = parse_frontmatter(text)
        return metadata if isinstance(metadata, dict) else {}
    except Exception:
        return {}


def _hermes_meta(frontmatter: dict[str, Any]) -> dict[str, Any]:
    metadata = frontmatter.get("metadata")
    hermes = metadata.get("hermes") if isinstance(metadata, dict) else None
    return hermes if isinstance(hermes, dict) else {}


def _related(frontmatter: dict[str, Any]) -> list[str]:
    raw = frontmatter.get("related_skills") or _hermes_meta(frontmatter).get(
        "related_skills"
    )
    if isinstance(raw, list):
        return [str(value).strip() for value in raw if str(value).strip()]
    if isinstance(raw, str):
        return [value.strip() for value in raw.strip("[]").split(",") if value.strip()]
    return []


def _category(frontmatter: dict[str, Any], skill_md: Path, root: Path) -> str:
    explicit = frontmatter.get("category") or _hermes_meta(frontmatter).get("category")
    if explicit:
        return str(explicit)
    try:
        relative = skill_md.relative_to(root)
    except ValueError:
        relative = skill_md
    return relative.parts[-3] if len(relative.parts) >= 3 else "general"


def _iter_skill_files(
    roots: Iterable[tuple[str, Path]],
) -> Iterator[tuple[str, Path, Path]]:
    for source, raw_root in roots:
        root = Path(raw_root)
        if not root.exists():
            continue
        try:
            candidates = sorted(root.rglob("SKILL.md"))
        except OSError:
            continue
        for path in candidates:
            if any(part in _EXCLUDED_SKILL_PARTS for part in path.parts):
                continue
            yield source, root, path


def _to_int_ts(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            parsed = float(value)
        else:
            raw = str(value).strip()
            if not raw:
                return None
            try:
                parsed = float(raw)
            except ValueError:
                date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                parsed = date.timestamp()
        if parsed > 100_000_000_000:
            parsed /= 1000
        return int(parsed) if parsed > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _usage_timestamp(record: dict[str, Any]) -> tuple[Optional[int], str]:
    for key in (
        "last_activity_at",
        "last_used_at",
        "last_viewed_at",
        "last_patched_at",
        "created_at",
    ):
        timestamp = _to_int_ts(record.get(key))
        if timestamp is not None:
            return timestamp, f"skill_usage.{key}"
    return None, "skill_mtime"


def _load_usage(home: Path | None = None) -> dict[str, dict[str, Any]]:
    path = Path(home or get_hermes_home()) / "skills" / ".usage.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(name): record
        for name, record in payload.items()
        if isinstance(record, dict)
    }


def _build_skill_nodes(
    roots: list[tuple[str, Path]],
    usage: dict[str, dict[str, Any]],
) -> dict[str, SkillNode]:
    nodes: dict[str, SkillNode] = {}
    for source, root, skill_md in _iter_skill_files(roots):
        try:
            frontmatter = _frontmatter(skill_md.read_text(encoding="utf-8")[:4000])
            file_timestamp = _to_int_ts(skill_md.stat().st_mtime)
        except OSError:
            continue
        name = str(frontmatter.get("name") or skill_md.parent.name).strip()
        if not name or name in nodes:
            continue
        record = usage.get(name, {})
        usage_timestamp, timestamp_source = _usage_timestamp(record)
        nodes[name] = SkillNode(
            name=name,
            category=_category(frontmatter, skill_md, root),
            source=source,
            timestamp=usage_timestamp or file_timestamp,
            timestamp_source=timestamp_source if usage_timestamp else "skill_mtime",
            use_count=int(record.get("use_count", 0) or 0),
            state=str(record.get("state", "active") or "active"),
            created_by=(str(record.get("created_by")).strip() or None)
            if record.get("created_by") is not None
            else None,
            pinned=bool(record.get("pinned", False)),
            related=_related(frontmatter),
        )
    return nodes


def build_skill_nodes(skill_roots: list[tuple[str, Path]]) -> dict[str, SkillNode]:
    """Compatibility helper used by graph tests and downstream tooling."""
    return _build_skill_nodes(skill_roots, _load_usage())


def build_edges(nodes: dict[str, SkillNode]) -> list[tuple[str, str]]:
    """Return de-duplicated undirected related-skill edges."""
    seen: set[tuple[str, str]] = set()
    edges: list[tuple[str, str]] = []
    for node in nodes.values():
        for target in node.related:
            if target not in nodes or target == node.name:
                continue
            edge = tuple(sorted((node.name, target)))
            if edge not in seen:
                seen.add(edge)
                edges.append(edge)
    return edges


def density_stats(
    nodes: dict[str, SkillNode], edges: list[tuple[str, str]]
) -> dict[str, Any]:
    linked = {endpoint for edge in edges for endpoint in edge}
    categories: dict[str, int] = {}
    for node in nodes.values():
        categories[node.category] = categories.get(node.category, 0) + 1
    count = len(nodes)
    return {
        "nodes": count,
        "related_edges": len(edges),
        "edges_per_node": round(len(edges) / count, 3) if count else 0.0,
        "linked_nodes": len(linked),
        "isolated_pct": round(100 * (count - len(linked)) / count, 1) if count else 0.0,
        "categories": len(categories),
        "agent_created": sum(1 for node in nodes.values() if node.created_by == "agent"),
        "used": sum(1 for node in nodes.values() if node.use_count > 0),
        "top_categories": sorted(categories.items(), key=lambda item: (-item[1], item[0]))[:8],
    }


def build_memory_cards(home: Path) -> list[dict[str, Any]]:
    from tools.memory_tool import MemoryStore

    resolved_home = Path(home).expanduser().resolve()
    base = resolved_home / "memories"
    scope_key = profile_scope_key(resolved_home)
    cards: list[dict[str, Any]] = []
    occurrence_by_revision: dict[tuple[str, str], int] = {}
    occurrence_by_content: dict[tuple[str, str], int] = {}
    for source, filename in MEMORY_FILES.items():
        path = base / filename
        try:
            entries = MemoryStore._read_file(path)
            file_timestamp = _to_int_ts(path.stat().st_mtime)
        except OSError:
            continue
        for local_index, body in enumerate(entries):
            revision = memory_revision(body)
            occurrence_key = (source, revision)
            occurrence = occurrence_by_revision.get(occurrence_key, 0)
            occurrence_by_revision[occurrence_key] = occurrence + 1
            exact_key = (source, body)
            exact_occurrence = occurrence_by_content.get(exact_key, 0)
            occurrence_by_content[exact_key] = exact_occurrence + 1
            first_line = body.splitlines()[0].strip().lstrip("# ").strip()
            cards.append(
                {
                    "id": canonical_memory_node_id(
                        source, body, occurrence, scope_key=scope_key
                    ),
                    "source": source,
                    "sourceFile": filename,
                    "localIndex": local_index,
                    "legacyIndex": len(cards),
                    "occurrence": occurrence,
                    "exactOccurrence": exact_occurrence,
                    "revision": revision,
                    "timestamp": file_timestamp,
                    "timestampSource": "memory_file_mtime",
                    "title": f"{first_line[:80]}…" if len(first_line) > 80 else first_line,
                    "body": body[:1200],
                }
            )
    return cards


def _memory_cards() -> list[dict[str, Any]]:
    """Upstream-compatible wrapper bound to the active Hermes home."""
    return build_memory_cards(get_hermes_home())


def _tokenize(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\w-]+", str(text or "").lower(), flags=re.UNICODE)
        if len(token) >= 3
    }


def _memory_skill_edges(
    memory_cards: list[dict[str, Any]], skills: list[SkillNode]
) -> list[tuple[str, str]]:
    skill_metadata = [
        (skill, _tokenize(skill.name), skill.name.lower()) for skill in skills
    ]
    edges: list[tuple[str, str]] = []
    for card in memory_cards:
        text = f"{card.get('title', '')}\n{card.get('body', '')}".lower()
        tokens = _tokenize(text)
        scored: list[tuple[int, str]] = []
        for skill, skill_tokens, skill_name in skill_metadata:
            score = (6 if skill_name in text else 0) + len(skill_tokens & tokens)
            if score > 0:
                scored.append((score, skill.name))
        for _, name in sorted(scored, key=lambda item: (-item[0], item[1]))[:4]:
            edges.append((str(card["id"]), name))
    return edges


class LearningGraphService:
    """Build the canonical learning graph for exactly one Hermes profile."""

    def __init__(self, hermes_home: str | Path, *, repo_root: str | Path | None = None):
        self.home = Path(hermes_home).expanduser().resolve()
        self.repo_root = Path(repo_root or Path(__file__).resolve().parent.parent).resolve()

    def _skill_roots(self) -> list[tuple[str, Path]]:
        return [
            ("base", self.repo_root / "skills"),
            ("profile", self.home / "skills"),
        ]

    def build(self) -> dict[str, Any]:
        scope_key = profile_scope_key(self.home)
        all_skills = _build_skill_nodes(self._skill_roots(), _load_usage(self.home))
        learned_skills = {
            name: node
            for name, node in all_skills.items()
            if node.source != "base"
            and (node.created_by == "agent" or node.use_count > 0)
        }
        related_edges = build_edges(learned_skills)
        memory_cards = build_memory_cards(self.home)

        nodes: list[dict[str, Any]] = []
        for node in learned_skills.values():
            nodes.append(
                {
                    "id": canonical_skill_node_id(node.name, scope_key),
                    "entityId": node.name,
                    "label": node.name,
                    "kind": "skill",
                    "timestamp": node.timestamp,
                    "timestampSource": node.timestamp_source,
                    "category": node.category,
                    "source": node.source,
                    "useCount": node.use_count,
                    "state": node.state,
                    "createdBy": node.created_by,
                    "pinned": node.pinned,
                }
            )
        for card in memory_cards:
            nodes.append(
                {
                    "id": card["id"],
                    "entityId": card["id"],
                    "label": card["title"],
                    "kind": "memory",
                    "memorySource": card["source"],
                    "source": card["sourceFile"],
                    "revision": card["revision"],
                    "timestamp": card["timestamp"],
                    "timestampSource": card["timestampSource"],
                    "category": "memory",
                    "useCount": 0,
                    "state": "active",
                    "createdBy": "memory",
                    "pinned": False,
                }
            )

        edges = [
            {
                "source": canonical_skill_node_id(left, scope_key),
                "target": canonical_skill_node_id(right, scope_key),
                "kind": "related_skill",
            }
            for left, right in related_edges
        ]
        edges.extend(
            {
                "source": left,
                "target": canonical_skill_node_id(right, scope_key),
                "kind": "memory_skill",
            }
            for left, right in _memory_skill_edges(
                memory_cards, list(learned_skills.values())
            )
        )

        clusters: dict[str, int] = {}
        for node in nodes:
            category = str(node.get("category") or "general")
            clusters[category] = clusters.get(category, 0) + 1

        ordered_nodes = sorted(
            nodes,
            key=lambda node: (
                int(node.get("timestamp") or 0),
                0 if node.get("kind") == "memory" else 1,
                str(node.get("id") or ""),
            ),
        )
        timeline = [
            {
                "sequence": sequence,
                "nodeId": node["id"],
                "kind": node["kind"],
                "label": node["label"],
                "category": node["category"],
                "timestamp": node.get("timestamp"),
                "timestampSource": node.get("timestampSource"),
            }
            for sequence, node in enumerate(ordered_nodes)
        ]
        stats = density_stats(learned_skills, related_edges)
        stats.update(
            {
                "memory_nodes": len(memory_cards),
                "memory_skill_edges": sum(
                    1 for edge in edges if edge["kind"] == "memory_skill"
                ),
                "learned_skills": len(learned_skills),
                "total_nodes": len(nodes),
            }
        )
        return {
            "schemaVersion": GRAPH_SCHEMA_VERSION,
            "scope": {"kind": "profile", "key": scope_key},
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "nodes": nodes,
            "edges": edges,
            "clusters": [
                {"category": category, "count": count}
                for category, count in sorted(
                    clusters.items(), key=lambda item: (-item[1], item[0])
                )
            ],
            "memory": memory_cards,
            "timeline": timeline,
            "stats": stats,
        }


def build_learning_graph(hermes_home: str | Path | None = None) -> dict[str, Any]:
    return LearningGraphService(hermes_home or get_hermes_home()).build()


if __name__ == "__main__":
    print(json.dumps(build_learning_graph(), ensure_ascii=False, indent=2))
