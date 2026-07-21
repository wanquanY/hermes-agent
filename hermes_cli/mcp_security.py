"""Compatibility exports for the canonical MCP security domain policy."""

from hermes_agent.domain.mcp_security import (
    is_mcp_server_entry_suspicious,
    partition_mcp_server_entries,
    validate_mcp_server_entry,
)

__all__ = [
    "is_mcp_server_entry_suspicious",
    "partition_mcp_server_entries",
    "validate_mcp_server_entry",
]
