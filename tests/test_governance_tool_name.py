from __future__ import annotations

import pytest

from scripts.governance_tool_name import (
    governance_tool_suffix,
    is_governance_tool,
    parse_mcp_tool_name,
)


@pytest.mark.parametrize(
    "server",
    [
        "unitares-governance",
        "governance",
        "unitares",
        "claude_ai_UNITARES",
    ],
)
def test_configured_governance_aliases_are_accepted_case_insensitively(server: str):
    assert governance_tool_suffix(f"mcp__{server}__SYNC_STATE") == "sync_state"


@pytest.mark.parametrize(
    "server",
    [
        "evil-unitares-proxy",
        "unitares-governance-shadow",
        "my_governance",
        "claude_ai_UNITARES_backup",
    ],
)
def test_governance_server_lookalikes_are_rejected(server: str):
    assert governance_tool_suffix(f"mcp__{server}__sync_state") is None
    assert not is_governance_tool(f"mcp__{server}__sync_state")


@pytest.mark.parametrize(
    "tool_name",
    [
        None,
        "",
        "Bash",
        "mcp__unitares",
        "mcp____sync_state",
        "mcp__unitares__",
    ],
)
def test_malformed_or_non_mcp_names_are_rejected(tool_name: object):
    assert parse_mcp_tool_name(tool_name) is None
    assert governance_tool_suffix(tool_name) is None


def test_suffix_filter_is_exact_and_case_normalized():
    tool_name = "MCP__GOVERNANCE__PROCESS_AGENT_UPDATE"
    assert is_governance_tool(tool_name, suffixes={"process_agent_update"})
    assert not is_governance_tool(tool_name, suffixes={"sync_state"})


def test_claude_trusts_the_exact_bundled_plugin_server_alias():
    tool_name = (
        "mcp__plugin_unitares-governance_unitares-governance__SYNC_STATE"
    )

    assert governance_tool_suffix(tool_name, host="claude") == "sync_state"
    assert governance_tool_suffix(tool_name, host="codex") is None
    assert governance_tool_suffix(
        "mcp__plugin_unitares-governance_unitares-governance-shadow__sync_state",
        host="claude",
    ) is None


@pytest.mark.parametrize(
    "server",
    ["unitares-governance", "unitares_governance"],
)
def test_codex_trusts_exact_native_and_code_mode_server_aliases(server: str):
    assert is_governance_tool(f"mcp__{server}__sync_state", host="codex")


def test_codex_rejects_noncanonical_governance_aliases():
    assert not is_governance_tool("mcp__governance__sync_state", host="codex")
    assert not is_governance_tool("mcp__unitares__sync_state", host="codex")
    assert not is_governance_tool(
        "mcp__unitares_governance_shadow__sync_state",
        host="codex",
    )
    assert not is_governance_tool(
        "mcp__plugin_unitares-governance_unitares-governance__sync_state",
        host="codex",
    )
