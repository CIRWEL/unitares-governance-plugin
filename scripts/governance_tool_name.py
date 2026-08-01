#!/usr/bin/env python3
"""Parse MCP tool names and recognize configured UNITARES server aliases."""

from __future__ import annotations

from collections.abc import Collection


# MCP server names are case-insensitive at this hook boundary. Keep this list
# explicit: accepting names by substring could pre-approve or mutate calls for
# an unrelated server whose name merely mentions UNITARES.
GOVERNANCE_SERVER_ALIASES = frozenset(
    {
        "unitares-governance",
        "governance",
        "unitares",
        "claude_ai_unitares",
        # Claude scopes plugin-bundled MCP servers as
        # plugin_<plugin-name>_<server-name> in hook payloads.
        "plugin_unitares-governance_unitares-governance",
    }
)
CODEX_GOVERNANCE_SERVER_ALIASES = frozenset({"unitares-governance"})


def governance_server_aliases(host: str | None = None) -> frozenset[str]:
    """Return aliases trusted by a host-specific hook boundary."""
    if host == "codex":
        return CODEX_GOVERNANCE_SERVER_ALIASES
    if host in {None, "claude"}:
        return GOVERNANCE_SERVER_ALIASES
    return frozenset()


def parse_mcp_tool_name(tool_name: object) -> tuple[str, str] | None:
    """Return the normalized ``(server, tool)`` segments of an MCP name."""
    if not isinstance(tool_name, str):
        return None

    parts = tool_name.split("__")
    if len(parts) < 3 or parts[0].casefold() != "mcp":
        return None

    server = "__".join(parts[1:-1]).casefold()
    tool = parts[-1].casefold()
    if not server or not tool:
        return None
    return server, tool


def governance_tool_suffix(tool_name: object, *, host: str | None = None) -> str | None:
    """Return the normalized tool suffix for a known UNITARES MCP alias."""
    parsed = parse_mcp_tool_name(tool_name)
    if parsed is None:
        return None
    server, tool = parsed
    if server not in governance_server_aliases(host):
        return None
    return tool


def is_governance_tool(
    tool_name: object,
    *,
    host: str | None = None,
    suffixes: Collection[str] | None = None,
) -> bool:
    """Return whether a name targets UNITARES and optionally an allowed tool."""
    suffix = governance_tool_suffix(tool_name, host=host)
    if suffix is None:
        return False
    if suffixes is None:
        return True
    return suffix in {candidate.casefold() for candidate in suffixes}
