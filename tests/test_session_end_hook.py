"""Contract test: SessionEnd stays within Claude's local cleanup budget."""

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


def test_session_end_does_not_attempt_governance_delivery(tmp_path):
    RecordingHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), RecordingHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    session_dir = tmp_path / ".unitares"
    session_dir.mkdir()
    slot = "test-slot"
    (session_dir / _slot_filename(slot)).write_text(json.dumps({
        "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
        "client_session_id": "agent-test1234",
        "continuity_token": "v1.tok",
        "slot": slot,
    }))

    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
            "UNITARES_CHECKIN_LOG": str(tmp_path / "cl.log"),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PWD": str(tmp_path),
        }
        hook = PLUGIN_ROOT / "hooks" / "session-end"
        # cwd=tmp_path REQUIRED: bash overwrites $PWD at startup to match
        # actual cwd, so the hook needs to run with tmp_path as working dir
        # for workspace-local slotted session cache to be found.
        subprocess.run(
            [str(hook)],
            env=env,
            cwd=str(tmp_path),
            input=json.dumps({"session_id": slot}),
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)

    assert RecordingHandler.calls == []
