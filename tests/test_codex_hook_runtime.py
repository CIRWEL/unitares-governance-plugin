from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

from tests.test_post_stop_hook import LazyOnboardHandler
from tests.test_session_start_checkin import RecordingHandler, _ReusableTCPServer


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _env(workspace: Path, port: int) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(workspace),
        "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
        "UNITARES_CHECKIN_LOG": str(workspace / "checkins.log"),
        "UNITARES_FILE_LEASES_ENABLED": "0",
        "PLUGIN_ROOT": str(PLUGIN_ROOT),
        "PWD": str(workspace),
    }


def _seed_session(workspace: Path, slot: str) -> None:
    state = workspace / ".unitares"
    state.mkdir(exist_ok=True)
    (state / f"session-{slot}.json").write_text(
        json.dumps(
            {
                "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
                "client_session_id": "agent-codex-test",
                "slot": slot,
            }
        )
    )


def test_codex_post_edit_is_local_only_and_records_all_patch_paths(tmp_path: Path):
    RecordingHandler.calls = []
    server = _ReusableTCPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    slot = "codex-edit-slot"
    _seed_session(tmp_path, slot)
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": slot,
        "tool_name": "apply_patch",
        "tool_use_id": "call_multi",
        "tool_input": {
            "command": """*** Begin Patch
*** Update File: src/a.py
*** Add File: src/b.py
*** End Patch"""
        },
    }
    try:
        result = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-edit"), "--host", "codex"],
            cwd=str(tmp_path),
            env={**_env(tmp_path, server.server_address[1]), "UNITARES_AUTO_CHECKIN_ENABLED": "1"},
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.returncode == 0, result.stderr
    milestone = json.loads((tmp_path / ".unitares" / "last-milestone.json").read_text())
    assert milestone["edit_count"] == 1
    assert milestone["files_touched"] == ["src/a.py", "src/b.py"]
    assert not [call for call in RecordingHandler.calls if call.get("name") == "process_agent_update"]


def test_codex_first_turn_edit_records_milestone_without_identity_cache(tmp_path: Path):
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "codex-first-turn",
        "tool_name": "apply_patch",
        "tool_use_id": "call_first_edit",
        "tool_input": {
            "command": """*** Begin Patch
*** Update File: src/first.py
*** End Patch"""
        },
    }

    result = subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / "post-edit"), "--host", "codex"],
        cwd=str(tmp_path),
        env=_env(tmp_path, 1),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    milestone = json.loads((tmp_path / ".unitares" / "last-milestone.json").read_text())
    assert milestone["edit_count"] == 1
    assert milestone["files_touched"] == ["src/first.py"]
    assert not list((tmp_path / ".unitares").glob("session-*.json"))


def test_codex_explicit_failed_post_edit_does_not_record_milestone(tmp_path: Path):
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "codex-failed-edit",
        "tool_name": "apply_patch",
        "tool_use_id": "call_failed_edit",
        "tool_input": {
            "command": """*** Begin Patch
*** Update File: src/failed.py
*** End Patch"""
        },
        "tool_response": {"success": False, "error": "patch did not apply"},
    }

    result = subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / "post-edit"), "--host", "codex"],
        cwd=str(tmp_path),
        env=_env(tmp_path, 1),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".unitares" / "last-milestone.json").exists()


def test_codex_stop_cleans_snapshot_using_canonicalized_session_slot(tmp_path: Path):
    raw_slot = "codex/stop:slot with spaces"
    cache_slot = "codex_stop_slot_with_spaces"
    _seed_session(tmp_path, cache_slot)
    subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "session_cache.py"),
            "snapshot-milestone",
            "--workspace",
            str(tmp_path),
            "--event-id",
            "failed-codex-checkin-special-slot",
            "--slot",
            cache_slot,
        ],
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "codex"],
        cwd=str(tmp_path),
        env={**_env(tmp_path, 1), "UNITARES_CHECKINS": "off"},
        input=json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": raw_slot,
                "last_assistant_message": "Done.",
            }
        ),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}
    assert not list(
        (tmp_path / ".unitares" / "milestone-snapshots").glob("*.json")
    )


def test_codex_stop_uses_last_assistant_message_without_fake_tool_count(tmp_path: Path):
    RecordingHandler.calls = []
    server = _ReusableTCPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    slot = "codex-stop-slot"
    _seed_session(tmp_path, slot)
    subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "session_cache.py"),
            "snapshot-milestone",
            "--workspace",
            str(tmp_path),
            "--event-id",
            "failed-codex-checkin",
            "--slot",
            slot,
        ],
        check=True,
        capture_output=True,
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": slot,
        "turn_id": "turn_1",
        "stop_hook_active": False,
        "last_assistant_message": "Implemented the Codex host contract.",
    }
    try:
        result = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "codex"],
            cwd=str(tmp_path),
            env=_env(tmp_path, server.server_address[1]),
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}
    checkins = [call for call in RecordingHandler.calls if call.get("name") == "process_agent_update"]
    assert len(checkins) == 1
    summary = checkins[0]["arguments"]["response_text"]
    assert "tool count unavailable" in summary
    assert "Implemented the Codex host contract" in summary
    assert "0 tool calls" not in summary
    assert not list(
        (tmp_path / ".unitares" / "milestone-snapshots").glob("*.json")
    )


def test_codex_stop_emits_json_when_no_session_is_available(tmp_path: Path):
    payload = {
        "hook_event_name": "Stop",
        "session_id": "",
        "turn_id": "turn_without_session",
        "stop_hook_active": False,
        "last_assistant_message": None,
    }

    result = subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "codex"],
        cwd=str(tmp_path),
        env={**_env(tmp_path, 9), "UNITARES_AUTO_ONBOARD": "off"},
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}


def test_codex_lazy_onboarding_uses_codex_identity_defaults(tmp_path: Path):
    LazyOnboardHandler.calls = []
    server = _ReusableTCPServer(("127.0.0.1", 0), LazyOnboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = {
        "hook_event_name": "Stop",
        "session_id": "codex-lazy-slot",
        "turn_id": "turn_2",
        "stop_hook_active": False,
        "last_assistant_message": "Inspected governance status.",
    }
    try:
        result = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "codex"],
            cwd=str(tmp_path),
            env={**_env(tmp_path, server.server_address[1]), "UNITARES_AUTO_ONBOARD": "on"},
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}
    assert [call.get("name") for call in LazyOnboardHandler.calls] == [
        "onboard",
        "process_agent_update",
    ]
    onboard = LazyOnboardHandler.calls[0]["arguments"]
    assert onboard["name"].startswith(f"codex-{tmp_path.name}#")
    assert onboard["model_type"] == "codex"


def test_codex_session_end_does_not_duplicate_stop_checkin(tmp_path: Path):
    RecordingHandler.calls = []
    server = _ReusableTCPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    slot = "codex-end-slot"
    _seed_session(tmp_path, slot)
    try:
        result = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "session-end"), "--host", "codex"],
            cwd=str(tmp_path),
            env=_env(tmp_path, server.server_address[1]),
            input=json.dumps({"hook_event_name": "SessionEnd", "session_id": slot}),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.returncode == 0, result.stderr
    assert not [call for call in RecordingHandler.calls if call.get("name") == "process_agent_update"]
