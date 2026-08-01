"""The bundled `.mcp.json` is what makes install seamless: Claude Code
auto-registers the governance MCP server from it, so a new adopter never
hand-edits a `mcpServers` block. These tests pin the shape Claude Code expects
and the one design subtlety that is easy to get wrong — the `/mcp/` suffix must
sit OUTSIDE the `${UNITARES_SERVER_URL:-...}` interpolation, so that overriding
the base URL still yields a `/mcp/`-suffixed endpoint.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_mcp() -> dict:
    return json.loads((ROOT / ".mcp.json").read_text())


def _expand(value: str, env: dict[str, str]) -> str:
    """Minimal stand-in for Claude Code's `${VAR:-default}` expansion, so we can
    assert both the default and the override paths behave."""
    def sub(m: re.Match) -> str:
        var, default = m.group(1), m.group(2)
        return env.get(var) or default
    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*):-([^}]*)\}", sub, value)


def test_mcp_json_is_valid_and_declares_governance_http_server():
    cfg = _load_mcp()
    server = cfg["mcpServers"]["unitares-governance"]
    # Claude Code's `.mcp.json` schema uses "http" (not "url"/"sse") for a
    # remote streamable-HTTP server.
    assert server["type"] == "http"
    assert "url" in server


def test_default_url_targets_local_governance_mcp_endpoint():
    url = _load_mcp()["mcpServers"]["unitares-governance"]["url"]
    expanded = _expand(url, env={})  # UNITARES_SERVER_URL unset -> default
    assert expanded == "http://localhost:8767/mcp/"


def test_override_base_url_still_gets_mcp_suffix():
    # The whole point of putting `/mcp/` outside the interpolation: an operator
    # who sets the *base* URL (the documented UNITARES_SERVER_URL convention)
    # must still end up with a `/mcp/` endpoint.
    url = _load_mcp()["mcpServers"]["unitares-governance"]["url"]
    expanded = _expand(url, env={"UNITARES_SERVER_URL": "https://gov.example.org"})
    assert expanded == "https://gov.example.org/mcp/"


def test_claude_mcp_uses_optional_bearer_header_expansion():
    server = _load_mcp()["mcpServers"]["unitares-governance"]
    authorization = server["headers"]["Authorization"]

    assert authorization == "Bearer ${UNITARES_HTTP_API_TOKEN:-}"
    assert _expand(authorization, env={}) == "Bearer "
    assert _expand(
        authorization,
        env={"UNITARES_HTTP_API_TOKEN": "client-token"},
    ) == "Bearer client-token"


@pytest.mark.parametrize(
    "manifest",
    [".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", ".codex-plugin/plugin.json"],
)
def test_manifest_versions_agree(manifest):
    # A plugin change is only picked up if the version string changes (Claude
    # Code caches by version — the #69/#70 stale-build incident). Keep the
    # canonical manifests in lockstep so a bump can't be half-applied.
    canonical = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"]
    data = json.loads((ROOT / manifest).read_text())
    version = data["version"] if "version" in data else data["plugins"][0]["version"]
    assert version == canonical
