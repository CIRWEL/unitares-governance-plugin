"""Unit tests for scripts/_session_lookup.py — slot-aware cache read."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _session_lookup import (
    _extract_slot,
    _slot_filename,
    load_session_for_hook,
    resolve_session_file,
)


def test_slot_filename_matches_onboard_helper():
    """Must stay byte-identical with onboard_helper._slot_filename."""
    from onboard_helper import _slot_filename as onboard_slot_filename  # type: ignore
    assert _slot_filename("abc-xyz") == onboard_slot_filename("abc-xyz")
    assert _slot_filename(None) == onboard_slot_filename(None)
    assert _slot_filename("") == onboard_slot_filename("")


def _write_invalid_authority(home: Path, slot: str) -> None:
    path = home / ".unitares-cache-authority" / _slot_filename(slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "generation": 7, "mirror_valid": False}),
        encoding="utf-8",
    )


def test_extract_slot_handles_missing_payload():
    assert _extract_slot("") is None
    assert _extract_slot("{}") is None
    assert _extract_slot("not json") is None


def test_extract_slot_reads_session_id():
    assert _extract_slot('{"session_id":"abc-123"}') == "abc-123"


def test_workspace_equal_to_home_still_honors_invalid_mirror_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    slot = "invalid-home-primary"
    cache = home / ".unitares" / _slot_filename(slot)
    cache.parent.mkdir(parents=True)
    cache.write_text('{"uuid": "stale"}', encoding="utf-8")
    _write_invalid_authority(home, slot)
    monkeypatch.setenv("HOME", str(home))

    assert resolve_session_file(home, slot) is None


def test_symlinked_home_cache_uses_authority_from_raw_home_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    mirror_target = tmp_path / "mirror-target"
    other_workspace = tmp_path / "other"
    home.mkdir()
    mirror_target.mkdir()
    try:
        (home / ".unitares").symlink_to(mirror_target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    slot = "symlinked-authority"
    (mirror_target / _slot_filename(slot)).write_text(
        '{"uuid": "stale"}',
        encoding="utf-8",
    )
    _write_invalid_authority(home, slot)
    monkeypatch.setenv("HOME", str(home))

    assert resolve_session_file(other_workspace, slot) is None


def test_extract_slot_uses_codex_transcript_fallback():
    transcript = "/home/user/.codex/sessions/2026/06/18/rollout.jsonl"
    expected = "codex-transcript_path-" + hashlib.sha256(transcript.encode()).hexdigest()[:16]
    assert _extract_slot(json.dumps({"transcript_path": transcript})) == expected


def test_resolve_prefers_slotted_file(tmp_path):
    (tmp_path / ".unitares").mkdir()
    slotted_name = _slot_filename("my-slot")
    slotted = tmp_path / ".unitares" / slotted_name
    unslotted = tmp_path / ".unitares" / "session.json"
    slotted.write_text("{}")
    unslotted.write_text("{}")
    assert resolve_session_file(tmp_path, "my-slot") == slotted


def test_resolve_with_slot_does_not_fall_back_to_unslotted(tmp_path):
    (tmp_path / ".unitares").mkdir()
    unslotted = tmp_path / ".unitares" / "session.json"
    unslotted.write_text("{}")
    # A live slot must not inherit the legacy flat cache when the slot misses.
    assert resolve_session_file(tmp_path, "my-slot") is None


def test_resolve_returns_none_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent_home"))
    assert resolve_session_file(tmp_path, None) is None


def test_load_session_for_hook_full_roundtrip(tmp_path):
    (tmp_path / ".unitares").mkdir()
    slot = "session-4321"
    path = tmp_path / ".unitares" / _slot_filename(slot)
    payload = {
        "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
        "client_session_id": "agent-test1234",
        "continuity_token": "v1.faketoken",
        "slot": slot,
    }
    path.write_text(json.dumps(payload))
    result = load_session_for_hook(tmp_path, json.dumps({"session_id": slot}))
    assert result["uuid"] == payload["uuid"]
    assert result["client_session_id"] == payload["client_session_id"]
    assert result["continuity_token"] == payload["continuity_token"]
    assert result["slot"] == slot


def test_load_session_for_hook_empty_stdin_returns_empty(tmp_path):
    (tmp_path / ".unitares").mkdir()
    # Workspace-flat file exists but workspace-flat fallback was retired (S20 §3d).
    # Empty stdin means no slot → no match → empty dict.
    unslotted = tmp_path / ".unitares" / "session.json"
    unslotted.write_text('{"uuid":"u","client_session_id":"c","continuity_token":"t","slot":"s"}')
    result = load_session_for_hook(tmp_path, "")
    assert result == {}


def test_load_session_for_hook_reads_codex_fallback_slot(tmp_path):
    (tmp_path / ".unitares").mkdir()
    transcript = "/home/user/.codex/sessions/2026/06/18/rollout.jsonl"
    slot = "codex-transcript_path-" + hashlib.sha256(transcript.encode()).hexdigest()[:16]
    path = tmp_path / ".unitares" / _slot_filename(slot)
    payload = {
        "uuid": "codex-u",
        "client_session_id": "agent-codex",
        "slot": slot,
    }
    path.write_text(json.dumps(payload))

    result = load_session_for_hook(tmp_path, json.dumps({"transcript_path": transcript}))

    assert result["uuid"] == "codex-u"
    assert result["client_session_id"] == "agent-codex"


def test_home_fallback_removed_closes_cross_agent_siphoning(tmp_path, monkeypatch):
    """Regression: ~/.unitares/session.json must NOT be silently shared
    across parallel agents.

    Scenario reproduces the 2026-04-18 siphoning incident: one agent writes
    its identity to $HOME/.unitares/session.json (via legacy onboard_helper
    or older hook). A second Claude Code session in a different workspace
    starts, its own slotted cache is empty, and resolve_session_file is
    called. Before this fix it would fall through to the HOME file and the
    second agent would silently adopt the first's UUID — identity-invariant
    #3 violation (per-instance isolation). After the fix it returns None.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    shared_home_cache = fake_home / ".unitares"
    shared_home_cache.mkdir()
    # Simulate a stale identity from a prior session sitting in $HOME
    (shared_home_cache / "session.json").write_text(json.dumps({
        "uuid": "stolen-uuid-from-another-agent",
        "client_session_id": "agent-stolen",
        "continuity_token": "v1.stolen_token",
    }))
    monkeypatch.setenv("HOME", str(fake_home))

    # Different workspace, empty .unitares — the vulnerable case
    other_workspace = tmp_path / "other_workspace"
    other_workspace.mkdir()

    result_path = resolve_session_file(other_workspace, "some-slot")
    assert result_path is None, (
        f"HOME fallback must not leak across workspaces; got {result_path}"
    )

    # And the hook-level helper must also return empty, not silently siphon
    hook_result = load_session_for_hook(
        other_workspace, json.dumps({"session_id": "some-slot"}),
    )
    assert hook_result == {}, (
        f"load_session_for_hook must return empty rather than siphoning "
        f"from $HOME; got {hook_result}"
    )


def test_slotted_home_fallback_when_workspace_misses(tmp_path, monkeypatch):
    """PWD-mismatch fix (2026-05-10): when post-identity wrote the slot file
    in PWD=X but post-edit fires from PWD=Y, the lookup should fall back to
    $HOME/.unitares/session-<slot>.json (which X==$HOME wrote) and find it.

    Reproduces the ~/.unitares/hook-skips.log evidence: 51 misses from
    /home/user/projects/trajectory-identity-paper despite the active
    session having a slot file in $HOME (session was started from $HOME).
    """
    fake_home = tmp_path / "home"
    home_unitares = fake_home / ".unitares"
    home_unitares.mkdir(parents=True)
    slot = "claude-session-12345"
    home_slot_file = home_unitares / _slot_filename(slot)
    home_slot_file.write_text(json.dumps({
        "uuid": "u-from-home",
        "client_session_id": "agent-from-home",
        "slot": slot,
    }))
    monkeypatch.setenv("HOME", str(fake_home))

    # PWD-at-edit is a project workspace with no .unitares/ dir at all.
    edit_pwd = tmp_path / "projects" / "trajectory-identity-paper"
    edit_pwd.mkdir(parents=True)

    result = resolve_session_file(edit_pwd, slot)
    assert result == home_slot_file, (
        f"slotted HOME fallback should resolve when workspace cache is empty; "
        f"got {result}"
    )

    # Hook helper roundtrip: load_session_for_hook must surface the cached payload
    hook_payload = load_session_for_hook(
        edit_pwd, json.dumps({"session_id": slot}),
    )
    assert hook_payload["uuid"] == "u-from-home"
    assert hook_payload["client_session_id"] == "agent-from-home"


def test_unslotted_home_still_blocked_after_slotted_fallback(tmp_path, monkeypatch):
    """Regression: the slotted HOME fallback (added 2026-05-10) must NOT
    re-introduce the unslotted-HOME siphoning hole that was closed
    2026-04-18. A reader with a slot key, given a workspace with no slot
    file, must still NOT fall through to $HOME/.unitares/session.json
    (the legacy shared file)."""
    fake_home = tmp_path / "home"
    home_unitares = fake_home / ".unitares"
    home_unitares.mkdir(parents=True)
    # Plant ONLY an unslotted file in HOME — the kind that previously
    # siphoned identity across agents
    (home_unitares / "session.json").write_text(json.dumps({
        "uuid": "stolen-uuid",
        "client_session_id": "agent-stolen",
    }))
    monkeypatch.setenv("HOME", str(fake_home))

    edit_pwd = tmp_path / "some_workspace"
    edit_pwd.mkdir()

    # Slotted lookup must NOT find the unslotted file
    result = resolve_session_file(edit_pwd, "victim-slot")
    assert result is None, (
        f"slotted lookup must not fall through to unslotted HOME file; "
        f"got {result}"
    )


def test_workspace_slotted_preferred_over_home(tmp_path, monkeypatch):
    """When both workspace AND home have a slot file for the same slot,
    workspace wins. Avoids stale-HOME-file shadowing legitimate per-workspace
    state if both happen to exist."""
    fake_home = tmp_path / "home"
    home_unitares = fake_home / ".unitares"
    home_unitares.mkdir(parents=True)
    slot = "shared-slot"
    (home_unitares / _slot_filename(slot)).write_text(json.dumps({"uuid": "from-home"}))
    monkeypatch.setenv("HOME", str(fake_home))

    edit_pwd = tmp_path / "ws"
    (edit_pwd / ".unitares").mkdir(parents=True)
    ws_path = edit_pwd / ".unitares" / _slot_filename(slot)
    ws_path.write_text(json.dumps({"uuid": "from-workspace"}))

    result = resolve_session_file(edit_pwd, slot)
    assert result == ws_path
    assert json.loads(result.read_text())["uuid"] == "from-workspace"


def test_workspace_local_unslotted_not_surfaced_after_s20_3d(tmp_path):
    """S20 §3d: workspace-flat fallback retired. A legacy session.json in the
    workspace .unitares/ dir must NOT be returned when slot is absent — it is
    a lineage candidate for cmd_list, not a hook read target."""
    ws = tmp_path / "ws"
    (ws / ".unitares").mkdir(parents=True)
    (ws / ".unitares" / "session.json").write_text('{"uuid":"ws-local"}')
    result = resolve_session_file(ws, None)
    assert result is None


def test_cli_emits_empty_fields_when_nothing_matches(tmp_path):
    """S20.1a / S20 §3d: with no stdin slot and no slotted file, the CLI must
    emit empty strings for all fields. Pre-S20.1a SLOT emitted the literal
    `"default"` which collapsed every slotless caller onto session-default.json."""
    import subprocess
    import sys as _sys

    (tmp_path / ".unitares").mkdir()
    # Legacy flat file exists but is no longer read (S20 §3d fallback retired).
    (tmp_path / ".unitares" / "session.json").write_text(json.dumps({
        "uuid": "u",
        "client_session_id": "c",
        "continuity_token": "t",
    }))

    script = Path(__file__).parent.parent / "scripts" / "_session_lookup.py"
    result = subprocess.run(
        [_sys.executable, str(script), "--workspace", str(tmp_path)],
        input="",
        capture_output=True,
        text=True,
        check=True,
    )
    assert 'SLOT=""' in result.stdout, (
        f"expected empty SLOT when no slotted file matches; got:\n{result.stdout}"
    )
    assert 'UUID=""' in result.stdout
    assert 'CSID=""' in result.stdout


def test_home_flat_not_reachable_even_when_workspace_is_home(tmp_path, monkeypatch):
    """S20 §3d: workspace-flat fallback retired. Passing workspace=$HOME with
    slot=None must NOT surface $HOME/.unitares/session.json — legacy flat files
    are lineage candidates for cmd_list only, not hook read targets."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".unitares").mkdir()
    (fake_home / ".unitares" / "session.json").write_text('{"uuid":"cli-shared"}')
    monkeypatch.setenv("HOME", str(fake_home))

    result = resolve_session_file(fake_home, None)
    assert result is None
