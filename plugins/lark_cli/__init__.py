"""Official Lark/Feishu CLI integration for Hermes.

The existing Feishu platform plugin remains the owner of message transport.
This plugin exposes business operations from the official ``lark-cli`` binary
without turning a long-running event consumer into a second channel.
"""

from __future__ import annotations

from plugins.lark_cli.cli import lark_command, register_lark_cli
from plugins.lark_cli.tools import TOOLS


def _dispatch_lark_cli(args) -> None:
    """Preserve plugin command exit codes in Hermes' side-effect dispatcher."""
    exit_code = lark_command(args)
    if exit_code:
        raise SystemExit(exit_code)


def register(ctx) -> None:
    """Register the Lark CLI tools and the human-facing management command."""
    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(
            name=name,
            toolset="lark_cli",
            schema=schema,
            handler=handler,
            emoji=emoji,
        )

    ctx.register_cli_command(
        name="lark",
        help="Manage and use the official Lark/Feishu CLI",
        setup_fn=register_lark_cli,
        handler_fn=_dispatch_lark_cli,
        description=(
            "Bind lark-cli to the active Hermes Feishu app, authorize a user, "
            "inspect official Skills, and run business commands."
        ),
    )
