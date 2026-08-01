#!/usr/bin/env python3
"""Check the strict identity thread-anchor contract.

This guardrail has two modes:

* local envelope check (default): verifies the plugin only sends a
  client_session_id when the orchestrated marker is present.
* live canary (``--live``): probes a running governance server and asserts the
  strict-mode boundary: bare thread anchors refuse, while orchestrated thread
  anchors first-bind and then resume the same UUID.

The live canary writes short-lived test identities to the target server. Use a
unique ``--anchor`` or let the script generate one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from onboard_helper import run_onboard, unwrap_tool_response  # noqa: E402
from _http_auth import authorization_safe_urlopen  # noqa: E402


DEFAULT_SERVER_URL = "http://127.0.0.1:8767"
DEFAULT_TIMEOUT = 10.0


@dataclass
class CheckResult:
    ok: bool
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
        }
        if self.detail:
            data["detail"] = self.detail
        return data


class ContractError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def result(self) -> CheckResult:
        return CheckResult(False, self.code, self.message, self.detail)


class RecordingTransport:
    def __init__(self, response: dict[str, Any]):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, payload: dict, timeout: float, token: str | None) -> dict:
        self.calls.append({
            "url": url,
            "payload": payload,
            "timeout": timeout,
            "token": token,
        })
        return self.response


def _ok_onboard(uuid_value: str, *, client_session_id: str = "agent:/thread-contract") -> dict:
    return {
        "result": {
            "success": True,
            "uuid": uuid_value,
            "agent_id": f"Contract_{uuid_value[:8]}",
            "client_session_id": client_session_id,
            "session_resolution_source": "explicit_client_session_id",
            "display_name": "contract-thread-canary",
        }
    }


def _sent_args(transport: RecordingTransport) -> dict[str, Any]:
    if not transport.calls:
        raise ContractError("local_no_onboard_call", "run_onboard did not call the transport")
    return transport.calls[0]["payload"]["arguments"]


def check_local_envelope() -> CheckResult:
    """Pin the plugin half of the contract without contacting a server."""
    with tempfile.TemporaryDirectory(prefix="unitares-anchor-contract-") as tmp:
        workspace = Path(tmp)

        resume_transport = RecordingTransport(_ok_onboard(
            "11111111-1111-4111-8111-111111111111"
        ))
        run_onboard(
            server_url="http://contract.invalid",
            agent_name="contract-thread-canary",
            model_type="contract-canary",
            workspace=workspace,
            slot="turn-1",
            client_session_id="agent:/thread-contract",
            orchestrated=True,
            post_json=resume_transport,
        )
        resume_args = _sent_args(resume_transport)
        if resume_args.get("client_session_id") != "agent:/thread-contract":
            raise ContractError(
                "local_missing_client_session_id",
                "orchestrated anchor did not send client_session_id",
                {"arguments": resume_args},
            )
        if resume_args.get("orchestrated") is not True:
            raise ContractError(
                "local_missing_orchestrated_marker",
                "orchestrated anchor did not send orchestrated=true",
                {"arguments": resume_args},
            )
        forbidden_resume = [
            key for key in ("force_new", "parent_agent_id", "spawn_reason")
            if key in resume_args
        ]
        if forbidden_resume:
            raise ContractError(
                "local_resume_declares_fresh_or_lineage",
                "orchestrated resume payload included fresh-mint or lineage fields",
                {"forbidden": forbidden_resume, "arguments": resume_args},
            )

        bare_transport = RecordingTransport(_ok_onboard(
            "22222222-2222-4222-8222-222222222222"
        ))
        run_onboard(
            server_url="http://contract.invalid",
            agent_name="contract-thread-canary",
            model_type="contract-canary",
            workspace=workspace,
            slot="turn-2",
            client_session_id="agent:/thread-contract",
            orchestrated=False,
            post_json=bare_transport,
        )
        bare_args = _sent_args(bare_transport)
        if "client_session_id" in bare_args or "orchestrated" in bare_args:
            raise ContractError(
                "local_bare_anchor_forwarded",
                "bare anchor leaked into the onboard payload",
                {"arguments": bare_args},
            )
        if bare_args.get("force_new") is not True:
            raise ContractError(
                "local_bare_anchor_not_fresh_mint",
                "bare anchor did not fall back to force_new fresh mint",
                {"arguments": bare_args},
            )

    return CheckResult(
        True,
        "local_envelope_ok",
        "plugin envelope honors anchors only with orchestrated marker",
    )


def _post_tool(
    *,
    server_url: str,
    name: str,
    arguments: dict[str, Any],
    timeout: float,
    auth_token: str | None,
) -> dict[str, Any]:
    url = f"{server_url.rstrip('/')}/v1/tools/call"
    body = json.dumps({"name": name, "arguments": arguments}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with authorization_safe_urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            raw = json.loads(exc.read().decode("utf-8"))
        except Exception:
            raw = {"error": f"HTTP {exc.code}: {exc.reason}"}
    except URLError as exc:
        raise ContractError(
            "live_server_unreachable",
            f"could not reach governance server: {exc}",
            {"server_url": server_url},
        ) from exc
    except TimeoutError as exc:
        raise ContractError(
            "live_server_timeout",
            "governance server did not respond before timeout",
            {"server_url": server_url, "timeout": timeout},
        ) from exc
    return unwrap_tool_response(raw)


def _is_lineage_required(parsed: dict[str, Any]) -> bool:
    return (
        parsed.get("status") == "lineage_declaration_required"
        or parsed.get("recovery", {}).get("reason") == "lineage_declaration_required"
    )


def check_live_contract(
    *,
    server_url: str,
    anchor: str,
    timeout: float,
    auth_token: str | None,
) -> CheckResult:
    """Probe a strict server's first-bind and resume behavior."""
    bare = _post_tool(
        server_url=server_url,
        name="onboard",
        arguments={
            "name": "contract-thread-canary-bare",
            "model_type": "contract-canary",
            "client_session_id": anchor,
        },
        timeout=timeout,
        auth_token=auth_token,
    )
    if not _is_lineage_required(bare):
        raise ContractError(
            "live_bare_anchor_not_refused",
            "strict server did not refuse a bare thread anchor resume miss",
            {"anchor": anchor, "response": bare},
        )

    with tempfile.TemporaryDirectory(prefix="unitares-anchor-live-") as tmp:
        workspace = Path(tmp)
        first = run_onboard(
            server_url=server_url,
            agent_name="contract-thread-canary",
            model_type="contract-canary",
            workspace=workspace,
            slot="turn-1",
            client_session_id=anchor,
            orchestrated=True,
            auth_token=auth_token,
            timeout=timeout,
        )
        if first.get("status") != "ok" or not first.get("uuid"):
            raise ContractError(
                "live_orchestrated_first_bind_failed",
                "orchestrated thread anchor did not first-bind successfully",
                {"anchor": anchor, "response": first},
            )

        second = run_onboard(
            server_url=server_url,
            agent_name="contract-thread-canary",
            model_type="contract-canary",
            workspace=workspace,
            slot="turn-2",
            client_session_id=anchor,
            orchestrated=True,
            auth_token=auth_token,
            timeout=timeout,
        )
        if second.get("status") != "ok" or not second.get("uuid"):
            raise ContractError(
                "live_orchestrated_resume_failed",
                "orchestrated thread anchor did not resume successfully",
                {"anchor": anchor, "first": first, "second": second},
            )
        if second["uuid"] != first["uuid"]:
            raise ContractError(
                "live_orchestrated_resume_uuid_mismatch",
                "second orchestrated turn resolved a different governance UUID",
                {"anchor": anchor, "first": first, "second": second},
            )

    return CheckResult(
        True,
        "live_contract_ok",
        "strict server refuses bare anchors and resumes orchestrated thread anchors",
        {"anchor": anchor, "uuid": first["uuid"]},
    )


def _default_anchor() -> str:
    return f"agent:/thread-contract-canary-{uuid.uuid4().hex[:12]}"


def _print_report(results: list[CheckResult], *, as_json: bool) -> None:
    payload = {
        "ok": all(result.ok for result in results),
        "results": [result.as_dict() for result in results],
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    for result in results:
        prefix = "PASS" if result.ok else "FAIL"
        print(f"{prefix} {result.code}: {result.message}")
        if result.detail:
            print(json.dumps(result.detail, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also probe a live strict governance server. This writes a canary identity.",
    )
    parser.add_argument(
        "--server-url",
        default=os.environ.get("UNITARES_SERVER_URL", DEFAULT_SERVER_URL),
        help="Governance server URL for --live.",
    )
    parser.add_argument(
        "--anchor",
        default="",
        help="Thread anchor for --live. Defaults to a unique agent:/thread-contract-canary-* anchor.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("UNITARES_HTTP_API_TOKEN", ""),
        help="Optional bearer token. Defaults to UNITARES_HTTP_API_TOKEN.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    results: list[CheckResult] = []
    try:
        results.append(check_local_envelope())
        if args.live:
            results.append(check_live_contract(
                server_url=args.server_url,
                anchor=(args.anchor.strip() or _default_anchor()),
                timeout=args.timeout,
                auth_token=(args.auth_token.strip() or None),
            ))
    except ContractError as exc:
        results.append(exc.result())
    except Exception as exc:
        results.append(CheckResult(
            False,
            "unexpected_error",
            str(exc),
            {"type": type(exc).__name__},
        ))

    _print_report(results, as_json=args.json)
    return 0 if all(result.ok for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
