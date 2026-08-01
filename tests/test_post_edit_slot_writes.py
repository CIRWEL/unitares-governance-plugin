"""S20.1a regression tests — hooks/post-edit milestone-timestamp write
must land in the slot-scoped session cache, not flat session.json.

Pre-S20.1a, the `set session ... last_checkin_ts` invocation at the end of
post-edit ran without `--slot`, writing to flat session.json. The post-PR-19
session-start hook deliberately ignores flat session.json (KG bug
2026-04-20T00:09:51 — cross-instance UUID menu siphoning), so the auto-
checkin forcing function silently degraded: each post-edit fire stamped a
file the next session-start would not read, and the next post-edit fire
would re-fire the threshold immediately because last_checkin_ts looked
ancient.

Companion to test_post_edit_refactor.py (which pins the checkin.py routing)
and test_hooks_slot_aware.py (which pins the cache READ contract). This
file pins the cache WRITE contract.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from _session_lookup import _slot_filename  # noqa: E402

from tests.test_session_start_checkin import RecordingHandler, _ReusableTCPServer  # noqa: E402


class RejectedCheckinHandler(http.server.BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        self.__class__.calls.append(json.loads(body))
        response = {
            "success": True,
            "name": "process_agent_update",
            "result": {"success": False, "error": "rejected"},
        }
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass


class SlowAcceptedCheckinHandler(http.server.BaseHTTPRequestHandler):
    calls: list[dict] = []
    calls_lock = threading.Lock()

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        payload = json.loads(body)
        with self.__class__.calls_lock:
            self.__class__.calls.append(payload)
        if payload.get("name") == "process_agent_update":
            time.sleep(0.75)
        encoded = json.dumps({"result": {"success": True}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass


def _seed_slotted(workspace: Path, slot: str, payload: dict) -> Path:
    unitares = workspace / ".unitares"
    unitares.mkdir(exist_ok=True)
    path = unitares / _slot_filename(slot)
    path.write_text(json.dumps(payload))
    return path


def _run_post_edit(
    workspace: Path,
    stdin_payload: dict,
    timeout: float = 15.0,
    handler=RecordingHandler,
):
    handler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
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
        hook = PLUGIN_ROOT / "hooks" / "post-edit"
        return subprocess.run(
            [str(hook)],
            env=env,
            cwd=str(workspace),
            input=json.dumps(stdin_payload),
            text=True,
            timeout=timeout,
            check=False,
            capture_output=True,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)


def test_last_checkin_ts_lands_in_slotted_cache(tmp_path: Path) -> None:
    """Realistic Claude Code path: stdin carries session_id, slotted cache
    exists with stale last_checkin_ts, threshold met. Post-fix the new stamp
    must land in session-<slot>.json — not in flat session.json."""
    slot = "claude-session-write-1"
    slotted_path = _seed_slotted(tmp_path, slot, {
        "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
        "client_session_id": "agent-write-1",
        "continuity_token": "",  # S20.1b: v2 cache holds empty token (lineage hint, not credential)
        "slot": slot,
        "last_checkin_ts": int(time.time()) - 10_000,  # past threshold
    })
    (tmp_path / ".unitares" / "last-milestone.json").write_text(json.dumps({
        "edit_count": 10,  # past edit threshold
        "files_touched": ["a.py"],
        "last_edit_ts": int(time.time()),
        "first_edit_ts": int(time.time()) - 10_000,
    }))

    flat_path = tmp_path / ".unitares" / "session.json"
    assert not flat_path.exists()

    before_ts = int(time.time())
    result = _run_post_edit(tmp_path, {
        "hook_event_name": "PostToolUse",
        "session_id": slot,
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "a.py")},
    })
    assert result.returncode == 0, result.stderr

    # Slotted cache got the new stamp
    slotted_after = json.loads(slotted_path.read_text())
    assert "last_checkin_ts" in slotted_after
    assert int(slotted_after["last_checkin_ts"]) >= before_ts

    # Flat session.json was NOT created
    assert not flat_path.exists(), (
        "post-edit wrote flat session.json instead of slot-scoped target — "
        "S20.1a contract violated; session-start would ignore this stamp"
    )


def test_rejected_auto_checkin_preserves_milestone_and_timestamp(tmp_path: Path) -> None:
    slot = "claude-rejected-checkin"
    original_ts = int(time.time()) - 10_000
    slotted_path = _seed_slotted(
        tmp_path,
        slot,
        {
            "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
            "client_session_id": "agent-rejected",
            "continuity_token": "",
            "slot": slot,
            "last_checkin_ts": original_ts,
        },
    )
    milestone_path = tmp_path / ".unitares" / "last-milestone.json"
    milestone_path.write_text(
        json.dumps(
            {
                "edit_count": 10,
                "files_touched": ["a.py"],
                "last_edit_ts": int(time.time()),
                "first_edit_ts": original_ts,
            }
        )
    )

    result = _run_post_edit(
        tmp_path,
        {
            "hook_event_name": "PostToolUse",
            "session_id": slot,
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "a.py")},
        },
        handler=RejectedCheckinHandler,
    )

    assert result.returncode == 0, result.stderr
    assert len(RejectedCheckinHandler.calls) == 1
    assert json.loads(slotted_path.read_text())["last_checkin_ts"] == original_ts
    milestone = json.loads(milestone_path.read_text())
    assert milestone["edit_count"] == 11


def test_concurrent_post_edits_submit_only_one_auto_checkin(tmp_path: Path) -> None:
    slot = "claude-concurrent-checkin"
    _seed_slotted(
        tmp_path,
        slot,
        {
            "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
            "client_session_id": "agent-concurrent",
            "continuity_token": "",
            "slot": slot,
            "last_checkin_ts": int(time.time()),
        },
    )
    milestone_path = tmp_path / ".unitares" / "last-milestone.json"
    milestone_path.write_text(
        json.dumps(
            {
                "revision": 4,
                "edit_count": 4,
                "files_touched": ["seed.py"],
                "last_edit_ts": int(time.time()),
                "first_edit_ts": int(time.time()),
            }
        )
    )

    SlowAcceptedCheckinHandler.calls = []
    server = _ReusableTCPServer(("127.0.0.1", 0), SlowAcceptedCheckinHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    processes: list[subprocess.Popen[str]] = []
    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{server.server_address[1]}",
            "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
            "UNITARES_AUTO_CHECKIN_ENABLED": "1",
            "UNITARES_AUTO_CHECKIN_EDITS": "5",
            "UNITARES_AUTO_CHECKIN_SECS": "100000",
            "UNITARES_WATCHER_ENABLED": "0",
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PWD": str(tmp_path),
        }
        hook = PLUGIN_ROOT / "hooks" / "post-edit"
        for index in range(2):
            process = subprocess.Popen(
                [str(hook), "--host", "claude"],
                env=env,
                cwd=str(tmp_path),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert process.stdin is not None
            process.stdin.write(
                json.dumps(
                    {
                        "hook_event_name": "PostToolUse",
                        "session_id": slot,
                        "tool_name": "Edit",
                        "tool_use_id": f"toolu_{index}",
                        "tool_input": {
                            "file_path": str(tmp_path / f"edit-{index}.py")
                        },
                    }
                )
            )
            process.stdin.close()
            processes.append(process)

        failures = []
        for process in processes:
            returncode = process.wait(timeout=15)
            assert process.stdout is not None
            assert process.stderr is not None
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            if returncode != 0:
                failures.append((returncode, stdout, stderr))
        assert not failures
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        server.shutdown()
        thread.join(timeout=2)

    checkins = [
        call
        for call in SlowAcceptedCheckinHandler.calls
        if call.get("name") == "process_agent_update"
    ]
    assert len(checkins) == 1
    assert not (tmp_path / ".unitares" / "milestone-delivery-claim.json").exists()
    milestone = json.loads(milestone_path.read_text())
    assert milestone["revision"] >= 6


def test_workspace_local_watcher_is_not_executed_without_opt_in(tmp_path: Path) -> None:
    slot = "claude-untrusted-watcher"
    _seed_slotted(
        tmp_path,
        slot,
        {
            "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
            "client_session_id": "agent-watcher",
            "continuity_token": "",
            "slot": slot,
            "last_checkin_ts": int(time.time()),
        },
    )
    watcher = tmp_path / "agents" / "watcher" / "watcher-hook.sh"
    watcher.parent.mkdir(parents=True)
    marker = tmp_path / "watcher-ran"
    watcher.write_text(f"#!/usr/bin/env bash\ntouch '{marker}'\n", encoding="utf-8")
    watcher.chmod(0o755)

    result = _run_post_edit(
        tmp_path,
        {
            "hook_event_name": "PostToolUse",
            "session_id": slot,
            "tool_name": "Edit",
            "tool_use_id": "toolu_untrusted_watcher",
            "tool_input": {"file_path": str(tmp_path / "edit.py")},
        },
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_slotless_stdin_is_full_no_op_with_breadcrumb(tmp_path: Path) -> None:
    """Pre-S20.1a, slotless stdin would either collapse onto session-default.json
    (cmd_set with literal "default" slot) or fire checkin.py without an
    atomic last_checkin_ts stamp (causing threshold flapping). Post-fix:
    the entire pipeline is a full no-op — no checkin.py call, no
    milestone reset, no cache write — and a [SLOTLESS_SKIP] breadcrumb
    lands on stderr so the degraded state is legible to debuggers and
    not collapsed with "ran cleanly" or "crashed."
    """
    # Seed a legitimate slotted cache + a stale legacy flat cache (the
    # exact case where the eval-loaded SLOT could leak through pre-fix).
    real_slot = "claude-real-session"
    _seed_slotted(tmp_path, real_slot, {
        "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
        "client_session_id": "agent-real",
        "continuity_token": "",  # S20.1b: v2 cache holds empty token
        "slot": real_slot,
        "last_checkin_ts": int(time.time()) - 10_000,
    })
    flat_path = tmp_path / ".unitares" / "session.json"
    flat_path.write_text(json.dumps({
        "uuid": "leg-uuid",
        "client_session_id": "agent-leg",
        "continuity_token": "v1.leg",  # legacy on-disk shape — written directly, not via helper
        # No `slot` field — pre-S11 cache shape
        "last_checkin_ts": int(time.time()) - 10_000,
    }))
    flat_content_before = flat_path.read_text()

    (tmp_path / ".unitares" / "last-milestone.json").write_text(json.dumps({
        "edit_count": 10,
        "files_touched": ["a.py"],
        "last_edit_ts": int(time.time()),
        "first_edit_ts": int(time.time()) - 10_000,
    }))

    # Stdin without session_id — slotless path
    result = _run_post_edit(tmp_path, {
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "a.py")},
    })
    assert result.returncode == 0, result.stderr

    # Architect honesty signal: stderr breadcrumb on the slotless path
    assert "[SLOTLESS_SKIP]" in result.stderr, (
        "post-edit slotless path must emit [SLOTLESS_SKIP] stderr breadcrumb "
        f"so degraded state is legible; got stderr: {result.stderr!r}"
    )

    # No session-default.json was synthesized
    default_path = tmp_path / ".unitares" / "session-default.json"
    assert not default_path.exists()

    # No checkin was fired (full no-op prevents flapping)
    checkins = [c for c in RecordingHandler.calls if c.get("name") == "process_agent_update"]
    assert len(checkins) == 0, (
        f"slotless post-edit fired checkin.py — would cause threshold flapping "
        f"because last_checkin_ts cannot be stamped atomically; got {len(checkins)} calls"
    )

    # Flat cache was not modified by the milestone-timestamp write
    assert flat_path.read_text() == flat_content_before


def test_stale_cache_slot_field_does_not_leak_into_write_target(tmp_path: Path) -> None:
    """Code-reviewer Issue 2 (2026-04-26): pre-amendment, when stdin lacked
    session_id but a flat session.json existed with a `slot` field set to
    a real-looking value (not the literal "default"), the eval-supplied SLOT
    would carry that stale value through, and the hook would write
    last_checkin_ts to a slot-scoped file under the stale identity. Post-fix:
    SLOT is strictly stdin-derived; cache-stored slot fields are ignored as
    a write-target source."""
    stale_slot = "stale-prior-session-from-yesterday"
    flat_path = tmp_path / ".unitares" / "session.json"
    flat_path.parent.mkdir()
    flat_path.write_text(json.dumps({
        "uuid": "stale-uuid",
        "client_session_id": "agent-stale",
        "continuity_token": "v1.stale-tok",  # legacy on-disk shape — written directly, not via helper
        "slot": stale_slot,  # the leak vector
        "last_checkin_ts": int(time.time()) - 10_000,
    }))
    flat_content_before = flat_path.read_text()
    (tmp_path / ".unitares" / "last-milestone.json").write_text(json.dumps({
        "edit_count": 10,
        "files_touched": ["a.py"],
        "last_edit_ts": int(time.time()),
        "first_edit_ts": int(time.time()) - 10_000,
    }))

    # Stdin without session_id — but flat cache has a real-looking slot value
    result = _run_post_edit(tmp_path, {
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "a.py")},
    })
    assert result.returncode == 0, result.stderr

    # The stale slot must not have been used as a write target
    leaked_path = tmp_path / ".unitares" / f"session-{stale_slot}.json"
    assert not leaked_path.exists(), (
        f"post-edit wrote to session-{stale_slot}.json using a stale slot "
        f"from the legacy flat cache — Issue 2 (code-reviewer 2026-04-26) "
        f"regression; SLOT must be strictly stdin-derived"
    )

    # And the flat cache must not have been modified either
    assert flat_path.read_text() == flat_content_before, (
        "post-edit modified flat session.json — strict-stdin SLOT path "
        "must not write to flat cache regardless of its `slot` field value"
    )


def test_stdin_session_id_overrides_stale_cache_slot(tmp_path: Path) -> None:
    """When stdin session_id and cache `slot` field disagree, the in-flight
    stdin value wins — it is the authoritative slot for the current turn,
    while the cache field is a stored echo of an earlier write."""
    cache_slot = "stale-cache-slot"
    live_slot = "live-stdin-slot"

    # Seed two slotted caches: one stale (with cache_slot), one fresh (with
    # live_slot). The fresh one is what the live session would read.
    _seed_slotted(tmp_path, cache_slot, {
        "uuid": "stale-uuid",
        "client_session_id": "agent-stale",
        "continuity_token": "",  # S20.1b: v2 cache shape; merge target stays clean
        "slot": cache_slot,
        "last_checkin_ts": int(time.time()) - 10_000,
    })
    live_path = _seed_slotted(tmp_path, live_slot, {
        "uuid": "live-uuid",
        "client_session_id": "agent-live",
        "continuity_token": "",  # S20.1b: v2 cache shape; merge target stays clean
        "slot": live_slot,
        "last_checkin_ts": int(time.time()) - 10_000,
    })
    (tmp_path / ".unitares" / "last-milestone.json").write_text(json.dumps({
        "edit_count": 10,
        "files_touched": ["a.py"],
        "last_edit_ts": int(time.time()),
        "first_edit_ts": int(time.time()) - 10_000,
    }))

    before_ts = int(time.time())
    result = _run_post_edit(tmp_path, {
        "hook_event_name": "PostToolUse",
        "session_id": live_slot,  # stdin canonical
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "a.py")},
    })
    assert result.returncode == 0, result.stderr

    # The stamp lands in the live slot (matches stdin), regardless of which
    # slot the eval-loaded cache reports.
    live_after = json.loads(live_path.read_text())
    assert int(live_after["last_checkin_ts"]) >= before_ts
