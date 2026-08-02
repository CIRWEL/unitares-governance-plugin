"""Tests for the detached, bounded Codex runtime observer."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import activity_observer  # noqa: E402
import runtime_observer  # noqa: E402


AGENT_UUID = "86ae619f-87e0-4040-8f29-eacece0c7904"
SESSION_ID = "agent-86ae619f-87e"
SLOT = "runtime-slot"


def _payload() -> str:
    return json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "session_id": SLOT,
            "tool_name": "exec_command",
        }
    )


def _seed_session(workspace: Path) -> None:
    root = workspace / ".unitares"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"session-{SLOT}.json").write_text(
        json.dumps(
            {
                "uuid": AGENT_UUID,
                "client_session_id": SESSION_ID,
                "server_url": "http://governance.test",
                "slot": SLOT,
            }
        ),
        encoding="utf-8",
    )


def _seed_activity(home: Path, count: int = 25) -> Path:
    for offset in range(count):
        assert (
            activity_observer.record_activity(_payload(), home=home, now=100.0 + offset)
            == "recorded"
        )
    path = activity_observer.activity_state_path(home, SLOT)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["worker_started_at"] = 100.0
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def test_activity_rollup_is_bounded_audit_only(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITARES_CODEX_ROLLUP_TOOLS", "25")
    _seed_session(tmp_path)
    state_path = _seed_activity(tmp_path)
    runtime_calls = []

    def runtime_sender(url, payload):
        runtime_calls.append((url, payload))
        return True

    result = runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=700.0,
        runtime_sender=runtime_sender,
    )

    assert result["activity_observation"] == "sent"
    assert "activity_checkin" not in result
    assert len(runtime_calls) == 1
    runtime_payload = runtime_calls[0][1]
    assert runtime_payload["observation_kind"] == "activity_rollup"
    assert runtime_payload["tool_delta"] == 25
    assert runtime_payload["execution_mode"] == "unknown"
    assert runtime_payload["execution_mode_source"] == "unspecified"
    assert runtime_payload["model"] == ""
    assert "host_process_alive" not in runtime_payload
    assert "response_text" not in runtime_payload

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_rollup_count"] == 25
    second = runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=701.0,
        runtime_sender=runtime_sender,
    )
    assert second == {"status": "idle"}


def test_activity_rollup_preserves_explicit_execution_provenance(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITARES_CODEX_ROLLUP_TOOLS", "25")
    _seed_session(tmp_path)
    state_path = _seed_activity(tmp_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "execution_mode": "automation",
            "execution_mode_source": "explicit_env",
            "model": "gpt-5.4",
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    runtime_calls = []

    result = runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=700.0,
        runtime_sender=lambda url, payload: runtime_calls.append(payload) or True,
    )

    assert result["activity_observation"] == "sent"
    assert runtime_calls[0]["execution_mode"] == "automation"
    assert runtime_calls[0]["execution_mode_source"] == "explicit_env"
    assert runtime_calls[0]["model"] == "gpt-5.4"


def test_failed_activity_observation_remains_pending(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITARES_CODEX_ROLLUP_TOOLS", "25")
    _seed_session(tmp_path)
    state_path = _seed_activity(tmp_path)

    assert (
        runtime_observer.observation_cycle(
            state_path,
            workspace=tmp_path,
            slot=SLOT,
            now=700.0,
            runtime_sender=lambda url, payload: False,
        )["activity_observation"]
        == "failed"
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state.get("last_rollup_count", 0) == 0
    assert state["last_rollup_attempt_at"] == 700.0


def test_heartbeat_is_audit_only(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITARES_CODEX_HEARTBEAT_SECS", "300")
    _seed_session(tmp_path)
    state_path = _seed_activity(tmp_path, count=1)
    runtime_calls = []

    result = runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=400.0,
        runtime_sender=lambda url, payload: runtime_calls.append(payload) or True,
    )

    assert result == {"heartbeat": "sent", "status": "processed"}
    assert runtime_calls[0]["observation_kind"] == "heartbeat"
    assert runtime_calls[0]["host_process_alive"] is True
    assert runtime_calls[0]["seconds_since_last_tool"] == 300.0
    assert "response_text" not in runtime_calls[0]


def test_execution_context_is_explicit_and_model_is_descriptive(monkeypatch):
    payload = json.dumps(
        {
            "hook_event_name": "SessionStart",
            "session_id": SLOT,
            "model": "gpt-5.4",
            "permission_mode": "bypassPermissions",
        }
    )
    assert runtime_observer._execution_context(payload) == (
        "unknown",
        "unspecified",
        "gpt-5.4",
    )

    monkeypatch.setenv("UNITARES_CODEX_EXECUTION_MODE", "automation")
    assert runtime_observer._execution_context(payload) == (
        "automation",
        "explicit_env",
        "gpt-5.4",
    )


def test_future_explicit_hook_mode_is_preserved(monkeypatch):
    monkeypatch.delenv("UNITARES_CODEX_EXECUTION_MODE", raising=False)
    payload = json.dumps(
        {
            "hook_event_name": "SessionStart",
            "execution_mode": "ephemeral",
            "model": "gpt-5.6-terra",
        }
    )
    assert runtime_observer._execution_context(payload) == (
        "ephemeral",
        "hook_payload",
        "gpt-5.6-terra",
    )


def test_idle_cycle_does_not_load_identity(monkeypatch, tmp_path):
    state_path = _seed_activity(tmp_path, count=1)
    monkeypatch.setattr(
        runtime_observer,
        "_load_session",
        lambda workspace, slot: (_ for _ in ()).throw(AssertionError("loaded")),
    )

    assert runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=101.0,
    ) == {"status": "idle"}


def test_worker_cycle_stops_when_pid_is_superseded(tmp_path):
    state_path = _seed_activity(tmp_path, count=1)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["worker_pid"] = 222
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=101.0,
        expected_worker_pid=333,
    ) == {"status": "stopped"}


def test_worker_waits_for_identity_without_network(tmp_path):
    state_path = _seed_activity(tmp_path, count=25)
    calls = []
    assert runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=700.0,
        runtime_sender=lambda url, payload: calls.append(payload) or True,
    ) == {"status": "waiting_identity"}
    assert calls == []


def test_runtime_post_targets_dedicated_sink(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"success": true}'

    def open_request(request, *, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(runtime_observer, "authorization_safe_urlopen", open_request)
    assert runtime_observer._post_runtime(
        "https://governance.test/",
        {"observation_kind": "heartbeat"},
        timeout=2.0,
    )
    assert seen["url"] == "https://governance.test/v1/runtime/observe"
    assert seen["body"] == {"observation_kind": "heartbeat"}
    assert seen["timeout"] == 2.0


def test_heartbeat_id_is_stable_when_tool_count_changes():
    first = runtime_observer._event_id(
        agent_uuid=AGENT_UUID,
        session_id=SESSION_ID,
        kind="heartbeat",
        observed_at="2026-08-02T09:00:00Z",
        tool_count=41,
    )
    retry = runtime_observer._event_id(
        agent_uuid=AGENT_UUID,
        session_id=SESSION_ID,
        kind="heartbeat",
        observed_at="2026-08-02T09:00:00Z",
        tool_count=42,
    )
    assert first == retry


def test_worker_start_is_singleton_and_stop_is_pid_guarded(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITARES_CODEX_RUNTIME_OBSERVATIONS", "on")
    monkeypatch.setenv("UNITARES_CHECKINS", "on")
    alive = {111}
    kills = []

    monkeypatch.setattr(
        runtime_observer,
        "_process_alive",
        lambda pid, token="": pid in alive,
    )
    monkeypatch.setattr(
        runtime_observer,
        "_process_start_token",
        lambda pid: f"token-{pid}" if pid else "",
    )

    class Popen:
        def __init__(self, *args, **kwargs):
            self.pid = 222
            alive.add(self.pid)

    monkeypatch.setattr(runtime_observer.subprocess, "Popen", Popen)
    monkeypatch.setattr(
        runtime_observer.os,
        "kill",
        lambda pid, sig: kills.append((pid, sig)) or alive.discard(pid),
    )

    assert (
        runtime_observer.ensure_runtime_worker(
            _payload(), workspace=tmp_path, host_pid=111, home=tmp_path
        )
        == "started"
    )
    assert (
        runtime_observer.ensure_runtime_worker(
            _payload(), workspace=tmp_path, host_pid=111, home=tmp_path
        )
        == "already_running"
    )
    assert (
        runtime_observer.stop_runtime_worker(_payload(), home=tmp_path, now=500.0)
        == "stopped"
    )
    assert kills and kills[0][0] == 222
    state_path = activity_observer.activity_state_path(tmp_path, SLOT)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "worker_pid" not in state
    assert state["stop_requested_at"] == 500.0
    assert state["execution_mode"] == "unknown"
    assert state["execution_mode_source"] == "unspecified"


def test_worker_singleton_uses_bounded_token_rechecks(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITARES_CODEX_RUNTIME_OBSERVATIONS", "on")
    monkeypatch.setenv("UNITARES_CHECKINS", "on")
    checks = []
    token_reads = []
    now = 1_000.0

    monkeypatch.setattr(runtime_observer.time, "time", lambda: now)
    monkeypatch.setattr(
        runtime_observer,
        "_process_alive",
        lambda pid, token="": checks.append((pid, token)) or pid in {111, 222},
    )
    monkeypatch.setattr(
        runtime_observer,
        "_process_start_token",
        lambda pid: token_reads.append(pid) or (f"token-{pid}" if pid else ""),
    )

    class Popen:
        def __init__(self, *args, **kwargs):
            self.pid = 222

    monkeypatch.setattr(runtime_observer.subprocess, "Popen", Popen)

    assert (
        runtime_observer.ensure_runtime_worker(
            _payload(), workspace=tmp_path, host_pid=111, home=tmp_path
        )
        == "started"
    )
    checks.clear()
    token_reads.clear()
    assert (
        runtime_observer.ensure_runtime_worker(
            _payload(), workspace=tmp_path, host_pid=111, home=tmp_path
        )
        == "already_running"
    )
    assert (222, "token-222") not in checks
    assert token_reads == []

    now += runtime_observer.DEFAULT_TOKEN_RECHECK_S
    checks.clear()
    assert (
        runtime_observer.ensure_runtime_worker(
            _payload(), workspace=tmp_path, host_pid=111, home=tmp_path
        )
        == "already_running"
    )
    assert (222, "token-222") in checks
    assert token_reads == []
