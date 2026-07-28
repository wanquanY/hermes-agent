#!/usr/bin/env python3
"""Shared contracts for the OpenClaw to Hermes migration helper.

This script migrates the parts of an OpenClaw user footprint that map cleanly
into Hermes Agent, archives selected unmapped docs for manual review, and
reports exactly what was skipped and why.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except Exception:  # pragma: no cover - handled at runtime
    yaml = None


ENTRY_DELIMITER = "\n§\n"
DEFAULT_MEMORY_CHAR_LIMIT = 2200
DEFAULT_USER_CHAR_LIMIT = 1375
SKILL_CATEGORY_DIRNAME = "openclaw-imports"
SKILL_CATEGORY_DESCRIPTION = (
    "Skills migrated from an OpenClaw workspace."
)
SKILL_CONFLICT_MODES = {"skip", "overwrite", "rename"}
SUPPORTED_SECRET_TARGETS={
    "TELEGRAM_BOT_TOKEN",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ELEVENLABS_API_KEY",
    "VOICE_TOOLS_OPENAI_KEY",
}
WORKSPACE_INSTRUCTIONS_FILENAME = "AGENTS" + ".md"
MIGRATION_OPTION_METADATA: Dict[str, Dict[str, str]] = {
    "soul": {
        "label": "SOUL.md",
        "description": "Import the OpenClaw persona file into Hermes.",
    },
    "workspace-agents": {
        "label": "Workspace instructions",
        "description": "Copy the OpenClaw workspace instructions file into a chosen workspace.",
    },
    "memory": {
        "label": "MEMORY.md",
        "description": "Import long-term memory entries into Hermes memories.",
    },
    "user-profile": {
        "label": "USER.md",
        "description": "Import user profile entries into Hermes memories.",
    },
    "messaging-settings": {
        "label": "Messaging settings",
        "description": "Import Hermes-compatible messaging settings such as allowlists and working directory.",
    },
    "secret-settings": {
        "label": "Allowlisted secrets",
        "description": "Import the small allowlist of Hermes-compatible secrets when explicitly enabled.",
    },
    "command-allowlist": {
        "label": "Command allowlist",
        "description": "Merge OpenClaw exec approval patterns into Hermes command_allowlist.",
    },
    "skills": {
        "label": "User skills",
        "description": "Copy OpenClaw skills into ~/.hermes/skills/openclaw-imports/.",
    },
    "tts-assets": {
        "label": "TTS assets",
        "description": "Copy compatible workspace TTS assets into ~/.hermes/tts/.",
    },
    "discord-settings": {
        "label": "Discord settings",
        "description": "Import Discord bot token and allowlist into Hermes .env.",
    },
    "slack-settings": {
        "label": "Slack settings",
        "description": "Import Slack bot/app tokens and allowlist into Hermes .env.",
    },
    "whatsapp-settings": {
        "label": "WhatsApp settings",
        "description": "Import WhatsApp allowlist into Hermes .env.",
    },
    "signal-settings": {
        "label": "Signal settings",
        "description": "Import Signal account, HTTP URL, and allowlist into Hermes .env.",
    },
    "provider-keys": {
        "label": "Provider API keys",
        "description": "Import model provider API keys into Hermes .env (requires --migrate-secrets).",
    },
    "model-config": {
        "label": "Default model",
        "description": "Import the default model setting into Hermes config.yaml.",
    },
    "tts-config": {
        "label": "TTS configuration",
        "description": "Import TTS provider and voice settings into Hermes config.yaml.",
    },
    "shared-skills": {
        "label": "Shared skills",
        "description": "Copy shared OpenClaw skills from ~/.openclaw/skills/ into Hermes.",
    },
    "daily-memory": {
        "label": "Daily memory files",
        "description": "Merge daily memory entries from workspace/memory/ into Hermes MEMORY.md.",
    },
    "archive": {
        "label": "Archive unmapped docs",
        "description": "Archive compatible-but-unmapped docs for later manual review.",
    },
    "mcp-servers": {
        "label": "MCP servers",
        "description": "Import MCP server definitions from OpenClaw into Hermes config.yaml.",
    },
    "plugins-config": {
        "label": "Plugins configuration",
        "description": "Archive OpenClaw plugin configuration and installed extensions for manual review.",
    },
    "cron-jobs": {
        "label": "Cron / scheduled tasks",
        "description": "Import cron job definitions. Archive for manual recreation via 'hermes cron'.",
    },
    "hooks-config": {
        "label": "Hooks and webhooks",
        "description": "Archive OpenClaw hook configuration (internal hooks, webhooks, Gmail integration).",
    },
    "agent-config": {
        "label": "Agent defaults and multi-agent setup",
        "description": "Import agent defaults (compaction, context, thinking) into Hermes config. Archive multi-agent list.",
    },
    "gateway-config": {
        "label": "Gateway configuration",
        "description": "Import gateway port and auth settings. Archive full gateway config for manual setup.",
    },
    "session-config": {
        "label": "Session configuration",
        "description": "Import session reset policies (daily/idle) into Hermes session_reset config.",
    },
    "full-providers": {
        "label": "Full model provider definitions",
        "description": "Import custom model providers (baseUrl, apiType, headers) into Hermes custom_providers.",
    },
    "deep-channels": {
        "label": "Deep channel configuration",
        "description": "Import extended channel settings (Matrix, Mattermost, IRC, group configs). Archive complex settings.",
    },
    "browser-config": {
        "label": "Browser configuration",
        "description": "Import browser automation settings into Hermes config.yaml.",
    },
    "tools-config": {
        "label": "Tools configuration",
        "description": "Import tool settings (exec timeout, sandbox, web search) into Hermes config.yaml.",
    },
    "approvals-config": {
        "label": "Approval rules",
        "description": "Import approval mode and rules into Hermes config.yaml approvals section.",
    },
    "memory-backend": {
        "label": "Memory backend configuration",
        "description": "Archive OpenClaw memory backend settings (QMD, vector search, citations) for manual review.",
    },
    "skills-config": {
        "label": "Skills registry configuration",
        "description": "Archive per-skill enabled/config/env settings from OpenClaw skills.entries.",
    },
    "ui-identity": {
        "label": "UI and identity settings",
        "description": "Archive OpenClaw UI theme, assistant identity, and display preferences.",
    },
    "logging-config": {
        "label": "Logging and diagnostics",
        "description": "Archive OpenClaw logging and diagnostics configuration.",
    },
}
MIGRATION_PRESETS: Dict[str, set[str]] = {
    "user-data": {
        "soul",
        "workspace-agents",
        "memory",
        "user-profile",
        "messaging-settings",
        "command-allowlist",
        "skills",
        "tts-assets",
        "discord-settings",
        "slack-settings",
        "whatsapp-settings",
        "signal-settings",
        "model-config",
        "tts-config",
        "shared-skills",
        "daily-memory",
        "archive",
        "mcp-servers",
        "agent-config",
        "session-config",
        "browser-config",
        "tools-config",
        "approvals-config",
        "deep-channels",
        "full-providers",
        "plugins-config",
        "cron-jobs",
        "hooks-config",
        "memory-backend",
        "skills-config",
        "ui-identity",
        "logging-config",
        "gateway-config",
    },
    "full": set(MIGRATION_OPTION_METADATA),
}


# ───────────────────────────────────────────────────────────────────────
# Item shape constants — kept stable for downstream consumers of report.json.
# Inspired by OpenClaw's src/plugin-sdk/migration.ts so both sides speak the
# same vocabulary.  Values intentionally match the strings already produced
# by this script (migrated/archived/skipped/conflict/error) so the addition
# is backward-compatible.
# ───────────────────────────────────────────────────────────────────────
STATUS_MIGRATED = "migrated"
STATUS_ARCHIVED = "archived"
STATUS_SKIPPED = "skipped"
STATUS_CONFLICT = "conflict"
STATUS_ERROR = "error"
STATUS_PLANNED = "planned"

REASON_TARGET_EXISTS = "Target exists and overwrite is disabled"
REASON_BLOCKED_BY_APPLY_CONFLICT = "blocked by earlier apply conflict"


@dataclass
class ItemResult:
    kind: str
    source: Optional[str]
    destination: Optional[str]
    status: str
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    sensitive: bool = False


def parse_selection_values(values: Optional[Sequence[str]]) -> List[str]:
    parsed: List[str] = []
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip().lower()
            if part:
                parsed.append(part)
    return parsed


def resolve_selected_options(
    include: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
    preset: Optional[str] = None,
) -> set[str]:
    include_values = parse_selection_values(include)
    exclude_values = parse_selection_values(exclude)
    valid = set(MIGRATION_OPTION_METADATA)
    preset_name = (preset or "").strip().lower()

    if preset_name and preset_name not in MIGRATION_PRESETS:
        raise ValueError(
            "Unknown migration preset: "
            + preset_name
            + ". Valid presets: "
            + ", ".join(sorted(MIGRATION_PRESETS))
        )

    unknown = (set(include_values) - {"all"} - valid) | (set(exclude_values) - {"all"} - valid)
    if unknown:
        raise ValueError(
            "Unknown migration option(s): "
            + ", ".join(sorted(unknown))
            + ". Valid options: "
            + ", ".join(sorted(valid))
        )

    if preset_name:
        selected = set(MIGRATION_PRESETS[preset_name])
    elif not include_values or "all" in include_values:
        selected = set(valid)
    else:
        selected = set(include_values)

    if "all" in exclude_values:
        selected.clear()
    selected -= (set(exclude_values) - {"all"})
    return selected


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def resolve_secret_input(value: Any, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Resolve an OpenClaw SecretInput value to a plain string.

    SecretInput can be:
    - A plain string: "sk-..."
    - An env template: "${OPENROUTER_API_KEY}"
    - A SecretRef object: {"source": "env", "id": "OPENROUTER_API_KEY"}
    """
    if isinstance(value, str):
        # Check for env template: "${VAR_NAME}"
        m = re.match(r"^\$\{(\w+)\}$", value.strip())
        if m and env:
            return env.get(m.group(1), "").strip() or None
        return value.strip() or None
    if isinstance(value, dict):
        source = value.get("source", "")
        ref_id = value.get("id", "")
        if source == "env" and ref_id and env:
            return env.get(ref_id, "").strip() or None
        # File/exec sources can't be resolved here — return None
    return None


def load_yaml_file(path: Path) -> Dict[str, Any]:
    if yaml is None or not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def dump_yaml_file(path: Path, data: Dict[str, Any]) -> None:
    if yaml is None:
        raise RuntimeError("PyYAML is required to update Hermes config.yaml")
    ensure_parent(path)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def parse_env_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    data: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip()
    return data


def save_env_file(path: Path, data: Dict[str, str]) -> None:
    ensure_parent(path)
    lines = [f"{key}={value}" for key, value in data.items()]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def backup_existing(path: Path, backup_root: Path) -> Optional[Path]:
    if not path.exists():
        return None
    rel = Path(*path.parts[1:]) if path.is_absolute() and len(path.parts) > 1 else path
    dest = backup_root / rel
    ensure_parent(dest)
    if path.is_dir():
        shutil.copytree(path, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(path, dest)
    return dest


# ── Brand rewriting ─────────────────────────────────────────
# Replace OpenClaw brand names with Hermes in migrated text so that
# memory entries, user profiles, SOUL.md, and workspace instructions
# read as self-referential to the new agent identity.
#
# Case-preserving: ``OpenClaw`` → ``Hermes`` (prose), but lowercase matches
# like ``openclaw`` → ``hermes`` (so filesystem paths like ``~/.openclaw``
# become ``~/.hermes`` — the real Hermes home — not the broken ``~/.Hermes``).
_REBRAND_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'\bOpen[\s-]?Claw\b', re.IGNORECASE), 'Hermes'),
    (re.compile(r'\bClawdBot\b', re.IGNORECASE), 'Hermes'),
    (re.compile(r'\bMoltBot\b', re.IGNORECASE), 'Hermes'),
]


def _case_preserving_replacement(replacement: str):
    """Return a re.sub replacement fn that lowercases the result when the
    matched text was all-lowercase.

    Keeps ``OpenClaw`` → ``Hermes`` but maps ``openclaw`` → ``hermes`` so a
    filesystem path like ``~/.openclaw/config.yaml`` rewrites to
    ``~/.hermes/config.yaml`` (the real Hermes home) instead of the broken
    ``~/.Hermes/config.yaml``.
    """
    def _sub(match: "re.Match[str]") -> str:
        matched = match.group(0)
        if matched and matched.islower():
            return replacement.lower()
        return replacement
    return _sub


def rebrand_text(text: str) -> str:
    """Replace OpenClaw / ClawdBot / MoltBot brand names with Hermes.

    Preserves case so filesystem-path matches (lowercase) don't become
    capitalized directory names that don't exist.
    """
    for pattern, replacement in _REBRAND_PATTERNS:
        text = pattern.sub(_case_preserving_replacement(replacement), text)
    return text


def parse_existing_memory_entries(path: Path) -> List[str]:
    if not path.exists():
        return []
    raw = read_text(path)
    if not raw.strip():
        return []
    if ENTRY_DELIMITER in raw:
        return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
    return extract_markdown_entries(raw)


def extract_markdown_entries(text: str) -> List[str]:
    entries: List[str] = []
    headings: List[str] = []
    paragraph_lines: List[str] = []

    def context_prefix() -> str:
        filtered = [h for h in headings if h and not re.search(r"\b(MEMORY|USER|SOUL|AGENTS|TOOLS|IDENTITY)\.md\b", h, re.I)]
        return " > ".join(filtered)

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        text_block = " ".join(line.strip() for line in paragraph_lines).strip()
        paragraph_lines = []
        if not text_block:
            return
        prefix = context_prefix()
        if prefix:
            entries.append(f"{prefix}: {text_block}")
        else:
            entries.append(text_block)

    in_code_block = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            flush_paragraph()
            continue
        if in_code_block:
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*\S)\s*$", stripped)
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group(1))
            text_value = heading_match.group(2).strip()
            while len(headings) >= level:
                headings.pop()
            headings.append(text_value)
            continue

        bullet_match = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*\S)\s*$", line)
        if bullet_match:
            flush_paragraph()
            content = bullet_match.group(1).strip()
            prefix = context_prefix()
            entries.append(f"{prefix}: {content}" if prefix else content)
            continue

        if not stripped:
            flush_paragraph()
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            flush_paragraph()
            continue

        paragraph_lines.append(stripped)

    flush_paragraph()

    deduped: List[str] = []
    seen = set()
    for entry in entries:
        normalized = normalize_text(entry)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(entry.strip())
    return deduped


def merge_entries(
    existing: Sequence[str],
    incoming: Sequence[str],
    limit: int,
) -> Tuple[List[str], Dict[str, int], List[str]]:
    merged = list(existing)
    seen = {normalize_text(entry) for entry in existing if entry.strip()}
    stats = {"existing": len(existing), "added": 0, "duplicates": 0, "overflowed": 0}
    overflowed: List[str] = []

    current_len = len(ENTRY_DELIMITER.join(merged)) if merged else 0

    for entry in incoming:
        normalized = normalize_text(entry)
        if not normalized:
            continue
        if normalized in seen:
            stats["duplicates"] += 1
            continue

        candidate_len = len(entry) if not merged else current_len + len(ENTRY_DELIMITER) + len(entry)
        if candidate_len > limit:
            stats["overflowed"] += 1
            overflowed.append(entry)
            continue

        merged.append(entry)
        seen.add(normalized)
        current_len = candidate_len
        stats["added"] += 1

    return merged, stats, overflowed


def relative_label(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# ───────────────────────────────────────────────────────────────────────
# Secret redaction for migration reports.
#
# The report JSON persists to disk inside the migration output directory and
# frequently ends up in bug reports or support channels.  Anything that looks
# like a credential — by key name or by value shape — is replaced with
# "[redacted]" before the report is written.
#
# Modelled on OpenClaw's src/plugin-sdk/migration.ts so both migration tools
# redact consistently.  Pure function — safe to call on any plain-data dict.
# ───────────────────────────────────────────────────────────────────────
REDACTED_MIGRATION_VALUE = "[redacted]"

_SECRET_KEY_MARKERS = (
    "accesstoken",
    "apikey",
    "authorization",
    "bearertoken",
    "clientsecret",
    "cookie",
    "credential",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
)

_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=\-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{8,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{12,}\b"),
)


def _normalize_secret_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _is_secret_key(key: str) -> bool:
    normalized = _normalize_secret_key(key)
    if normalized == "token" or normalized.endswith("token"):
        return True
    if normalized in {"auth", "authorization"}:
        return True
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def _redact_string(value: str) -> str:
    for pattern in _SECRET_VALUE_PATTERNS:
        value = pattern.sub(REDACTED_MIGRATION_VALUE, value)
    return value


def redact_migration_value(value: Any) -> Any:
    """Return a deep copy of ``value`` with secret-looking content replaced.

    Applied to every report written to disk.  Keys whose normalized form
    matches a credential marker get their value replaced wholesale.  Strings
    anywhere in the tree are scanned for common token patterns (sk-..., ghp_...,
    xox*-, AIza*, Bearer ...) and those substrings are replaced inline.
    """
    return _redact_internal(value, set())


def _redact_internal(value: Any, seen: set) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, (list, tuple)):
        return [_redact_internal(entry, seen) for entry in value]
    if isinstance(value, dict):
        obj_id = id(value)
        if obj_id in seen:
            return REDACTED_MIGRATION_VALUE
        seen.add(obj_id)
        out: Dict[str, Any] = {}
        for key, entry in value.items():
            if isinstance(key, str) and _is_secret_key(key):
                out[key] = REDACTED_MIGRATION_VALUE
            else:
                out[key] = _redact_internal(entry, seen)
        return out
    return value


def write_report(output_dir: Path, report: Dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Always redact before persisting.  Callers who need the raw object
    # (in-process) still get it back from build_report(); only the on-disk
    # copy is redacted.
    redacted = redact_migration_value(report)
    (output_dir / "report.json").write_text(
        json.dumps(redacted, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in redacted["items"]:
        grouped.setdefault(item["status"], []).append(item)

    lines = [
        "# OpenClaw -> Hermes Migration Report",
        "",
        f"- Timestamp: {redacted['timestamp']}",
        f"- Mode: {redacted['mode']}",
        f"- Source: `{redacted['source_root']}`",
        f"- Target: `{redacted['target_root']}`",
        "",
        "## Summary",
        "",
    ]

    for key, value in redacted["summary"].items():
        lines.append(f"- {key}: {value}")

    warnings = redacted.get("warnings") or []
    if warnings:
        lines.extend(["", "## Warnings", ""])
        for warning in warnings:
            lines.append(f"- {warning}")

    lines.extend(["", "## What Was Not Fully Brought Over", ""])
    skipped = grouped.get("skipped", []) + grouped.get("conflict", []) + grouped.get("error", [])
    if not skipped:
        lines.append("- Nothing. All discovered items were either migrated or archived.")
    else:
        for item in skipped:
            source = item["source"] or "(n/a)"
            dest = item["destination"] or "(n/a)"
            reason = item["reason"] or item["status"]
            lines.append(f"- `{source}` -> `{dest}`: {reason}")

    next_steps = redacted.get("next_steps") or []
    if next_steps:
        lines.extend(["", "## Next Steps", ""])
        for step in next_steps:
            lines.append(f"- {step}")

    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
