"""Tests for the detached, bounded Codex runtime observer."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


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
        assert activity_observer.record_activity(
            _payload(), home=home, now=100.0 + offset
        ) == "recorded"
    path = activity_observer.activity_state_path(home, SLOT)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["worker_started_at"] = 100.0
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def test_activity_rollup_is_bounded_and_labeled(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITARES_CODEX_ROLLUP_TOOLS", "25")
    _seed_session(tmp_path)
    state_path = _seed_activity(tmp_path)
    runtime_calls = []
    checkins = []

    def runtime_sender(url, payload):
        runtime_calls.append((url, payload))
        return True

    def checkin_sender(**kwargs):
        checkins.append(kwargs)
        return "sent"

    result = runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=700.0,
        runtime_sender=runtime_sender,
        checkin_sender=checkin_sender,
    )

    assert result["activity_observation"] == "sent"
    assert result["activity_checkin"] == "sent"
    assert len(runtime_calls) == 1
    runtime_payload = runtime_calls[0][1]
    assert runtime_payload["observation_kind"] == "activity_rollup"
    assert runtime_payload["tool_delta"] == 25
    assert "host_process_alive" not in runtime_payload
    assert len(checkins) == 1
    assert checkins[0]["event"] == "codex_activity_rollup"
    assert checkins[0]["epistemic_class"] == "substrate_interpretation"
    assert "No semantic progress, intent, or EISV is inferred" in checkins[0][
        "response_text"
    ]

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
    assert len(checkins) == 1


def test_heartbeat_never_creates_checkin(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITARES_CODEX_HEARTBEAT_SECS", "300")
    _seed_session(tmp_path)
    state_path = _seed_activity(tmp_path, count=1)
    runtime_calls = []
    checkins = []

    result = runtime_observer.observation_cycle(
        state_path,
        workspace=tmp_path,
        slot=SLOT,
        now=400.0,
        runtime_sender=lambda url, payload: runtime_calls.append(payload) or True,
        checkin_sender=lambda **kwargs: checkins.append(kwargs) or "sent",
    )

    assert result == {"heartbeat": "sent", "status": "processed"}
    assert checkins == []
    assert runtime_calls[0]["observation_kind"] == "heartbeat"
    assert runtime_calls[0]["host_process_alive"] is True
    assert runtime_calls[0]["seconds_since_last_tool"] == 300.0
    assert "response_text" not in runtime_calls[0]


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

    assert runtime_observer.ensure_runtime_worker(
        _payload(), workspace=tmp_path, host_pid=111, home=tmp_path
    ) == "started"
    assert runtime_observer.ensure_runtime_worker(
        _payload(), workspace=tmp_path, host_pid=111, home=tmp_path
    ) == "already_running"
    assert runtime_observer.stop_runtime_worker(
        _payload(), home=tmp_path, now=500.0
    ) == "stopped"
    assert kills and kills[0][0] == 222
    state_path = activity_observer.activity_state_path(tmp_path, SLOT)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "worker_pid" not in state
    assert state["stop_requested_at"] == 500.0
