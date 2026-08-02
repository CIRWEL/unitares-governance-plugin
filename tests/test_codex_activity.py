"""Contract tests for local, hook-derived Codex liveness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
HOOK = ROOT / "hooks" / "post-activity"
sys.path.insert(0, str(SCRIPTS))

import activity_observer  # noqa: E402


def _payload(slot: str = "codex-slot", tool: str = "exec_command") -> str:
    return json.dumps(
        {
            "session_id": slot,
            "hook_event_name": "PostToolUse",
            "tool_name": tool,
            "tool_use_id": "tool-1",
            "tool_input": {"ignored": "agent content is not persisted"},
            "tool_response": {"ignored": "tool output is not persisted"},
        }
    )


def _state(home: Path, slot: str = "codex-slot") -> dict:
    path = activity_observer.activity_state_path(home, slot)
    return json.loads(path.read_text(encoding="utf-8"))


def test_record_is_local_and_ledger_is_identity_free(tmp_path):
    assert activity_observer.record_activity(
        _payload(), home=tmp_path, now=100.0
    ) == "recorded"

    state = _state(tmp_path)
    assert state["source"] == "codex_post_tool_use_hook"
    assert state["evidence_source"] == "hook_derived"
    assert state["measurement_scope"] == "host_event_receipt"
    assert state["network_emission"] == "none"
    assert state["slot"] == "codex-slot"
    assert state["tool_count"] == 1
    assert "tool_input" not in state
    assert "tool_response" not in state
    for forbidden in (
        "uuid",
        "agent_id",
        "client_session_id",
        "continuity_token",
        "eisv",
        "confidence",
        "progress",
    ):
        assert forbidden not in state


def test_completed_tool_events_increment_the_same_slot(tmp_path):
    assert activity_observer.record_activity(
        _payload(), home=tmp_path, now=100.0
    ) == "recorded"
    assert activity_observer.record_activity(
        _payload(tool="apply_patch"), home=tmp_path, now=160.0
    ) == "recorded"

    state = _state(tmp_path)
    assert state["tool_count"] == 2
    assert state["first_activity_at"] == 100.0
    assert state["last_activity_at"] == 160.0


def test_disabled_slotless_or_wrong_event_activity_is_not_recorded(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("UNITARES_CODEX_LIVENESS", "off")
    assert activity_observer.record_activity(_payload(), home=tmp_path) == "skip_disabled"
    monkeypatch.setenv("UNITARES_CODEX_LIVENESS", "on")
    assert activity_observer.record_activity("{}", home=tmp_path) == "skip_no_slot"
    wrong_event = json.dumps(
        {"session_id": "slot", "hook_event_name": "Stop", "tool_name": "x"}
    )
    assert (
        activity_observer.record_activity(wrong_event, home=tmp_path)
        == "skip_wrong_event"
    )
    assert not (tmp_path / ".unitares").exists()


def test_activity_paths_do_not_collide_after_slot_sanitization(tmp_path):
    assert activity_observer.activity_state_path(
        tmp_path, "slot/a"
    ) != activity_observer.activity_state_path(tmp_path, "slot:a")


def test_shell_hook_records_codex_payload_without_user_visible_output(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run(
        [str(HOOK), "--host", "codex"],
        input=_payload(slot="shell-slot"),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert _state(tmp_path, "shell-slot")["tool_count"] == 1


def test_shell_hook_is_codex_only(tmp_path):
    result = subprocess.run(
        [str(HOOK), "--host", "claude"],
        input=_payload(),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path)},
        timeout=5,
    )
    assert result.returncode == 0
    assert not (tmp_path / ".unitares").exists()
