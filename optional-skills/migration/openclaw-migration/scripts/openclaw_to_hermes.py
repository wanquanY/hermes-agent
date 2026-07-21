#!/usr/bin/env python3
"""OpenClaw to Hermes migration CLI and compatibility API."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _openclaw_migration_core import (  # noqa: E402
    DEFAULT_MEMORY_CHAR_LIMIT,
    DEFAULT_USER_CHAR_LIMIT,
    ENTRY_DELIMITER,
    MIGRATION_OPTION_METADATA,
    MIGRATION_PRESETS,
    REASON_BLOCKED_BY_APPLY_CONFLICT,
    REDACTED_MIGRATION_VALUE,
    SKILL_CATEGORY_DESCRIPTION,
    SKILL_CATEGORY_DIRNAME,
    SKILL_CONFLICT_MODES,
    STATUS_ARCHIVED,
    STATUS_CONFLICT,
    STATUS_ERROR,
    STATUS_MIGRATED,
    STATUS_PLANNED,
    STATUS_SKIPPED,
    SUPPORTED_SECRET_TARGETS,
    WORKSPACE_INSTRUCTIONS_FILENAME,
    ItemResult,
    backup_existing,
    dump_yaml_file,
    ensure_parent,
    extract_markdown_entries,
    load_yaml_file,
    merge_entries,
    parse_env_file,
    parse_existing_memory_entries,
    read_text,
    rebrand_text,
    redact_migration_value,
    resolve_secret_input,
    resolve_selected_options,
    save_env_file,
    sha256_file,
    write_report,
    yaml,
)
from _openclaw_migration_extended import MigratorExtendedMixin  # noqa: E402

__all__ = [
    "MIGRATION_PRESETS",
    "REASON_BLOCKED_BY_APPLY_CONFLICT",
    "REDACTED_MIGRATION_VALUE",
    "SKILL_CATEGORY_DIRNAME",
    "STATUS_ARCHIVED",
    "STATUS_CONFLICT",
    "STATUS_ERROR",
    "STATUS_MIGRATED",
    "STATUS_SKIPPED",
    "ItemResult",
    "Migrator",
    "extract_markdown_entries",
    "main",
    "merge_entries",
    "parse_args",
    "rebrand_text",
    "redact_migration_value",
    "resolve_selected_options",
    "write_report",
]


class Migrator(MigratorExtendedMixin):
    def __init__(
        self,
        source_root: Path,
        target_root: Path,
        execute: bool,
        workspace_target: Optional[Path],
        overwrite: bool,
        migrate_secrets: bool,
        output_dir: Optional[Path],
        selected_options: Optional[set[str]] = None,
        preset_name: str = "",
        skill_conflict_mode: str = "skip",
    ):
        self.source_root = source_root
        self.target_root = target_root
        self.execute = execute
        self.workspace_target = workspace_target
        self.overwrite = overwrite
        self.migrate_secrets = migrate_secrets
        self.selected_options = set(selected_options or MIGRATION_OPTION_METADATA.keys())
        self.preset_name = preset_name.strip().lower()
        self.skill_conflict_mode = skill_conflict_mode.strip().lower() or "skip"
        self.timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        self.output_dir = output_dir or (
            target_root / "migration" / "openclaw" / self.timestamp if execute else None
        )
        self.archive_dir = self.output_dir / "archive" if self.output_dir else None
        self.backup_dir = self.output_dir / "backups" if self.output_dir else None
        self.overflow_dir = self.output_dir / "overflow" if self.output_dir else None
        self.items: List[ItemResult] = []
        # Once a config.yaml write hits conflict/error mid-run, later
        # config.yaml writes are deliberately short-circuited to avoid
        # leaving config in a partially-written state.  Modelled on
        # OpenClaw's extensions/migrate-hermes/apply.ts "blocked by earlier
        # apply conflict" sequencing.
        self._config_apply_blocked: bool = False

        # Resolve the configured workspace directory from openclaw.json.
        # Many users (especially those who started before the OpenClaw rebrand)
        # have a custom workspace path (e.g. ~/clawd/) that differs from the
        # default ~/.openclaw/workspace/.  Reading agents.defaults.workspace
        # lets source_candidate() find files in the actual workspace.
        self._custom_workspace: Optional[Path] = None
        oc_config = self.load_openclaw_config()
        ws = (oc_config.get("agents", {}).get("defaults", {}).get("workspace") or "").strip()
        if ws:
            ws_path = Path(ws).expanduser().resolve()
            # Only use it if it exists and is outside the source_root tree
            # (otherwise the standard relative-path logic already covers it).
            if ws_path.is_dir():
                try:
                    ws_path.relative_to(self.source_root)
                except ValueError:
                    # ws_path is outside source_root — use it as custom workspace
                    self._custom_workspace = ws_path

        config = load_yaml_file(self.target_root / "config.yaml")
        mem_cfg = config.get("memory", {}) if isinstance(config.get("memory"), dict) else {}
        self.memory_limit = int(mem_cfg.get("memory_char_limit", DEFAULT_MEMORY_CHAR_LIMIT))
        self.user_limit = int(mem_cfg.get("user_char_limit", DEFAULT_USER_CHAR_LIMIT))

        if self.skill_conflict_mode not in SKILL_CONFLICT_MODES:
            raise ValueError(
                "Unknown skill conflict mode: "
                + self.skill_conflict_mode
                + ". Valid modes: "
                + ", ".join(sorted(SKILL_CONFLICT_MODES))
            )

    def is_selected(self, option_id: str) -> bool:
        return option_id in self.selected_options

    # Option ids that mutate the Hermes config.yaml file.  Once any one of
    # them records a conflict/error on config.yaml, subsequent ones are
    # short-circuited to avoid partial writes.  Keep in sync with methods
    # that call load_yaml_file(target_root / "config.yaml") + dump_yaml_file.
    _CONFIG_MUTATING_OPTIONS = frozenset({
        "model-config",
        "tts-config",
        "mcp-servers",
        "plugins-config",
        "cron-jobs",
        "hooks-config",
        "agent-config",
        "gateway-config",
        "session-config",
        "full-providers",
        "deep-channels",
        "browser-config",
        "tools-config",
        "approvals-config",
        "memory-backend",
        "skills-config",
        "ui-identity",
        "logging-config",
        "command-allowlist",
    })

    def record(
        self,
        kind: str,
        source: Optional[Path],
        destination: Optional[Path],
        status: str,
        reason: str = "",
        **details: Any,
    ) -> None:
        sensitive = bool(details.pop("sensitive", False))
        self.items.append(
            ItemResult(
                kind=kind,
                source=str(source) if source else None,
                destination=str(destination) if destination else None,
                status=status,
                reason=reason,
                details=details,
                sensitive=sensitive,
            )
        )
        # Flip the config-block flag when a conflict/error occurs on a
        # config.yaml write.  Later config-mutating options will skip rather
        # than attempting a partial write.
        if status in {STATUS_CONFLICT, STATUS_ERROR} and destination is not None:
            dest_str = str(destination)
            if dest_str.endswith("config.yaml") or dest_str.endswith("config.yml"):
                self._config_apply_blocked = True

    def source_candidate(self, *relative_paths: str) -> Optional[Path]:
        for rel in relative_paths:
            candidate = self.source_root / rel
            if candidate.exists():
                return candidate
            # OpenClaw renamed workspace/ to workspace-main/ (and workspace-{agentId}
            # for multi-agent).  Try the new path as a fallback.
            if rel.startswith("workspace/"):
                suffix = rel[len("workspace/"):]
                for variant in ("workspace-main", "workspace-assistant"):
                    alt = self.source_root / variant / suffix
                    if alt.exists():
                        return alt
            elif rel.startswith("workspace.default/"):
                suffix = rel[len("workspace.default/"):]
                alt = self.source_root / "workspace-main" / suffix
                if alt.exists():
                    return alt

        # Final fallback: check the configured workspace directory from
        # agents.defaults.workspace in openclaw.json.  Users who started
        # before the OpenClaw rebrand (when the project was named clawd /
        # clawdbot) often have a custom workspace path outside ~/.openclaw/.
        if self._custom_workspace:
            for rel in relative_paths:
                # Strip the leading "workspace/" or "workspace.default/"
                # prefix to get the bare filename/subpath.
                for prefix in ("workspace/", "workspace.default/"):
                    if rel.startswith(prefix):
                        suffix = rel[len(prefix):]
                        alt = self._custom_workspace / suffix
                        if alt.exists():
                            return alt
                        break

        return None

    def resolve_skill_destination(self, destination: Path) -> Path:
        if self.skill_conflict_mode != "rename" or not destination.exists():
            return destination

        suffix = "-imported"
        candidate = destination.with_name(destination.name + suffix)
        counter = 2
        while candidate.exists():
            candidate = destination.with_name(f"{destination.name}{suffix}-{counter}")
            counter += 1
        return candidate

    def migrate(self) -> Dict[str, Any]:
        if not self.source_root.exists():
            self.record("source", self.source_root, None, "error", "OpenClaw directory does not exist")
            return self.build_report()

        config = self.load_openclaw_config()

        self.run_if_selected("soul", self.migrate_soul)
        self.run_if_selected("workspace-agents", self.migrate_workspace_agents)
        self.run_if_selected(
            "memory",
            lambda: self.migrate_memory(
                self.source_candidate("workspace/MEMORY.md", "workspace.default/MEMORY.md"),
                self.target_root / "memories" / "MEMORY.md",
                self.memory_limit,
                kind="memory",
            ),
        )
        self.run_if_selected(
            "user-profile",
            lambda: self.migrate_memory(
                self.source_candidate("workspace/USER.md", "workspace.default/USER.md"),
                self.target_root / "memories" / "USER.md",
                self.user_limit,
                kind="user-profile",
            ),
        )
        self.run_if_selected("messaging-settings", lambda: self.migrate_messaging_settings(config))
        self.run_if_selected("secret-settings", lambda: self.handle_secret_settings(config))
        self.run_if_selected("discord-settings", lambda: self.migrate_discord_settings(config))
        self.run_if_selected("slack-settings", lambda: self.migrate_slack_settings(config))
        self.run_if_selected("whatsapp-settings", lambda: self.migrate_whatsapp_settings(config))
        self.run_if_selected("signal-settings", lambda: self.migrate_signal_settings(config))
        self.run_if_selected("provider-keys", lambda: self.handle_provider_keys(config))
        self.run_if_selected("model-config", lambda: self.migrate_model_config(config))
        self.run_if_selected("tts-config", lambda: self.migrate_tts_config(config))
        self.run_if_selected("command-allowlist", self.migrate_command_allowlist)
        self.run_if_selected("skills", self.migrate_skills)
        self.run_if_selected("shared-skills", self.migrate_shared_skills)
        self.run_if_selected("daily-memory", self.migrate_daily_memory)
        self.run_if_selected(
            "tts-assets",
            lambda: self.copy_tree_non_destructive(
                self.source_candidate("workspace/tts"),
                self.target_root / "tts",
                kind="tts-assets",
                ignore_dir_names={".venv", "generated", "__pycache__"},
            ),
        )
        self.run_if_selected("archive", self.archive_docs)

        # ── v2 migration modules ──────────────────────────────
        self.run_if_selected("mcp-servers", lambda: self.migrate_mcp_servers(config))
        self.run_if_selected("plugins-config", lambda: self.migrate_plugins_config(config))
        self.run_if_selected("cron-jobs", lambda: self.migrate_cron_jobs(config))
        self.run_if_selected("hooks-config", lambda: self.migrate_hooks_config(config))
        self.run_if_selected("agent-config", lambda: self.migrate_agent_config(config))
        self.run_if_selected("gateway-config", lambda: self.migrate_gateway_config(config))
        self.run_if_selected("session-config", lambda: self.migrate_session_config(config))
        self.run_if_selected("full-providers", lambda: self.migrate_full_providers(config))
        self.run_if_selected("deep-channels", lambda: self.migrate_deep_channels(config))
        self.run_if_selected("browser-config", lambda: self.migrate_browser_config(config))
        self.run_if_selected("tools-config", lambda: self.migrate_tools_config(config))
        self.run_if_selected("approvals-config", lambda: self.migrate_approvals_config(config))
        self.run_if_selected("memory-backend", lambda: self.migrate_memory_backend(config))
        self.run_if_selected("skills-config", lambda: self.migrate_skills_config(config))
        self.run_if_selected("ui-identity", lambda: self.migrate_ui_identity(config))
        self.run_if_selected("logging-config", lambda: self.migrate_logging_config(config))

        # Generate migration notes
        self.generate_migration_notes()

        return self.build_report()

    def run_if_selected(self, option_id: str, func) -> None:
        if not self.is_selected(option_id):
            meta = MIGRATION_OPTION_METADATA[option_id]
            self.record(option_id, None, None, "skipped", "Not selected for this run", option_label=meta["label"])
            return
        # If a previous config.yaml write hit a conflict/error during apply,
        # skip remaining config-mutating options rather than risk a partial
        # write.  Dry-run mode never blocks — the user needs the full preview
        # to decide how to proceed (re-run with --overwrite, etc.).
        if (
            self.execute
            and self._config_apply_blocked
            and option_id in self._CONFIG_MUTATING_OPTIONS
        ):
            meta = MIGRATION_OPTION_METADATA[option_id]
            self.record(
                option_id,
                None,
                None,
                STATUS_SKIPPED,
                REASON_BLOCKED_BY_APPLY_CONFLICT,
                option_label=meta["label"],
            )
            return
        func()

    def build_report(self) -> Dict[str, Any]:
        summary: Dict[str, int] = {
            "migrated": 0,
            "archived": 0,
            "skipped": 0,
            "conflict": 0,
            "error": 0,
        }
        for item in self.items:
            summary[item.status] = summary.get(item.status, 0) + 1

        report = {
            "timestamp": self.timestamp,
            "mode": "execute" if self.execute else "dry-run",
            "source_root": str(self.source_root),
            "target_root": str(self.target_root),
            "workspace_target": str(self.workspace_target) if self.workspace_target else None,
            "output_dir": str(self.output_dir) if self.output_dir else None,
            "migrate_secrets": self.migrate_secrets,
            "preset": self.preset_name or None,
            "skill_conflict_mode": self.skill_conflict_mode,
            "selection": {
                "selected": sorted(self.selected_options),
                "preset": self.preset_name or None,
                "skill_conflict_mode": self.skill_conflict_mode,
                "available": [
                    {"id": option_id, **meta}
                    for option_id, meta in MIGRATION_OPTION_METADATA.items()
                ],
                "presets": [
                    {"id": preset_id, "selected": sorted(option_ids)}
                    for preset_id, option_ids in MIGRATION_PRESETS.items()
                ],
            },
            "summary": summary,
            "items": [asdict(item) for item in self.items],
            "warnings": self._build_warnings(summary),
            "next_steps": self._build_next_steps(summary),
        }

        if self.output_dir:
            write_report(self.output_dir, report)

        return report

    def _build_warnings(self, summary: Dict[str, int]) -> List[str]:
        """Structured warnings surfaced on the report for downstream consumers.

        Modelled on OpenClaw's extensions/migrate-hermes/plan.ts warnings[].
        Keep the messages actionable — they show up in summary.md and the
        JSON report.
        """
        warnings: List[str] = []
        if summary.get("conflict", 0) > 0:
            warnings.append(
                "Conflicts were found. Re-run with --overwrite to replace conflicting "
                "targets after item-level backups."
            )
        if summary.get("error", 0) > 0:
            warnings.append(
                "One or more items failed. Inspect the report and re-run after fixing "
                "the underlying cause."
            )
        if self._config_apply_blocked and self.execute:
            warnings.append(
                "A config.yaml write hit a conflict or error mid-apply; later config "
                "items were skipped to avoid a partial write."
            )
        # Detect whether secrets were detected but not migrated.
        provider_keys_skipped = any(
            item.kind == "provider-keys" and item.status == STATUS_SKIPPED
            for item in self.items
        )
        if provider_keys_skipped and not self.migrate_secrets:
            warnings.append(
                "API keys and other credentials were detected but not imported. "
                "Re-run with --migrate-secrets to copy supported keys into the "
                "Hermes env file."
            )
        return warnings

    def _build_next_steps(self, summary: Dict[str, int]) -> List[str]:
        """Human-readable next-step guidance baked into the report."""
        if not self.execute:
            return [
                "Re-run without --dry-run to apply the migration.",
                "Pass --overwrite to resolve conflicts, or --migrate-secrets to "
                "include API keys.",
            ]
        steps: List[str] = []
        if summary.get("migrated", 0) > 0:
            steps.append(
                "Review the migration report at "
                f"{self.output_dir}/summary.md"
                if self.output_dir
                else "Review the migration report."
            )
            steps.append(
                "Start a new Hermes session (or /reset) to pick up the imported config."
            )
        if summary.get("conflict", 0) > 0:
            steps.append(
                "Re-run with --overwrite to apply items that were blocked by conflicts."
            )
        return steps

    def maybe_backup(self, path: Path) -> Optional[Path]:
        if not self.execute or not self.backup_dir or not path.exists():
            return None
        return backup_existing(path, self.backup_dir)

    def write_overflow_entries(self, kind: str, entries: Sequence[str]) -> Optional[Path]:
        if not entries or not self.overflow_dir:
            return None
        self.overflow_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{kind.replace('-', '_')}_overflow.txt"
        path = self.overflow_dir / filename
        path.write_text("\n".join(entries) + "\n", encoding="utf-8")
        return path

    def copy_file(self, source: Path, destination: Path, kind: str,
                  transform: Optional[Any] = None) -> None:
        if not source or not source.exists():
            return

        if destination.exists():
            if not transform and sha256_file(source) == sha256_file(destination):
                self.record(kind, source, destination, "skipped", "Target already matches source")
                return
            if not self.overwrite:
                self.record(kind, source, destination, "conflict", "Target exists and overwrite is disabled")
                return

        if self.execute:
            backup_path = self.maybe_backup(destination)
            ensure_parent(destination)
            if transform:
                content = read_text(source)
                content = transform(content)
                destination.write_text(content, encoding="utf-8")
                shutil.copystat(source, destination)
            else:
                shutil.copy2(source, destination)
            self.record(kind, source, destination, "migrated", backup=str(backup_path) if backup_path else None)
        else:
            self.record(kind, source, destination, "migrated", "Would copy")

    def migrate_soul(self) -> None:
        source = self.source_candidate("workspace/SOUL.md", "workspace.default/SOUL.md")
        if not source:
            self.record("soul", None, self.target_root / "SOUL.md", "skipped", "No OpenClaw SOUL.md found")
            return
        self.copy_file(source, self.target_root / "SOUL.md", kind="soul", transform=rebrand_text)

    def migrate_workspace_agents(self) -> None:
        source = self.source_candidate(
            f"workspace/{WORKSPACE_INSTRUCTIONS_FILENAME}",
            f"workspace.default/{WORKSPACE_INSTRUCTIONS_FILENAME}",
        )
        if source is None:
            self.record("workspace-agents", "workspace/AGENTS.md", "", "skipped", "Source file not found")
            return
        if not self.workspace_target:
            self.record("workspace-agents", source, None, "skipped", "No workspace target was provided")
            return
        destination = self.workspace_target / WORKSPACE_INSTRUCTIONS_FILENAME
        self.copy_file(source, destination, kind="workspace-agents", transform=rebrand_text)

    def migrate_memory(self, source: Optional[Path], destination: Path, limit: int, kind: str) -> None:
        if not source or not source.exists():
            self.record(kind, None, destination, "skipped", "Source file not found")
            return

        incoming = extract_markdown_entries(read_text(source))
        if not incoming:
            self.record(kind, source, destination, "skipped", "No importable entries found")
            return
        incoming = [rebrand_text(entry) for entry in incoming]

        existing = parse_existing_memory_entries(destination)
        merged, stats, overflowed = merge_entries(existing, incoming, limit)
        details = {
            "existing_entries": stats["existing"],
            "added_entries": stats["added"],
            "duplicate_entries": stats["duplicates"],
            "overflowed_entries": stats["overflowed"],
            "char_limit": limit,
            "final_char_count": len(ENTRY_DELIMITER.join(merged)) if merged else 0,
        }
        overflow_file = self.write_overflow_entries(kind, overflowed)
        if overflow_file is not None:
            details["overflow_file"] = str(overflow_file)

        if self.execute:
            if stats["added"] == 0 and not overflowed:
                self.record(kind, source, destination, "skipped", "No new entries to import", **details)
                return
            backup_path = self.maybe_backup(destination)
            ensure_parent(destination)
            destination.write_text(ENTRY_DELIMITER.join(merged) + ("\n" if merged else ""), encoding="utf-8")
            self.record(
                kind,
                source,
                destination,
                "migrated",
                backup=str(backup_path) if backup_path else "",
                overflow_preview=overflowed[:5],
                **details,
            )
        else:
            self.record(kind, source, destination, "migrated", "Would merge entries", overflow_preview=overflowed[:5], **details)

    def migrate_command_allowlist(self) -> None:
        source = self.source_root / "exec-approvals.json"
        destination = self.target_root / "config.yaml"
        if not source.exists():
            self.record("command-allowlist", None, destination, "skipped", "No OpenClaw exec approvals file found")
            return
        if yaml is None:
            self.record("command-allowlist", source, destination, "error", "PyYAML is not available")
            return

        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.record("command-allowlist", source, destination, "error", f"Invalid JSON: {exc}")
            return

        patterns: List[str] = []
        agents = data.get("agents", {})
        if isinstance(agents, dict):
            for agent_data in agents.values():
                allowlist = agent_data.get("allowlist", []) if isinstance(agent_data, dict) else []
                for entry in allowlist:
                    pattern = entry.get("pattern") if isinstance(entry, dict) else None
                    if pattern:
                        patterns.append(pattern)

        patterns = sorted(dict.fromkeys(patterns))
        if not patterns:
            self.record("command-allowlist", source, destination, "skipped", "No allowlist patterns found")
            return
        if not destination.exists():
            self.record("command-allowlist", source, destination, "skipped", "Hermes config.yaml does not exist yet")
            return

        config = load_yaml_file(destination)
        current = config.get("command_allowlist", [])
        if not isinstance(current, list):
            current = []
        merged = sorted(dict.fromkeys(list(current) + patterns))
        added = [pattern for pattern in merged if pattern not in current]
        if not added:
            self.record("command-allowlist", source, destination, "skipped", "All patterns already present")
            return

        if self.execute:
            backup_path = self.maybe_backup(destination)
            config["command_allowlist"] = merged
            dump_yaml_file(destination, config)
            self.record(
                "command-allowlist",
                source,
                destination,
                "migrated",
                backup=str(backup_path) if backup_path else "",
                added_patterns=added,
            )
        else:
            self.record("command-allowlist", source, destination, "migrated", "Would merge patterns", added_patterns=added)

    def load_openclaw_config(self) -> Dict[str, Any]:
        # Check current name and legacy config filenames
        for name in ("openclaw.json", "clawdbot.json", "moltbot.json"):
            config_path = self.source_root / name
            if config_path.exists():
                try:
                    data = json.loads(config_path.read_text(encoding="utf-8"))
                    return data if isinstance(data, dict) else {}
                except json.JSONDecodeError:
                    continue
        return {}

    def load_openclaw_env(self) -> Dict[str, str]:
        """Load the OpenClaw .env file for secrets that live there instead of config."""
        return parse_env_file(self.source_root / ".env")

    def merge_env_values(self, additions: Dict[str, str], kind: str, source: Path) -> None:
        destination = self.target_root / ".env"
        env_data = parse_env_file(destination)
        added: Dict[str, str] = {}
        conflicts: List[str] = []

        for key, value in additions.items():
            current = env_data.get(key)
            if current == value:
                continue
            if current and not self.overwrite:
                conflicts.append(key)
                continue
            env_data[key] = value
            added[key] = value

        if conflicts and not added:
            self.record(kind, source, destination, "conflict", "Destination .env already has different values", conflicting_keys=conflicts)
            return
        if not conflicts and not added:
            self.record(kind, source, destination, "skipped", "All env values already present")
            return

        if self.execute:
            backup_path = self.maybe_backup(destination)
            save_env_file(destination, env_data)
            self.record(
                kind,
                source,
                destination,
                "migrated",
                backup=str(backup_path) if backup_path else "",
                added_keys=sorted(added.keys()),
                conflicting_keys=conflicts,
            )
        else:
            self.record(
                kind,
                source,
                destination,
                "migrated",
                "Would merge env values",
                added_keys=sorted(added.keys()),
                conflicting_keys=conflicts,
            )

    def migrate_messaging_settings(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        additions: Dict[str, str] = {}

        workspace = (
            config.get("agents", {})
            .get("defaults", {})
            .get("workspace")
        )
        if isinstance(workspace, str) and workspace.strip():
            ws_path = workspace.strip()
            # Skip if the workspace points inside the OpenClaw source directory —
            # that path will be stale after migration and would cause the Hermes
            # gateway to use the old OpenClaw workspace as its cwd, picking up
            # OpenClaw's AGENTS.md, MEMORY.md, etc.
            try:
                inside_source = Path(ws_path).resolve().is_relative_to(self.source_root.resolve())
            except (ValueError, OSError):
                inside_source = False
            if not inside_source:
                additions["MESSAGING_CWD"] = ws_path

        allowlist_path = self.source_root / "credentials" / "telegram-default-allowFrom.json"
        if allowlist_path.exists():
            try:
                allow_data = json.loads(allowlist_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.record("messaging-settings", allowlist_path, self.target_root / ".env", "error", "Invalid JSON in Telegram allowlist file")
            else:
                allow_from = allow_data.get("allowFrom", [])
                if isinstance(allow_from, list):
                    users = [str(user).strip() for user in allow_from if str(user).strip()]
                    if users:
                        additions["TELEGRAM_ALLOWED_USERS"] = ",".join(users)

        if additions:
            self.merge_env_values(additions, "messaging-settings", self.source_root / "openclaw.json")
        else:
            self.record("messaging-settings", self.source_root / "openclaw.json", self.target_root / ".env", "skipped", "No Hermes-compatible messaging settings found")

    def handle_secret_settings(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        if self.migrate_secrets:
            self.migrate_secret_settings(config)
            return

        config_path = self.source_root / "openclaw.json"
        if config_path.exists():
            self.record(
                "secret-settings",
                config_path,
                self.target_root / ".env",
                "skipped",
                "Secret migration disabled. Re-run with --migrate-secrets to import allowlisted secrets.",
                supported_targets=sorted(SUPPORTED_SECRET_TARGETS),
            )
        else:
            self.record(
                "secret-settings",
                config_path,
                self.target_root / ".env",
                "skipped",
                "OpenClaw config file not found",
                supported_targets=sorted(SUPPORTED_SECRET_TARGETS),
            )

    def migrate_secret_settings(self, config: Dict[str, Any]) -> None:
        secret_additions: Dict[str, str] = {}

        tg_cfg = config.get("channels", {}).get("telegram", {})
        telegram_token = self._get_channel_field(tg_cfg, "botToken") if isinstance(tg_cfg, dict) else None
        if isinstance(telegram_token, str) and telegram_token.strip():
            secret_additions["TELEGRAM_BOT_TOKEN"] = telegram_token.strip()

        if secret_additions:
            self.merge_env_values(secret_additions, "secret-settings", self.source_root / "openclaw.json")
        else:
            self.record(
                "secret-settings",
                self.source_root / "openclaw.json",
                self.target_root / ".env",
                "skipped",
                "No allowlisted Hermes-compatible secrets found",
                supported_targets=sorted(SUPPORTED_SECRET_TARGETS),
            )

    def _resolve_channel_secret(self, value: Any) -> Optional[str]:
        """Resolve a channel config value that may be a SecretRef."""
        return resolve_secret_input(value, self.load_openclaw_env())

    @staticmethod
    def _get_channel_field(ch_cfg: Dict[str, Any], field: str) -> Any:
        """Get a field from channel config, checking both flat and accounts.default layout."""
        val = ch_cfg.get(field)
        if val is not None:
            return val
        accounts = ch_cfg.get("accounts")
        if isinstance(accounts, dict):
            default = accounts.get("default")
            if isinstance(default, dict):
                return default.get(field)
        return None

    def migrate_discord_settings(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        additions: Dict[str, str] = {}
        discord = config.get("channels", {}).get("discord", {})
        if isinstance(discord, dict):
            token = self._get_channel_field(discord, "token")
            if isinstance(token, str) and token.strip():
                additions["DISCORD_BOT_TOKEN"] = token.strip()
            allow_from = self._get_channel_field(discord, "allowFrom") or []
            if isinstance(allow_from, list):
                users = [str(u).strip() for u in allow_from if str(u).strip()]
                if users:
                    additions["DISCORD_ALLOWED_USERS"] = ",".join(users)
        if additions:
            self.merge_env_values(additions, "discord-settings", self.source_root / "openclaw.json")
        else:
            self.record("discord-settings", self.source_root / "openclaw.json", self.target_root / ".env", "skipped", "No Discord settings found")

    def migrate_slack_settings(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        additions: Dict[str, str] = {}
        slack = config.get("channels", {}).get("slack", {})
        if isinstance(slack, dict):
            bot_token = self._get_channel_field(slack, "botToken")
            if isinstance(bot_token, str) and bot_token.strip():
                additions["SLACK_BOT_TOKEN"] = bot_token.strip()
            app_token = self._get_channel_field(slack, "appToken")
            if isinstance(app_token, str) and app_token.strip():
                additions["SLACK_APP_TOKEN"] = app_token.strip()
            allow_from = self._get_channel_field(slack, "allowFrom") or []
            if isinstance(allow_from, list):
                users = [str(u).strip() for u in allow_from if str(u).strip()]
                if users:
                    additions["SLACK_ALLOWED_USERS"] = ",".join(users)
        if additions:
            self.merge_env_values(additions, "slack-settings", self.source_root / "openclaw.json")
        else:
            self.record("slack-settings", self.source_root / "openclaw.json", self.target_root / ".env", "skipped", "No Slack settings found")

    def migrate_whatsapp_settings(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        additions: Dict[str, str] = {}
        whatsapp = config.get("channels", {}).get("whatsapp", {})
        if isinstance(whatsapp, dict):
            allow_from = self._get_channel_field(whatsapp, "allowFrom") or []
            if isinstance(allow_from, list):
                users = [str(u).strip() for u in allow_from if str(u).strip()]
                if users:
                    additions["WHATSAPP_ALLOWED_USERS"] = ",".join(users)
        if additions:
            self.merge_env_values(additions, "whatsapp-settings", self.source_root / "openclaw.json")
        else:
            self.record("whatsapp-settings", self.source_root / "openclaw.json", self.target_root / ".env", "skipped", "No WhatsApp settings found")

    def migrate_signal_settings(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        additions: Dict[str, str] = {}
        signal = config.get("channels", {}).get("signal", {})
        if isinstance(signal, dict):
            account = self._get_channel_field(signal, "account")
            if isinstance(account, str) and account.strip():
                additions["SIGNAL_ACCOUNT"] = account.strip()
            http_url = self._get_channel_field(signal, "httpUrl")
            if isinstance(http_url, str) and http_url.strip():
                additions["SIGNAL_HTTP_URL"] = http_url.strip()
            allow_from = self._get_channel_field(signal, "allowFrom") or []
            if isinstance(allow_from, list):
                users = [str(u).strip() for u in allow_from if str(u).strip()]
                if users:
                    additions["SIGNAL_ALLOWED_USERS"] = ",".join(users)
        if additions:
            self.merge_env_values(additions, "signal-settings", self.source_root / "openclaw.json")
        else:
            self.record("signal-settings", self.source_root / "openclaw.json", self.target_root / ".env", "skipped", "No Signal settings found")

    def handle_provider_keys(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        if not self.migrate_secrets:
            config_path = self.source_root / "openclaw.json"
            self.record(
                "provider-keys",
                config_path,
                self.target_root / ".env",
                "skipped",
                "Secret migration disabled. Re-run with --migrate-secrets to import provider API keys.",
                supported_targets=sorted(SUPPORTED_SECRET_TARGETS),
            )
            return
        self.migrate_provider_keys(config)

    def migrate_provider_keys(self, config: Dict[str, Any]) -> None:
        secret_additions: Dict[str, str] = {}

        # Extract provider API keys from models.providers
        # Note: apiKey values can be strings, env templates, or SecretRef objects
        openclaw_env = self.load_openclaw_env()
        providers = config.get("models", {}).get("providers", {})
        if isinstance(providers, dict):
            for provider_name, provider_cfg in providers.items():
                if not isinstance(provider_cfg, dict):
                    continue
                raw_key = provider_cfg.get("apiKey")
                api_key = resolve_secret_input(raw_key, openclaw_env)
                if not api_key:
                    # Warn if a SecretRef with file/exec source was silently unresolvable
                    if isinstance(raw_key, dict) and raw_key.get("source") in {"file", "exec"}:
                        self.record(
                            "provider-keys",
                            self.source_root / "openclaw.json",
                            None,
                            "skipped",
                            f"Provider '{provider_name}' uses a {raw_key['source']}-backed SecretRef "
                            f"that cannot be auto-migrated. Add this key manually via: hermes config set",
                        )
                    continue

                base_url = provider_cfg.get("baseUrl", "")
                api_type = provider_cfg.get("api", "")
                env_var = None

                # Match by baseUrl first
                if isinstance(base_url, str):
                    if "openrouter" in base_url.lower():
                        env_var = "OPENROUTER_API_KEY"
                    elif "openai.com" in base_url.lower():
                        env_var = "OPENAI_API_KEY"
                    elif "anthropic" in base_url.lower():
                        env_var = "ANTHROPIC_API_KEY"

                # Match by api type
                if not env_var and isinstance(api_type, str) and api_type == "anthropic-messages":
                    env_var = "ANTHROPIC_API_KEY"

                # Match by provider name
                if not env_var:
                    name_lower = provider_name.lower()
                    if name_lower == "openrouter":
                        env_var = "OPENROUTER_API_KEY"
                    elif "openai" in name_lower:
                        env_var = "OPENAI_API_KEY"

                if env_var:
                    secret_additions[env_var] = api_key

        # Extract TTS API keys
        tts = config.get("messages", {}).get("tts", {})
        if isinstance(tts, dict):
            elevenlabs = tts.get("elevenlabs", {})
            if isinstance(elevenlabs, dict):
                el_key = elevenlabs.get("apiKey")
                if isinstance(el_key, str) and el_key.strip():
                    secret_additions["ELEVENLABS_API_KEY"] = el_key.strip()
            openai_tts = tts.get("openai", {})
            if isinstance(openai_tts, dict):
                oai_key = openai_tts.get("apiKey")
                if isinstance(oai_key, str) and oai_key.strip():
                    secret_additions["VOICE_TOOLS_OPENAI_KEY"] = oai_key.strip()

        # Also check the OpenClaw .env file — many users store keys there
        # instead of inline in openclaw.json
        openclaw_env = self.load_openclaw_env()
        env_key_mapping = {
            "OPENROUTER_API_KEY": "OPENROUTER_API_KEY",
            "OPENAI_API_KEY": "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
            "ELEVENLABS_API_KEY": "ELEVENLABS_API_KEY",
            "TELEGRAM_BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
            "DEEPSEEK_API_KEY": "DEEPSEEK_API_KEY",
            "GEMINI_API_KEY": "GEMINI_API_KEY",
            "ZAI_API_KEY": "ZAI_API_KEY",
            "MINIMAX_API_KEY": "MINIMAX_API_KEY",
        }
        for oc_key, hermes_key in env_key_mapping.items():
            val = openclaw_env.get(oc_key, "").strip()
            if val and hermes_key not in secret_additions:
                secret_additions[hermes_key] = val

        # Check the openclaw.json "env" sub-object — some OpenClaw setups
        # store API keys here instead of in a separate .env file.
        # Keys can be at env.<KEY> or env.vars.<KEY>.
        json_env = config.get("env")
        if isinstance(json_env, dict):
            env_vars = json_env.get("vars")
            sources = [json_env]
            if isinstance(env_vars, dict):
                sources.append(env_vars)
            for src in sources:
                for oc_key, hermes_key in env_key_mapping.items():
                    val = src.get(oc_key)
                    if isinstance(val, str) and val.strip() and hermes_key not in secret_additions:
                        secret_additions[hermes_key] = val.strip()

        # Check per-agent auth-profiles.json for additional credentials
        auth_profiles_path = self.source_root / "agents" / "main" / "agent" / "auth-profiles.json"
        if auth_profiles_path.exists():
            try:
                profiles = json.loads(auth_profiles_path.read_text(encoding="utf-8"))
                if isinstance(profiles, dict):
                    # auth-profiles.json wraps profiles in a "profiles" key
                    profile_entries = profiles.get("profiles", profiles) if isinstance(profiles.get("profiles"), dict) else profiles
                    for profile_name, profile_data in profile_entries.items():
                        if not isinstance(profile_data, dict):
                            continue
                        # Canonical field is "key", "apiKey" is accepted as alias
                        api_key = profile_data.get("key", "") or profile_data.get("apiKey", "")
                        if not isinstance(api_key, str) or not api_key.strip():
                            continue
                        name_lower = profile_name.lower()
                        if "openrouter" in name_lower and "OPENROUTER_API_KEY" not in secret_additions:
                            secret_additions["OPENROUTER_API_KEY"] = api_key.strip()
                        elif "openai" in name_lower and "OPENAI_API_KEY" not in secret_additions:
                            secret_additions["OPENAI_API_KEY"] = api_key.strip()
                        elif "anthropic" in name_lower and "ANTHROPIC_API_KEY" not in secret_additions:
                            secret_additions["ANTHROPIC_API_KEY"] = api_key.strip()
            except (json.JSONDecodeError, OSError):
                pass

        if secret_additions:
            self.merge_env_values(secret_additions, "provider-keys", self.source_root / "openclaw.json")
        else:
            self.record(
                "provider-keys",
                self.source_root / "openclaw.json",
                self.target_root / ".env",
                "skipped",
                "No provider API keys found",
                supported_targets=sorted(SUPPORTED_SECRET_TARGETS),
            )

    def migrate_model_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        destination = self.target_root / "config.yaml"
        source_path = self.source_root / "openclaw.json"

        model_value = config.get("agents", {}).get("defaults", {}).get("model")
        if model_value is None:
            self.record("model-config", source_path, destination, "skipped", "No default model found in OpenClaw config")
            return

        if isinstance(model_value, dict):
            model_str = model_value.get("primary")
        else:
            model_str = model_value

        if not isinstance(model_str, str) or not model_str.strip():
            self.record("model-config", source_path, destination, "skipped", "Default model value is empty or invalid")
            return

        model_str = model_str.strip()

        # Resolve a model alias against the OpenClaw model catalog.
        # OpenClaw stores agents.defaults.model as either a bare string or
        # {"primary": "<value>"}, and that value can be either:
        #   - a full provider/model API ID (e.g. "anthropic/claude-opus-4-6"), or
        #   - a display alias (e.g. "Claude Opus 4.6") that maps to one.
        # The catalog at agents.defaults.models is keyed by the full
        # provider/model API ID with an "alias" field on the value, e.g.:
        #   {"anthropic/claude-opus-4-6": {"alias": "Claude Opus 4.6"}}
        # If model_str matches an alias in the catalog, rewrite it to the
        # catalog key (the real API ID).  If it's already an API ID or has
        # no catalog match, leave it alone and let downstream pass it through.
        model_catalog = config.get("agents", {}).get("defaults", {}).get("models", {})
        if isinstance(model_catalog, dict) and model_str not in model_catalog:
            for api_id, entry in model_catalog.items():
                if not isinstance(api_id, str):
                    continue
                if isinstance(entry, dict) and entry.get("alias") == model_str:
                    model_str = api_id
                    break
                if isinstance(entry, str) and entry == model_str:
                    model_str = api_id
                    break

        if yaml is None:
            self.record("model-config", source_path, destination, "error", "PyYAML is not available")
            return

        hermes_config = load_yaml_file(destination)
        current_model = hermes_config.get("model")
        if current_model == model_str:
            self.record("model-config", source_path, destination, "skipped", "Model already set to the same value")
            return
        if current_model and not self.overwrite:
            self.record("model-config", source_path, destination, "conflict", "Model already set and overwrite is disabled", current=current_model, incoming=model_str)
            return

        if self.execute:
            backup_path = self.maybe_backup(destination)
            existing_model = hermes_config.get("model")
            if isinstance(existing_model, dict):
                existing_model["default"] = model_str
            else:
                hermes_config["model"] = {"default": model_str}
            dump_yaml_file(destination, hermes_config)
            self.record("model-config", source_path, destination, "migrated", backup=str(backup_path) if backup_path else "", model=model_str)
        else:
            self.record("model-config", source_path, destination, "migrated", "Would set model", model=model_str)

    def migrate_tts_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        destination = self.target_root / "config.yaml"
        source_path = self.source_root / "openclaw.json"

        tts = config.get("messages", {}).get("tts", {})
        if not isinstance(tts, dict) or not tts:
            self.record("tts-config", source_path, destination, "skipped", "No TTS configuration found in OpenClaw config")
            return

        if yaml is None:
            self.record("tts-config", source_path, destination, "error", "PyYAML is not available")
            return

        tts_data: Dict[str, Any] = {}

        provider = tts.get("provider")
        if isinstance(provider, str) and provider in {"elevenlabs", "openai", "edge", "microsoft"}:
            # OpenClaw renamed "edge" to "microsoft"; Hermes still uses "edge"
            tts_data["provider"] = "edge" if provider == "microsoft" else provider

        # TTS provider settings live under messages.tts.providers.{provider}
        # in OpenClaw (not messages.tts.elevenlabs directly)
        providers = tts.get("providers") or {}

        # Also check the top-level "talk" config which has provider settings too
        talk_cfg = (config or self.load_openclaw_config()).get("talk") or {}
        talk_providers = talk_cfg.get("providers") or {}

        # Merge: messages.tts.providers takes priority, then talk.providers,
        # then legacy flat keys (messages.tts.elevenlabs, etc.)
        elevenlabs = (
            (providers.get("elevenlabs") or {})
            if isinstance(providers.get("elevenlabs"), dict) else
            (talk_providers.get("elevenlabs") or {})
            if isinstance(talk_providers.get("elevenlabs"), dict) else
            (tts.get("elevenlabs") or {})
        )
        if isinstance(elevenlabs, dict):
            el_settings: Dict[str, str] = {}
            voice_id = elevenlabs.get("voiceId") or talk_cfg.get("voiceId")
            if isinstance(voice_id, str) and voice_id.strip():
                el_settings["voice_id"] = voice_id.strip()
            model_id = elevenlabs.get("modelId") or talk_cfg.get("modelId")
            if isinstance(model_id, str) and model_id.strip():
                el_settings["model_id"] = model_id.strip()
            if el_settings:
                tts_data["elevenlabs"] = el_settings

        openai_tts = (
            (providers.get("openai") or {})
            if isinstance(providers.get("openai"), dict) else
            (talk_providers.get("openai") or {})
            if isinstance(talk_providers.get("openai"), dict) else
            (tts.get("openai") or {})
        )
        if isinstance(openai_tts, dict):
            oai_settings: Dict[str, str] = {}
            oai_model = openai_tts.get("model") or openai_tts.get("modelId")
            if isinstance(oai_model, str) and oai_model.strip():
                oai_settings["model"] = oai_model.strip()
            oai_voice = openai_tts.get("voice")
            if isinstance(oai_voice, str) and oai_voice.strip():
                oai_settings["voice"] = oai_voice.strip()
            if oai_settings:
                tts_data["openai"] = oai_settings

        edge_tts = (
            (providers.get("edge") or providers.get("microsoft") or {})
            if isinstance(providers.get("edge"), dict) or isinstance(providers.get("microsoft"), dict) else
            (tts.get("edge") or tts.get("microsoft") or {})
        )
        if isinstance(edge_tts, dict):
            edge_voice = edge_tts.get("voice")
            if isinstance(edge_voice, str) and edge_voice.strip():
                tts_data["edge"] = {"voice": edge_voice.strip()}

        if not tts_data:
            self.record("tts-config", source_path, destination, "skipped", "No compatible TTS settings found")
            return

        hermes_config = load_yaml_file(destination)
        existing_tts = hermes_config.get("tts", {})
        if not isinstance(existing_tts, dict):
            existing_tts = {}

        if self.execute:
            backup_path = self.maybe_backup(destination)
            merged_tts = dict(existing_tts)
            for key, value in tts_data.items():
                if isinstance(value, dict) and isinstance(merged_tts.get(key), dict):
                    merged_tts[key] = {**merged_tts[key], **value}
                else:
                    merged_tts[key] = value
            hermes_config["tts"] = merged_tts
            dump_yaml_file(destination, hermes_config)
            self.record("tts-config", source_path, destination, "migrated", backup=str(backup_path) if backup_path else "", settings=list(tts_data.keys()))
        else:
            self.record("tts-config", source_path, destination, "migrated", "Would set TTS config", settings=list(tts_data.keys()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate OpenClaw user state into Hermes Agent.")
    parser.add_argument("--source", default=str(Path.home() / ".openclaw"), help="OpenClaw home directory")
    parser.add_argument("--target", default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"), help="Hermes home directory")
    parser.add_argument(
        "--workspace-target",
        help="Optional workspace root where the workspace instructions file should be copied",
    )
    parser.add_argument("--execute", action="store_true", help="Apply changes instead of reporting a dry run")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing Hermes targets after backing them up")
    parser.add_argument(
        "--migrate-secrets",
        action="store_true",
        help="Import a narrow allowlist of Hermes-compatible secrets into the target env file",
    )
    parser.add_argument(
        "--skill-conflict",
        choices=sorted(SKILL_CONFLICT_MODES),
        default="skip",
        help="How to handle imported skill directory conflicts: skip, overwrite, or rename the imported copy.",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(MIGRATION_PRESETS),
        help="Apply a named migration preset. 'user-data' excludes allowlisted secrets; 'full' includes all compatible groups.",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Comma-separated migration option ids to include (default: all). "
             f"Valid ids: {', '.join(sorted(MIGRATION_OPTION_METADATA))}",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Comma-separated migration option ids to skip. "
             f"Valid ids: {', '.join(sorted(MIGRATION_OPTION_METADATA))}",
    )
    parser.add_argument("--output-dir", help="Where to write report, backups, and archived docs")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print the migration report as JSON on stdout (redacted). "
             "Combine with no --execute for a safe plan-only machine-readable preview.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        selected_options = resolve_selected_options(args.include, args.exclude, preset=args.preset)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
        return 2
    migrator = Migrator(
        source_root=Path(os.path.expanduser(args.source)).resolve(),
        target_root=Path(os.path.expanduser(args.target)).resolve(),
        execute=bool(args.execute),
        workspace_target=Path(os.path.expanduser(args.workspace_target)).resolve() if args.workspace_target else None,
        overwrite=bool(args.overwrite),
        migrate_secrets=bool(args.migrate_secrets),
        output_dir=Path(os.path.expanduser(args.output_dir)).resolve() if args.output_dir else None,
        selected_options=selected_options,
        preset_name=args.preset or "",
        skill_conflict_mode=args.skill_conflict,
    )
    report = migrator.migrate()

    # ── Machine-readable JSON mode ────────────────────────────
    # When --json is set, print the redacted report to stdout and skip the
    # human-readable terminal recap.  Useful for CI and scripted wrappers.
    if getattr(args, "json_output", False):
        print(json.dumps(redact_migration_value(report), indent=2, ensure_ascii=False))
        return 0

    # ── Human-readable terminal recap ─────────────────────────
    s = report["summary"]
    items = report["items"]
    mode_label = "DRY RUN" if not args.execute else "EXECUTED"
    total = sum(s.values())

    print()
    print("  ╔══════════════════════════════════════════════════════╗")
    print(f"  ║   OpenClaw -> Hermes Migration   [{mode_label:>8s}]   ║")
    print("  ╠══════════════════════════════════════════════════════╣")
    print(f"  ║  Source:  {str(report['source_root'])[:42]:<42s}  ║")
    print(f"  ║  Target:  {str(report['target_root'])[:42]:<42s}  ║")
    print("  ╠══════════════════════════════════════════════════════╣")
    print(f"  ║  ✔ Migrated:  {s.get('migrated', 0):>3d}    ◆ Archived:  {s.get('archived', 0):>3d}        ║")
    print(f"  ║  ⊘ Skipped:   {s.get('skipped', 0):>3d}    ⚠ Conflicts: {s.get('conflict', 0):>3d}        ║")
    print(f"  ║  ✖ Errors:    {s.get('error', 0):>3d}    Total:       {total:>3d}        ║")
    print("  ╚══════════════════════════════════════════════════════╝")

    # Show what was migrated
    migrated = [i for i in items if i["status"] == "migrated"]
    if migrated:
        print()
        print("  Migrated:")
        seen_kinds = set()
        for item in migrated:
            label = item["kind"]
            if label in seen_kinds:
                continue
            seen_kinds.add(label)
            dest = item.get("destination") or ""
            if dest.startswith(str(report["target_root"])):
                dest = "~/.hermes/" + dest[len(str(report["target_root"])) + 1:]
            meta = MIGRATION_OPTION_METADATA.get(label, {})
            display = meta.get("label", label)
            print(f"    ✔ {display:<35s} -> {dest}")

    # Show what was archived
    archived = [i for i in items if i["status"] == "archived"]
    if archived:
        print()
        print("  Archived (manual review needed):")
        seen_kinds = set()
        for item in archived:
            label = item["kind"]
            if label in seen_kinds:
                continue
            seen_kinds.add(label)
            reason = item.get("reason", "")
            meta = MIGRATION_OPTION_METADATA.get(label, {})
            display = meta.get("label", label)
            short_reason = reason[:50] + "..." if len(reason) > 50 else reason
            print(f"    ◆ {display:<35s}  {short_reason}")

    # Show conflicts
    conflicts = [i for i in items if i["status"] == "conflict"]
    if conflicts:
        print()
        print("  Conflicts (use --overwrite to force):")
        for item in conflicts:
            print(f"    ⚠ {item['kind']}: {item.get('reason', '')}")

    # Show errors
    errors = [i for i in items if i["status"] == "error"]
    if errors:
        print()
        print("  Errors:")
        for item in errors:
            print(f"    ✖ {item['kind']}: {item.get('reason', '')}")

    # PM2 reassurance
    print()
    print("  ℹ PM2 processes (Discord/Telegram bots) are NOT affected.")

    # Next steps
    if args.execute:
        print()
        print("  Next steps:")
        print("    1. Review ~/.hermes/config.yaml")
        print("    2. Run: hermes mcp list")
        if any(i["kind"] == "cron-jobs" and i["status"] == "archived" for i in items):
            print("    3. Recreate cron jobs: hermes cron")
        if report.get("output_dir"):
            print(f"    → Full report: {report['output_dir']}/MIGRATION_NOTES.md")
    elif not args.execute:
        print()
        print("  This was a dry run. Add --execute to apply changes.")

    print()

    # Also dump JSON for programmatic use
    if os.environ.get("MIGRATION_JSON_OUTPUT"):
        print(json.dumps(report, indent=2, ensure_ascii=False))

    return 0 if s.get("error", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
