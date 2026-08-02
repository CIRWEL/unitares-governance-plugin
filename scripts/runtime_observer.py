#!/usr/bin/env python3
"""Bounded Codex runtime observation and activity-rollup scheduler.

One detached worker is kept per Codex slot while the host process is alive.
The worker has two deliberately different outputs:

* ``heartbeat`` and ``activity_rollup`` runtime observations go to
  ``/v1/runtime/observe`` and are stored as identity-bound audit evidence.
* a bounded activity rollup may also call ``process_agent_update`` with
  ``epistemic_class=substrate_interpretation``.

Neither path claims semantic progress. Heartbeats never create EISV state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _http_auth import authorization_safe_urlopen, governance_json_headers  # noqa: E402
from _session_lookup import resolve_session_file  # noqa: E402
from activity_observer import (  # noqa: E402
    _lock_timeout_s,
    _read_state,
    _state_lock,
    _write_state,
    activity_state_path,
    slot_from_activity_payload,
)
from checkin import _plugin_version, submit_checkin  # noqa: E402


DEFAULT_SERVER_URL = "http://localhost:8767"
DEFAULT_POLL_S = 30.0
DEFAULT_HEARTBEAT_S = 1800.0
DEFAULT_ROLLUP_S = 1800.0
DEFAULT_ROLLUP_TOOLS = 25
DEFAULT_COOLDOWN_S = 600.0
DEFAULT_HTTP_TIMEOUT_S = 5.0
DEFAULT_CHECKIN_TIMEOUT_S = 8.0
_EVENT_NAMESPACE = uuid.UUID("63a3c5f5-e15e-4ac8-ae3a-8adf3cfecc91")


def _truthy(raw: str | None, *, default: bool = True) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "off", "false", "no"}


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def runtime_enabled() -> bool:
    return (
        _truthy(os.environ.get("UNITARES_CODEX_LIVENESS"), default=True)
        and _truthy(
            os.environ.get("UNITARES_CODEX_RUNTIME_OBSERVATIONS"), default=True
        )
        and os.environ.get("UNITARES_CHECKINS", "on").strip().lower() != "off"
    )


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slot_hash(slot: str) -> str:
    return hashlib.sha256(slot.encode("utf-8")).hexdigest()[:32]


def _process_start_token(pid: int) -> str:
    """Best-effort PID reuse guard, portable across Linux/macOS/Windows."""
    if pid <= 0:
        return ""
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        fields = proc_stat.read_text(encoding="utf-8").split()
        if len(fields) > 21:
            return f"proc:{fields[21]}"
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
        value = result.stdout.strip()
        return f"ps:{value}" if value else ""
    except Exception:
        return ""


def _process_alive(pid: int, start_token: str = "") -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    if start_token:
        current = _process_start_token(pid)
        if current and current != start_token:
            return False
    return True


def _load_session(workspace: Path, slot: str) -> dict[str, Any]:
    path = resolve_session_file(workspace, slot)
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(value, dict):
        return {}
    if not value.get("uuid") or not value.get("client_session_id"):
        return {}
    return value


def _event_id(
    *, agent_uuid: str, session_id: str, kind: str, observed_at: str, tool_count: int
) -> str:
    parts = [agent_uuid, session_id, kind, observed_at]
    if kind == "activity_rollup":
        parts.append(str(tool_count))
    key = "|".join(parts)
    return str(uuid.uuid5(_EVENT_NAMESPACE, key))


def _post_runtime(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
) -> bool:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/v1/runtime/observe",
        data=json.dumps(payload).encode("utf-8"),
        headers=governance_json_headers(),
        method="POST",
    )
    try:
        with authorization_safe_urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        return bool(body.get("success"))
    except (urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return False


def _runtime_payload(
    *,
    session: dict[str, Any],
    slot: str,
    kind: str,
    observed_at: str,
    tool_count: int,
    tool_delta: int,
    window_seconds: float,
    seconds_since_last_tool: float | None,
) -> dict[str, Any]:
    payload = {
        "agent_uuid": session["uuid"],
        "client_session_id": session["client_session_id"],
        "observation_kind": kind,
        "host_family": "codex",
        "slot_hash": _slot_hash(slot),
        "observed_at": observed_at,
        "tool_count": max(0, tool_count),
        "tool_delta": max(0, tool_delta),
        "window_seconds": max(0.0, window_seconds),
        "plugin_version": _plugin_version(),
    }
    if seconds_since_last_tool is not None:
        payload["seconds_since_last_tool"] = max(0.0, seconds_since_last_tool)
    if kind == "heartbeat":
        payload["host_process_alive"] = True
    payload["event_id"] = _event_id(
        agent_uuid=session["uuid"],
        session_id=session["client_session_id"],
        kind=kind,
        observed_at=observed_at,
        tool_count=tool_count,
    )
    return payload


def due_actions(state: dict[str, Any], *, now: float) -> tuple[bool, bool]:
    """Return ``(activity_rollup_due, heartbeat_due)`` for one ledger."""
    tool_count = max(0, int(state.get("tool_count") or 0))
    rolled_count = max(0, int(state.get("last_rollup_count") or 0))
    tool_delta = max(0, tool_count - rolled_count)
    first_activity = float(state.get("first_activity_at") or now)
    last_rollup = float(state.get("last_rollup_at") or first_activity)
    last_attempt = float(state.get("last_rollup_attempt_at") or 0.0)
    cooldown = _bounded_float(
        "UNITARES_CODEX_ROLLUP_COOLDOWN_S", DEFAULT_COOLDOWN_S, 60.0, 3600.0
    )
    by_tools = tool_delta >= _bounded_int(
        "UNITARES_CODEX_ROLLUP_TOOLS", DEFAULT_ROLLUP_TOOLS, 5, 1000
    )
    by_time = tool_delta > 0 and now - last_rollup >= _bounded_float(
        "UNITARES_CODEX_ROLLUP_SECS", DEFAULT_ROLLUP_S, 300.0, 21600.0
    )
    rollup_due = (by_tools or by_time) and now - last_attempt >= cooldown

    worker_started = float(state.get("worker_started_at") or now)
    last_heartbeat = float(state.get("last_heartbeat_at") or worker_started)
    heartbeat_due = now - last_heartbeat >= _bounded_float(
        "UNITARES_CODEX_HEARTBEAT_SECS", DEFAULT_HEARTBEAT_S, 300.0, 21600.0
    )
    heartbeat_attempt = float(state.get("last_heartbeat_attempt_at") or 0.0)
    heartbeat_due = heartbeat_due and now - heartbeat_attempt >= min(cooldown, 300.0)
    return rollup_due, heartbeat_due


def observation_cycle(
    state_path: Path,
    *,
    workspace: Path,
    slot: str,
    now: float | None = None,
    runtime_sender: Callable[[str, dict[str, Any]], bool] | None = None,
    checkin_sender: Callable[..., str] | None = None,
) -> dict[str, str]:
    """Execute one bounded worker cycle; exposed for deterministic tests."""
    timestamp = time.time() if now is None else float(now)
    runtime_sender = runtime_sender or (
        lambda url, payload: _post_runtime(
            url,
            payload,
            timeout=_bounded_float(
                "UNITARES_CODEX_RUNTIME_HTTP_TIMEOUT_S",
                DEFAULT_HTTP_TIMEOUT_S,
                0.2,
                15.0,
            ),
        )
    )
    checkin_sender = checkin_sender or submit_checkin

    with _state_lock(state_path, timeout_s=_lock_timeout_s()):
        state = _read_state(state_path)
    session = _load_session(workspace, slot)
    if not session:
        return {"status": "waiting_identity"}

    rollup_due, heartbeat_due = due_actions(state, now=timestamp)
    if not rollup_due and not heartbeat_due:
        return {"status": "idle"}

    server_url = str(
        session.get("server_url")
        or os.environ.get("UNITARES_SERVER_URL")
        or DEFAULT_SERVER_URL
    )
    tool_count = max(0, int(state.get("tool_count") or 0))
    outcomes: dict[str, str] = {}

    if heartbeat_due:
        interval = _bounded_float(
            "UNITARES_CODEX_HEARTBEAT_SECS", DEFAULT_HEARTBEAT_S, 300.0, 21600.0
        )
        worker_started = float(state.get("worker_started_at") or timestamp)
        period = max(1, int((timestamp - worker_started) // interval))
        heartbeat_at = worker_started + period * interval
        observed_at = _iso(heartbeat_at)
        last_activity = state.get("last_activity_at")
        seconds_since_last_tool = (
            max(0.0, timestamp - float(last_activity))
            if last_activity is not None
            else None
        )
        payload = _runtime_payload(
            session=session,
            slot=slot,
            kind="heartbeat",
            observed_at=observed_at,
            tool_count=tool_count,
            tool_delta=0,
            window_seconds=interval,
            seconds_since_last_tool=seconds_since_last_tool,
        )
        ok = runtime_sender(server_url, payload)
        outcomes["heartbeat"] = "sent" if ok else "failed"
        with _state_lock(state_path, timeout_s=_lock_timeout_s()):
            current = _read_state(state_path)
            current["last_heartbeat_attempt_at"] = timestamp
            if ok:
                current["last_heartbeat_at"] = heartbeat_at
                current["last_heartbeat_at_iso"] = observed_at
                current["network_emission"] = "identity_bound_runtime_observation"
            _write_state(state_path, current)

    if rollup_due:
        rolled_count = max(0, int(state.get("last_rollup_count") or 0))
        tool_delta = max(0, tool_count - rolled_count)
        last_rollup = float(
            state.get("last_rollup_at") or state.get("first_activity_at") or timestamp
        )
        window_seconds = max(0.0, timestamp - last_rollup)
        observed_at = str(state.get("last_activity_at_iso") or _iso(timestamp))
        runtime_payload = _runtime_payload(
            session=session,
            slot=slot,
            kind="activity_rollup",
            observed_at=observed_at,
            tool_count=tool_count,
            tool_delta=tool_delta,
            window_seconds=window_seconds,
            seconds_since_last_tool=max(
                0.0, timestamp - float(state.get("last_activity_at") or timestamp)
            ),
        )
        runtime_ok = runtime_sender(server_url, runtime_payload)
        outcomes["activity_observation"] = "sent" if runtime_ok else "failed"

        minutes = max(1, round(window_seconds / 60.0))
        response_text = (
            "Host-derived Codex activity rollup: "
            f"{tool_delta} completed-tool receipts over about {minutes} minutes "
            f"({tool_count} total). No semantic progress, intent, or EISV is inferred."
        )
        checkin_status = checkin_sender(
            event="codex_activity_rollup",
            response_text=response_text,
            complexity=0.3,
            confidence=0.95,
            client_session_id=session["client_session_id"],
            slot=slot,
            uuid=session["uuid"],
            server_url=server_url,
            epistemic_class="substrate_interpretation",
            timeout=_bounded_float(
                "UNITARES_CODEX_ROLLUP_CHECKIN_TIMEOUT_S",
                DEFAULT_CHECKIN_TIMEOUT_S,
                0.5,
                15.0,
            ),
        )
        outcomes["activity_checkin"] = checkin_status
        with _state_lock(state_path, timeout_s=_lock_timeout_s()):
            current = _read_state(state_path)
            current["last_rollup_attempt_at"] = timestamp
            current["last_activity_observation_status"] = outcomes[
                "activity_observation"
            ]
            if checkin_status == "sent":
                current["last_rollup_at"] = timestamp
                current["last_rollup_at_iso"] = _iso(timestamp)
                current["last_rollup_count"] = tool_count
                current["network_emission"] = "bounded_activity_rollup"
            _write_state(state_path, current)

    outcomes.setdefault("status", "processed")
    return outcomes


def _worker_command(
    *, state_path: Path, workspace: Path, slot: str, host_pid: int, host_token: str
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--state-file",
        str(state_path),
        "--workspace",
        str(workspace),
        "--slot",
        slot,
        "--host-pid",
        str(host_pid),
        "--host-token",
        host_token,
    ]


def ensure_runtime_worker(
    payload_text: str,
    *,
    workspace: Path,
    host_pid: int,
    home: Path | None = None,
) -> str:
    """Start one detached worker for a Codex slot, if one is not live."""
    if not runtime_enabled():
        return "skip_disabled"
    slot = slot_from_activity_payload(payload_text)
    if not slot or host_pid <= 0 or not _process_alive(host_pid):
        return "skip_missing_host"
    state_path = activity_state_path(home or Path.home(), slot)
    host_token = _process_start_token(host_pid)
    now = time.time()

    with _state_lock(state_path, timeout_s=_lock_timeout_s()):
        state = _read_state(state_path)
        worker_pid = int(state.get("worker_pid") or 0)
        worker_token = str(state.get("worker_start_token") or "")
        if _process_alive(worker_pid, worker_token):
            return "already_running"
        state.update(
            {
                "schema_version": 2,
                "slot": slot[:256],
                "host_pid": host_pid,
                "host_start_token": host_token,
                "worker_started_at": now,
                "worker_started_at_iso": _iso(now),
            }
        )
        state.pop("stop_requested_at", None)
        _write_state(state_path, state)

        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            # Do not leave a long-lived repo-rooted process. The worker has an
            # explicit workspace argument for cache lookup and needs no cwd.
            "cwd": str(Path.home()),
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        worker = subprocess.Popen(
            _worker_command(
                state_path=state_path,
                workspace=workspace,
                slot=slot,
                host_pid=host_pid,
                host_token=host_token,
            ),
            **kwargs,
        )
        state["worker_pid"] = worker.pid
        state["worker_start_token"] = _process_start_token(worker.pid)
        _write_state(state_path, state)
    return "started"


def stop_runtime_worker(
    payload_text: str, *, home: Path | None = None, now: float | None = None
) -> str:
    slot = slot_from_activity_payload(payload_text)
    if not slot:
        return "skip_no_slot"
    state_path = activity_state_path(home or Path.home(), slot)
    if not state_path.exists():
        return "not_running"
    timestamp = time.time() if now is None else float(now)
    with _state_lock(state_path, timeout_s=_lock_timeout_s()):
        state = _read_state(state_path)
        worker_pid = int(state.get("worker_pid") or 0)
        worker_token = str(state.get("worker_start_token") or "")
        state["stop_requested_at"] = timestamp
        state["stop_requested_at_iso"] = _iso(timestamp)
        state.pop("worker_pid", None)
        state.pop("worker_start_token", None)
        _write_state(state_path, state)
    if _process_alive(worker_pid, worker_token):
        try:
            os.kill(worker_pid, signal.SIGTERM)
        except OSError:
            pass
    return "stopped"


def run_worker(
    *,
    state_path: Path,
    workspace: Path,
    slot: str,
    host_pid: int,
    host_token: str,
) -> int:
    poll_s = _bounded_float(
        "UNITARES_CODEX_RUNTIME_POLL_S", DEFAULT_POLL_S, 1.0, 300.0
    )
    worker_pid = os.getpid()
    while runtime_enabled() and _process_alive(host_pid, host_token):
        with _state_lock(state_path, timeout_s=_lock_timeout_s()):
            state = _read_state(state_path)
        if int(state.get("worker_pid") or 0) != worker_pid:
            break
        if state.get("stop_requested_at"):
            break
        try:
            observation_cycle(
                state_path,
                workspace=workspace,
                slot=slot,
            )
        except Exception:
            pass
        time.sleep(poll_s)
    return 0


def _payload_from_args(value: str | None) -> str:
    if value is not None:
        return value
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex runtime observation worker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--payload", default=None)
    start.add_argument("--workspace", default=os.getcwd())
    start.add_argument("--host-pid", type=int, required=True)

    stop = subparsers.add_parser("stop")
    stop.add_argument("--payload", default=None)

    run = subparsers.add_parser("run")
    run.add_argument("--state-file", type=Path, required=True)
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--slot", required=True)
    run.add_argument("--host-pid", type=int, required=True)
    run.add_argument("--host-token", default="")

    args = parser.parse_args()
    if args.command == "start":
        ensure_runtime_worker(
            _payload_from_args(args.payload),
            workspace=Path(args.workspace),
            host_pid=args.host_pid,
        )
        return 0
    if args.command == "stop":
        stop_runtime_worker(_payload_from_args(args.payload))
        return 0
    return run_worker(
        state_path=args.state_file,
        workspace=args.workspace,
        slot=args.slot,
        host_pid=args.host_pid,
        host_token=args.host_token,
    )


if __name__ == "__main__":
    raise SystemExit(main())
