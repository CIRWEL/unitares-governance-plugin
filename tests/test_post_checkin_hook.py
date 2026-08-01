"""Integration test for hooks/post-checkin.

The hook is a small shell script, but its contract — reset the accumulator
and stamp session.last_checkin_ts whenever process_agent_update runs — is
load-bearing for the auto-checkin forcing function. A broken or missing
reset causes the post-edit hook to re-fire on the very next edit.

S20.1a (2026-04-26): the stamp must land in the slot-scoped session cache
so the post-PR-19 session-start hook (which reads slot-scoped only) and the
post-edit auto-checkin hook (which reads via slot-aware _session_lookup)
both see the timestamp on subsequent fires. Tests below exercise the
realistic Claude Code stdin shape (session_id present) and pin the no-flat-
write contract for the slotless fallback case.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION_CACHE = ROOT / "scripts" / "session_cache.py"
POST_CHECKIN = ROOT / "hooks" / "post-checkin"
PRE_GOVERNANCE_CALL = ROOT / "hooks" / "pre-governance-call"


def _seed_session(workspace: Path, slot: str | None = None, **extra) -> None:
    # S20.1b: v2-canonical identity is uuid + client_session_id; the legacy
    # `continuity_token` seed is no longer valid via the helper. Tests now
    # seed the cache the way a v2 onboard hook would.
    payload = {
        "uuid": "00000000-0000-0000-0000-000000000abc",
        "client_session_id": "sid-xyz",
        **extra,
    }
    cmd = [
        sys.executable,
        str(SESSION_CACHE),
        "set",
        "session",
        "--workspace",
        str(workspace),
        "--stamp",
        "--json",
        json.dumps(payload),
    ]
    if slot:
        cmd.extend(["--slot", slot])
    subprocess.run(cmd, check=True, capture_output=True)


def _seed_milestone_edits(workspace: Path, n: int) -> None:
    for i in range(n):
        subprocess.run(
            [
                sys.executable,
                str(SESSION_CACHE),
                "bump-edit",
                "--workspace",
                str(workspace),
                "--file-path",
                f"/w/f{i}.py",
            ],
            check=True,
            capture_output=True,
        )


def _read(kind: str, workspace: Path, slot: str | None = None) -> dict:
    cmd = [
        sys.executable,
        str(SESSION_CACHE),
        "get",
        kind,
        "--workspace",
        str(workspace),
    ]
    if slot:
        cmd.extend(["--slot", slot])
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def _run_hook(
    workspace: Path,
    tool_name: str = "mcp__unitares__process_agent_update",
    session_id: str | None = None,
    tool_use_id: str | None = None,
    tool_response: object | None = None,
):
    success_response = {
        "content": [
            {"type": "text", "text": json.dumps({"success": True})}
        ]
    }
    payload_dict = {
        "tool_name": tool_name,
        "tool_input": {},
        "tool_response": tool_response if tool_response is not None else success_response,
    }
    if session_id is not None:
        payload_dict["session_id"] = session_id
    if tool_use_id is not None:
        payload_dict["tool_use_id"] = tool_use_id
    payload = json.dumps(payload_dict)
    return subprocess.run(
        ["bash", str(POST_CHECKIN)],
        input=payload,
        text=True,
        cwd=str(workspace),
        capture_output=True,
    )


def _run_pre_hook(
    workspace: Path,
    session_id: str,
    tool_use_id: str,
    tool_name: str = "mcp__unitares__process_agent_update",
):
    payload = json.dumps(
        {
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "tool_input": {"response_text": "manual check-in"},
        }
    )
    return subprocess.run(
        ["bash", str(PRE_GOVERNANCE_CALL)],
        input=payload,
        text=True,
        cwd=str(workspace),
        capture_output=True,
    )


SLOT = "claude-session-abc-123"


def test_idless_hook_preserves_accumulator_and_stamps_last_checkin(tmp_path: Path) -> None:
    _seed_session(tmp_path, slot=SLOT)
    _seed_milestone_edits(tmp_path, 3)

    before_milestone = _read("milestone", tmp_path)
    assert before_milestone["edit_count"] == 3
    assert before_milestone["first_edit_ts"] is not None

    before_ts = int(time.time())
    result = _run_hook(tmp_path, session_id=SLOT)
    assert result.returncode == 0, result.stderr

    after_milestone = _read("milestone", tmp_path)
    assert after_milestone["edit_count"] == 3
    assert after_milestone["files_touched"]
    assert after_milestone["first_edit_ts"] is not None
    assert after_milestone["last_edit_ts"] is not None

    after_session = _read("session", tmp_path, slot=SLOT)
    assert "last_checkin_ts" in after_session
    assert int(after_session["last_checkin_ts"]) >= before_ts


def test_correlated_hook_resets_snapshot_without_newer_edits(tmp_path: Path) -> None:
    _seed_session(tmp_path, slot=SLOT)
    _seed_milestone_edits(tmp_path, 2)
    tool_use_id = "toolu_correlated_success"
    pre = _run_pre_hook(tmp_path, SLOT, tool_use_id)
    assert pre.returncode == 0, pre.stderr

    result = _run_hook(tmp_path, session_id=SLOT, tool_use_id=tool_use_id)

    assert result.returncode == 0, result.stderr
    assert _read("milestone", tmp_path)["edit_count"] == 0
    assert list((tmp_path / ".unitares" / "milestone-snapshots").glob("*.json")) == []


def test_hook_is_noop_without_session(tmp_path: Path) -> None:
    # Never onboarded — the hook must not create files or crash. Claude will
    # install this plugin in workspaces that never call governance, and we
    # don't want the hook to leave behind a stub .unitares/ directory.
    result = _run_hook(tmp_path, session_id=SLOT)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".unitares").exists()


def test_failed_checkin_without_session_discards_snapshot(tmp_path: Path) -> None:
    _seed_milestone_edits(tmp_path, 1)
    tool_use_id = "toolu_no_session_failure"
    pre = _run_pre_hook(tmp_path, SLOT, tool_use_id)
    assert pre.returncode == 0, pre.stderr
    snapshots = tmp_path / ".unitares" / "milestone-snapshots"
    assert len(list(snapshots.glob("*.json"))) == 1

    post = _run_hook(
        tmp_path,
        session_id=SLOT,
        tool_use_id=tool_use_id,
        tool_response={"success": False, "error": "denied"},
    )

    assert post.returncode == 0, post.stderr
    assert list(snapshots.glob("*.json")) == []
    assert _read("milestone", tmp_path)["edit_count"] == 1


def test_pre_hook_does_not_snapshot_non_governance_checkin(tmp_path: Path) -> None:
    _seed_milestone_edits(tmp_path, 1)

    pre = _run_pre_hook(
        tmp_path,
        SLOT,
        "toolu_fake_checkin",
        tool_name="mcp__fake__sync_state",
    )

    assert pre.returncode == 0, pre.stderr
    snapshots = tmp_path / ".unitares" / "milestone-snapshots"
    assert not snapshots.exists() or list(snapshots.glob("*.json")) == []


def test_post_hook_does_not_acknowledge_non_governance_checkin(tmp_path: Path) -> None:
    _seed_session(tmp_path, slot=SLOT)
    _seed_milestone_edits(tmp_path, 2)

    post = _run_hook(
        tmp_path,
        tool_name="mcp__fake__process_agent_update",
        session_id=SLOT,
        tool_use_id="toolu_fake_checkin",
    )

    assert post.returncode == 0, post.stderr
    assert _read("milestone", tmp_path)["edit_count"] == 2
    assert "last_checkin_ts" not in _read("session", tmp_path, slot=SLOT)


def test_hook_updates_last_checkin_on_each_call(tmp_path: Path) -> None:
    _seed_session(tmp_path, slot=SLOT, last_checkin_ts=1_000_000_000)
    _seed_milestone_edits(tmp_path, 1)

    _run_hook(tmp_path, session_id=SLOT)
    first = _read("session", tmp_path, slot=SLOT)["last_checkin_ts"]
    assert int(first) > 1_000_000_000

    # A second check-in should push the stamp forward, not preserve the
    # original — the forcing function relies on last_checkin_ts tracking
    # the most recent successful check-in.
    time.sleep(1)
    _seed_milestone_edits(tmp_path, 1)
    _run_hook(tmp_path, session_id=SLOT)
    second = _read("session", tmp_path, slot=SLOT)["last_checkin_ts"]
    assert int(second) >= int(first)


def test_hook_preserves_edit_made_during_manual_checkin(tmp_path: Path) -> None:
    _seed_session(tmp_path, slot=SLOT)
    _seed_milestone_edits(tmp_path, 1)
    tool_use_id = "toolu_manual_race"

    pre = _run_pre_hook(tmp_path, SLOT, tool_use_id)
    assert pre.returncode == 0, pre.stderr
    assert len(list((tmp_path / ".unitares" / "milestone-snapshots").glob("*.json"))) == 1

    _seed_milestone_edits(tmp_path, 1)
    post = _run_hook(tmp_path, session_id=SLOT, tool_use_id=tool_use_id)
    assert post.returncode == 0, post.stderr

    milestone = _read("milestone", tmp_path)
    assert milestone["edit_count"] == 2
    assert list((tmp_path / ".unitares" / "milestone-snapshots").glob("*.json")) == []


def test_failed_checkin_preserves_milestone_and_timestamp(tmp_path: Path) -> None:
    _seed_session(tmp_path, slot=SLOT, last_checkin_ts=1_000_000_000)
    _seed_milestone_edits(tmp_path, 2)
    tool_use_id = "toolu_failed_checkin"
    pre = _run_pre_hook(tmp_path, SLOT, tool_use_id)
    assert pre.returncode == 0, pre.stderr

    post = _run_hook(
        tmp_path,
        session_id=SLOT,
        tool_use_id=tool_use_id,
        tool_response={"success": False, "error": "rejected"},
    )

    assert post.returncode == 0, post.stderr
    milestone = _read("milestone", tmp_path)
    assert milestone["edit_count"] == 2
    assert _read("session", tmp_path, slot=SLOT)["last_checkin_ts"] == 1_000_000_000
    assert list((tmp_path / ".unitares" / "milestone-snapshots").glob("*.json")) == []


def test_outer_success_cannot_mask_nested_checkin_failure(tmp_path: Path) -> None:
    _seed_session(tmp_path, slot=SLOT, last_checkin_ts=1_000_000_000)
    _seed_milestone_edits(tmp_path, 1)

    post = _run_hook(
        tmp_path,
        session_id=SLOT,
        tool_response={"success": True, "result": {"success": False}},
    )

    assert post.returncode == 0, post.stderr
    assert _read("milestone", tmp_path)["edit_count"] == 1
    assert _read("session", tmp_path, slot=SLOT)["last_checkin_ts"] == 1_000_000_000


def test_hook_does_not_write_flat_session_when_slot_known(tmp_path: Path) -> None:
    """S20.1a contract: with a session_id on stdin, the milestone-timestamp
    write lands in session-<slot>.json — flat session.json must not be
    created or modified by the hook.
    """
    _seed_session(tmp_path, slot=SLOT)
    _seed_milestone_edits(tmp_path, 1)
    flat_path = tmp_path / ".unitares" / "session.json"
    assert not flat_path.exists(), "test setup created flat session.json"

    result = _run_hook(tmp_path, session_id=SLOT)
    assert result.returncode == 0, result.stderr
    assert not flat_path.exists(), (
        "post-checkin wrote flat session.json instead of slot-scoped target — "
        "S20.1a contract violated; auto-checkin pipeline relies on slotted writes"
    )

    slotted = tmp_path / ".unitares" / f"session-{SLOT}.json"
    assert slotted.exists(), "post-checkin did not write slot-scoped cache"
    payload = json.loads(slotted.read_text())
    assert "last_checkin_ts" in payload


def test_hook_emits_slotless_skip_breadcrumb(tmp_path: Path) -> None:
    """Architect honesty signal (2026-04-26): the slotless-stdin path must
    emit a [SLOTLESS_SKIP] stderr breadcrumb so degraded state is legible
    rather than collapsed with "ran cleanly" or "crashed." Pre-fix the
    silent-skip variant was indistinguishable from a successful run."""
    _seed_session(tmp_path, slot=SLOT)
    result = _run_hook(tmp_path, session_id=None)
    assert result.returncode == 0
    assert "[SLOTLESS_SKIP]" in result.stderr, (
        f"post-checkin slotless path must emit [SLOTLESS_SKIP] breadcrumb; "
        f"got stderr: {result.stderr!r}"
    )


def test_hook_skips_when_slot_unrecoverable(tmp_path: Path) -> None:
    """Slotless-stdin path: the hook must not synthesize a slot or fall back
    to flat session.json. With no addressable slot the hook is a full no-op —
    the milestone accumulator is per-workspace and shared across slots, so
    resetting it on behalf of an unidentifiable caller would clobber other
    agents' windows. Pre-S20 behavior would have stamped flat session.json.
    """
    _seed_session(tmp_path, slot=SLOT)
    _seed_milestone_edits(tmp_path, 2)
    flat_path = tmp_path / ".unitares" / "session.json"

    # Stdin without session_id (manual invocation, non-Claude-Code context).
    result = _run_hook(tmp_path, session_id=None)
    assert result.returncode == 0, result.stderr

    # No flat session.json was created.
    assert not flat_path.exists(), (
        "post-checkin wrote flat session.json on slotless stdin — "
        "S20.1a expects skip-and-exit, not flat-fallback"
    )

    # Slotted cache unchanged — hook had no slot to address.
    slotted_after = _read("session", tmp_path, slot=SLOT)
    assert "last_checkin_ts" not in slotted_after

    # Milestone accumulator unchanged — workspace-scoped, but resetting on
    # behalf of an unidentifiable caller would clobber other agents' windows.
    after_milestone = _read("milestone", tmp_path)
    assert after_milestone["edit_count"] == 2
