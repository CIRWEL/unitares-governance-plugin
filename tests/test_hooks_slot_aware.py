"""Regression test: post-stop and post-edit both find the
slotted session cache when Claude Code passes a session_id on stdin.

Before commit that introduces scripts/_session_lookup.py, these hooks
read only the unslotted session.json and silently exited when the
slotted file was the only one present.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
import sys

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from _session_lookup import _slot_filename  # noqa: E402

from tests.test_session_start_checkin import RecordingHandler, _ReusableTCPServer  # noqa: E402


def _seed_slotted_cache(workspace: Path, slot: str, payload: dict) -> Path:
    unitares = workspace / ".unitares"
    unitares.mkdir(exist_ok=True)
    path = unitares / _slot_filename(slot)
    path.write_text(json.dumps(payload))
    return path


def _run_hook_with_mock_server(
    hook_name: str,
    workspace: Path,
    slot: str,
    extra_stdin_fields: dict | None = None,
    extra_env: dict | None = None,
    return_all: bool = False,
):
    """Launch mock server, invoke hook with {"session_id": slot}, collect calls."""
    RecordingHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), RecordingHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    stdin_payload = {"session_id": slot}
    if extra_stdin_fields:
        stdin_payload.update(extra_stdin_fields)
    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(workspace),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
            "UNITARES_CHECKIN_LOG": str(workspace / "checkins.log"),
            "UNITARES_AUTO_CHECKIN_ENABLED": "1",
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PWD": str(workspace),
        }
        if extra_env:
            env.update(extra_env)
        hook = PLUGIN_ROOT / "hooks" / hook_name
        subprocess.run(
            [str(hook)],
            env=env,
            cwd=str(workspace),
            input=json.dumps(stdin_payload),
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)
    if return_all:
        return list(RecordingHandler.calls)
    return [c for c in RecordingHandler.calls if c.get("name") == "process_agent_update"]


def test_post_stop_reads_slotted_cache(tmp_path):
    slot = "real-slot-1234"
    _seed_slotted_cache(tmp_path, slot, {
        "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
        "client_session_id": "agent-real-1234",
        "continuity_token": "v1.real-tok",
        "slot": slot,
    })
    checkins = _run_hook_with_mock_server("post-stop", tmp_path, slot)
    events = [c["arguments"]["metadata"]["event"] for c in checkins]
    assert "turn_stop" in events, (
        f"post-stop did not fire turn_stop when only the slotted cache existed; "
        f"events: {events}"
    )


def _onboard_args(calls: list[dict]) -> dict:
    onboard_calls = [c for c in calls if c.get("name") == "onboard"]
    assert onboard_calls, f"expected onboard call in {calls!r}"
    return onboard_calls[0]["arguments"]


def test_post_stop_bare_anchor_mints_instead_of_resuming(tmp_path):
    """A leaked UNITARES_CLIENT_SESSION_ID is inert without the orchestration marker."""
    calls = _run_hook_with_mock_server(
        "post-stop",
        tmp_path,
        "turn-slot-bare-anchor",
        extra_env={"UNITARES_CLIENT_SESSION_ID": "agent:/leaked-global-anchor"},
        return_all=True,
    )

    args = _onboard_args(calls)
    assert args["force_new"] is True
    assert "client_session_id" not in args
    assert "orchestrated" not in args
    assert "#" in args["name"]


def test_post_stop_orchestrated_anchor_resumes(tmp_path):
    """Headless orchestrated turn-children opt into one identity per anchor."""
    calls = _run_hook_with_mock_server(
        "post-stop",
        tmp_path,
        "turn-slot-orchestrated-anchor",
        extra_env={
            "UNITARES_CLIENT_SESSION_ID": "agent:/thread-123",
            "UNITARES_ORCHESTRATED": "yes",
        },
        return_all=True,
    )

    args = _onboard_args(calls)
    assert args["client_session_id"] == "agent:/thread-123"
    assert args["orchestrated"] is True
    assert "force_new" not in args
    assert "parent_agent_id" not in args
    assert "#" not in args["name"]


def test_session_end_does_not_duplicate_stop_checkin(tmp_path):
    slot = "real-slot-5678"
    _seed_slotted_cache(tmp_path, slot, {
        "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
        "client_session_id": "agent-real-5678",
        "continuity_token": "v1.real-tok",
        "slot": slot,
    })
    checkins = _run_hook_with_mock_server("session-end", tmp_path, slot)
    assert checkins == []


def test_post_edit_reads_slotted_cache(tmp_path):
    slot = "real-slot-9012"
    _seed_slotted_cache(tmp_path, slot, {
        "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
        "client_session_id": "agent-real-9012",
        "continuity_token": "v1.real-tok",
        "slot": slot,
        "last_checkin_ts": int(time.time()) - 10_000,
    })
    milestone = tmp_path / ".unitares" / "last-milestone.json"
    milestone.write_text(json.dumps({
        "edit_count": 10,
        "files_touched": ["a.py"],
        "last_edit_ts": int(time.time()),
    }))
    checkins = _run_hook_with_mock_server(
        "post-edit", tmp_path, slot,
        extra_stdin_fields={
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "a.py")},
        },
    )
    events = [c["arguments"]["metadata"]["event"] for c in checkins]
    assert "auto_edit" in events, (
        f"post-edit did not fire auto_edit when only the slotted cache existed; "
        f"events: {events}"
    )
