"""Contract test: Stop hook fires exactly one turn_stop check-in."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from _session_lookup import _slot_filename  # noqa: E402

# Reuse mock server + reusable TCP subclass from the session-start test file.
from tests.test_session_start_checkin import RecordingHandler, _ReusableTCPServer  # noqa: E402


class LazyOnboardHandler(RecordingHandler):
    """Mock governance server that returns a usable identity for onboard."""

    calls: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            data = {"raw": body.decode(errors="replace")}
        LazyOnboardHandler.calls.append(data)

        if data.get("name") == "onboard":
            response = {
                "result": {
                    "success": True,
                    "uuid": "11111111-2222-4333-8444-555555555555",
                    "agent_id": "Claude_20260615",
                    "client_session_id": "agent-11111111-222",
                    "continuity_token": "v1.transient-token",
                    "session_resolution_source": "force_new",
                    "continuity_token_supported": True,
                    "display_name": "claude-test#slot",
                }
            }
        else:
            response = {"result": {"success": True}}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


def test_post_stop_emits_turn_stop_checkin(tmp_path):
    """post-stop hook posts a check-in with event='turn_stop'."""
    RecordingHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), RecordingHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    # Pre-populate a slot-scoped session cache in $PWD/.unitares.
    session_dir = tmp_path / ".unitares"
    session_dir.mkdir()
    slot = "test-slot"
    (session_dir / _slot_filename(slot)).write_text(json.dumps({
        "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
        "client_session_id": "agent-test1234",
        "continuity_token": "v1.faketoken",
        "slot": slot,
    }))

    # Current Claude Stop payload: the tool count is not included.
    stop_payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": slot,
        "stop_hook_active": False,
        "last_assistant_message": "Completed the refactor; all tests pass.",
        "model": "claude-opus-4-1-20250805",
        "model_provider": "anthropic",
        "harness_version": "1.0.83",
    })

    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
            "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PWD": str(tmp_path),
        }
        hook = PLUGIN_ROOT / "hooks" / "post-stop"
        # cwd=tmp_path is REQUIRED: bash overwrites $PWD at startup to
        # match the actual working directory, so env PWD alone doesn't
        # tell the hook where to find the session cache.
        subprocess.run(
            [str(hook)],
            env=env,
            cwd=str(tmp_path),
            input=stop_payload,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)

    checkins = [
        c for c in RecordingHandler.calls
        if c.get("name") == "process_agent_update"
        and c["arguments"].get("metadata", {}).get("event") == "turn_stop"
    ]
    assert len(checkins) == 1, (
        f"expected exactly 1 turn_stop check-in; got {len(checkins)}: "
        f"{[c.get('name') for c in RecordingHandler.calls]}"
    )
    text = checkins[0]["arguments"]["response_text"]
    assert "tool count unavailable" in text
    assert "Completed the refactor" in text
    assert checkins[0]["arguments"]["epistemic_class"] == "substrate_interpretation"
    assert "continuity_token" not in checkins[0]["arguments"]
    runtime = checkins[0]["arguments"]["provenance_context"]["runtime_provenance"]
    assert runtime["model"]["identifier"] == "claude-opus-4-1-20250805"
    assert runtime["model"]["provider"] == "anthropic"
    assert runtime["model"]["source"] == "harness_reported"
    assert runtime["model"]["exact"] is True
    assert runtime["harness"]["type"] == "claude-code"
    assert runtime["harness"]["version"] == "1.0.83"


def test_post_stop_lazy_onboards_when_cache_missing(tmp_path):
    """An un-onboarded Claude turn should get a slot-scoped identity before
    post-stop emits turn_stop. This is the fix for floor-only sessions that
    never called start_session manually."""
    LazyOnboardHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), LazyOnboardHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    slot = "lazy-slot-1234"
    stop_payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": slot,
        "stop_hook_active": False,
        "last_assistant_message": "Inspected governance status.",
    })

    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
            "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
            "UNITARES_AUTO_ONBOARD": "on",
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PWD": str(tmp_path),
        }
        hook = PLUGIN_ROOT / "hooks" / "post-stop"
        subprocess.run(
            [str(hook)],
            env=env,
            cwd=str(tmp_path),
            input=stop_payload,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)

    tool_names = [c.get("name") for c in LazyOnboardHandler.calls]
    assert tool_names == ["onboard", "process_agent_update"]
    assert LazyOnboardHandler.calls[0]["arguments"]["model_type"] == "claude-code"
    assert (
        LazyOnboardHandler.calls[0]["arguments"]["onboard_origin"]
        == "harness_backstop"
    )

    checkin = LazyOnboardHandler.calls[1]["arguments"]
    assert checkin["client_session_id"] == "agent-11111111-222"
    assert checkin["metadata"]["event"] == "turn_stop"
    assert checkin["epistemic_class"] == "substrate_interpretation"
    assert "continuity_token" not in checkin

    cache_path = tmp_path / ".unitares" / _slot_filename(slot)
    cached = json.loads(cache_path.read_text())
    assert cached["uuid"] == "11111111-2222-4333-8444-555555555555"
    assert cached["client_session_id"] == "agent-11111111-222"
    assert "continuity_token" not in cached
