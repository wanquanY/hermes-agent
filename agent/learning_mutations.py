"""Profile-scoped mutation service for canonical learning graph nodes."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote

from agent.learning_graph import (
    MEMORY_FILES,
    LearningGraphService,
    build_memory_cards,
    canonical_skill_node_id,
    memory_revision,
    profile_scope_key,
)
from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


@contextmanager
def _scoped_home(home: Path) -> Iterator[None]:
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def parse_node_kind(node_id: str) -> str:
    return "memory" if str(node_id or "").startswith("memory:") else "skill"


class LearningMutationService:
    """Inspect and mutate learning nodes owned by one profile home."""

    def __init__(self, hermes_home: str | Path):
        self.home = Path(hermes_home).expanduser().resolve()

    def _memory_target(self, source: str) -> str:
        if source == "memory":
            return "memory"
        if source == "profile":
            return "user"
        raise ValueError(f"bad memory source: {source!r}")

    def _memory_store(self):
        """Build MemoryStore with this profile's configured budgets."""
        from tools.memory_tool import MemoryStore

        memory_config: dict[str, Any] = {}
        with _scoped_home(self.home):
            try:
                from hermes_cli.config import load_config

                config = load_config()
                raw = config.get("memory") if isinstance(config, dict) else {}
                memory_config = raw if isinstance(raw, dict) else {}
            except Exception:
                memory_config = {}
        return MemoryStore(
            memory_char_limit=int(memory_config.get("memory_char_limit", 2200)),
            user_char_limit=int(memory_config.get("user_char_limit", 1375)),
        )

    def _resolve_memory(self, node_id: str) -> dict[str, Any]:
        parts = str(node_id or "").split(":")
        if parts[0] != "memory" or len(parts) not in {3, 4, 5}:
            raise ValueError(f"bad memory node id: {node_id!r}")
        if len(parts) == 5:
            if parts[1] != profile_scope_key(self.home):
                raise ValueError("learning node belongs to a different profile")
            source, revision, occurrence_raw = parts[2], parts[3], parts[4]
        elif len(parts) == 4:
            source, revision, occurrence_raw = parts[1], parts[2], parts[3]
        else:
            source, revision, occurrence_raw = parts[1], "", ""
        if source not in MEMORY_FILES:
            raise ValueError(f"bad memory node id: {node_id!r}")
        cards = build_memory_cards(self.home)
        if len(parts) == 3:
            try:
                legacy_index = int(parts[2])
            except ValueError as exc:
                raise ValueError(f"bad memory node id: {node_id!r}") from exc
            matches = [
                card
                for card in cards
                if card["source"] == source and card["legacyIndex"] == legacy_index
            ]
        else:
            try:
                occurrence = int(occurrence_raw)
            except ValueError as exc:
                raise ValueError(f"bad memory node id: {node_id!r}") from exc
            matches = [
                card
                for card in cards
                if card["source"] == source
                and card["revision"] == revision
                and card["occurrence"] == occurrence
            ]
        if len(matches) != 1:
            raise ValueError("memory node is stale or no longer exists; refresh the learning graph")

        card = matches[0]
        from tools.memory_tool import MemoryStore

        path = self.home / "memories" / MEMORY_FILES[source]
        entries = MemoryStore._read_file(path)
        index = int(card["localIndex"])
        if not 0 <= index < len(entries):
            raise ValueError("memory node is stale or no longer exists; refresh the learning graph")
        content = entries[index]
        if memory_revision(content) != card["revision"]:
            raise ValueError("memory node changed; refresh the learning graph")
        return {**card, "content": content, "target": self._memory_target(source)}

    def _resolve_skill_name(self, node_id: str) -> str:
        raw = str(node_id or "").strip()
        if raw.startswith("skill:"):
            payload = raw.removeprefix("skill:")
            scope, separator, encoded = payload.partition(":")
            if separator:
                if scope != profile_scope_key(self.home):
                    raise ValueError("learning node belongs to a different profile")
                name = unquote(encoded)
            else:
                name = unquote(scope)
        else:
            name = raw
        if not name:
            raise ValueError("skill node id required")
        graph = LearningGraphService(self.home).build()
        valid = {
            str(node.get("entityId") or "")
            for node in graph["nodes"]
            if node.get("kind") == "skill"
        }
        if name not in valid:
            raise ValueError(f"learned skill {name!r} not found in this profile")
        return name

    def detail(self, node_id: str) -> dict[str, Any]:
        try:
            if parse_node_kind(node_id) == "memory":
                card = self._resolve_memory(node_id)
                return {
                    "ok": True,
                    "kind": "memory",
                    "id": card["id"],
                    "label": card["title"],
                    "content": card["content"],
                    "revision": card["revision"],
                    "source": card["source"],
                }
            name = self._resolve_skill_name(node_id)
            with _scoped_home(self.home):
                from tools.skill_manager_tool import _find_skill

                found = _find_skill(name)
            if not found:
                raise ValueError(f"learned skill {name!r} not found")
            skill_dir = Path(str(found.get("path") or "")).resolve()
            profile_skills = (self.home / "skills").resolve()
            try:
                skill_dir.relative_to(profile_skills)
            except ValueError as exc:
                raise ValueError("refusing to mutate a non-profile skill") from exc
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                raise ValueError(f"SKILL.md missing for {name!r}")
            return {
                "ok": True,
                "kind": "skill",
                "id": canonical_skill_node_id(name, profile_scope_key(self.home)),
                "label": name,
                "content": skill_md.read_text(encoding="utf-8"),
            }
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}

    def delete(self, node_id: str) -> dict[str, Any]:
        try:
            if parse_node_kind(node_id) == "memory":
                card = self._resolve_memory(node_id)
                with _scoped_home(self.home):
                    store = self._memory_store()
                    result = store.remove_exact(
                        card["target"],
                        card["content"],
                        occurrence=int(card["exactOccurrence"]),
                    )
                if not result.get("success"):
                    return {"ok": False, "message": result.get("error", "delete failed")}
                return {
                    "ok": True,
                    "message": f"deleted memory from {MEMORY_FILES[card['source']]}",
                }

            name = self._resolve_skill_name(node_id)
            with _scoped_home(self.home):
                from tools import skill_usage

                if skill_usage.get_record(name).get("pinned"):
                    return {
                        "ok": False,
                        "message": f"{name!r} is pinned; unpin it before deleting",
                    }
                ok, message = skill_usage.archive_skill(name)
                if ok:
                    _clear_skill_cache()
            return {
                "ok": bool(ok),
                "message": (
                    f"archived {name!r}; restore with: hermes curator restore {name}"
                    if ok
                    else message
                ),
            }
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}

    def edit(self, node_id: str, content: str) -> dict[str, Any]:
        body = str(content or "").strip()
        if not body:
            return {"ok": False, "message": "empty content; use delete to remove the node"}
        try:
            if parse_node_kind(node_id) == "memory":
                card = self._resolve_memory(node_id)
                with _scoped_home(self.home):
                    store = self._memory_store()
                    result = store.replace_exact(
                        card["target"],
                        card["content"],
                        body,
                        occurrence=int(card["exactOccurrence"]),
                    )
                if not result.get("success"):
                    return {"ok": False, "message": result.get("error", "edit failed")}
                graph = LearningGraphService(self.home).build()
                replacement = next(
                    (
                        node
                        for node in graph["nodes"]
                        if node.get("kind") == "memory"
                        and node.get("revision") == memory_revision(body)
                        and node.get("memorySource") == card["source"]
                    ),
                    None,
                )
                return {
                    "ok": True,
                    "message": f"updated memory in {MEMORY_FILES[card['source']]}",
                    "node": replacement,
                }

            name = self._resolve_skill_name(node_id)
            with _scoped_home(self.home):
                from tools.skill_manager_tool import _edit_skill

                result = _edit_skill(name, body)
                if result.get("success"):
                    _clear_skill_cache()
            return {
                "ok": bool(result.get("success")),
                "message": f"updated {name!r}"
                if result.get("success")
                else result.get("error", "edit failed"),
            }
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}


def _clear_skill_cache() -> None:
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache

        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        pass


def node_detail(node_id: str, *, hermes_home: str | Path | None = None) -> dict[str, Any]:
    return LearningMutationService(hermes_home or get_hermes_home()).detail(node_id)


def delete_node(node_id: str, *, hermes_home: str | Path | None = None) -> dict[str, Any]:
    return LearningMutationService(hermes_home or get_hermes_home()).delete(node_id)


def edit_node(
    node_id: str,
    content: str,
    *,
    hermes_home: str | Path | None = None,
) -> dict[str, Any]:
    return LearningMutationService(hermes_home or get_hermes_home()).edit(node_id, content)
