#!/usr/bin/env python3
"""Bounded Codex host-observation scheduler.

One detached worker may be started after a completed-tool hook for a Codex
slot. Activity rollups and optional hook-parent heartbeats go to the legacy
``/v1/runtime/observe`` route and are stored as identity-bound audit evidence.
Neither path proves continuous agent runtime.

The two kinds are not the same class of evidence, and they are routed
differently (the payload says so itself, via ``session_activity_evidence`` and
``agent_runtime_evidence``):

``heartbeat`` — hook-parent process liveness
    Reports that the *hook parent* PID exists. Codex desktop may share that PID
    across chats, so it is not evidence of an agent at all. Opt-in
    (``UNITARES_CODEX_HOST_HEARTBEATS``, default off). It never calls
    ``process_agent_update`` and never creates EISV state, matching the
    server-side invariant in ``src/runtime_observations.py``.

``activity_rollup`` carrying ``session_activity_evidence`` — completed-tool receipts
    A receipt that N tools completed in a bounded window. That is a behavioral
    observable of the same class Claude's Stop hook already submits as a
    ``substrate_interpretation`` check-in (turn shape -> complexity), so this
    path submits one too. Codex has no equivalent per-turn Stop cadence, so
    without it a Codex slot accumulates audit presence but no governed state —
    and the behavioral estimator returns nothing below three history entries.

    ``substrate_interpretation`` is explicitly NOT an agent-authored check-in,
    which is what ``runtime_observations.py`` forbids synthesizing. The
    distinction is the whole point: the substrate may describe what it observed;
    only the agent may speak in the agent's voice.

A rollup without ``session_activity_evidence`` (no completed tools) submits
nothing, so an idle or undelivered window can never be mistaken for a calm
agent.
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
DEFAULT_POLL_S = 300.0
DEFAULT_TOKEN_RECHECK_S = 300.0
DEFAULT_HEARTBEAT_S = 1800.0
DEFAULT_ROLLUP_S = 1800.0
DEFAULT_ROLLUP_TOOLS = 25
DEFAULT_COOLDOWN_S = 600.0
DEFAULT_HTTP_TIMEOUT_S = 5.0
DEFAULT_IDLE_EXIT_S = 3600.0
EXECUTION_MODES = {"interactive", "automation", "ephemeral", "unknown"}
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
        and _truthy(os.environ.get("UNITARES_CODEX_RUNTIME_OBSERVATIONS"), default=True)
        and os.environ.get("UNITARES_CHECKINS", "on").strip().lower() != "off"
    )


def host_heartbeats_enabled() -> bool:
    """Return whether explicitly requested hook-parent heartbeats are enabled."""
    return _truthy(os.environ.get("UNITARES_CODEX_HOST_HEARTBEATS"), default=False)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slot_hash(slot: str) -> str:
    return hashlib.sha256(slot.encode("utf-8")).hexdigest()[:32]


def _execution_context(payload_text: str) -> tuple[str, str, str]:
    """Return mode, provenance source, and model without guessing from names."""
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    explicit = (
        str(os.environ.get("UNITARES_CODEX_EXECUTION_MODE") or "").strip().lower()
    )
    if explicit in EXECUTION_MODES - {"unknown"}:
        mode, source = explicit, "explicit_env"
    else:
        hook_mode = str(payload.get("execution_mode") or "").strip().lower()
        if hook_mode in EXECUTION_MODES - {"unknown"}:
            mode, source = hook_mode, "hook_payload"
        else:
            mode, source = "unknown", "unspecified"
    return mode, source, str(payload.get("model") or "").strip()[:80]


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


def rollup_complexity(tool_delta: int) -> float:
    """Complexity for a rollup window, on Claude's Stop-hook scale.

    ``scripts/stop_hook_event.py`` derives a turn's complexity as
    ``min(tool_count / 10, 0.85)``. A rollup window is the same quantity
    measured over a bounded interval instead of a turn, so it uses the same
    curve — the two hosts stay comparable rather than each inventing a scale.
    """
    return min(max(0, int(tool_delta)) / 10.0, 0.85)


def rollup_summary(tool_delta: int, window_seconds: float) -> str:
    """Describe the window in observed quantities only.

    Deliberately free of intent or progress language: this is a tool-completion
    receipt, not a claim about what the agent was trying to do.
    """
    minutes = max(0.0, float(window_seconds)) / 60.0
    tools = max(0, int(tool_delta))
    plural = "" if tools == 1 else "s"
    return f"{tools} tool call{plural} completed over {minutes:.0f}m (codex runtime window)"


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
    execution_mode: str = "unknown",
    execution_mode_source: str = "unspecified",
    model: str = "",
) -> dict[str, Any]:
    payload = {
        "agent_uuid": session["uuid"],
        "client_session_id": session["client_session_id"],
        "observation_kind": kind,
        "host_family": "codex",
        "execution_mode": execution_mode,
        "execution_mode_source": execution_mode_source,
        "model": model[:80],
        "slot_hash": _slot_hash(slot),
        "observed_at": observed_at,
        "tool_count": max(0, tool_count),
        "tool_delta": max(0, tool_delta),
        "window_seconds": max(0.0, window_seconds),
        "plugin_version": _plugin_version(),
        "measurement_scope": (
            "hook_parent_process_liveness"
            if kind == "heartbeat"
            else "completed_tool_event_receipts"
        ),
        "session_activity_evidence": kind == "activity_rollup" and tool_delta > 0,
        "agent_runtime_evidence": False,
    }
    if seconds_since_last_tool is not None:
        payload["seconds_since_last_tool"] = max(0.0, seconds_since_last_tool)
    if kind == "heartbeat":
        payload["host_process_alive"] = True
        payload["host_process_scope"] = "hook_parent"
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
    heartbeat_due = (
        host_heartbeats_enabled()
        and now - last_heartbeat
        >= _bounded_float(
            "UNITARES_CODEX_HEARTBEAT_SECS", DEFAULT_HEARTBEAT_S, 300.0, 21600.0
        )
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
    expected_worker_pid: int | None = None,
) -> dict[str, str]:
    """Execute one bounded worker cycle; exposed for deterministic tests."""
    timestamp = time.time() if now is None else float(now)
    checkin_sender = checkin_sender or submit_checkin
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
    with _state_lock(state_path, timeout_s=_lock_timeout_s()):
        state = _read_state(state_path)
    if expected_worker_pid is not None and (
        int(state.get("worker_pid") or 0) != expected_worker_pid
        or state.get("stop_requested_at")
    ):
        return {"status": "stopped"}

    rollup_due, heartbeat_due = due_actions(state, now=timestamp)
    if not rollup_due and not heartbeat_due:
        return {"status": "idle"}

    session = _load_session(workspace, slot)
    if not session:
        return {"status": "waiting_identity"}

    server_url = str(
        session.get("server_url")
        or os.environ.get("UNITARES_SERVER_URL")
        or DEFAULT_SERVER_URL
    )
    tool_count = max(0, int(state.get("tool_count") or 0))
    execution_mode = str(state.get("execution_mode") or "unknown")
    execution_mode_source = str(state.get("execution_mode_source") or "unspecified")
    model = str(state.get("model") or "")
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
            execution_mode=execution_mode,
            execution_mode_source=execution_mode_source,
            model=model,
        )
        ok = runtime_sender(server_url, payload)
        outcomes["heartbeat"] = "sent" if ok else "failed"
        with _state_lock(state_path, timeout_s=_lock_timeout_s()):
            current = _read_state(state_path)
            current["last_heartbeat_attempt_at"] = timestamp
            if ok:
                current["last_heartbeat_at"] = heartbeat_at
                current["last_heartbeat_at_iso"] = observed_at
                current["network_emission"] = "identity_bound_host_observation"
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
            execution_mode=execution_mode,
            execution_mode_source=execution_mode_source,
            model=model,
        )
        runtime_ok = runtime_sender(server_url, runtime_payload)
        outcomes["activity_observation"] = "sent" if runtime_ok else "failed"

        # Submit the same window as a substrate_interpretation check-in so a
        # Codex slot accumulates governed state, not just audit presence.
        #
        # Gated on `runtime_ok` because that is the same commit point as the
        # ledger: `last_rollup_count` only advances when the observation lands,
        # so a failed send is retried with an identical `tool_delta`. Firing the
        # check-in on every attempt would submit the same work twice.
        #
        # Gated on the payload's own `session_activity_evidence` flag rather
        # than re-deriving `tool_delta > 0` here: that flag is what the
        # observation asserted on the wire, so the check-in cannot drift from
        # the receipt it claims to summarize. A window with no completed tools
        # is not evidence of a calm agent — it is the absence of evidence, and
        # the estimator must not read one as the other.
        checkin_status = "skipped_no_work"
        if runtime_ok and runtime_payload.get("session_activity_evidence"):
            checkin_status = checkin_sender(
                event="codex_activity_rollup",
                response_text=rollup_summary(tool_delta, window_seconds),
                complexity=rollup_complexity(tool_delta),
                # No confidence: a tool-completion receipt carries no belief.
                # Supplying one would mint a tactical prediction that no agent
                # made and score it into the fleet calibration curve.
                confidence=None,
                client_session_id=str(session.get("client_session_id") or ""),
                slot=slot,
                uuid=str(session.get("uuid") or ""),
                server_url=server_url,
                epistemic_class="substrate_interpretation",
                timeout=_bounded_float(
                    "UNITARES_CODEX_ROLLUP_CHECKIN_TIMEOUT_S", 10.0, 0.2, 20.0
                ),
            )
        outcomes["activity_checkin"] = checkin_status

        with _state_lock(state_path, timeout_s=_lock_timeout_s()):
            current = _read_state(state_path)
            current["last_rollup_attempt_at"] = timestamp
            current["last_activity_observation_status"] = outcomes[
                "activity_observation"
            ]
            current["last_activity_checkin_status"] = checkin_status
            if runtime_ok:
                current["last_rollup_at"] = timestamp
                current["last_rollup_at_iso"] = _iso(timestamp)
                current["last_rollup_count"] = tool_count
                current["network_emission"] = "identity_bound_host_observation"
            if checkin_status == "sent":
                current["last_activity_checkin_at"] = timestamp
                current["last_activity_checkin_at_iso"] = _iso(timestamp)
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
    now = time.time()
    execution_mode, execution_mode_source, model = _execution_context(payload_text)

    with _state_lock(state_path, timeout_s=_lock_timeout_s()):
        state = _read_state(state_path)
        worker_pid = int(state.get("worker_pid") or 0)
        worker_token = str(state.get("worker_start_token") or "")
        if _process_alive(worker_pid):
            last_verified = float(state.get("worker_token_verified_at") or 0.0)
            token_recheck_s = _bounded_float(
                "UNITARES_CODEX_RUNTIME_TOKEN_RECHECK_S",
                DEFAULT_TOKEN_RECHECK_S,
                30.0,
                3600.0,
            )
            if not worker_token or now - last_verified < token_recheck_s:
                return "already_running"
            if _process_alive(worker_pid, worker_token):
                state["worker_token_verified_at"] = now
                _write_state(state_path, state)
                return "already_running"
        host_token = _process_start_token(host_pid)
        state.update(
            {
                "schema_version": 2,
                "slot": slot[:256],
                "host_pid": host_pid,
                "host_start_token": host_token,
                "host_process_scope": "hook_parent",
                "execution_mode": execution_mode,
                "execution_mode_source": execution_mode_source,
                "model": model,
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
        state["worker_token_verified_at"] = now
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


def _idle_exit_due(state: dict[str, Any], *, now: float) -> bool:
    """Bound a slot worker even when its hook parent is a shared long-lived PID."""
    last_evidence = float(
        state.get("last_activity_at") or state.get("worker_started_at") or now
    )
    idle_exit_s = _bounded_float(
        "UNITARES_CODEX_RUNTIME_IDLE_EXIT_S",
        DEFAULT_IDLE_EXIT_S,
        600.0,
        86400.0,
    )
    return now - last_evidence >= idle_exit_s


def _clear_worker_registration(state_path: Path, worker_pid: int) -> None:
    """Clear only this worker's registration; never clobber a replacement."""
    try:
        with _state_lock(state_path, timeout_s=_lock_timeout_s()):
            state = _read_state(state_path)
            if int(state.get("worker_pid") or 0) != worker_pid:
                return
            state.pop("worker_pid", None)
            state.pop("worker_start_token", None)
            state.pop("worker_token_verified_at", None)
            _write_state(state_path, state)
    except Exception:
        pass


def run_worker(
    *,
    state_path: Path,
    workspace: Path,
    slot: str,
    host_pid: int,
    host_token: str,
) -> int:
    poll_s = _bounded_float("UNITARES_CODEX_RUNTIME_POLL_S", DEFAULT_POLL_S, 5.0, 300.0)
    worker_pid = os.getpid()
    try:
        while runtime_enabled() and _process_alive(host_pid, host_token):
            try:
                result = observation_cycle(
                    state_path,
                    workspace=workspace,
                    slot=slot,
                    expected_worker_pid=worker_pid,
                )
            except Exception:
                result = {"status": "failed"}
            if result.get("status") == "stopped":
                break
            with _state_lock(state_path, timeout_s=_lock_timeout_s()):
                state = _read_state(state_path)
            if _idle_exit_due(state, now=time.time()):
                break
            time.sleep(poll_s)
    finally:
        _clear_worker_registration(state_path, worker_pid)
    return 0


def _payload_from_args(value: str | None) -> str:
    if value is not None:
        return value
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex host-observation worker")
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
