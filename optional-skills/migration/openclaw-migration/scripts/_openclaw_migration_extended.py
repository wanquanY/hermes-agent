"""Extended content and configuration migrations for OpenClaw."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from _openclaw_migration_core import (
    ENTRY_DELIMITER,
    SKILL_CATEGORY_DESCRIPTION,
    SKILL_CATEGORY_DIRNAME,
    dump_yaml_file,
    ensure_parent,
    extract_markdown_entries,
    load_yaml_file,
    merge_entries,
    parse_env_file,
    parse_existing_memory_entries,
    read_text,
    rebrand_text,
    relative_label,
    save_env_file,
    sha256_file,
)


class MigratorExtendedMixin:
    def migrate_shared_skills(self) -> None:
        # Check all OpenClaw skill sources: managed, personal, project-level
        skill_sources = [
            (self.source_root / "skills", "shared-skills", "managed skills"),
            (Path.home() / ".agents" / "skills", "personal-skills", "personal cross-project skills"),
            (self.source_root / "workspace" / ".agents" / "skills", "project-skills", "project-level shared skills"),
            (self.source_root / "workspace.default" / ".agents" / "skills", "project-skills", "project-level shared skills"),
        ]
        found_any = False
        for source_root, kind_label, desc in skill_sources:
            if source_root.exists():
                found_any = True
                self._import_skill_directory(source_root, kind_label, desc)
        if not found_any:
            destination_root = self.target_root / "skills" / SKILL_CATEGORY_DIRNAME
            self.record("shared-skills", None, destination_root, "skipped", "No shared OpenClaw skills directories found")

    def _import_skill_directory(self, source_root: Path, kind_label: str, desc: str) -> None:
        """Import skills from a single source directory into openclaw-imports."""
        destination_root = self.target_root / "skills" / SKILL_CATEGORY_DIRNAME

        skill_dirs = [p for p in sorted(source_root.iterdir()) if p.is_dir() and (p / "SKILL.md").exists()]
        if not skill_dirs:
            self.record(kind_label, source_root, destination_root, "skipped", f"No skills with SKILL.md found in {desc}")
            return

        for skill_dir in skill_dirs:
            destination = destination_root / skill_dir.name
            final_destination = destination
            if destination.exists():
                if self.skill_conflict_mode == "skip":
                    self.record(kind_label, skill_dir, destination, "conflict", "Destination skill already exists")
                    continue
                if self.skill_conflict_mode == "rename":
                    final_destination = self.resolve_skill_destination(destination)
            if self.execute:
                backup_path = None
                if final_destination == destination and destination.exists():
                    backup_path = self.maybe_backup(destination)
                final_destination.parent.mkdir(parents=True, exist_ok=True)
                if final_destination == destination and destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(skill_dir, final_destination)
                details: Dict[str, Any] = {"backup": str(backup_path) if backup_path else ""}
                if final_destination != destination:
                    details["renamed_from"] = str(destination)
                self.record(kind_label, skill_dir, final_destination, "migrated", **details)
            else:
                if final_destination != destination:
                    self.record(
                        kind_label,
                        skill_dir,
                        final_destination,
                        "migrated",
                        f"Would copy {desc} directory under a renamed folder",
                        renamed_from=str(destination),
                    )
                else:
                    self.record(kind_label, skill_dir, final_destination, "migrated", f"Would copy {desc} directory")

        desc_path = destination_root / "DESCRIPTION.md"
        if self.execute:
            desc_path.parent.mkdir(parents=True, exist_ok=True)
            if not desc_path.exists():
                desc_path.write_text(SKILL_CATEGORY_DESCRIPTION + "\n", encoding="utf-8")
        elif not desc_path.exists():
            self.record("shared-skill-category", None, desc_path, "migrated", "Would create category description")

    def migrate_daily_memory(self) -> None:
        source_dir = self.source_candidate("workspace/memory")
        destination = self.target_root / "memories" / "MEMORY.md"
        if not source_dir or not source_dir.is_dir():
            self.record("daily-memory", None, destination, "skipped", "No workspace/memory/ directory found")
            return

        md_files = sorted(p for p in source_dir.iterdir() if p.is_file() and p.suffix == ".md")
        if not md_files:
            self.record("daily-memory", source_dir, destination, "skipped", "No .md files found in workspace/memory/")
            return

        all_incoming: List[str] = []
        for md_file in md_files:
            entries = extract_markdown_entries(read_text(md_file))
            all_incoming.extend(entries)

        if not all_incoming:
            self.record("daily-memory", source_dir, destination, "skipped", "No importable entries found in daily memory files")
            return
        all_incoming = [rebrand_text(entry) for entry in all_incoming]

        existing = parse_existing_memory_entries(destination)
        merged, stats, overflowed = merge_entries(existing, all_incoming, self.memory_limit)
        details = {
            "source_files": len(md_files),
            "existing_entries": stats["existing"],
            "added_entries": stats["added"],
            "duplicate_entries": stats["duplicates"],
            "overflowed_entries": stats["overflowed"],
            "char_limit": self.memory_limit,
            "final_char_count": len(ENTRY_DELIMITER.join(merged)) if merged else 0,
        }
        overflow_file = self.write_overflow_entries("daily-memory", overflowed)
        if overflow_file is not None:
            details["overflow_file"] = str(overflow_file)

        if self.execute:
            if stats["added"] == 0 and not overflowed:
                self.record("daily-memory", source_dir, destination, "skipped", "No new entries to import", **details)
                return
            backup_path = self.maybe_backup(destination)
            ensure_parent(destination)
            destination.write_text(ENTRY_DELIMITER.join(merged) + ("\n" if merged else ""), encoding="utf-8")
            self.record(
                "daily-memory",
                source_dir,
                destination,
                "migrated",
                backup=str(backup_path) if backup_path else "",
                overflow_preview=overflowed[:5],
                **details,
            )
        else:
            self.record("daily-memory", source_dir, destination, "migrated", "Would merge daily memory entries", overflow_preview=overflowed[:5], **details)

    def migrate_skills(self) -> None:
        source_root = self.source_candidate("workspace/skills")
        destination_root = self.target_root / "skills" / SKILL_CATEGORY_DIRNAME
        if not source_root or not source_root.exists():
            self.record("skills", None, destination_root, "skipped", "No OpenClaw skills directory found")
            return

        skill_dirs = [p for p in sorted(source_root.iterdir()) if p.is_dir() and (p / "SKILL.md").exists()]
        if not skill_dirs:
            self.record("skills", source_root, destination_root, "skipped", "No skills with SKILL.md found")
            return

        for skill_dir in skill_dirs:
            destination = destination_root / skill_dir.name
            final_destination = destination
            if destination.exists():
                if self.skill_conflict_mode == "skip":
                    self.record("skill", skill_dir, destination, "conflict", "Destination skill already exists")
                    continue
                if self.skill_conflict_mode == "rename":
                    final_destination = self.resolve_skill_destination(destination)
            if self.execute:
                backup_path = None
                if final_destination == destination and destination.exists():
                    backup_path = self.maybe_backup(destination)
                final_destination.parent.mkdir(parents=True, exist_ok=True)
                if final_destination == destination and destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(skill_dir, final_destination)
                details: Dict[str, Any] = {"backup": str(backup_path) if backup_path else ""}
                if final_destination != destination:
                    details["renamed_from"] = str(destination)
                self.record("skill", skill_dir, final_destination, "migrated", **details)
            else:
                if final_destination != destination:
                    self.record(
                        "skill",
                        skill_dir,
                        final_destination,
                        "migrated",
                        "Would copy skill directory under a renamed folder",
                        renamed_from=str(destination),
                    )
                else:
                    self.record("skill", skill_dir, final_destination, "migrated", "Would copy skill directory")

        desc_path = destination_root / "DESCRIPTION.md"
        if self.execute:
            desc_path.parent.mkdir(parents=True, exist_ok=True)
            if not desc_path.exists():
                desc_path.write_text(SKILL_CATEGORY_DESCRIPTION + "\n", encoding="utf-8")
        elif not desc_path.exists():
            self.record("skill-category", None, desc_path, "migrated", "Would create category description")

    def copy_tree_non_destructive(
        self,
        source_root: Optional[Path],
        destination_root: Path,
        kind: str,
        ignore_dir_names: Optional[set[str]] = None,
    ) -> None:
        if not source_root or not source_root.exists():
            self.record(kind, None, destination_root, "skipped", "Source directory not found")
            return

        ignore_dir_names = ignore_dir_names or set()
        files = [
            p
            for p in source_root.rglob("*")
            if p.is_file() and not any(part in ignore_dir_names for part in p.relative_to(source_root).parts[:-1])
        ]
        if not files:
            self.record(kind, source_root, destination_root, "skipped", "No files found")
            return

        copied = 0
        skipped = 0
        conflicts = 0

        for source in files:
            rel = source.relative_to(source_root)
            destination = destination_root / rel
            if destination.exists():
                if sha256_file(source) == sha256_file(destination):
                    skipped += 1
                    continue
                if not self.overwrite:
                    conflicts += 1
                    self.record(kind, source, destination, "conflict", "Destination file already exists")
                    continue

            if self.execute:
                self.maybe_backup(destination)
                ensure_parent(destination)
                shutil.copy2(source, destination)
            copied += 1

        status = "migrated" if copied else "skipped"
        reason = ""
        if not copied and conflicts:
            status = "conflict"
            reason = "All candidate files conflicted with existing destination files"
        elif not copied:
            reason = "No new files to copy"

        self.record(kind, source_root, destination_root, status, reason, copied_files=copied, unchanged_files=skipped, conflicts=conflicts)

    def archive_docs(self) -> None:
        candidates = [
            self.source_candidate("workspace/IDENTITY.md", "workspace.default/IDENTITY.md"),
            self.source_candidate("workspace/TOOLS.md", "workspace.default/TOOLS.md"),
            self.source_candidate("workspace/HEARTBEAT.md", "workspace.default/HEARTBEAT.md"),
            self.source_candidate("workspace/BOOTSTRAP.md", "workspace.default/BOOTSTRAP.md"),
        ]
        for candidate in candidates:
            if candidate:
                self.archive_path(candidate, reason="No direct Hermes destination; archived for manual review")

        for rel in ("workspace/.learnings", "workspace/memory"):
            candidate = self.source_root / rel
            if candidate.exists():
                self.archive_path(candidate, reason="No direct Hermes destination; archived for manual review")

        partially_extracted = [
            ("openclaw.json", "Selected Hermes-compatible values were extracted; raw OpenClaw config was not copied."),
            ("credentials/telegram-default-allowFrom.json", "Selected Hermes-compatible values were extracted; raw credentials file was not copied."),
        ]
        for rel, reason in partially_extracted:
            candidate = self.source_root / rel
            if candidate.exists():
                self.record("raw-config-skip", candidate, None, "skipped", reason)

        skipped_sensitive = [
            "memory/main.sqlite",
            "credentials",
            "devices",
            "identity",
            "workspace.zip",
        ]
        for rel in skipped_sensitive:
            candidate = self.source_root / rel
            if candidate.exists():
                self.record("sensitive-skip", candidate, None, "skipped", "Contains secrets, binary state, or product-specific runtime data")

    def archive_path(self, source: Path, reason: str) -> None:
        destination = self.archive_dir / relative_label(source, self.source_root) if self.archive_dir else None
        if self.execute and destination is not None:
            ensure_parent(destination)
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)
            self.record("archive", source, destination, "archived", reason)
        else:
            self.record("archive", source, destination, "archived", reason)

    # ── MCP servers ─────────────────────────────────────────────
    def migrate_mcp_servers(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        mcp_raw = (config.get("mcp") or {}).get("servers") or {}
        if not mcp_raw:
            self.record("mcp-servers", None, None, "skipped", "No MCP servers found in OpenClaw config")
            return

        hermes_cfg_path = self.target_root / "config.yaml"
        hermes_cfg = load_yaml_file(hermes_cfg_path)
        existing_mcp = hermes_cfg.get("mcp_servers") or {}
        added = 0

        for name, srv in mcp_raw.items():
            if not isinstance(srv, dict):
                continue
            if name in existing_mcp and not self.overwrite:
                self.record("mcp-servers", f"mcp.servers.{name}", f"mcp_servers.{name}", "conflict",
                            "MCP server already exists in Hermes config")
                continue

            hermes_srv: Dict[str, Any] = {}
            # STDIO transport
            if srv.get("command"):
                hermes_srv["command"] = srv["command"]
                if srv.get("args"):
                    hermes_srv["args"] = srv["args"]
                if srv.get("env"):
                    hermes_srv["env"] = srv["env"]
                if srv.get("cwd"):
                    hermes_srv["cwd"] = srv["cwd"]
            # HTTP/SSE transport
            if srv.get("url"):
                hermes_srv["url"] = srv["url"]
                if srv.get("headers"):
                    hermes_srv["headers"] = srv["headers"]
                if srv.get("auth"):
                    hermes_srv["auth"] = srv["auth"]
            # Common fields
            if srv.get("enabled") is False:
                hermes_srv["enabled"] = False
            if srv.get("timeout"):
                hermes_srv["timeout"] = srv["timeout"]
            if srv.get("connectTimeout"):
                hermes_srv["connect_timeout"] = srv["connectTimeout"]
            # Tool filtering
            tools_cfg = srv.get("tools") or {}
            if tools_cfg.get("include") or tools_cfg.get("exclude"):
                hermes_srv["tools"] = {}
                if tools_cfg.get("include"):
                    hermes_srv["tools"]["include"] = tools_cfg["include"]
                if tools_cfg.get("exclude"):
                    hermes_srv["tools"]["exclude"] = tools_cfg["exclude"]
            # Sampling
            sampling = srv.get("sampling")
            if sampling and isinstance(sampling, dict):
                hermes_srv["sampling"] = {
                    k: v for k, v in {
                        "enabled": sampling.get("enabled"),
                        "model": sampling.get("model"),
                        "max_tokens_cap": sampling.get("maxTokensCap") or sampling.get("max_tokens_cap"),
                        "timeout": sampling.get("timeout"),
                        "max_rpm": sampling.get("maxRpm") or sampling.get("max_rpm"),
                    }.items() if v is not None
                }

            existing_mcp[name] = hermes_srv
            added += 1
            self.record("mcp-servers", f"mcp.servers.{name}", f"config.yaml mcp_servers.{name}",
                        "migrated", servers_added=added)

        if added > 0 and self.execute:
            self.maybe_backup(hermes_cfg_path)
            hermes_cfg["mcp_servers"] = existing_mcp
            dump_yaml_file(hermes_cfg_path, hermes_cfg)

    # ── Plugins ───────────────────────────────────────────────
    def migrate_plugins_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        plugins = config.get("plugins") or {}
        if not plugins:
            self.record("plugins-config", None, None, "skipped", "No plugins configuration found")
            return

        # Archive the full plugins config
        if self.archive_dir and self.execute:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            dest = self.archive_dir / "plugins-config.json"
            dest.write_text(json.dumps(plugins, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.record("plugins-config", "openclaw.json plugins.*", str(dest), "archived",
                        "Plugins config archived for manual review")
        else:
            self.record("plugins-config", "openclaw.json plugins.*", "archive/plugins-config.json",
                        "archived" if not self.execute else "migrated", "Would archive plugins config")

        # Copy extensions directory if it exists
        ext_dir = self.source_root / "extensions"
        if ext_dir.is_dir() and self.archive_dir:
            dest_ext = self.archive_dir / "extensions"
            if self.execute:
                shutil.copytree(ext_dir, dest_ext, dirs_exist_ok=True)
            self.record("plugins-config", str(ext_dir), str(dest_ext), "archived",
                        "Extensions directory archived")

        # Extract any plugin env vars
        entries = plugins.get("entries") or {}
        for plugin_name, plugin_cfg in entries.items():
            if isinstance(plugin_cfg, dict):
                env_vars = plugin_cfg.get("env") or {}
                api_key = plugin_cfg.get("apiKey")
                if api_key and self.migrate_secrets:
                    env_key = f"PLUGIN_{plugin_name.upper().replace('-', '_')}_API_KEY"
                    self._set_env_var(env_key, api_key, f"plugins.entries.{plugin_name}.apiKey")

    # ── Cron jobs ─────────────────────────────────────────────
    def migrate_cron_jobs(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        cron = config.get("cron") or {}
        cron_store = self.source_root / "cron"
        found_any = False

        # Archive the full cron config when present
        if cron:
            found_any = True
            if self.archive_dir and self.execute:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                dest = self.archive_dir / "cron-config.json"
                dest.write_text(json.dumps(cron, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                self.record("cron-jobs", "openclaw.json cron.*", str(dest), "archived",
                            "Cron config archived. Use 'hermes cron' to recreate jobs manually.")
            else:
                self.record("cron-jobs", "openclaw.json cron.*", "archive/cron-config.json",
                            "archived", "Would archive cron config")

        # Also check for cron store files even when config.cron is missing
        if cron_store.is_dir() and self.archive_dir:
            found_any = True
            dest_cron = self.archive_dir / "cron-store"
            if self.execute:
                shutil.copytree(cron_store, dest_cron, dirs_exist_ok=True)
            self.record("cron-jobs", str(cron_store), str(dest_cron), "archived",
                        "Cron job store archived")

        if not found_any:
            self.record("cron-jobs", None, None, "skipped", "No cron configuration found")

    # ── Hooks ─────────────────────────────────────────────────
    def migrate_hooks_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        hooks = config.get("hooks") or {}
        if not hooks:
            self.record("hooks-config", None, None, "skipped", "No hooks configuration found")
            return

        # Archive the full hooks config
        if self.archive_dir and self.execute:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            dest = self.archive_dir / "hooks-config.json"
            dest.write_text(json.dumps(hooks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.record("hooks-config", "openclaw.json hooks.*", str(dest), "archived",
                        "Hooks config archived for manual review")
        else:
            self.record("hooks-config", "openclaw.json hooks.*", "archive/hooks-config.json",
                        "archived", "Would archive hooks config")

        # Copy workspace hooks directory
        for ws_name in ("workspace", "workspace.default"):
            hooks_dir = self.source_root / ws_name / "hooks"
            if hooks_dir.is_dir() and self.archive_dir:
                dest_hooks = self.archive_dir / "workspace-hooks"
                if self.execute:
                    shutil.copytree(hooks_dir, dest_hooks, dirs_exist_ok=True)
                self.record("hooks-config", str(hooks_dir), str(dest_hooks), "archived",
                            "Workspace hooks directory archived")
                break

    # ── Agent config ──────────────────────────────────────────
    def migrate_agent_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        agents = config.get("agents") or {}
        defaults = agents.get("defaults") or {}
        agent_list = agents.get("list") or []

        if not defaults and not agent_list:
            self.record("agent-config", None, None, "skipped", "No agent configuration found")
            return

        hermes_cfg_path = self.target_root / "config.yaml"
        hermes_cfg = load_yaml_file(hermes_cfg_path)
        changes = False

        # Map agent defaults
        agent_cfg = hermes_cfg.get("agent") or {}
        if defaults.get("contextTokens"):
            # No direct mapping but useful context
            pass
        if defaults.get("timeoutSeconds"):
            agent_cfg["max_turns"] = min(defaults["timeoutSeconds"] // 10, 200)
            changes = True
        if defaults.get("verboseDefault"):
            agent_cfg["verbose"] = defaults["verboseDefault"]
            changes = True
        if defaults.get("thinkingDefault"):
            # Map OpenClaw thinking -> Hermes reasoning_effort
            thinking = defaults["thinkingDefault"]
            if thinking in {"always", "high", "xhigh"}:
                agent_cfg["reasoning_effort"] = "high"
            elif thinking in {"auto", "medium", "adaptive"}:
                agent_cfg["reasoning_effort"] = "medium"
            elif thinking in {"off", "low", "none", "minimal"}:
                agent_cfg["reasoning_effort"] = "low"
            changes = True

        # Map compaction -> compression
        compaction = defaults.get("compaction") or {}
        if compaction:
            compression = hermes_cfg.get("compression") or {}
            if compaction.get("mode") == "off":
                compression["enabled"] = False
            else:
                compression["enabled"] = True
            if compaction.get("timeout"):
                pass  # No direct mapping
            if compaction.get("model"):
                aux = hermes_cfg.setdefault("auxiliary", {})
                aux_comp = aux.setdefault("compression", {})
                aux_comp["model"] = compaction["model"]
            hermes_cfg["compression"] = compression
            changes = True

        # Map humanDelay
        human_delay = defaults.get("humanDelay") or {}
        if human_delay:
            hd = hermes_cfg.get("human_delay") or {}
            hd_mode = human_delay.get("mode") or ("natural" if human_delay.get("enabled") else None)
            if hd_mode and hd_mode != "off":
                hd["mode"] = hd_mode
            if human_delay.get("minMs"):
                hd["min_ms"] = human_delay["minMs"]
            if human_delay.get("maxMs"):
                hd["max_ms"] = human_delay["maxMs"]
            hermes_cfg["human_delay"] = hd
            changes = True

        # Map userTimezone
        if defaults.get("userTimezone"):
            hermes_cfg["timezone"] = defaults["userTimezone"]
            changes = True

        # Map terminal/exec settings
        exec_cfg = (config.get("tools") or {}).get("exec") or {}
        if exec_cfg:
            terminal_cfg = hermes_cfg.get("terminal") or {}
            if exec_cfg.get("timeoutSec") or exec_cfg.get("timeout"):
                terminal_cfg["timeout"] = exec_cfg.get("timeoutSec") or exec_cfg.get("timeout")
                changes = True
            hermes_cfg["terminal"] = terminal_cfg

        # Map sandbox -> terminal docker settings
        sandbox = defaults.get("sandbox") or {}
        if sandbox and sandbox.get("backend") == "docker":
            terminal_cfg = hermes_cfg.get("terminal") or {}
            terminal_cfg["backend"] = "docker"
            if sandbox.get("docker", {}).get("image"):
                terminal_cfg["docker_image"] = sandbox["docker"]["image"]
            hermes_cfg["terminal"] = terminal_cfg
            changes = True

        if changes:
            hermes_cfg["agent"] = agent_cfg
            if self.execute:
                self.maybe_backup(hermes_cfg_path)
                dump_yaml_file(hermes_cfg_path, hermes_cfg)
            self.record("agent-config", "openclaw.json agents.defaults", "config.yaml agent/compression/terminal",
                        "migrated", "Agent defaults mapped to Hermes config")

        # Archive multi-agent list
        if agent_list:
            if self.archive_dir and self.execute:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                dest = self.archive_dir / "agents-list.json"
                dest.write_text(json.dumps(agent_list, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.record("agent-config", "openclaw.json agents.list", "archive/agents-list.json",
                        "archived", f"Multi-agent setup ({len(agent_list)} agents) archived for manual recreation")

        # Archive bindings
        bindings = config.get("bindings") or []
        if bindings:
            if self.archive_dir and self.execute:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                dest = self.archive_dir / "bindings.json"
                dest.write_text(json.dumps(bindings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.record("agent-config", "openclaw.json bindings", "archive/bindings.json",
                        "archived", f"Agent routing bindings ({len(bindings)} rules) archived")

    # ── Gateway config ────────────────────────────────────────
    def migrate_gateway_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        gateway = config.get("gateway") or {}
        if not gateway:
            self.record("gateway-config", None, None, "skipped", "No gateway configuration found")
            return

        # Archive the full gateway config (complex, many settings)
        if self.archive_dir and self.execute:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            dest = self.archive_dir / "gateway-config.json"
            dest.write_text(json.dumps(gateway, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.record("gateway-config", "openclaw.json gateway.*", "archive/gateway-config.json",
                    "archived", "Gateway config archived. Use 'hermes gateway' to configure.")

        # Extract gateway auth token to .env if present
        auth = gateway.get("auth") or {}
        if auth.get("token") and self.migrate_secrets:
            self._set_env_var("HERMES_GATEWAY_TOKEN", auth["token"], "gateway.auth.token")

    # ── Session config ────────────────────────────────────────
    def migrate_session_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        session = config.get("session") or {}
        if not session:
            self.record("session-config", None, None, "skipped", "No session configuration found")
            return

        hermes_cfg_path = self.target_root / "config.yaml"
        hermes_cfg = load_yaml_file(hermes_cfg_path)
        sr = hermes_cfg.get("session_reset") or {}
        changes = False

        # OpenClaw uses session.reset (structured) and session.resetTriggers (string array)
        reset = session.get("reset") or {}
        reset_triggers = session.get("resetTriggers") or session.get("reset_triggers") or []

        if reset:
            # Structured reset config: has mode, atHour, idleMinutes
            mode = reset.get("mode", "")
            if mode == "daily":
                sr["mode"] = "daily"
            elif mode == "idle":
                sr["mode"] = "idle"
            else:
                sr["mode"] = mode or "none"
            if reset.get("atHour") is not None:
                sr["at_hour"] = reset["atHour"]
            if reset.get("idleMinutes"):
                sr["idle_minutes"] = reset["idleMinutes"]
            changes = True
        elif isinstance(reset_triggers, list) and reset_triggers:
            # Simple string triggers: ["daily", "idle"]
            has_daily = "daily" in reset_triggers
            has_idle = "idle" in reset_triggers
            if has_daily and has_idle:
                sr["mode"] = "both"
            elif has_daily:
                sr["mode"] = "daily"
            elif has_idle:
                sr["mode"] = "idle"
            changes = True

        if changes:
            hermes_cfg["session_reset"] = sr
            if self.execute:
                self.maybe_backup(hermes_cfg_path)
                dump_yaml_file(hermes_cfg_path, hermes_cfg)
            self.record("session-config", "openclaw.json session.resetTriggers",
                        "config.yaml session_reset", "migrated")

        # Archive full session config (identity links, thread bindings, etc.)
        complex_keys = {"identityLinks", "threadBindings", "maintenance", "scope", "sendPolicy"}
        complex_session = {k: v for k, v in session.items() if k in complex_keys and v}
        if complex_session and self.archive_dir:
            if self.execute:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                dest = self.archive_dir / "session-config.json"
                dest.write_text(json.dumps(complex_session, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.record("session-config", "openclaw.json session (advanced)",
                        "archive/session-config.json", "archived",
                        "Advanced session settings archived (identity links, thread bindings, etc.)")

    # ── Full model providers ──────────────────────────────────
    def migrate_full_providers(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        models = config.get("models") or {}
        providers = models.get("providers") or {}
        if not providers:
            self.record("full-providers", None, None, "skipped", "No model providers found")
            return

        hermes_cfg_path = self.target_root / "config.yaml"
        hermes_cfg = load_yaml_file(hermes_cfg_path)
        custom_providers = hermes_cfg.get("custom_providers") or []
        added = 0

        # Well-known providers: just extract API keys
        WELL_KNOWN = {"openrouter", "openai", "anthropic", "deepseek", "google", "groq"}

        for prov_name, prov_cfg in providers.items():
            if not isinstance(prov_cfg, dict):
                continue

            # Extract API key to .env
            api_key = prov_cfg.get("apiKey") or prov_cfg.get("api_key")
            if api_key and self.migrate_secrets:
                env_key = f"{prov_name.upper().replace('-', '_')}_API_KEY"
                self._set_env_var(env_key, api_key, f"models.providers.{prov_name}.apiKey")

            # For non-well-known providers, create custom_providers entry
            if prov_name.lower() not in WELL_KNOWN and prov_cfg.get("baseUrl"):
                # Check if already exists
                existing_names = {p.get("name", "").lower() for p in custom_providers}
                if prov_name.lower() in existing_names and not self.overwrite:
                    self.record("full-providers", f"models.providers.{prov_name}",
                                "config.yaml custom_providers", "conflict",
                                f"Provider '{prov_name}' already exists")
                    continue

                api_type = prov_cfg.get("apiType") or prov_cfg.get("api") or prov_cfg.get("type") or "openai"
                api_mode_map = {
                    "openai": "chat_completions",
                    "openai-completions": "chat_completions",
                    "openai-responses": "chat_completions",
                    "anthropic": "anthropic_messages",
                    "anthropic-messages": "anthropic_messages",
                    "google-generative-ai": "chat_completions",
                    "cohere": "chat_completions",
                }
                entry = {
                    "name": prov_name,
                    "base_url": prov_cfg["baseUrl"],
                    "api_key": "",  # referenced from .env
                    "api_mode": api_mode_map.get(api_type, "chat_completions"),
                }
                custom_providers.append(entry)
                added += 1
                self.record("full-providers", f"models.providers.{prov_name}",
                            f"config.yaml custom_providers[{prov_name}]", "migrated")

        if added > 0 and self.execute:
            self.maybe_backup(hermes_cfg_path)
            hermes_cfg["custom_providers"] = custom_providers
            dump_yaml_file(hermes_cfg_path, hermes_cfg)

        # Archive model aliases/catalog
        agent_defaults = (config.get("agents") or {}).get("defaults") or {}
        model_aliases = agent_defaults.get("models") or {}
        if model_aliases:
            if self.archive_dir and self.execute:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                dest = self.archive_dir / "model-aliases.json"
                dest.write_text(json.dumps(model_aliases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.record("full-providers", "agents.defaults.models", "archive/model-aliases.json",
                        "archived", f"Model aliases/catalog ({len(model_aliases)} entries) archived")

    # ── Deep channel config ───────────────────────────────────
    def migrate_deep_channels(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        channels = config.get("channels") or {}
        if not channels:
            self.record("deep-channels", None, None, "skipped", "No channel configuration found")
            return

        # Extended channel token/allowlist mapping
        CHANNEL_ENV_MAP = {
            "matrix": {"token": "MATRIX...OKEN", "tokenField": "accessToken", "allowFrom": "MATRIX_ALLOWED_USERS",
                        "extras": {"homeserverUrl": "MATRIX_HOMESERVER_URL", "userId": "MATRIX_USER_ID"}},
            "mattermost": {"token": "MATTERMOST_BOT_TOKEN", "allowFrom": "MATTERMOST_ALLOWED_USERS",
                           "extras": {"url": "MATTERMOST_URL", "teamId": "MATTERMOST_TEAM_ID"}},
            "irc": {"extras": {"server": "IRC_SERVER", "nick": "IRC_NICK", "channels": "IRC_CHANNELS"}},
            "googlechat": {"extras": {"serviceAccountKeyPath": "GOOGLE_CHAT_SA_KEY_PATH"}},
            "imessage": {},
            "bluebubbles": {"extras": {"server": "BLUEBUBBLES_SERVER", "password": "BLUEBUBBLES_PASSWORD"}},
            "msteams": {"token": "MSTEAMS_BOT_TOKEN", "allowFrom": "MSTEAMS_ALLOWED_USERS"},
            "nostr": {"extras": {"nsec": "NOSTR_NSEC", "relays": "NOSTR_RELAYS"}},
            "twitch": {"token": "TWITCH_BOT_TOKEN", "extras": {"channels": "TWITCH_CHANNELS"}},
        }

        for ch_name, ch_mapping in CHANNEL_ENV_MAP.items():
            ch_cfg = channels.get(ch_name) or {}
            if not ch_cfg:
                continue

            # Extract tokens (check flat path, then accounts.default)
            token_field = ch_mapping.get("tokenField", "botToken")
            bot_token = self._get_channel_field(ch_cfg, token_field)
            if ch_mapping.get("token") and bot_token and self.migrate_secrets:
                self._set_env_var(ch_mapping["token"], str(bot_token),
                                  f"channels.{ch_name}.{token_field}")
            allow_val = self._get_channel_field(ch_cfg, "allowFrom")
            if ch_mapping.get("allowFrom") and allow_val:
                if isinstance(allow_val, list):
                    allow_val = ",".join(str(x) for x in allow_val)
                self._set_env_var(ch_mapping["allowFrom"], str(allow_val),
                                  f"channels.{ch_name}.allowFrom")
            # Extra fields
            for oc_key, env_key in (ch_mapping.get("extras") or {}).items():
                val = self._get_channel_field(ch_cfg, oc_key)
                if val:
                    if isinstance(val, list):
                        val = ",".join(str(x) for x in val)
                    is_secret = "password" in oc_key.lower() or "token" in oc_key.lower() or "nsec" in oc_key.lower()
                    if is_secret and not self.migrate_secrets:
                        continue
                    self._set_env_var(env_key, str(val), f"channels.{ch_name}.{oc_key}")

        # Map Discord-specific settings to Hermes config
        discord_cfg = channels.get("discord") or {}
        if discord_cfg:
            hermes_cfg_path = self.target_root / "config.yaml"
            hermes_cfg = load_yaml_file(hermes_cfg_path)
            discord_hermes = hermes_cfg.get("discord") or {}
            changed = False
            if "requireMention" in discord_cfg:
                discord_hermes["require_mention"] = discord_cfg["requireMention"]
                changed = True
            if discord_cfg.get("autoThread") is not None:
                discord_hermes["auto_thread"] = discord_cfg["autoThread"]
                changed = True
            if changed and self.execute:
                hermes_cfg["discord"] = discord_hermes
                dump_yaml_file(hermes_cfg_path, hermes_cfg)

        # Archive complex channel configs (group settings, thread bindings, etc.)
        complex_archive = {}
        for ch_name, ch_cfg in channels.items():
            if not isinstance(ch_cfg, dict):
                continue
            complex_keys = {k: v for k, v in ch_cfg.items()
                          if k not in {"botToken", "appToken", "allowFrom", "enabled"}
                          and v and k not in {"requireMention", "autoThread"}}
            if complex_keys:
                complex_archive[ch_name] = complex_keys

        if complex_archive and self.archive_dir:
            if self.execute:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                dest = self.archive_dir / "channels-deep-config.json"
                dest.write_text(json.dumps(complex_archive, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.record("deep-channels", "openclaw.json channels (advanced settings)",
                        "archive/channels-deep-config.json", "archived",
                        f"Deep channel config for {len(complex_archive)} channels archived")

    # ── Browser config ────────────────────────────────────────
    def migrate_browser_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        browser = config.get("browser") or {}
        if not browser:
            self.record("browser-config", None, None, "skipped", "No browser configuration found")
            return

        hermes_cfg_path = self.target_root / "config.yaml"
        hermes_cfg = load_yaml_file(hermes_cfg_path)
        browser_hermes = hermes_cfg.get("browser") or {}
        changed = False

        # Map fields that have Hermes equivalents
        if browser.get("cdpUrl"):
            browser_hermes["cdp_url"] = browser["cdpUrl"]
            changed = True
        if browser.get("headless") is not None:
            browser_hermes["headless"] = browser["headless"]
            changed = True

        if changed:
            hermes_cfg["browser"] = browser_hermes
            if self.execute:
                self.maybe_backup(hermes_cfg_path)
                dump_yaml_file(hermes_cfg_path, hermes_cfg)
            self.record("browser-config", "openclaw.json browser.*", "config.yaml browser",
                        "migrated")

        # Archive remaining browser settings
        advanced = {k: v for k, v in browser.items()
                   if k not in {"cdpUrl", "headless"} and v}
        if advanced and self.archive_dir:
            if self.execute:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                dest = self.archive_dir / "browser-config.json"
                dest.write_text(json.dumps(advanced, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.record("browser-config", "openclaw.json browser (advanced)",
                        "archive/browser-config.json", "archived")

    # ── Tools config ──────────────────────────────────────────
    def migrate_tools_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        tools = config.get("tools") or {}
        if not tools:
            self.record("tools-config", None, None, "skipped", "No tools configuration found")
            return

        hermes_cfg_path = self.target_root / "config.yaml"
        hermes_cfg = load_yaml_file(hermes_cfg_path)
        changed = False

        # Map exec timeout -> terminal timeout (field is timeoutSec in OpenClaw)
        exec_cfg = tools.get("exec") or {}
        timeout_val = exec_cfg.get("timeoutSec") or exec_cfg.get("timeout")
        if timeout_val:
            terminal_cfg = hermes_cfg.get("terminal") or {}
            terminal_cfg["timeout"] = timeout_val
            hermes_cfg["terminal"] = terminal_cfg
            changed = True

        # Map web search API key (path: tools.web.search.brave.apiKey in OpenClaw)
        web_cfg = tools.get("web") or tools.get("webSearch") or {}
        search_cfg = web_cfg.get("search") or web_cfg if not web_cfg.get("search") else web_cfg["search"]
        brave_cfg = search_cfg.get("brave") or {}
        brave_key = brave_cfg.get("apiKey") or search_cfg.get("braveApiKey") or web_cfg.get("braveApiKey")
        if brave_key and isinstance(brave_key, str) and self.migrate_secrets:
            self._set_env_var("BRAVE_API_KEY", brave_key, "tools.web.search.brave.apiKey")

        if changed and self.execute:
            self.maybe_backup(hermes_cfg_path)
            dump_yaml_file(hermes_cfg_path, hermes_cfg)
            self.record("tools-config", "openclaw.json tools.*", "config.yaml terminal",
                        "migrated")

        # Archive full tools config
        if self.archive_dir:
            if self.execute:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                dest = self.archive_dir / "tools-config.json"
                dest.write_text(json.dumps(tools, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.record("tools-config", "openclaw.json tools (full)", "archive/tools-config.json",
                        "archived", "Full tools config archived for reference")

    # ── Approvals config ──────────────────────────────────────
    def migrate_approvals_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        approvals = config.get("approvals") or {}
        if not approvals:
            self.record("approvals-config", None, None, "skipped", "No approvals configuration found")
            return

        hermes_cfg_path = self.target_root / "config.yaml"
        hermes_cfg = load_yaml_file(hermes_cfg_path)

        # Map approval mode (nested under approvals.exec.mode in OpenClaw)
        exec_approvals = approvals.get("exec") or {}
        mode = (exec_approvals.get("mode") if isinstance(exec_approvals, dict) else None) or approvals.get("mode") or approvals.get("defaultMode")
        if mode:
            mode_map = {"auto": "off", "always": "manual", "smart": "smart", "manual": "manual"}
            hermes_mode = mode_map.get(mode, "manual")
            hermes_cfg.setdefault("approvals", {})["mode"] = hermes_mode
            if self.execute:
                self.maybe_backup(hermes_cfg_path)
                dump_yaml_file(hermes_cfg_path, hermes_cfg)
            self.record("approvals-config", "openclaw.json approvals.mode",
                        "config.yaml approvals.mode", "migrated", f"Mapped '{mode}' -> '{hermes_mode}'")

        # Archive full approvals config
        if len(approvals) > 1 and self.archive_dir:
            if self.execute:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                dest = self.archive_dir / "approvals-config.json"
                dest.write_text(json.dumps(approvals, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.record("approvals-config", "openclaw.json approvals (rules)",
                        "archive/approvals-config.json", "archived")

    # ── Memory backend ────────────────────────────────────────
    def migrate_memory_backend(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        memory = config.get("memory") or {}
        if not memory:
            self.record("memory-backend", None, None, "skipped", "No memory backend configuration found")
            return

        if self.archive_dir and self.execute:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            dest = self.archive_dir / "memory-backend-config.json"
            dest.write_text(json.dumps(memory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.record("memory-backend", "openclaw.json memory.*", "archive/memory-backend-config.json",
                    "archived", "Memory backend config (QMD, vector search, citations) archived for manual review")

    # ── Skills config ─────────────────────────────────────────
    def migrate_skills_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        skills = config.get("skills") or {}
        entries = skills.get("entries") or {}
        if not entries and not skills:
            self.record("skills-config", None, None, "skipped", "No skills registry configuration found")
            return

        if self.archive_dir and self.execute:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            dest = self.archive_dir / "skills-registry-config.json"
            dest.write_text(json.dumps(skills, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.record("skills-config", "openclaw.json skills.*", "archive/skills-registry-config.json",
                    "archived", f"Skills registry config ({len(entries)} entries) archived")

    # ── UI / Identity ─────────────────────────────────────────
    def migrate_ui_identity(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        ui = config.get("ui") or {}
        if not ui:
            self.record("ui-identity", None, None, "skipped", "No UI/identity configuration found")
            return

        if self.archive_dir and self.execute:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            dest = self.archive_dir / "ui-identity-config.json"
            dest.write_text(json.dumps(ui, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.record("ui-identity", "openclaw.json ui.*", "archive/ui-identity-config.json",
                    "archived", "UI theme and identity settings archived")

    # ── Logging / Diagnostics ─────────────────────────────────
    def migrate_logging_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or self.load_openclaw_config()
        logging_cfg = config.get("logging") or {}
        diagnostics = config.get("diagnostics") or {}
        combined = {}
        if logging_cfg:
            combined["logging"] = logging_cfg
        if diagnostics:
            combined["diagnostics"] = diagnostics
        if not combined:
            self.record("logging-config", None, None, "skipped", "No logging/diagnostics configuration found")
            return

        if self.archive_dir and self.execute:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            dest = self.archive_dir / "logging-diagnostics-config.json"
            dest.write_text(json.dumps(combined, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.record("logging-config", "openclaw.json logging/diagnostics",
                    "archive/logging-diagnostics-config.json", "archived")

    # ── Helper: set env var ───────────────────────────────────
    def _set_env_var(self, key: str, value: str, source_label: str) -> None:
        env_path = self.target_root / ".env"
        if self.execute:
            env_data = parse_env_file(env_path)
            if key in env_data and not self.overwrite:
                self.record("env-var", source_label, f".env {key}", "conflict",
                            f"Env var {key} already set")
                return
            env_data[key] = value
            save_env_file(env_path, env_data)
        self.record("env-var", source_label, f".env {key}", "migrated")

    # ── Generate migration notes ──────────────────────────────
    def generate_migration_notes(self) -> None:
        if not self.output_dir:
            return
        notes = [
            "# OpenClaw -> Hermes Migration Notes",
            "",
            "This document lists items that require manual attention after migration.",
            "",
            "## PM2 / External Processes",
            "",
            "Your PM2 processes (Discord bots, Telegram bots, etc.) are NOT affected",
            "by this migration. They run independently and will continue working.",
            "No action needed for PM2-managed processes.",
            "",
        ]

        archived = [i for i in self.items if i.status == "archived"]
        if archived:
            notes.extend([
                "## Archived Items (Manual Review Needed)",
                "",
                "These OpenClaw configurations were archived because they don't have a",
                "direct 1:1 mapping in Hermes. Review each file and recreate manually:",
                "",
            ])
            for item in archived:
                notes.append(f"- **{item.kind}**: `{item.destination}` -- {item.reason}")
            notes.append("")

        conflicts = [i for i in self.items if i.status == "conflict"]
        if conflicts:
            notes.extend([
                "## Conflicts (Existing Hermes Config Not Overwritten)",
                "",
                "These items already existed in your Hermes config. Re-run with",
                "`--overwrite` to force, or merge manually:",
                "",
            ])
            for item in conflicts:
                notes.append(f"- **{item.kind}**: {item.reason}")
            notes.append("")

        has_cron_config_archive = any(
            i.kind == "cron-jobs" and i.status == "archived" and i.destination and i.destination.endswith("cron-config.json")
            for i in self.items
        )
        has_cron_store_archive = any(
            i.kind == "cron-jobs" and i.status == "archived" and i.destination and i.destination.endswith("cron-store")
            for i in self.items
        )

        notes.extend([
            "## IMPORTANT: Archive the OpenClaw Directory",
            "",
            "After migration, your OpenClaw directory still exists on disk with workspace",
            "state files (todo.json, sessions, logs). If the Hermes agent discovers these",
            "directories, it may read/write to them instead of the Hermes state, causing",
            "confusion (e.g., cron jobs reading a different todo list than interactive sessions).",
            "",
            "**Strongly recommended:** Run `hermes claw cleanup` to rename the OpenClaw",
            "directory to `.openclaw.pre-migration`. This prevents the agent from finding it.",
            "The directory is renamed, not deleted — you can undo this at any time.",
            "",
            "If you skip this step and notice the agent getting confused about workspaces",
            "or todo lists, run `hermes claw cleanup` to fix it.",
            "",
            "## Hermes-Specific Setup",
            "",
            "After migration, you may want to:",
            "- Run `hermes claw cleanup` to archive the OpenClaw directory (prevents state confusion)",
            "- Run `hermes setup` to configure any remaining settings",
            "- Run `hermes mcp list` to verify MCP servers were imported correctly",
        ])

        if has_cron_config_archive:
            notes.append("- Run `hermes cron` to recreate scheduled tasks (see archive/cron-config.json)")
        elif has_cron_store_archive:
            notes.append("- Run `hermes cron` to recreate scheduled tasks (see archived cron-store)")

        # Check if skills were imported
        has_skills = any(i.kind == "skills" and i.status == "migrated" for i in self.items)
        if has_skills:
            notes.extend([
                "",
                "## Imported Skills",
                "",
                "Imported skills require a new session to take effect. After migration,",
                "restart your agent or start a new chat session, then run `/skills`",
                "to verify they loaded correctly.",
                "",
            ])

        # Check if WhatsApp was detected
        has_whatsapp = any(i.kind == "whatsapp-settings" and i.status == "migrated" for i in self.items)
        if has_whatsapp:
            notes.extend([
                "",
                "## WhatsApp Requires Re-Pairing",
                "",
                "WhatsApp uses QR-code pairing, not token-based auth. Your allowlist",
                "was migrated, but you must re-pair the device by running:",
                "",
                "    hermes whatsapp",
                "",
            ])

        notes.extend([
            "- Run `hermes gateway install` if you need the gateway service",
            "- Review `~/.hermes/config.yaml` for any adjustments",
            "",
        ])

        if self.execute:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / "MIGRATION_NOTES.md").write_text(
                "\n".join(notes) + "\n", encoding="utf-8"
            )
