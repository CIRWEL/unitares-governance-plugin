"""Contract tests for the pre-governance-call PreToolUse injector.

The injector adds the slot-cached client_session_id to governance MCP calls
that carry no identity arguments, so driver calls resolve at tier=strong
instead of through the displaceable IP:UA onboard pin. See
scripts/governance_call_inject.py for the full contract.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
HOOK = PLUGIN_ROOT / "hooks" / "pre-governance-call"
SLOT = "test-session-abc"
SID = "agent-cafe1234-aaa"


def _write_cache(workspace: Path, slot: str = SLOT, sid: str = SID) -> None:
    cache_dir = workspace / ".unitares"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)[:64]
    (cache_dir / f"session-{safe}.json").write_text(json.dumps({
        "uuid": "cafe1234-0000-0000-0000-000000000000",
        "client_session_id": sid,
        "schema_version": 2,
        "slot": slot,
    }))


def _run(hook_input: dict, workspace: Path, *, host: str | None = None):
    command = [str(HOOK)]
    if host is not None:
        command.extend(("--host", host))
    return subprocess.run(
        command,
        input=json.dumps(hook_input),
        text=True,
        capture_output=True,
        timeout=10,
        cwd=str(workspace),
    )


def _hook_input(tool_name: str, tool_input: dict | None = None, slot: str = SLOT) -> dict:
    return {
        "session_id": slot,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input if tool_input is not None else {},
    }


def _updated_input(result) -> dict | None:
    out = result.stdout.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["updatedInput"]


class TestInjection:

    def test_injects_into_argless_checkin(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update",
            {"response_text": "did work"},
        ), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID
        assert updated["response_text"] == "did work"  # original fields echoed

    def test_injects_for_alias_tool(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__unitares-governance__sync_state", {}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID

    def test_injects_for_codex_governance_server_alias(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__governance__sync_state", {}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID

    def test_injects_for_legacy_unitares_server_alias(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__UNITARES__sync_state", {}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID

    def test_injects_for_gateway_server_name(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__claude_ai_UNITARES__knowledge", {"action": "search"}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID
        assert updated["action"] == "search"

    def test_injects_for_native_claude_plugin_scoped_server(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(
            _hook_input(
                "mcp__plugin_unitares-governance_unitares-governance__sync_state",
                {},
            ),
            tmp_path,
            host="claude",
        )
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID

    def test_no_permission_decision_emitted(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update", {}), tmp_path)
        payload = json.loads(result.stdout.strip())
        assert "permissionDecision" not in payload["hookSpecificOutput"]

    def test_codex_rewrite_includes_required_allow_decision(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(
            _hook_input("mcp__unitares-governance__sync_state", {}),
            tmp_path,
            host="codex",
        )
        payload = json.loads(result.stdout.strip())["hookSpecificOutput"]
        assert payload["permissionDecision"] == "allow"
        assert payload["updatedInput"]["client_session_id"] == SID

    def test_codex_legacy_alias_is_not_rewritten_or_preapproved(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(
            _hook_input("mcp__governance__sync_state", {}),
            tmp_path,
            host="codex",
        )

        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestTagNormalization:

    def test_normalizes_tags_and_injects_identity_together(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__knowledge",
            {"action": "note", "tags": ["Postgres", "DB_Pool", "postgres"]},
        ), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        # tags normalized + de-duped
        assert updated["tags"] == ["postgres", "db-pool"]
        # identity still injected in the same updatedInput
        assert updated["client_session_id"] == SID
        assert updated["action"] == "note"

    def test_normalizes_tags_even_when_identity_injection_skipped(self, tmp_path):
        # Proof field present => identity injection skipped, but tag
        # normalization must still happen.
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__knowledge",
            {"action": "search", "tags": ["Postgres"], "agent_id": "some-uuid"},
        ), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["tags"] == ["postgres"]
        # explicit proof field preserved, no injected session id
        assert updated["agent_id"] == "some-uuid"
        assert "client_session_id" not in updated

    def test_normalizes_tags_with_no_cache(self, tmp_path):
        # No session cache => no identity injection, but tags still normalize.
        result = _run(_hook_input(
            "mcp__unitares-governance__leave_note",
            {"tags": ["Foo_Bar", "foo-bar"]},
        ), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["tags"] == ["foo-bar"]

    def test_already_canonical_tags_no_output_without_injection(self, tmp_path):
        # Canonical tags + a proof field => nothing to change => empty output.
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__knowledge",
            {"action": "search", "tags": ["postgres"], "agent_id": "u"},
        ), tmp_path)
        assert result.stdout.strip() == ""

    def test_tags_untouched_on_non_tag_bearing_tool(self, tmp_path):
        # process_agent_update is not tag-bearing; a stray tags field is
        # passed through unchanged (only identity is injected).
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update",
            {"tags": ["Postgres"]},
        ), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["tags"] == ["Postgres"]  # not normalized
        assert updated["client_session_id"] == SID


class TestAnchoredMintingTools:

    def test_anchored_start_session_without_force_new_injects_env_anchor(self, tmp_path, monkeypatch):
        # Mirrors the anchored-session banner contract: a forgetful agent's
        # bare start_session() resumes through the per-thread anchor instead
        # of falling back to server-side pin/name heuristics.
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        monkeypatch.setenv("UNITARES_ORCHESTRATED", "1")
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session", {}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == "agent:/thread-123"

    def test_anchored_onboard_without_force_new_injects_env_anchor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        monkeypatch.setenv("UNITARES_ORCHESTRATED", "yes")
        result = _run(_hook_input(
            "mcp__unitares-governance__onboard", {"name": "claude-thread"}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["name"] == "claude-thread"
        assert updated["client_session_id"] == "agent:/thread-123"

    def test_unanchored_bare_start_session_stays_bare(self, tmp_path, monkeypatch):
        monkeypatch.delenv("UNITARES_CLIENT_SESSION_ID", raising=False)
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session", {}), tmp_path)
        assert result.stdout.strip() == ""

    def test_leaked_anchor_without_orchestration_marker_stays_bare(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/leaked-anchor")
        monkeypatch.delenv("UNITARES_ORCHESTRATED", raising=False)
        result = _run(
            _hook_input("mcp__unitares-governance__start_session", {}),
            tmp_path,
            host="codex",
        )
        assert result.stdout.strip() == ""

    def test_explicit_force_new_blocks_anchor_injection(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session", {"force_new": True}), tmp_path)
        assert result.stdout.strip() == ""

    def test_force_new_key_blocks_anchor_injection_even_when_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session", {"force_new": False}), tmp_path)
        assert result.stdout.strip() == ""

    def test_explicit_proof_field_blocks_anchor_injection(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session",
            {"client_session_id": "agent:/explicit"},
        ), tmp_path)
        assert result.stdout.strip() == ""


class TestExclusions:

    def test_codex_does_not_rewrite_admin_capable_tool(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(
            _hook_input("mcp__governance__config", {"action": "get"}),
            tmp_path,
            host="codex",
        )
        assert result.stdout.strip() == ""

    def test_never_injects_into_onboard(self, tmp_path):
        # Explicit force_new keeps its fresh-mint semantics even when the
        # minting tool has an anchored-session exception for bare calls.
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__onboard", {"force_new": True}), tmp_path)
        assert result.stdout.strip() == ""

    def test_never_injects_into_start_session(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session", {"force_new": True}), tmp_path)
        assert result.stdout.strip() == ""

    def test_never_injects_into_bind_session(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__bind_session", {}), tmp_path)
        assert result.stdout.strip() == ""

    def test_never_injects_into_identity(self, tmp_path):
        # identity() with a client_session_id is a resume/assert signal; a
        # bare identity() must stay bare or the post-identity hook would
        # cache the resumed (possibly stale) identity — end-to-end capture
        # via the pre→post hook chain (council finding 2026-06-12).
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__identity", {}), tmp_path)
        assert result.stdout.strip() == ""

    def test_skips_non_unitares_server(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__GitHub__agent", {}), tmp_path)
        assert result.stdout.strip() == ""

    def test_codex_skips_unitares_lookalike_without_preapproval(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(
            _hook_input("mcp__evil-unitares-proxy__sync_state", {}),
            tmp_path,
            host="codex",
        )
        assert result.stdout.strip() == ""

    def test_skips_non_mcp_tool(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("Bash", {"command": "ls"}), tmp_path)
        assert result.stdout.strip() == ""

    def test_skips_unknown_suffix(self, tmp_path):
        # New/unknown tools degrade to pin behavior, never risk an
        # extra-field validation error.
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__unitares-governance__skills", {}), tmp_path)
        assert result.stdout.strip() == ""


class TestProofFieldsWin:

    def test_explicit_client_session_id_wins(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update",
            {"client_session_id": "agent-explicit-111"}), tmp_path)
        assert result.stdout.strip() == ""

    def test_explicit_agent_id_wins(self, tmp_path):
        # A call naming its target carries its own proof signal — a
        # contract-following subagent passing agent_id is never captured.
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__agent",
            {"action": "get", "agent_id": "some-uuid"}), tmp_path)
        assert result.stdout.strip() == ""

    def test_explicit_continuity_token_wins(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__identity", {"continuity_token": "v1.tok"}), tmp_path)
        assert result.stdout.strip() == ""

    def test_empty_string_proof_field_does_not_block(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update",
            {"client_session_id": ""}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID


class TestFailOpen:

    def test_no_cache_no_output(self, tmp_path):
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update", {}), tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_wrong_slot_no_output(self, tmp_path):
        # Slot-scoped read only: another session's cache must never leak in.
        _write_cache(tmp_path, slot="other-session-xyz")
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update", {}, slot=SLOT), tmp_path)
        assert result.stdout.strip() == ""

    def test_missing_session_id_no_output(self, tmp_path):
        _write_cache(tmp_path)
        hook_input = _hook_input("mcp__unitares-governance__process_agent_update", {})
        del hook_input["session_id"]
        result = _run(hook_input, tmp_path)
        assert result.stdout.strip() == ""

    def test_malformed_stdin_exits_zero(self, tmp_path):
        result = subprocess.run(
            [str(HOOK)], input="not json", text=True,
            capture_output=True, timeout=10, cwd=str(tmp_path),
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_cache_without_sid_no_output(self, tmp_path):
        cache_dir = tmp_path / ".unitares"
        cache_dir.mkdir(parents=True)
        (cache_dir / f"session-{SLOT}.json").write_text(json.dumps({"uuid": "u-1"}))
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update", {}), tmp_path)
        assert result.stdout.strip() == ""
