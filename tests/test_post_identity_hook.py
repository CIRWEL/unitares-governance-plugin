"""Contract tests for the post-identity PostToolUse hook.

The hook fires after `mcp__<server>__(onboard|identity|bind_session)` tool
calls and writes the response's identity fields to the slot-scoped session
cache. This lets the agent's own first MCP call become the source of truth
for session identity — Part C of the identity honesty series.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent.parent
HOOK = PLUGIN_ROOT / "hooks" / "post-identity"
PRE_HOOK = PLUGIN_ROOT / "hooks" / "pre-governance-call"
SESSION_CACHE = PLUGIN_ROOT / "scripts" / "session_cache.py"
_EVENT_IDS = itertools.count()


def _hook_env(workspace: Path, home: Path | None = None) -> dict[str, str]:
    return {**os.environ, "HOME": str(home or workspace / "home")}


def _run_pre_hook(
    hook_input: dict,
    workspace: Path,
    *,
    host: str | None = None,
    home: Path | None = None,
):
    command = [str(PRE_HOOK)]
    if host:
        command.extend(["--host", host])
    return subprocess.run(
        command,
        input=json.dumps(hook_input),
        text=True,
        capture_output=True,
        timeout=10,
        cwd=str(workspace),
        env=_hook_env(workspace, home),
    )


def _run_post_hook(
    hook_input: dict,
    workspace: Path,
    *,
    host: str | None = None,
    home: Path | None = None,
):
    command = [str(HOOK)]
    if host:
        command.extend(["--host", host])
    return subprocess.run(
        command,
        input=json.dumps(hook_input),
        text=True,
        capture_output=True,
        timeout=10,
        cwd=str(workspace),
        env=_hook_env(workspace, home),
    )


def _run_hook(
    hook_input: dict,
    workspace: Path,
    *,
    host: str | None = None,
    home: Path | None = None,
):
    payload = json.loads(json.dumps(hook_input))
    if not payload.get("tool_use_id") and not payload.get("tool_call_id"):
        payload["tool_use_id"] = f"identity-test-{next(_EVENT_IDS)}"
    _run_pre_hook(payload, workspace, host=host, home=home)
    return _run_post_hook(payload, workspace, host=host, home=home)


def _read_session_cache(workspace: Path, slot: str | None = None) -> dict:
    """Read the slotted session cache directly."""
    filename = "session.json"
    if slot:
        safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)[:64]
        filename = f"session-{safe}.json"
    path = workspace / ".unitares" / filename
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _mcp_response(uuid="u-123", agent_id="Test_Agent", sid="agent-abc",
                  token="v1.tok", display_name="TestAgent"):
    """Build a realistic MCP tool response envelope (legacy dict-wrapped shape)."""
    inner = {
        "success": True,
        "uuid": uuid,
        "agent_id": agent_id,
        "client_session_id": sid,
        "continuity_token": token,
        "display_name": display_name,
        "continuity_token_supported": True,
    }
    return {"content": [{"type": "text", "text": json.dumps(inner)}]}


def _mcp_response_list(uuid="u-123", agent_id="Test_Agent", sid="agent-abc",
                       token="v1.tok", display_name="TestAgent"):
    """Build an MCP tool response in the bare-list shape Claude Code actually sends.

    Empirically (2026-04-19) Claude Code passes tool_response as a raw list of
    content parts, not wrapped in {"content": [...]}. Captured from live hook
    stdin during a process_agent_update / identity call.
    """
    inner = {
        "success": True,
        "uuid": uuid,
        "agent_id": agent_id,
        "client_session_id": sid,
        "continuity_token": token,
        "display_name": display_name,
        "continuity_token_supported": True,
    }
    return [{"type": "text", "text": json.dumps(inner)}]


def _start_session_response(uuid="u-123", agent_id="Test_Agent", sid="agent-abc",
                            token="v1.tok", display_name="TestAgent"):
    """Build the friendly start_session envelope returned by current servers."""
    inner = {
        "success": True,
        "tool": "start_session",
        "agent_uuid": uuid,
        "client_session_id": sid,
        "next_action": "Save agent_uuid and client_session_id, then check in.",
        "state_summary": {"lineage_state": "fresh"},
        "raw_governance": {
            "success": True,
            "uuid": uuid,
            "agent_id": agent_id,
            "client_session_id": sid,
            "continuity_token": token,
            "display_name": display_name,
            "continuity_token_supported": True,
            "session_resolution_source": "force_new",
        },
    }
    return {"content": [{"type": "text", "text": json.dumps(inner)}]}


class TestPostIdentityRecordsResponse:
    def test_onboard_response_writes_slotted_cache(self, tmp_path):
        slot = "session-xyz-1234"
        hook_input = {
            "session_id": slot,
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"name": "my-agent"},
            "tool_response": _mcp_response(uuid="u-onboard-1"),
        }
        result = _run_hook(hook_input, tmp_path)
        assert result.returncode == 0

        cache = _read_session_cache(tmp_path, slot)
        assert cache["uuid"] == "u-onboard-1"
        assert cache["agent_id"] == "Test_Agent"
        assert cache["client_session_id"] == "agent-abc"
        # S11 (identity ontology): hook no longer writes continuity_token to
        # the cache. The UUID is the lineage anchor; the token is a
        # server-resume credential the cache must not stylize as its own.
        assert cache["continuity_token"] == ""
        assert cache["schema_version"] == 2
        assert "updated_at" in cache, "should stamp updated_at"

    def test_identity_response_writes_slotted_cache(self, tmp_path):
        hook_input = {
            "session_id": "slot-id",
            "tool_name": "mcp__unitares-governance__identity",
            "tool_input": {"agent_uuid": "u-resume-1", "resume": True},
            "tool_response": _mcp_response(uuid="u-resume-1"),
        }
        result = _run_hook(hook_input, tmp_path)
        assert result.returncode == 0
        assert _read_session_cache(tmp_path, "slot-id")["uuid"] == "u-resume-1"

    def test_bind_session_response_writes_slotted_cache(self, tmp_path):
        hook_input = {
            "session_id": "slot-bind",
            "tool_name": "mcp__unitares-governance__bind_session",
            "tool_input": {"agent_uuid": "u-bind-1", "resume": True},
            "tool_response": _mcp_response(uuid="u-bind-1"),
        }
        result = _run_hook(hook_input, tmp_path)
        assert result.returncode == 0
        assert _read_session_cache(tmp_path, "slot-bind")["uuid"] == "u-bind-1"

    def test_claude_start_session_envelope_writes_slotted_cache(self, tmp_path):
        # start_session is the friendly-workflow alias for onboard; without
        # this, alias-onboarded sessions never write the cache and run dark.
        hook_input = {
            "session_id": "slot-claude-alias",
            "tool_name": "mcp__plugin_unitares-governance_unitares-governance__start_session",
            "tool_input": {"force_new": True},
            "tool_response": _start_session_response(uuid="u-claude-alias"),
        }
        result = _run_hook(hook_input, tmp_path, host="claude")
        assert result.returncode == 0
        cache = _read_session_cache(tmp_path, "slot-claude-alias")
        assert cache["uuid"] == "u-claude-alias"
        assert cache["agent_id"] == "Test_Agent"
        assert cache["client_session_id"] == "agent-abc"
        assert cache["display_name"] == "TestAgent"
        assert cache["continuity_token_supported"] is True
        assert cache["session_resolution_source"] == "force_new"

    def test_codex_start_session_envelope_writes_slotted_cache(self, tmp_path):
        hook_input = {
            "session_id": "slot-codex-alias",
            # Codex code mode normalizes the configured hyphenated server name
            # to underscores in the PostToolUse envelope.
            "tool_name": "mcp__unitares_governance__start_session",
            "tool_input": {"force_new": True},
            "tool_response": _start_session_response(uuid="u-codex-alias"),
        }
        result = _run_hook(hook_input, tmp_path, host="codex")
        assert result.returncode == 0

        cache = _read_session_cache(tmp_path, "slot-codex-alias")
        assert cache["uuid"] == "u-codex-alias"
        assert cache["agent_id"] == "Test_Agent"
        assert cache["client_session_id"] == "agent-abc"
        assert cache["display_name"] == "TestAgent"

    def test_codex_transcript_path_writes_hashed_slot_cache(self, tmp_path):
        transcript = "/home/user/.codex/sessions/2026/06/18/rollout.jsonl"
        slot = "codex-transcript_path-" + hashlib.sha256(transcript.encode()).hexdigest()[:16]
        hook_input = {
            "transcript_path": transcript,
            "tool_name": "mcp__governance__start_session",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-codex-1"),
        }

        result = _run_hook(hook_input, tmp_path)

        assert result.returncode == 0
        cache = _read_session_cache(tmp_path, slot)
        assert cache["uuid"] == "u-codex-1"
        assert cache["client_session_id"] == "agent-abc"

    def test_raw_session_id_uses_same_sanitized_slot_in_pre_and_post(self, tmp_path):
        hook_input = {
            "session_id": "slot/with:chars",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-sanitized"),
        }

        assert _run_hook(hook_input, tmp_path).returncode == 0
        assert _read_session_cache(tmp_path, "slot_with_chars")["uuid"] == "u-sanitized"


class TestPostIdentityGenerationGuard:
    def test_older_async_response_cannot_overwrite_newer_identity(self, tmp_path):
        slot = "identity-ordering"
        older = {
            "session_id": slot,
            "tool_use_id": "toolu_identity_older",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-older", sid="agent-older"),
        }
        newer = {
            "session_id": slot,
            "tool_use_id": "toolu_identity_newer",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-newer", sid="agent-newer"),
        }

        assert _run_pre_hook(older, tmp_path).returncode == 0
        assert _run_pre_hook(newer, tmp_path).returncode == 0
        assert _run_post_hook(newer, tmp_path).returncode == 0
        assert _run_post_hook(older, tmp_path).returncode == 0

        cache = _read_session_cache(tmp_path, slot)
        assert cache["uuid"] == "u-newer"
        assert cache["client_session_id"] == "agent-newer"
        assert list((tmp_path / ".unitares" / "session-snapshots").glob("*.json")) == []

    def test_later_invocation_wins_even_when_older_response_finishes_first(
        self,
        tmp_path,
    ):
        slot = "identity-invocation-order"
        older = {
            "session_id": slot,
            "tool_use_id": "toolu_invocation_older",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-first", sid="agent-first"),
        }
        newer = {
            "session_id": slot,
            "tool_use_id": "toolu_invocation_newer",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-second", sid="agent-second"),
        }

        assert _run_pre_hook(older, tmp_path).returncode == 0
        assert _run_pre_hook(newer, tmp_path).returncode == 0
        assert _run_post_hook(older, tmp_path).returncode == 0
        assert _run_post_hook(newer, tmp_path).returncode == 0

        cache = _read_session_cache(tmp_path, slot)
        assert cache["uuid"] == "u-second"
        assert cache["client_session_id"] == "agent-second"

    def test_shared_home_rejects_stale_response_from_another_workspace(self, tmp_path):
        workspace_a = tmp_path / "workspace-a"
        workspace_b = tmp_path / "workspace-b"
        shared_home = tmp_path / "shared-home"
        workspace_a.mkdir()
        workspace_b.mkdir()
        shared_home.mkdir()
        slot = "shared-home-order"
        older = {
            "session_id": slot,
            "tool_use_id": "toolu_shared_older",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-shared-old", sid="agent-old"),
        }
        newer = {
            "session_id": slot,
            "tool_use_id": "toolu_shared_newer",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-shared-new", sid="agent-new"),
        }

        assert _run_pre_hook(older, workspace_a, home=shared_home).returncode == 0
        assert _run_pre_hook(newer, workspace_b, home=shared_home).returncode == 0
        assert _run_post_hook(newer, workspace_b, home=shared_home).returncode == 0
        assert _run_post_hook(older, workspace_a, home=shared_home).returncode == 0

        assert _read_session_cache(shared_home, slot)["uuid"] == "u-shared-new"
        assert _read_session_cache(workspace_b, slot)["uuid"] == "u-shared-new"
        assert _read_session_cache(workspace_a, slot) == {}

    def test_cross_workspace_clear_prevents_home_resurrection(self, tmp_path):
        workspace_a = tmp_path / "workspace-a"
        workspace_b = tmp_path / "workspace-b"
        shared_home = tmp_path / "shared-home"
        workspace_a.mkdir()
        workspace_b.mkdir()
        shared_home.mkdir()
        slot = "shared-home-clear"
        seed = {
            "session_id": slot,
            "tool_use_id": "toolu_shared_seed",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-seed"),
        }
        delayed = {
            "session_id": slot,
            "tool_use_id": "toolu_shared_delayed",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-delayed"),
        }
        assert _run_hook(seed, workspace_b, home=shared_home).returncode == 0
        assert _run_pre_hook(delayed, workspace_a, home=shared_home).returncode == 0

        cleared = subprocess.run(
            [
                sys.executable,
                str(SESSION_CACHE),
                "clear",
                "session",
                "--workspace",
                str(workspace_b),
                "--slot",
                slot,
            ],
            text=True,
            capture_output=True,
            timeout=10,
            env=_hook_env(workspace_b, shared_home),
        )
        assert cleared.returncode == 0, cleared.stderr
        assert _run_post_hook(delayed, workspace_a, home=shared_home).returncode == 0
        assert _read_session_cache(shared_home, slot) == {}
        assert _read_session_cache(workspace_a, slot) == {}

    def test_home_recovery_does_not_let_old_response_overwrite_new_identity(
        self,
        tmp_path,
    ):
        workspace_a = tmp_path / "workspace-a"
        workspace_b = tmp_path / "workspace-b"
        shared_home = tmp_path / "shared-home"
        workspace_a.mkdir()
        workspace_b.mkdir()
        shared_home.mkdir()
        blocked_mirror = shared_home / ".unitares"
        blocked_mirror.write_text("not a directory", encoding="utf-8")
        slot = "home-recovery-order"
        older = {
            "session_id": slot,
            "tool_use_id": "toolu_recovery_older",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-recovery-old"),
        }
        newer = {
            "session_id": slot,
            "tool_use_id": "toolu_recovery_newer",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-recovery-new"),
        }

        assert _run_pre_hook(older, workspace_a, home=shared_home).returncode == 0
        blocked_mirror.unlink()
        blocked_mirror.mkdir()
        assert _run_pre_hook(newer, workspace_b, home=shared_home).returncode == 0
        assert _run_post_hook(newer, workspace_b, home=shared_home).returncode == 0
        assert _run_post_hook(older, workspace_a, home=shared_home).returncode == 0

        assert _read_session_cache(shared_home, slot)["uuid"] == "u-recovery-new"
        assert _read_session_cache(workspace_b, slot)["uuid"] == "u-recovery-new"
        assert _read_session_cache(workspace_a, slot) == {}

    def test_clear_supersedes_inflight_identity_response(self, tmp_path):
        slot = "identity-clear-race"
        event = {
            "session_id": slot,
            "tool_use_id": "toolu_identity_cleared",
            "tool_name": "mcp__unitares-governance__start_session",
            "tool_input": {"force_new": True},
            "tool_response": _start_session_response(uuid="u-cleared"),
        }

        assert _run_pre_hook(event, tmp_path).returncode == 0
        cleared = subprocess.run(
            [
                sys.executable,
                str(SESSION_CACHE),
                "clear",
                "session",
                "--workspace",
                str(tmp_path),
                "--slot",
                slot,
            ],
            text=True,
            capture_output=True,
            timeout=10,
            env=_hook_env(tmp_path),
        )
        assert cleared.returncode == 0, cleared.stderr
        assert _run_post_hook(event, tmp_path).returncode == 0
        assert _read_session_cache(tmp_path, slot) == {}

    def test_post_without_pretool_snapshot_does_not_write(self, tmp_path):
        event = {
            "session_id": "identity-no-pre",
            "tool_use_id": "toolu_identity_no_pre",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-no-pre"),
        }

        assert _run_post_hook(event, tmp_path).returncode == 0
        assert _read_session_cache(tmp_path, "identity-no-pre") == {}

    def test_post_without_tool_id_does_not_write(self, tmp_path):
        event = {
            "session_id": "identity-no-id",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-no-id"),
        }

        assert _run_post_hook(event, tmp_path).returncode == 0
        assert _read_session_cache(tmp_path, "identity-no-id") == {}

    def test_failed_response_discards_generation_snapshot(self, tmp_path):
        event = {
            "session_id": "identity-failed",
            "tool_use_id": "toolu_identity_failed",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": False, "error": "rejected"}),
                    }
                ]
            },
        }

        assert _run_pre_hook(event, tmp_path).returncode == 0
        assert _run_post_hook(event, tmp_path).returncode == 0
        assert list((tmp_path / ".unitares" / "session-snapshots").glob("*.json")) == []


class TestSubagentOnboardGuard:
    """Client-side mirror of server PR #604: a subagent's onboard fires this
    hook under the DRIVER's session_id and must not capture the driver's
    slot cache (observed live 2026-06-12: slot 155d00fa cached the
    council-refire-verify subagent as the workspace lineage candidate)."""

    def test_subagent_onboard_does_not_overwrite_driver_cache(self, tmp_path):
        slot = "driver-slot"
        driver = {
            "session_id": slot,
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True, "spawn_reason": "new_session"},
            "tool_response": _mcp_response(uuid="u-driver-1", sid="agent-driver"),
        }
        assert _run_hook(driver, tmp_path).returncode == 0
        assert _read_session_cache(tmp_path, slot)["uuid"] == "u-driver-1"

        subagent = {
            "session_id": slot,  # same Claude session — hooks share the slot
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {
                "force_new": True,
                "spawn_reason": "subagent",
                "parent_agent_id": "u-driver-1",
            },
            "tool_response": _mcp_response(uuid="u-council-1", sid="agent-council"),
        }
        assert _run_hook(subagent, tmp_path).returncode == 0

        cache = _read_session_cache(tmp_path, slot)
        assert cache["uuid"] == "u-driver-1", "subagent onboard must not capture the slot"
        assert cache["client_session_id"] == "agent-driver"

    def test_subagent_onboard_does_not_seed_empty_cache(self, tmp_path):
        # Explicit-only contract, mirroring #604's NX-for-declared-subagents:
        # even with no standing cache, a declared-subagent onboard is not the
        # session's identity. (Server-side NX may claim an empty PIN, but the
        # slot cache feeds lineage candidates — a subagent there poisons the
        # next session's parent_agent_id hint.)
        hook_input = {
            "session_id": "fresh-slot",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True, "spawn_reason": "subagent"},
            "tool_response": _mcp_response(uuid="u-council-2"),
        }
        assert _run_hook(hook_input, tmp_path).returncode == 0
        assert _read_session_cache(tmp_path, "fresh-slot") == {}

    def test_subagent_identity_call_does_not_capture_driver_cache(self, tmp_path):
        # identity() carries client_session_id, not spawn_reason — the
        # spawn_reason guard can't see it. Observed live 2026-06-12: a
        # council verifier's late identity() re-captured the driver's slot
        # cache minutes after the onboard guard shipped. Resume-shaped tools
        # only write when the response UUID matches the cached one.
        slot = "driver-slot-2"
        driver = {
            "session_id": slot,
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True, "spawn_reason": "new_session"},
            "tool_response": _mcp_response(uuid="u-driver-2", sid="agent-driver2"),
        }
        assert _run_hook(driver, tmp_path).returncode == 0

        subagent_identity = {
            "session_id": slot,
            "tool_name": "mcp__unitares-governance__identity",
            "tool_input": {"client_session_id": "agent-council2"},
            "tool_response": _mcp_response(uuid="u-council-9", sid="agent-council2"),
        }
        assert _run_hook(subagent_identity, tmp_path).returncode == 0

        cache = _read_session_cache(tmp_path, slot)
        assert cache["uuid"] == "u-driver-2", "resume-shaped mismatch must not capture"
        assert cache["client_session_id"] == "agent-driver2"

    def test_driver_rebind_same_uuid_still_writes(self, tmp_path):
        # The legitimate recovery path: identity(agent_uuid=<own>, resume)
        # after a server restart refreshes the cached client_session_id.
        slot = "driver-slot-3"
        driver = {
            "session_id": slot,
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-driver-3", sid="agent-old"),
        }
        assert _run_hook(driver, tmp_path).returncode == 0

        rebind = {
            "session_id": slot,
            "tool_name": "mcp__unitares-governance__identity",
            "tool_input": {"agent_uuid": "u-driver-3", "resume": True},
            "tool_response": _mcp_response(uuid="u-driver-3", sid="agent-new"),
        }
        assert _run_hook(rebind, tmp_path).returncode == 0
        cache = _read_session_cache(tmp_path, slot)
        assert cache["uuid"] == "u-driver-3"
        assert cache["client_session_id"] == "agent-new"

    def test_identity_call_seeds_empty_cache(self, tmp_path):
        # First governance call in a fresh session may be identity() —
        # an empty cache accepts the bind.
        hook_input = {
            "session_id": "fresh-slot-2",
            "tool_name": "mcp__unitares-governance__identity",
            "tool_input": {"agent_uuid": "u-fresh-9", "resume": True},
            "tool_response": _mcp_response(uuid="u-fresh-9"),
        }
        assert _run_hook(hook_input, tmp_path).returncode == 0
        assert _read_session_cache(tmp_path, "fresh-slot-2")["uuid"] == "u-fresh-9"

    def test_bind_session_mismatch_skipped(self, tmp_path):
        slot = "driver-slot-4"
        driver = {
            "session_id": slot,
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-driver-4"),
        }
        assert _run_hook(driver, tmp_path).returncode == 0
        bind = {
            "session_id": slot,
            "tool_name": "mcp__unitares-governance__bind_session",
            "tool_input": {"agent_uuid": "u-other-1"},
            "tool_response": _mcp_response(uuid="u-other-1"),
        }
        assert _run_hook(bind, tmp_path).returncode == 0
        assert _read_session_cache(tmp_path, slot)["uuid"] == "u-driver-4"

    def test_non_subagent_spawn_reasons_still_write(self, tmp_path):
        for reason in ("new_session", "compaction", "explicit", ""):
            slot = f"slot-{reason or 'none'}"
            tool_input = {"force_new": True}
            if reason:
                tool_input["spawn_reason"] = reason
            hook_input = {
                "session_id": slot,
                "tool_name": "mcp__unitares-governance__onboard",
                "tool_input": tool_input,
                "tool_response": _mcp_response(uuid=f"u-{reason or 'none'}"),
            }
            assert _run_hook(hook_input, tmp_path).returncode == 0
            assert _read_session_cache(tmp_path, slot)["uuid"] == f"u-{reason or 'none'}"


class TestPostIdentityIgnoresOtherTools:
    def test_foreign_onboard_response_cannot_poison_slot_cache(self, tmp_path):
        slot = "foreign-server-slot"
        legitimate = {
            "session_id": slot,
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-driver", sid="agent-driver"),
        }
        assert _run_hook(legitimate, tmp_path).returncode == 0

        foreign = {
            "session_id": slot,
            "tool_name": "mcp__evil-unitares-proxy__onboard",
            "tool_input": {"force_new": True},
            "tool_response": _mcp_response(uuid="u-foreign", sid="agent-foreign"),
        }
        assert _run_hook(foreign, tmp_path).returncode == 0

        cache = _read_session_cache(tmp_path, slot)
        assert cache["uuid"] == "u-driver"
        assert cache["client_session_id"] == "agent-driver"

    def test_ignores_process_agent_update(self, tmp_path):
        """process_agent_update has its own hook — post-identity must skip."""
        hook_input = {
            "session_id": "s1",
            "tool_name": "mcp__unitares-governance__process_agent_update",
            "tool_input": {"response_text": "..."},
            "tool_response": _mcp_response(uuid="u-checkin"),
        }
        _run_hook(hook_input, tmp_path)
        assert _read_session_cache(tmp_path, "s1") == {}, "no cache should be written"

    def test_ignores_get_governance_metrics(self, tmp_path):
        hook_input = {
            "session_id": "s1",
            "tool_name": "mcp__unitares-governance__get_governance_metrics",
            "tool_input": {},
            "tool_response": _mcp_response(),
        }
        _run_hook(hook_input, tmp_path)
        assert _read_session_cache(tmp_path, "s1") == {}


class TestPostIdentityResilience:
    def test_no_stdin_exits_cleanly(self, tmp_path):
        """Running with no stdin must not error."""
        result = subprocess.run(
            [str(HOOK)],
            input="",
            text=True,
            capture_output=True,
            timeout=5,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0

    def test_malformed_json_exits_cleanly(self, tmp_path):
        result = subprocess.run(
            [str(HOOK)],
            input="not valid json{{{",
            text=True,
            capture_output=True,
            timeout=5,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0
        assert _read_session_cache(tmp_path) == {}

    def test_response_without_uuid_skips_write(self, tmp_path):
        """A failed onboard response (no uuid) must not write cache."""
        failed_response = {
            "content": [{"type": "text", "text": json.dumps({
                "success": False,
                "error": "trajectory_required",
            })}]
        }
        hook_input = {
            "session_id": "s1",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"name": "x"},
            "tool_response": failed_response,
        }
        _run_hook(hook_input, tmp_path)
        assert _read_session_cache(tmp_path, "s1") == {}

    def test_bare_list_response_shape_is_parsed(self, tmp_path):
        """Claude Code delivers tool_response as a bare list, not dict-wrapped.

        Regression guard for the silent-bail bug where `isinstance(resp, dict)
        and "content" in resp` rejected the real Claude Code shape, causing
        every onboard to exit without writing the session cache.
        """
        hook_input = {
            "session_id": "slot-list",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_input": {"name": "x"},
            "tool_response": _mcp_response_list(uuid="u-list-1"),
        }
        result = _run_hook(hook_input, tmp_path)
        assert result.returncode == 0
        cache = _read_session_cache(tmp_path, "slot-list")
        assert cache["uuid"] == "u-list-1"
        # S11: token stripped regardless of wire shape (bare-list or
        # dict-wrapped). Cache is a lineage anchor, not a resume credential.
        assert cache["continuity_token"] == ""
        assert cache["schema_version"] == 2

    def test_bound_identity_uuid_recovered_on_resume(self, tmp_path):
        """identity(resume=true) may return bound_identity dict instead of top-level uuid."""
        inner = {
            "success": True,
            "resumed": True,
            "bound_identity": {
                "uuid": "u-bound-1",
                "agent_id": "ResumedAgent",
                "display_name": "Resumed",
            },
            "continuity_token": "v1.recovered",
        }
        response = {"content": [{"type": "text", "text": json.dumps(inner)}]}
        hook_input = {
            "session_id": "s-bound",
            "tool_name": "mcp__unitares-governance__identity",
            "tool_input": {"agent_uuid": "u-bound-1"},
            "tool_response": response,
        }
        _run_hook(hook_input, tmp_path)
        cache = _read_session_cache(tmp_path, "s-bound")
        assert cache["uuid"] == "u-bound-1"
        assert cache["agent_id"] == "ResumedAgent"
