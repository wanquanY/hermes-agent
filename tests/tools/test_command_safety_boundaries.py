"""Mutation-sensitive acceptance tests for the canonical command boundary."""

import pytest

from tools.approval import detect_dangerous_command, detect_hardline_command


@pytest.mark.parametrize(
    "command",
    (
        "rm${IFS}-rf${IFS}/",
        "rm -r\\\nf /",
        'rm -rf "/./"',
        "rm -rf '${HOME}'",
        "$(rm -rf /)",
        "{ rm -rf /; }",
        "sudo env X=1 rm -rf /",
    ),
)
def test_root_delete_mutations_hit_hardline(command):
    assert detect_hardline_command(command)[0] is True


@pytest.mark.parametrize(
    "command",
    (
        'echo "rm -rf /"',
        "git commit -m 'do not run rm -rf /'",
        "printf '%s' 'shutdown now'",
    ),
)
def test_quoted_prose_is_not_a_hardline_command(command):
    assert detect_hardline_command(command)[0] is False


@pytest.mark.parametrize(
    "command",
    (
        "git reset --h HEAD~1",
        "git reset --har HEAD~1",
        "git branch --delete --force topic",
        "git branch --force --delete topic",
        "sudo --stdi id",
        "sudo --ask id",
        "bash <<'EOF'\necho hi\nEOF",
        "printenv | tee .env backup.env",
        "echo x > .env # audit",
    ),
)
def test_dangerous_mutations_require_approval(command):
    assert detect_dangerous_command(command)[0] is True


@pytest.mark.parametrize(
    "command",
    ("git reset --soft HEAD~1", "git reset --help", "git branch --delete topic"),
)
def test_non_destructive_git_variants_remain_safe(command):
    assert detect_dangerous_command(command)[0] is False

