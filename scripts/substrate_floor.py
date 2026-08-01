#!/usr/bin/env python3
"""Identity-free substrate FLOOR poster.

When a host session never onboarded (no client_session_id), the Stop hook
still emits one MEASUREMENT that the session ran — POSTed to the governance
server's ``/v1/substrate/observe`` endpoint. This is NOT a check-in and NOT an
identity claim: it writes to ``core.substrate_observations`` (an identity-free
sink) keyed on the raw host session slot. Its only job is to turn the silent
zero of an un-onboarded session into a counted, measurable coverage gap.

Deliberately separate from ``checkin.py``: the floor must never touch the
identity-bound check-in path (``process_agent_update``), which under strict
identity would reject it or, worse, mint a claimed identity.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    from ._http_auth import authorization_safe_urlopen, governance_json_headers
except ImportError:  # Executed directly from an installed plugin.
    from _http_auth import authorization_safe_urlopen, governance_json_headers

DEFAULT_SERVER_URL = "http://localhost:8767"
POST_TIMEOUT_SEC = 5.0
SLOT_MAX = 256
SUMMARY_MAX = 512

# Reuse plugin-local helpers when importable; degrade gracefully if not so the
# floor can never crash a turn on an import error.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from checkin import (  # type: ignore
        redact_secrets,
        _plugin_version,
        _is_killed,
        _append_log,
    )
except Exception:  # pragma: no cover - standalone fallback
    def redact_secrets(s: str) -> str:
        return s

    def _plugin_version() -> str:
        return ""

    def _is_killed() -> bool:
        return False

    def _append_log(**kwargs) -> None:
        pass


def _post(url: str, payload: dict, timeout: float = POST_TIMEOUT_SEC):
    """POST to the substrate-observe endpoint. Returns (ok, latency_ms, err)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/v1/substrate/observe",
        data=data,
        headers=governance_json_headers(),
    )
    t0 = time.monotonic()
    try:
        with authorization_safe_urlopen(req, timeout=timeout) as resp:
            resp.read()  # drain
        return True, int((time.monotonic() - t0) * 1000), None
    except urllib.error.URLError as e:
        return False, int((time.monotonic() - t0) * 1000), str(getattr(e, "reason", e))
    except Exception as e:
        return False, int((time.monotonic() - t0) * 1000), str(e)


def submit_floor(
    *,
    slot: str,
    event: str = "turn_stop",
    tool_count: int = 0,
    summary: str = "",
    server_url: str | None = None,
    plugin_version: str | None = None,
) -> str:
    """Emit one identity-free floor observation. Returns a status string."""
    slot = (slot or "").strip()
    if not slot:
        return "skip_no_slot"
    if _is_killed():
        _append_log(slot=slot, event="substrate_floor", uuid="", status="skip_kill_switch")
        return "skip_kill_switch"
    url = server_url or os.environ.get("UNITARES_SERVER_URL", DEFAULT_SERVER_URL)
    try:
        tc = max(0, int(tool_count))
    except (TypeError, ValueError):
        tc = 0
    payload = {
        "slot_key": slot[:SLOT_MAX],
        "event": str(event or "turn_stop"),
        "tool_count": tc,
        "summary_excerpt": redact_secrets(summary or "")[:SUMMARY_MAX],
        "plugin_version": plugin_version or _plugin_version(),
    }
    ok, latency_ms, err = _post(url, payload)
    status = "floor_sent" if ok else "floor_fail"
    _append_log(
        slot=slot,
        event=f"substrate_{event}",
        uuid="",
        status=status,
        latency_ms=latency_ms,
        error=err,
    )
    return status


def main() -> int:
    p = argparse.ArgumentParser(description="Identity-free substrate floor poster")
    p.add_argument("--slot", required=True, help="raw host session_id (the floor disambiguator)")
    p.add_argument("--event", default="turn_stop")
    p.add_argument("--tool-count", type=int, default=0)
    p.add_argument("--summary", default="")
    p.add_argument("--server-url", default=None)
    p.add_argument("--plugin-version", default=None)
    args = p.parse_args()
    print(
        submit_floor(
            slot=args.slot,
            event=args.event,
            tool_count=args.tool_count,
            summary=args.summary,
            server_url=args.server_url,
            plugin_version=args.plugin_version,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
