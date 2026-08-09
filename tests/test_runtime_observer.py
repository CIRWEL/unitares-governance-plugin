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


def _recording_checkin(status: str = "sent"):
    """Return a (calls, sender) pair so no test reaches the network."""
    calls: list[dict] = []

    def sender(**kwargs):
        calls.append(kwargs)
        return status

    return calls, sender


def test_activity_rollup_stays_bounded_and_unauthored(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITARES_CODEX_ROLLUP_TOOLS", "25")
    _seed_session(tmp_path)
    state_path = _seed_activity(tmp_path)
    runtime_calls = []
    checkin_calls, checkin_sender = _recording_checkin()

    def runtime_sender(url, payload):
        runtime_calls.append((url, payload))
        return True

    result = runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=700.0,
        runtime_sender=runtime_sender,
        checkin_sender=checkin_sender,
    )

    assert result["activity_observation"] == "sent"
    assert len(runtime_calls) == 1
    runtime_payload = runtime_calls[0][1]
    assert runtime_payload["observation_kind"] == "activity_rollup"
    assert runtime_payload["tool_delta"] == 25
    assert runtime_payload["execution_mode"] == "unknown"
    assert runtime_payload["execution_mode_source"] == "unspecified"
    assert runtime_payload["model"] == ""
    assert runtime_payload["measurement_scope"] == "completed_tool_event_receipts"
    assert runtime_payload["session_activity_evidence"] is True
    assert runtime_payload["agent_runtime_evidence"] is False
    assert "host_process_alive" not in runtime_payload
    # The runtime observation itself stays a bare receipt — the narrative rides
    # on the separate check-in, never on the audit-plane payload.
    assert "response_text" not in runtime_payload

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_rollup_count"] == 25
    second = runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=701.0,
        runtime_sender=runtime_sender,
        checkin_sender=checkin_sender,
    )
    assert second == {"status": "idle"}
    # One window of work produces exactly one check-in, never one per cycle.
    assert len(checkin_calls) == 1


def test_worked_rollup_submits_substrate_interpretation_checkin(monkeypatch, tmp_path):
    """A window with completed tools becomes governed state, unauthored."""
    monkeypatch.setenv("UNITARES_CODEX_ROLLUP_TOOLS", "25")
    _seed_session(tmp_path)
    state_path = _seed_activity(tmp_path)
    checkin_calls, checkin_sender = _recording_checkin()

    result = runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=700.0,
        runtime_sender=lambda url, payload: True,
        checkin_sender=checkin_sender,
    )

    assert result["activity_checkin"] == "sent"
    assert len(checkin_calls) == 1
    call = checkin_calls[0]
    assert call["epistemic_class"] == "substrate_interpretation"
    assert call["event"] == "codex_activity_rollup"
    assert call["client_session_id"] == SESSION_ID
    assert call["uuid"] == AGENT_UUID
    assert call["complexity"] == runtime_observer.rollup_complexity(25)
    # A tool-completion receipt states no belief. Supplying a confidence would
    # mint a tactical prediction and score it into the calibration curve.
    assert call["confidence"] is None
    # Observed quantities only — no intent or progress language.
    assert "25 tool calls" in call["response_text"]

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_activity_checkin_status"] == "sent"
    assert state["last_activity_checkin_at"] == 700.0


def test_failed_observation_does_not_check_in(monkeypatch, tmp_path):
    """The ledger and the check-in share one commit point, so retries can't double-submit."""
    monkeypatch.setenv("UNITARES_CODEX_ROLLUP_TOOLS", "25")
    _seed_session(tmp_path)
    state_path = _seed_activity(tmp_path)
    checkin_calls, checkin_sender = _recording_checkin()

    result = runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=700.0,
        runtime_sender=lambda url, payload: False,
        checkin_sender=checkin_sender,
    )

    assert result["activity_observation"] == "failed"
    assert checkin_calls == []
    state = json.loads(state_path.read_text(encoding="utf-8"))
    # tool_delta is still pending, so the retry re-sends the same window once.
    assert state.get("last_rollup_count", 0) == 0


def test_heartbeat_alone_never_produces_a_checkin(monkeypatch, tmp_path):
    """Liveness is not proprioception: a PID existing says nothing about state.

    Heartbeats are opt-in since #111 (the hook-parent PID may be shared across
    Codex chats), so enable them explicitly here — the point of the test is that
    even a heartbeat that IS delivered produces no check-in.
    """
    monkeypatch.setenv("UNITARES_CODEX_HOST_HEARTBEATS", "1")
    monkeypatch.setenv("UNITARES_CODEX_HEARTBEAT_SECS", "300")
    _seed_session(tmp_path)
    state_path = _seed_activity(tmp_path, count=1)
    # All observed work is already rolled up, so the only thing still due is
    # the liveness beat.
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["last_rollup_count"] = state["tool_count"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    checkin_calls, checkin_sender = _recording_checkin()

    result = runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=100_000.0,
        runtime_sender=lambda url, payload: True,
        checkin_sender=checkin_sender,
    )

    assert result["heartbeat"] == "sent"
    assert "activity_checkin" not in result
    assert checkin_calls == []


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
    monkeypatch.setenv("UNITARES_CODEX_HOST_HEARTBEATS", "on")
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
    assert runtime_calls[0]["host_process_scope"] == "hook_parent"
    assert runtime_calls[0]["measurement_scope"] == "hook_parent_process_liveness"
    assert runtime_calls[0]["session_activity_evidence"] is False
    assert runtime_calls[0]["agent_runtime_evidence"] is False
    assert runtime_calls[0]["seconds_since_last_tool"] == 300.0
    assert "response_text" not in runtime_calls[0]


def test_hook_parent_heartbeat_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("UNITARES_CODEX_HOST_HEARTBEATS", raising=False)
    monkeypatch.setenv("UNITARES_CODEX_HEARTBEAT_SECS", "300")
    _seed_session(tmp_path)
    state_path = _seed_activity(tmp_path, count=1)
    calls = []

    assert runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=400.0,
        runtime_sender=lambda url, payload: calls.append(payload) or True,
    ) == {"status": "idle"}
    assert calls == []


def test_worker_idle_exit_is_bounded_by_last_completed_tool(monkeypatch):
    monkeypatch.setenv("UNITARES_CODEX_RUNTIME_IDLE_EXIT_S", "600")
    state = {"worker_started_at": 50.0, "last_activity_at": 100.0}

    assert runtime_observer._idle_exit_due(state, now=699.0) is False
    assert runtime_observer._idle_exit_due(state, now=700.0) is True


def test_worker_registration_cleanup_is_pid_guarded(tmp_path):
    state_path = _seed_activity(tmp_path, count=1)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "worker_pid": 222,
            "worker_start_token": "token-222",
            "worker_token_verified_at": 100.0,
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")

    runtime_observer._clear_worker_registration(state_path, 333)
    assert json.loads(state_path.read_text(encoding="utf-8"))["worker_pid"] == 222

    runtime_observer._clear_worker_registration(state_path, 222)
    cleaned = json.loads(state_path.read_text(encoding="utf-8"))
    assert "worker_pid" not in cleaned
    assert "worker_start_token" not in cleaned
    assert "worker_token_verified_at" not in cleaned


def test_worker_exits_and_unregisters_after_slot_idle_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITARES_CODEX_LIVENESS", "on")
    monkeypatch.setenv("UNITARES_CODEX_RUNTIME_OBSERVATIONS", "on")
    monkeypatch.setenv("UNITARES_CHECKINS", "on")
    monkeypatch.setenv("UNITARES_CODEX_RUNTIME_IDLE_EXIT_S", "600")
    state_path = _seed_activity(tmp_path, count=1)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({"worker_pid": 222, "worker_started_at": 100.0})
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(runtime_observer.os, "getpid", lambda: 222)
    monkeypatch.setattr(runtime_observer.time, "time", lambda: 700.0)
    monkeypatch.setattr(runtime_observer, "_process_alive", lambda pid, token="": True)
    monkeypatch.setattr(
        runtime_observer.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("worker slept")),
    )

    assert (
        runtime_observer.run_worker(
            state_path=state_path,
            workspace=tmp_path,
            slot=SLOT,
            host_pid=111,
            host_token="token-111",
        )
        == 0
    )
    assert "worker_pid" not in json.loads(state_path.read_text(encoding="utf-8"))


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
