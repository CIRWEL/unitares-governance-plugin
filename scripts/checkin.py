"""Shared check-in helper for UNITARES governance plugin hooks.

One entry point: ``submit_checkin``. Builds a ``process_agent_update``
REST payload, applies secret redaction, POSTs to the governance server,
and appends one diagnostic line to ``UNITARES_CHECKIN_LOG``.

Fire-and-forget semantics: never raises, always returns a status string
that callers may record. The kill switch ``UNITARES_CHECKINS=off``
short-circuits every call.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from _http_auth import authorization_safe_urlopen, governance_json_headers
from _redact import redact_secrets

DEFAULT_SERVER_URL = "http://localhost:8767"
DEFAULT_LOG_PATH = "~/.unitares/checkins.log"
DEFAULT_PLUGIN_VERSION = "0.4.14+codex.skills20260821"
# process_agent_update can take 5-10s under the anyio-asyncio mitigation
# paths in governance_core. 20s is the default; synchronous hosts may pass a
# smaller timeout so the request fits inside their outer hook deadline.
POST_TIMEOUT_SEC = 20.0
RESPONSE_TEXT_MAX = 512


def _plugin_version() -> str:
    """Read package metadata so hook telemetry tracks the installed plugin."""
    plugin_root = Path(__file__).resolve().parent.parent
    versions: list[str] = []
    for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        path = plugin_root / rel
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        version = data.get("version")
        if isinstance(version, str) and version:
            versions.append(version)
    return versions[0] if versions else DEFAULT_PLUGIN_VERSION


def _is_killed() -> bool:
    return os.environ.get("UNITARES_CHECKINS", "on").strip().lower() == "off"


def _log_path() -> Path:
    raw = os.environ.get("UNITARES_CHECKIN_LOG", DEFAULT_LOG_PATH)
    return Path(raw).expanduser()


def _append_log(
    *,
    slot: str,
    event: str,
    uuid: str,
    status: str,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """Append one line to the diagnostic log. Never raises."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts = [
            stamp,
            f"slot={slot}",
            f"event={event}",
            f"uuid={uuid[:8] if uuid else '?'}",
            f"status={status}",
        ]
        if latency_ms is not None:
            parts.append(f"latency_ms={latency_ms}")
        if error:
            # Strip anything that could corrupt the line-oriented log:
            # newlines split a record in two, backslashes and quotes break the
            # quoted-string shape. 120-char cap applied after sanitization so a
            # long unicode sequence can't sneak past via the escape expansion.
            safe_err = (
                str(error)
                .replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", " ")
                .replace("\r", " ")
                .replace("|", "/")
            )[:120]
            parts.append(f'err="{safe_err}"')
        line = " | ".join(parts) + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        # Logging failures are swallowed — we must not break the hook.
        pass


def _post_to_governance(
    url: str,
    payload: dict,
    timeout: float = POST_TIMEOUT_SEC,
) -> tuple[bool, int, Optional[str]]:
    """POST payload to /v1/tools/call. Returns (success, latency_ms, err_text)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/v1/tools/call",
        data=data,
        headers=governance_json_headers(),
    )
    t0 = time.monotonic()
    try:
        with authorization_safe_urlopen(req, timeout=timeout) as resp:
            raw_response = resp.read()
        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            response = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return False, latency_ms, f"invalid governance response: {exc}"

        def explicit_success(value: object, depth: int = 0) -> Optional[bool]:
            if depth > 5:
                return None
            if isinstance(value, list):
                verdicts = [explicit_success(item, depth + 1) for item in value]
                if False in verdicts:
                    return False
                return True if True in verdicts else None
            if not isinstance(value, dict):
                return None
            if value.get("isError") is True:
                return False
            current = value.get("success")
            if current is False:
                return False
            nested_verdicts: list[Optional[bool]] = []
            for key in ("result", "structuredContent", "content"):
                if key in value:
                    nested_verdicts.append(explicit_success(value[key], depth + 1))
            text = value.get("text")
            if isinstance(text, str):
                try:
                    nested_verdicts.append(
                        explicit_success(json.loads(text), depth + 1)
                    )
                except json.JSONDecodeError:
                    pass
            if False in nested_verdicts:
                return False
            if current is True or True in nested_verdicts:
                return True
            return None

        verdict = explicit_success(response)
        if verdict is not True:
            error = "governance response did not confirm success"
            if isinstance(response, dict):
                nested = response.get("result")
                detail = response.get("error")
                if not detail and isinstance(nested, dict):
                    detail = nested.get("error")
                if detail:
                    error = str(detail)
            return False, latency_ms, error
        return True, latency_ms, None
    except urllib.error.URLError as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return False, latency_ms, str(getattr(e, "reason", e))
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return False, latency_ms, str(e)


def submit_checkin(
    *,
    event: str,
    response_text: str,
    complexity: float,
    confidence: Optional[float] = None,
    client_session_id: str,
    slot: str,
    continuity_token: str = "",
    uuid: str = "",
    server_url: Optional[str] = None,
    plugin_version: Optional[str] = None,
    epistemic_class: Optional[str] = None,
    timeout: Optional[float] = None,
) -> str:
    """Send one check-in. Returns a status string suitable for logging."""
    if _is_killed():
        _append_log(slot=slot, event=event, uuid=uuid, status="skip_kill_switch")
        return "skip_kill_switch"
    try:
        safe_text = redact_secrets(response_text)[:RESPONSE_TEXT_MAX]
        url = server_url or os.environ.get("UNITARES_SERVER_URL", DEFAULT_SERVER_URL)
        arguments = {
            "response_text": safe_text,
            "complexity": max(0.0, min(1.0, float(complexity))),
            "client_session_id": client_session_id,
            "metadata": {
                "source": "plugin_hook",
                "event": event,
                "plugin_version": plugin_version or _plugin_version(),
            },
        }
        # `confidence` is a belief the agent holds, and a hook holds none. When
        # the caller has no agent-authored value it must omit the field rather
        # than substitute a literal: the server mints a tactical prediction from
        # whatever arrives (process_agent_update registers one whenever
        # confidence is not None), and that prediction is scored into the
        # fleet calibration curve. A constant supplied by a hook therefore
        # shows up as a real, well-calibrated-looking bin that no agent ever
        # predicted. Omitting it leaves ctx.confidence None and mints nothing.
        if confidence is not None:
            arguments["confidence"] = max(0.0, min(1.0, float(confidence)))
        if epistemic_class:
            arguments["epistemic_class"] = epistemic_class
        payload = {
            "name": "process_agent_update",
            "arguments": arguments,
        }
        if timeout is None:
            ok, latency_ms, err = _post_to_governance(url, payload)
        else:
            ok, latency_ms, err = _post_to_governance(
                url,
                payload,
                timeout=max(0.1, float(timeout)),
            )
        status = "sent" if ok else "fail"
        _append_log(
            slot=slot, event=event, uuid=uuid, status=status,
            latency_ms=latency_ms, error=err,
        )
        return status
    except Exception as e:
        _append_log(slot=slot, event=event, uuid=uuid, status="error", error=str(e))
        return "error"


def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Submit one governance check-in.")
    p.add_argument("--event", required=True)
    p.add_argument("--response-text", required=True)
    p.add_argument("--complexity", type=float, required=True)
    p.add_argument(
        "--confidence",
        type=float,
        default=None,
        help=(
            "Agent-authored confidence. Omit for substrate-derived check-ins: "
            "a supplied value mints a tactical prediction that is scored into "
            "the calibration curve, so a hook-chosen constant becomes a "
            "fabricated forecast."
        ),
    )
    p.add_argument("--client-session-id", required=True)
    p.add_argument(
        "--continuity-token",
        default="",
        help=(
            "Deprecated compatibility flag; ignored for process_agent_update. "
            "Use continuity_token only with explicit identity(agent_uuid, "
            "continuity_token, resume=true) PATH 0 rebinds."
        ),
    )
    p.add_argument("--slot", required=True)
    p.add_argument("--uuid", default="")
    p.add_argument("--server-url", default=None)
    p.add_argument("--plugin-version", default=None)
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="HTTP timeout in seconds; defaults to the helper's 20-second budget.",
    )
    p.add_argument(
        "--epistemic-class",
        default=None,
        choices=[
            "agent_report",
            "substrate_observation",
            "substrate_interpretation",
            "prediction",
        ],
        help="Optional process_agent_update epistemic_class label.",
    )
    p.add_argument(
        "--require-delivery",
        action="store_true",
        help="Return nonzero when the check-in kill switch skips delivery.",
    )
    args = p.parse_args()

    status = submit_checkin(
        event=args.event,
        response_text=args.response_text,
        complexity=args.complexity,
        confidence=args.confidence,
        client_session_id=args.client_session_id,
        continuity_token=args.continuity_token,
        slot=args.slot,
        uuid=args.uuid,
        server_url=args.server_url,
        plugin_version=args.plugin_version,
        epistemic_class=args.epistemic_class,
        timeout=args.timeout,
    )
    if status == "sent":
        return 0
    if status == "skip_kill_switch" and not args.require_delivery:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
