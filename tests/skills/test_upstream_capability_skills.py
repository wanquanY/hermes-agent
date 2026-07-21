"""Repository contracts for the upstream capability skill batch."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATHS = (
    "skills/computer-use",
    "skills/hermes-desktop-plugins",
    "skills/productivity/petdex",
    "skills/software-development/simplify-code",
    "optional-skills/autonomous-ai-agents/antigravity-cli",
    "optional-skills/autonomous-ai-agents/grok",
    "optional-skills/autonomous-ai-agents/openhands",
    "optional-skills/creative/unreal-mcp",
    "optional-skills/devops/hermes-s6-container-supervision",
    "optional-skills/mcp/mcp-oauth-remote-gateway",
    "optional-skills/payments/mpp-agent",
    "optional-skills/payments/stripe-link-cli",
    "optional-skills/payments/stripe-projects",
    "optional-skills/productivity/shop",
    "optional-skills/security/unbroker",
    "optional-skills/security/web-pentest",
    "optional-skills/software-development/code-wiki",
    "optional-skills/web-development/cloudflare-temporary-deploy",
)
UPDATED_SKILL_PATHS = (
    "optional-skills/autonomous-ai-agents/honcho",
    "optional-skills/blockchain/hyperliquid",
    "optional-skills/creative/blender-mcp",
    "optional-skills/creative/kanban-video-orchestrator",
    "optional-skills/devops/watchers",
    "optional-skills/migration/openclaw-migration",
    "optional-skills/productivity/canvas",
    "optional-skills/productivity/shopify",
    "optional-skills/productivity/siyuan",
    "optional-skills/productivity/telephony",
    "optional-skills/security/1password",
    "optional-skills/software-development/rest-graphql-debug",
    "skills/apple/apple-reminders",
    "skills/autonomous-ai-agents/codex",
    "skills/autonomous-ai-agents/hermes-agent",
    "skills/creative/claude-design",
    "skills/creative/creative-ideation",
    "skills/creative/humanizer",
    "skills/email/himalaya",
    "skills/github/github-auth",
    "skills/github/github-code-review",
    "skills/github/github-issues",
    "skills/github/github-pr-workflow",
    "skills/github/github-repo-management",
    "skills/media/gif-search",
    "skills/media/youtube-content",
    "skills/note-taking/obsidian",
    "skills/productivity/airtable",
    "skills/productivity/notion",
    "skills/productivity/teams-meeting-pipeline",
    "skills/research/llm-wiki",
    "skills/research/research-paper-writing",
    "skills/software-development/hermes-agent-skill-authoring",
    "skills/software-development/plan",
    "skills/software-development/requesting-code-review",
    "skills/software-development/spike",
    "skills/software-development/systematic-debugging",
    "skills/software-development/test-driven-development",
)
AUDITED_SKILL_PATHS = SKILL_PATHS + UPDATED_SKILL_PATHS
ALLOWED_SUPPORT_DIRS = {"assets", "references", "scripts", "templates"}
SUPPORTED_PLATFORMS = {"linux", "macos", "windows"}
TEXT_SUFFIXES = {".js", ".json", ".md", ".py", ".sh", ".txt"}
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
LOCAL_LINK_RE = re.compile(r"]\(((?:assets|references|scripts|templates)/[^)#]+)")


def _load_skill(relative_path: str) -> tuple[Path, dict, str]:
    skill_dir = REPO_ROOT / relative_path
    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(content)
    assert match, f"{relative_path} must have closed YAML frontmatter"
    return skill_dir, yaml.safe_load(match.group(1)), match.group(2).strip()


def test_imported_skills_follow_discovery_metadata_contract():
    for relative_path in AUDITED_SKILL_PATHS:
        skill_dir, frontmatter, body = _load_skill(relative_path)
        description = frontmatter["description"]

        assert frontmatter["name"] == skill_dir.name
        assert isinstance(description, str)
        assert "\n" not in description
        assert len(description) <= 60
        assert description.endswith(".")
        assert str(frontmatter.get("author", "")).strip()
        assert set(frontmatter["platforms"]) <= SUPPORTED_PLATFORMS
        assert frontmatter["platforms"]
        assert body


def test_imported_skills_have_only_runtime_package_entries():
    for relative_path in SKILL_PATHS:
        skill_dir = REPO_ROOT / relative_path
        entries = {path.name for path in skill_dir.iterdir()}
        assert entries <= {"SKILL.md", *ALLOWED_SUPPORT_DIRS}


def test_imported_skill_local_links_resolve():
    for relative_path in SKILL_PATHS:
        skill_dir, _, body = _load_skill(relative_path)
        for target in LOCAL_LINK_RE.findall(body):
            assert (skill_dir / target).is_file(), f"missing {relative_path}/{target}"


def test_imported_skill_text_files_stay_below_repository_line_limit():
    for relative_path in AUDITED_SKILL_PATHS:
        skill_dir = REPO_ROOT / relative_path
        for path in skill_dir.rglob("*"):
            if path.is_file() and path.suffix in TEXT_SUFFIXES:
                line_count = len(path.read_text(encoding="utf-8").splitlines())
                assert line_count <= 2_000, f"{path.relative_to(REPO_ROOT)} has {line_count} lines"


def test_shop_upgrade_removes_the_legacy_duplicate():
    assert not (REPO_ROOT / "optional-skills/productivity/shop-app/SKILL.md").exists()
    assert (REPO_ROOT / "optional-skills/productivity/shop/references/safety.md").is_file()
