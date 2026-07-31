from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _handlers(config: dict):
    for event, groups in config["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                yield event, group, handler


def test_codex_manifest_declares_host_specific_mcp_and_hooks():
    manifest = _load(".codex-plugin/plugin.json")

    assert manifest["mcpServers"] == "./.codex-mcp.json"
    assert manifest["hooks"] == "./hooks/codex-hooks.json"
    assert (ROOT / manifest["mcpServers"]).is_file()
    assert (ROOT / manifest["hooks"]).is_file()


def test_codex_mcp_file_uses_concrete_supported_transport():
    config = _load(".codex-mcp.json")

    assert set(config) == {"unitares-governance"}
    server = config["unitares-governance"]
    assert server["type"] == "http"
    assert server["url"] == "http://localhost:8767/mcp/"
    assert "${" not in server["url"]
    assert server["startup_timeout_sec"] == 10
    assert server["tool_timeout_sec"] == 60


def test_codex_hooks_are_synchronous_and_cover_continuity_path():
    config = _load("hooks/codex-hooks.json")

    assert {"PreToolUse", "PostToolUse", "SessionStart", "SessionEnd", "Stop"} <= set(
        config["hooks"]
    )
    handlers = list(_handlers(config))
    assert handlers
    assert all("async" not in handler for _event, _group, handler in handlers)
    assert all("${PLUGIN_ROOT}" in handler["command"] for _event, _group, handler in handlers)
    assert all("commandWindows" in handler for _event, _group, handler in handlers)

    commands = {handler["command"] for _event, _group, handler in handlers}
    assert any(command.endswith(" post-stop") for command in commands)
    assert any(command.endswith(" post-identity") for command in commands)
    assert any(command.endswith(" post-checkin") for command in commands)
    assert any(command.endswith(" pre-governance-call") for command in commands)

    edit_matchers = [
        group["matcher"]
        for event, group, handler in handlers
        if event == "PreToolUse" and handler["command"].endswith(" pre-edit")
    ]
    assert any(re.fullmatch(matcher, "apply_patch") for matcher in edit_matchers)


def test_both_hosts_route_anchored_mint_calls_through_injector():
    for relative in ("hooks/hooks.json", "hooks/codex-hooks.json"):
        config = _load(relative)
        matchers = [
            group["matcher"]
            for event, group, handler in _handlers(config)
            if event == "PreToolUse"
            and handler["command"].endswith(" pre-governance-call")
        ]
        assert all(
            any(
                re.fullmatch(matcher, f"mcp__unitares-governance__{tool}")
                for matcher in matchers
            )
            for tool in ("onboard", "start_session")
        )


def test_claude_keeps_separate_async_post_hooks():
    claude = _load("hooks/hooks.json")
    codex = _load("hooks/codex-hooks.json")

    claude_post_handlers = [
        handler
        for event, _group, handler in _handlers(claude)
        if event in {"PostToolUse", "Stop"}
    ]
    assert claude_post_handlers
    assert all(handler.get("async") is True for handler in claude_post_handlers)
    assert all(
        "async" not in handler
        for event, _group, handler in _handlers(codex)
        if event in {"PostToolUse", "Stop"}
    )


def test_native_repo_marketplace_exposes_root_plugin():
    marketplace = _load(".agents/plugins/marketplace.json")

    assert marketplace["name"] == "unitares-governance"
    assert marketplace["interface"]["displayName"] == "UNITARES Governance"
    assert len(marketplace["plugins"]) == 1
    entry = marketplace["plugins"][0]
    assert entry["name"] == "unitares-governance"
    assert entry["source"] == {"source": "local", "path": "./"}
    assert entry["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert entry["category"] == "Productivity"


def test_codex_setup_docs_include_install_and_hook_trust_steps():
    start = (ROOT / "CODEX_START.md").read_text(encoding="utf-8")

    assert "codex plugin marketplace add cirwel/unitares-governance-plugin" in start
    assert "`/plugins`" in start
    assert "`/hooks`" in start
