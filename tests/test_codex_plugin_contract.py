from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _handlers(config: dict):
    for event, groups in config["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                yield event, group, handler


def _invokes(handler: dict, script: str, *, host: str | None = None) -> bool:
    suffix = f" {script}"
    if host:
        suffix += f" --host {host}"
    return handler["command"].endswith(suffix)


def test_codex_manifest_declares_host_specific_mcp_and_hooks():
    manifest = _load(".codex-plugin/plugin.json")

    assert manifest["mcpServers"] == "./.codex-mcp.json"
    assert manifest["hooks"] == "./hooks/codex-hooks.json"
    assert (ROOT / manifest["mcpServers"]).is_file()
    assert (ROOT / manifest["hooks"]).is_file()


def test_claude_hooks_avoid_codex_default_discovery_path():
    manifest = _load(".claude-plugin/plugin.json")

    assert manifest["hooks"] == "./hooks/claude-hooks.json"
    assert (ROOT / manifest["hooks"]).is_file()
    assert not (ROOT / "hooks/hooks.json").exists()


def test_codex_mcp_file_uses_concrete_supported_transport():
    config = _load(".codex-mcp.json")

    assert set(config) == {"unitares-governance"}
    server = config["unitares-governance"]
    assert server["type"] == "http"
    assert server["url"] == "http://localhost:8767/mcp/"
    assert "${" not in server["url"]
    assert "bearer_token_env_var" not in server
    assert server["startup_timeout_sec"] == 10
    assert server["tool_timeout_sec"] == 60


def test_defaults_preserve_explicit_operator_environment():
    defaults = ROOT / "config" / "defaults.env"
    env = {
        **os.environ,
        "UNITARES_SERVER_URL": "https://governance.example.test",
        "UNITARES_CHECKINS": "off",
        "UNITARES_AUTO_CHECKIN_ENABLED": "0",
        "UNITARES_CODEX_LIVENESS": "off",
        "UNITARES_CODEX_HOST_HEARTBEATS": "on",
        "UNITARES_CODEX_RUNTIME_IDLE_EXIT_S": "7200",
        "UNITARES_FILE_LEASES_ENABLED": "0",
        "UNITARES_FILE_LEASES_REQUIRED": "1",
        "LEASE_PLANE_BASE_URL": "https://leases.example.test",
    }
    env.pop("UNITARES_FILE_LEASE_TTL_S", None)
    env.pop("UNITARES_AUTO_CHECKIN_CLAIM_TTL_S", None)
    env.pop("UNITARES_WATCHER_ENABLED", None)
    env.pop("UNITARES_CODEX_ACTIVITY_LOCK_TIMEOUT_S", None)
    script = r"""
source "$1"
"$2" -c 'import json, os; print(json.dumps([
    os.environ["UNITARES_SERVER_URL"],
    os.environ["UNITARES_CHECKINS"],
    os.environ["UNITARES_AUTO_CHECKIN_ENABLED"],
    os.environ["UNITARES_CODEX_LIVENESS"],
    os.environ["UNITARES_CODEX_HOST_HEARTBEATS"],
    os.environ["UNITARES_CODEX_RUNTIME_IDLE_EXIT_S"],
    os.environ["UNITARES_CODEX_ACTIVITY_LOCK_TIMEOUT_S"],
    os.environ["UNITARES_FILE_LEASES_ENABLED"],
    os.environ["UNITARES_FILE_LEASES_REQUIRED"],
    os.environ["UNITARES_FILE_LEASE_TTL_S"],
    os.environ["UNITARES_AUTO_CHECKIN_CLAIM_TTL_S"],
    os.environ["UNITARES_WATCHER_ENABLED"],
    os.environ["LEASE_PLANE_BASE_URL"],
]))'
"""

    result = subprocess.run(
        ["bash", "-c", script, "bash", str(defaults), sys.executable],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )

    assert json.loads(result.stdout) == [
        "https://governance.example.test",
        "off",
        "0",
        "off",
        "on",
        "7200",
        "1.0",
        "0",
        "1",
        "30",
        "30",
        "0",
        "https://leases.example.test",
    ]


def test_codex_host_evidence_defaults_are_conservative():
    defaults = ROOT / "config" / "defaults.env"
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "UNITARES_CODEX_HOST_HEARTBEATS",
            "UNITARES_CODEX_RUNTIME_IDLE_EXIT_S",
        }
    }
    script = r"""
source "$1"
"$2" -c 'import json, os; print(json.dumps([
    os.environ["UNITARES_CODEX_HOST_HEARTBEATS"],
    os.environ["UNITARES_CODEX_RUNTIME_IDLE_EXIT_S"],
]))'
"""

    result = subprocess.run(
        ["bash", "-c", script, "bash", str(defaults), sys.executable],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )

    assert json.loads(result.stdout) == ["off", "3600"]


def test_codex_hooks_are_synchronous_and_cover_continuity_path():
    config = _load("hooks/codex-hooks.json")

    assert {"PreToolUse", "PostToolUse", "SessionStart", "SessionEnd", "Stop"} <= set(
        config["hooks"]
    )
    handlers = list(_handlers(config))
    assert handlers
    assert all("async" not in handler for _event, _group, handler in handlers)
    assert all(
        "${PLUGIN_ROOT}" in handler["command"] for _event, _group, handler in handlers
    )
    assert all("commandWindows" in handler for _event, _group, handler in handlers)

    assert any(
        _invokes(handler, "post-stop", host="codex") for _, _, handler in handlers
    )
    assert any(
        _invokes(handler, "session-start", host="codex") for _, _, handler in handlers
    )
    assert not any(
        event == "SessionStart" and _invokes(handler, "runtime-start", host="codex")
        for event, _, handler in handlers
    )
    assert any(
        _invokes(handler, "session-end", host="codex") for _, _, handler in handlers
    )
    assert any(
        _invokes(handler, "post-identity", host="codex") for _, _, handler in handlers
    )
    assert any(
        _invokes(handler, "post-checkin", host="codex") for _, _, handler in handlers
    )
    assert any(
        _invokes(handler, "pre-governance-call", host="codex")
        for _, _, handler in handlers
    )

    edit_matchers = [
        group["matcher"]
        for event, group, handler in handlers
        if event == "PreToolUse" and _invokes(handler, "pre-edit", host="codex")
    ]
    assert any(re.fullmatch(matcher, "apply_patch") for matcher in edit_matchers)
    assert all(not re.fullmatch(matcher, "MultiEdit") for matcher in edit_matchers)

    edit_handlers = [
        handler
        for event, _group, handler in handlers
        if event == "PostToolUse" and _invokes(handler, "post-edit", host="codex")
    ]
    assert len(edit_handlers) == 1
    assert edit_handlers[0]["timeout"] <= 6

    edit_release_handlers = [
        handler
        for event, _group, handler in handlers
        if event == "PostToolUse"
        and _invokes(handler, "post-edit-release", host="codex")
    ]
    assert len(edit_release_handlers) == 1
    assert edit_release_handlers[0]["timeout"] <= 6
    assert "release-edit" not in (ROOT / "hooks" / "post-edit").read_text(
        encoding="utf-8"
    )

    activity_handlers = [
        (group, handler)
        for event, group, handler in handlers
        if event == "PostToolUse" and _invokes(handler, "post-activity", host="codex")
    ]
    assert len(activity_handlers) == 1
    activity_group, activity_handler = activity_handlers[0]
    assert re.fullmatch(activity_group["matcher"], "functions.exec")
    assert re.fullmatch(
        activity_group["matcher"],
        "mcp__unitares_governance__start_session",
    )
    assert activity_handler["timeout"] <= 4
    assert config["hooks"]["SessionEnd"][0]["matcher"] == "other"

    stop_handler = next(
        handler
        for handler in config["hooks"]["Stop"][0]["hooks"]
        if _invokes(handler, "post-stop", host="codex")
    )
    hook_text = (ROOT / "hooks" / "post-stop").read_text(encoding="utf-8")
    onboard_match = re.search(
        r'^CODEX_ONBOARD_TIMEOUT_CAP="(\d+)"$', hook_text, re.MULTILINE
    )
    checkin_match = re.search(
        r'^CODEX_CHECKIN_TIMEOUT_CAP="(\d+)"$', hook_text, re.MULTILINE
    )
    assert onboard_match and checkin_match
    assert int(onboard_match.group(1)) + int(checkin_match.group(1)) <= (
        stop_handler["timeout"] - 5
    )


def test_windows_hook_wrapper_propagates_exit_codes_and_fails_required_edits_closed():
    wrapper = (ROOT / "hooks" / "run-hook.cmd").read_text(encoding="utf-8")

    assert "setlocal EnableDelayedExpansion" in wrapper
    assert "exit /b !ERRORLEVEL!" in wrapper
    assert "exit /b %ERRORLEVEL%" not in wrapper
    assert "Git Bash is required" in wrapper
    for required in ("1", "true", "on", "yes"):
        assert f'"%UNITARES_FILE_LEASES_REQUIRED%"=="{required}"' in wrapper
    assert '"permissionDecision":"deny"' in wrapper


def test_pre_edit_blocks_when_required_lease_helper_is_missing(tmp_path: Path):
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    copied_hook = hook_dir / "pre-edit"
    copied_hook.write_text(
        (ROOT / "hooks" / "pre-edit").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(copied_hook), "--host", "codex"],
        cwd=str(tmp_path),
        env={**os.environ, "UNITARES_FILE_LEASES_REQUIRED": "true"},
        input="{}",
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 2
    assert "file_lease_hook.py is missing" in result.stderr


def test_pre_edit_required_mode_overrides_disabled_flag_for_both_hosts(
    tmp_path: Path,
):
    payloads = {
        "claude": {
            "session_id": "claude-required-disabled",
            "tool_name": "Edit",
            "tool_use_id": "toolu_required_disabled",
            "tool_input": {"file_path": str(tmp_path / "claude.py")},
        },
        "codex": {
            "session_id": "codex-required-disabled",
            "tool_name": "apply_patch",
            "tool_use_id": "call_required_disabled",
            "tool_input": {
                "command": "*** Begin Patch\n*** Update File: codex.py\n*** End Patch"
            },
        },
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "LEASE_PLANE_BEARER_TOKEN",
            "UNITARES_LEASE_PLANE_BEARER_TOKEN",
            "GOVERNANCE_TOKEN",
        }
    }
    env.update(
        {
            "UNITARES_FILE_LEASES_ENABLED": "0",
            "UNITARES_FILE_LEASES_REQUIRED": "yes",
            "UNITARES_SECRETS_ENV": "/dev/null",
        }
    )

    for host, payload in payloads.items():
        result = subprocess.run(
            ["bash", str(ROOT / "hooks" / "pre-edit"), "--host", host],
            cwd=str(tmp_path),
            env=env,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )

        assert result.returncode == 2, host
        assert "missing LEASE_PLANE_BEARER_TOKEN" in result.stderr


def test_pre_edit_converts_unexpected_required_helper_failure_to_block(
    tmp_path: Path,
):
    payload = {
        "session_id": "required-helper-failure",
        "tool_name": "Edit",
        "tool_use_id": "toolu_required_failure",
        "tool_input": {"file_path": str(tmp_path / "a.py")},
    }
    result = subprocess.run(
        ["bash", str(ROOT / "hooks" / "pre-edit"), "--host", "claude"],
        cwd=str(tmp_path),
        env={
            **os.environ,
            "UNITARES_FILE_LEASES_REQUIRED": "1",
            "LEASE_PLANE_BEARER_TOKEN": "test-token",
            "LEASE_PLANE_BASE_URL": "://",
        },
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 2
    assert "required file lease helper exited with status 1" in result.stderr


def test_both_hosts_route_identity_calls_through_pretool_generation_guard():
    for relative, host in (
        ("hooks/claude-hooks.json", "claude"),
        ("hooks/codex-hooks.json", "codex"),
    ):
        config = _load(relative)
        matchers = [
            group["matcher"]
            for event, group, handler in _handlers(config)
            if event == "PreToolUse"
            and _invokes(handler, "pre-governance-call", host=host)
        ]
        assert all(
            any(
                re.fullmatch(matcher, f"mcp__unitares-governance__{tool}")
                for matcher in matchers
            )
            for tool in ("onboard", "start_session", "identity", "bind_session")
        )


def test_claude_separates_sync_lease_release_from_async_post_hooks():
    claude = _load("hooks/claude-hooks.json")
    codex = _load("hooks/codex-hooks.json")

    claude_async_post_handlers = [
        handler
        for event, _group, handler in _handlers(claude)
        if event in {"PostToolUse", "Stop"}
        and not _invokes(handler, "post-edit-release", host="claude")
    ]
    assert claude_async_post_handlers
    assert all(handler.get("async") is True for handler in claude_async_post_handlers)
    release_handlers = [
        handler
        for event, _group, handler in _handlers(claude)
        if event == "PostToolUse"
        and _invokes(handler, "post-edit-release", host="claude")
    ]
    assert len(release_handlers) == 1
    assert release_handlers[0].get("async") is False
    assert release_handlers[0]["timeout"] <= 6
    failed_release_handlers = [
        handler
        for event, _group, handler in _handlers(claude)
        if event == "PostToolUseFailure"
        and _invokes(handler, "post-edit-release", host="claude")
    ]
    assert len(failed_release_handlers) == 1
    assert failed_release_handlers[0].get("async") is False
    assert any(
        _invokes(handler, "post-checkin", host="claude")
        for event, _group, handler in _handlers(claude)
        if event == "PostToolUseFailure"
    )
    assert any(
        _invokes(handler, "post-edit-release", host="claude")
        for event, _group, handler in _handlers(claude)
        if event == "PermissionDenied"
    )
    assert any(
        _invokes(handler, "post-checkin", host="claude")
        for event, _group, handler in _handlers(claude)
        if event == "PermissionDenied"
    )
    assert not any(
        "release-session" in handler["command"]
        or "session-lease-release" in handler["command"]
        for event, _group, handler in _handlers(claude)
        if event == "Stop"
    )
    assert any(
        _invokes(handler, "post-edit-batch-release", host="claude")
        and handler.get("async") is False
        and handler["timeout"] <= 6
        for event, _group, handler in _handlers(claude)
        if event == "PostToolBatch"
    )
    assert "release-batch" in (ROOT / "hooks" / "post-edit-batch-release").read_text(
        encoding="utf-8"
    )
    assert not any(
        "release-session" in handler["command"]
        or "session-lease-release" in handler["command"]
        for event, _group, handler in _handlers(codex)
        if event == "Stop"
    )
    assert all(
        "async" not in handler
        for event, _group, handler in _handlers(codex)
        if event in {"PostToolUse", "Stop"}
    )
    assert any(
        _invokes(handler, "pre-edit", host="claude")
        for event, _group, handler in _handlers(claude)
        if event == "PreToolUse"
    )
    assert any(
        _invokes(handler, "post-edit", host="claude")
        for event, _group, handler in _handlers(claude)
        if event == "PostToolUse"
    )
    assert any(
        _invokes(handler, "post-checkin", host="claude")
        for event, _group, handler in _handlers(claude)
        if event == "PostToolUse"
    )
    assert any(
        _invokes(handler, "post-stop", host="claude")
        for event, _group, handler in _handlers(claude)
        if event == "Stop"
    )
    assert any(
        _invokes(handler, "session-end", host="claude")
        for event, _group, handler in _handlers(claude)
        if event == "SessionEnd"
    )
    session_end = claude["hooks"]["SessionEnd"][0]["hooks"][0]
    assert session_end["timeout"] <= 1
    assert "--budget 0.8" in (ROOT / "hooks" / "session-end").read_text(
        encoding="utf-8"
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
