#!/usr/bin/env python3
"""Local Codex hook-liveness observer.

``PostToolUse`` proves only that the host delivered a completed-tool event for
one Codex slot. It does not prove agent intent, semantic progress, EISV state,
or even that the model is currently sampling. This helper therefore writes a
local slot-scoped ledger and performs no network delivery.

The ledger never stores a governance UUID, client session binding, tool input,
or tool response. Agent-authored state remains the responsibility of
``sync_state``. The identity-free ``/v1/substrate/observe`` endpoint is also
intentionally not used: that sink measures never-onboarded/dark sessions, not
liveness for an already bound process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _slot_from_stdin import slot_from_payload  # noqa: E402


SCHEMA_VERSION = 1
DEFAULT_LOCK_TIMEOUT_S = 1.0
ACTIVITY_DIRNAME = "codex-activity"


def _truthy(raw: str | None, *, default: bool = True) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "off", "false", "no"}


def _bounded_float(
    raw: str | float | int | None,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _activity_root(home: Path | None = None) -> Path:
    configured = os.environ.get("UNITARES_CODEX_ACTIVITY_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (home or Path.home()) / ".unitares" / ACTIVITY_DIRNAME


def activity_state_path(home: Path, slot: str) -> Path:
    """Return a collision-resistant path for an opaque raw host slot."""
    safe_slot = slot_from_payload(json.dumps({"session_id": slot})) or "slot"
    digest = hashlib.sha256(slot.encode("utf-8")).hexdigest()[:12]
    return _activity_root(home) / f"activity-{safe_slot}-{digest}.json"


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, data)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


@contextmanager
def _state_lock(path: Path, timeout_s: float) -> Iterator[None]:
    """Serialize ledger mutation on a stable sidecar inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    acquired = False
    deadline = time.monotonic() + timeout_s
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while not acquired:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"activity lock timed out: {lock_path}")
                    time.sleep(0.025)
        else:
            import fcntl

            while not acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"activity lock timed out: {lock_path}")
                    time.sleep(0.025)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _lock_timeout_s() -> float:
    return _bounded_float(
        os.environ.get("UNITARES_CODEX_ACTIVITY_LOCK_TIMEOUT_S"),
        DEFAULT_LOCK_TIMEOUT_S,
        minimum=0.05,
        maximum=4.0,
    )


def record_activity(
    payload_text: str,
    *,
    now: float | None = None,
    home: Path | None = None,
) -> str:
    """Record one completed Codex tool event without network or identity I/O."""
    if not _truthy(os.environ.get("UNITARES_CODEX_LIVENESS"), default=True):
        return "skip_disabled"
    try:
        payload = json.loads(payload_text)
    except Exception:
        return "skip_invalid_payload"
    if not isinstance(payload, dict):
        return "skip_invalid_payload"
    event_name = str(payload.get("hook_event_name") or "")
    if event_name and event_name != "PostToolUse":
        return "skip_wrong_event"

    raw_slot = payload.get("session_id")
    if isinstance(raw_slot, str) and raw_slot.strip():
        slot = raw_slot.strip()
    else:
        # Codex may expose only a conversation/transcript anchor on some
        # events. The shared helper hashes those values into an opaque slot.
        slot = slot_from_payload(payload_text)
    if not slot:
        return "skip_no_slot"

    timestamp = time.time() if now is None else float(now)
    path = activity_state_path(home or Path.home(), slot)
    # Keep the full raw value only in the path digest; the ledger needs no more
    # than its bounded opaque slot key.
    slot = slot[:256]
    try:
        with _state_lock(path, timeout_s=_lock_timeout_s()):
            state = _read_state(path)
            if state.get("slot") not in (None, slot):
                state = {}
            try:
                total = max(0, int(state.get("tool_count", 0))) + 1
            except (TypeError, ValueError):
                total = 1
            try:
                first_at = float(state.get("first_activity_at", timestamp))
            except (TypeError, ValueError):
                first_at = timestamp
            state.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "source": "codex_post_tool_use_hook",
                    "evidence_source": "hook_derived",
                    "measurement_scope": "host_event_receipt",
                    "network_emission": "none",
                    "slot": slot,
                    "first_activity_at": first_at,
                    "first_activity_at_iso": _iso(first_at),
                    "last_activity_at": timestamp,
                    "last_activity_at_iso": _iso(timestamp),
                    "tool_count": total,
                }
            )
            _write_state(path, state)
    except Exception:
        return "record_error"
    return "recorded"


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Record hook-derived Codex activity")
    parser.add_argument("--payload", default=None)
    args = parser.parse_args()
    payload = args.payload
    if payload is None and not sys.stdin.isatty():
        payload = sys.stdin.read()
    print(record_activity(payload or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
