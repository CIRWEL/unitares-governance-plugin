#!/usr/bin/env python3
"""Local UNITARES identity sidecar.

This is a thin REST sidecar for clients that do not have lifecycle hooks. It
does not implement governance policy and does not replace the UNITARES server.
It wraps the server's `/v1/tools/call` surface with local lifecycle help:

* lazy onboard when a slot has no cached `client_session_id`
* inject `client_session_id` into attribution-relevant governance calls
* force fresh posture for bare `onboard` / `start_session` calls
* stamp the slot cache after successful check-ins
* expose the local identity-contract audit
* proxy minimal JSON-RPC MCP `tools/call` requests through `/mcp/`

Phase 1 intentionally stays dependency-free: REST plus minimal JSON-RPC MCP.
A full streamable-MCP/SSE proxy can reuse the same core behavior once that
transport boundary is worth owning directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_identity_contract import audit_checkin_log, audit_session_caches, parse_since  # noqa: E402
from checkin import _plugin_version, submit_checkin  # noqa: E402
from governance_call_inject import INJECT_SUFFIXES, PROOF_FIELDS  # noqa: E402
from _http_auth import authorization_safe_urlopen  # noqa: E402
from _redact import sanitize_model_visible_payload  # noqa: E402
from onboard_helper import (  # noqa: E402
    CacheGenerationToken,
    DEFAULT_SERVER_URL,
    _cache_path,
    _read_cache,
    _read_cache_snapshot,
    _update_cache,
    _update_cache_transaction,
    _write_cache,
    run_onboard,
    unwrap_tool_response,
)
from _session_cache_io import session_cache_lock  # noqa: E402


IDENTITY_TOOL_NAMES = {"onboard", "start_session", "identity", "bind_session"}
START_TOOL_NAMES = {"onboard", "start_session"}
CHECKIN_TOOL_NAMES = {"process_agent_update", "sync_state"}
DEFAULT_PORT = 8768
DEFAULT_TIMEOUT = 20.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_hash(workspace: Path) -> str:
    return hashlib.md5(str(workspace).encode("utf-8")).hexdigest()[:8]


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    visible = sanitize_model_visible_payload(payload)
    body = (json.dumps(visible, indent=2, sort_keys=True) + "\n").encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_request_json(handler: BaseHTTPRequestHandler) -> Any:
    raw_len = handler.headers.get("Content-Length") or "0"
    try:
        length = max(0, int(raw_len))
    except ValueError:
        length = 0
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return data


def _handler_base_url(handler: BaseHTTPRequestHandler) -> str:
    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "")
    scheme = (forwarded_proto.split(",")[0].strip() if forwarded_proto else "") or "http"
    forwarded_host = handler.headers.get("X-Forwarded-Host", "")
    host = (forwarded_host.split(",")[0].strip() if forwarded_host else "") or handler.headers.get("Host")
    if not host:
        address = handler.server.server_address
        host = f"{address[0]}:{address[1]}"
    return f"{scheme}://{host}".rstrip("/")


def _has_proof_field(arguments: dict[str, Any]) -> bool:
    for field in PROOF_FIELDS:
        value = arguments.get(field)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


def _post_json(url: str, payload: dict[str, Any], timeout: float, token: str | None) -> tuple[dict[str, Any], int, str | None]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    started = time.monotonic()
    try:
        with authorization_safe_urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return {}, int((time.monotonic() - started) * 1000), str(getattr(exc, "reason", exc))
    try:
        data = json.loads(raw)
    except Exception as exc:
        return {}, int((time.monotonic() - started) * 1000), f"invalid json: {exc}"
    return data if isinstance(data, dict) else {}, int((time.monotonic() - started) * 1000), None


def _post_json_any(
    url: str,
    payload: Any,
    timeout: float,
    token: str | None,
) -> tuple[Any, int, int, str | None]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    started = time.monotonic()
    try:
        with authorization_safe_urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return None, 502, int((time.monotonic() - started) * 1000), str(getattr(exc, "reason", exc))
    try:
        data = json.loads(raw) if raw else {}
    except Exception as exc:
        return None, status, int((time.monotonic() - started) * 1000), f"invalid json: {exc}"
    return data, status, int((time.monotonic() - started) * 1000), None


class IdentitySidecar:
    def __init__(
        self,
        *,
        server_url: str,
        workspace: Path,
        agent_name: str,
        model_type: str,
        default_slot: str,
        timeout: float = DEFAULT_TIMEOUT,
        auth_token: str | None = None,
        log_path: Path | None = None,
        audit_log_tail: int | None = 200,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.workspace = workspace
        self.agent_name = agent_name
        self.model_type = model_type
        self.default_slot = default_slot
        self.timeout = timeout
        self.auth_token = auth_token
        self.log_path = log_path or Path(os.environ.get("UNITARES_CHECKIN_LOG", "~/.unitares/checkins.log")).expanduser()
        self.audit_log_tail = audit_log_tail

    def slot_from(self, body: dict[str, Any], headers: Any) -> str:
        header_slot = headers.get("X-UNITARES-Slot") if headers else ""
        sidecar = body.get("sidecar")
        body_slot = ""
        if isinstance(sidecar, dict):
            raw = sidecar.get("slot")
            body_slot = raw if isinstance(raw, str) else ""
        raw_top = body.get("slot")
        if isinstance(raw_top, str) and raw_top.strip():
            body_slot = raw_top
        slot = (header_slot or body_slot or self.default_slot or "").strip()
        return slot or f"sidecar-{_workspace_hash(self.workspace)}"

    def read_session(self, slot: str) -> dict[str, Any]:
        return _read_cache(self.workspace, slot)

    def write_session(self, slot: str, payload: dict[str, Any]) -> None:
        clean = {k: v for k, v in payload.items() if k != "continuity_token"}
        clean.setdefault("schema_version", 2)
        clean["updated_at"] = _now_iso()
        _write_cache(self.workspace, clean, slot)

    def stamp_session(self, slot: str) -> None:
        def stamp(session: dict[str, Any]) -> dict[str, Any] | None:
            if not session:
                return None
            session.pop("continuity_token", None)
            session.setdefault("schema_version", 2)
            session["last_checkin_ts"] = int(time.time())
            session["updated_at"] = _now_iso()
            return session

        _update_cache(self.workspace, stamp, slot)

    def ensure_session(self, slot: str) -> dict[str, Any]:
        # The HTTP server is threaded. A distinct stable lock prevents two
        # processes or threads from both minting an upstream identity for the
        # same empty slot while leaving ordinary cache/clear transactions free.
        onboard_claim = _cache_path(self.workspace, slot).with_name(
            f"{_cache_path(self.workspace, slot).name}.onboard"
        )
        try:
            with session_cache_lock(onboard_claim):
                session = self.read_session(slot)
                sid = session.get("client_session_id")
                if isinstance(sid, str) and sid.strip():
                    return {
                        "status": "cached",
                        "uuid": session.get("uuid", ""),
                        "client_session_id": sid.strip(),
                        "session": session,
                    }
                result = run_onboard(
                    server_url=self.server_url,
                    agent_name=self.agent_name,
                    model_type=self.model_type,
                    workspace=self.workspace,
                    slot=slot,
                    force_new=False,
                    auth_token=self.auth_token,
                    timeout=self.timeout,
                )
        except TimeoutError:
            return {
                "status": "onboard_in_progress",
                "error": "another sidecar request is onboarding this slot",
            }
        if result.get("status") != "ok":
            return result
        # run_onboard writes the cache; add schema/stamp fields for the sidecar
        # contract without persisting the transient continuity_token.
        def stamp_schema(session: dict[str, Any]) -> dict[str, Any] | None:
            if not session:
                return None
            session.setdefault("schema_version", 2)
            session["updated_at"] = _now_iso()
            return session

        result["session"] = _update_cache(self.workspace, stamp_schema, slot)
        current_sid = str(result["session"].get("client_session_id") or "").strip()
        if not current_sid or current_sid != str(result.get("client_session_id") or "").strip():
            return {
                "status": "onboard_superseded",
                "error": "session cache changed after onboarding; newer local state preserved",
            }
        return result

    def update_cache_from_identity_response(
        self,
        slot: str,
        parsed: dict[str, Any],
        *,
        expected_generation: CacheGenerationToken | None = None,
    ) -> None:
        if not isinstance(parsed, dict):
            return
        canonical = parsed.get("raw_governance")
        if not isinstance(canonical, dict):
            canonical = {}
        uuid = (
            parsed.get("uuid")
            or parsed.get("agent_uuid")
            or canonical.get("uuid")
            or canonical.get("agent_uuid")
        )
        sid = parsed.get("client_session_id") or canonical.get("client_session_id")
        if not uuid and not sid:
            return
        def merge_identity(existing: dict[str, Any]) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "server_url": self.server_url,
                "agent_name": self.agent_name,
                "slot": slot,
                "uuid": uuid or existing.get("uuid", ""),
                "agent_id": parsed.get("agent_id")
                or parsed.get("resolved_agent_id")
                or canonical.get("agent_id")
                or canonical.get("resolved_agent_id")
                or existing.get("agent_id", ""),
                "client_session_id": sid or existing.get("client_session_id", ""),
                "session_resolution_source": parsed.get("session_resolution_source")
                or canonical.get("session_resolution_source")
                or existing.get("session_resolution_source", ""),
                "display_name": parsed.get("display_name")
                or canonical.get("display_name")
                or existing.get("display_name", ""),
                "schema_version": 2,
                "updated_at": _now_iso(),
            }
            return payload

        _update_cache_transaction(
            self.workspace,
            merge_identity,
            slot,
            expected_generation=expected_generation,
        )

    def prepare_tool_arguments(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        slot: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        arguments = dict(arguments)
        lifecycle: dict[str, Any] = {"slot": slot, "session_proof_injected": False}

        if name in START_TOOL_NAMES:
            arguments.setdefault("force_new", True)
            arguments.setdefault("name", self.agent_name)
            arguments.setdefault("model_type", self.model_type)
        elif name in INJECT_SUFFIXES and not _has_proof_field(arguments):
            ensured = self.ensure_session(slot)
            lifecycle["ensure_session"] = {k: v for k, v in ensured.items() if k != "session"}
            sid = ensured.get("client_session_id")
            if isinstance(sid, str) and sid.strip():
                arguments["client_session_id"] = sid.strip()
                lifecycle["session_proof_injected"] = True
        return arguments, lifecycle

    def process_tool_response(
        self,
        *,
        name: str,
        slot: str,
        raw: Any,
        identity_generation: CacheGenerationToken | None = None,
    ) -> None:
        if not isinstance(raw, dict):
            return
        parsed = unwrap_tool_response(raw)
        if name in IDENTITY_TOOL_NAMES:
            self.update_cache_from_identity_response(
                slot,
                parsed,
                expected_generation=identity_generation,
            )
        if name in CHECKIN_TOOL_NAMES:
            self.stamp_session(slot)

    def tool_call(self, body: dict[str, Any], headers: Any) -> tuple[int, dict[str, Any]]:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return 400, {"success": False, "error": "missing tool name"}
        name = name.strip()
        arguments = body.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return 400, {"success": False, "error": "arguments must be an object"}
        slot = self.slot_from(body, headers)
        arguments, lifecycle = self.prepare_tool_arguments(
            name=name,
            arguments=arguments,
            slot=slot,
        )
        identity_generation = None
        if name in IDENTITY_TOOL_NAMES:
            _session, identity_generation = _read_cache_snapshot(self.workspace, slot)

        upstream_payload = {"name": name, "arguments": arguments}
        raw, latency_ms, error = _post_json(
            f"{self.server_url}/v1/tools/call",
            upstream_payload,
            self.timeout,
            self.auth_token,
        )
        if error:
            return 502, sanitize_model_visible_payload({
                "success": False,
                "error": error,
                "sidecar": lifecycle | {"upstream_latency_ms": latency_ms},
            })

        self.process_tool_response(
            name=name,
            slot=slot,
            raw=raw,
            identity_generation=identity_generation,
        )

        raw.setdefault("sidecar", {})
        if isinstance(raw["sidecar"], dict):
            raw["sidecar"].update(lifecycle | {"upstream_latency_ms": latency_ms})
        return 200, sanitize_model_visible_payload(raw)

    def mcp_proxy(self, body: Any, headers: Any, path: str) -> tuple[int, Any]:
        """Proxy a minimal JSON-RPC MCP request, intercepting tools/call.

        This intentionally covers JSON request/response MCP traffic. It is not
        an SSE stream implementation; callers needing full streamable transport
        should continue to use the upstream MCP endpoint directly until that
        path is explicitly implemented here.
        """
        slot = self.slot_from(body if isinstance(body, dict) else {}, headers)
        requests = body if isinstance(body, list) else [body]
        if not all(isinstance(item, dict) for item in requests):
            return 400, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "invalid JSON-RPC request"},
            }

        metadata_by_id: dict[Any, tuple[str, dict[str, Any], int | None]] = {}
        mutated: list[dict[str, Any]] = []
        for item in requests:
            req = dict(item)
            if req.get("method") == "tools/call":
                params = req.get("params")
                if isinstance(params, dict):
                    name = params.get("name")
                    arguments = params.get("arguments")
                    if isinstance(name, str):
                        prepared, lifecycle = self.prepare_tool_arguments(
                            name=name,
                            arguments=arguments if isinstance(arguments, dict) else {},
                            slot=slot,
                        )
                        new_params = dict(params)
                        new_params["arguments"] = prepared
                        req["params"] = new_params
                        identity_generation = None
                        if name in IDENTITY_TOOL_NAMES:
                            _session, identity_generation = _read_cache_snapshot(
                                self.workspace,
                                slot,
                            )
                        metadata_by_id[req.get("id")] = (
                            name,
                            lifecycle,
                            identity_generation,
                        )
            mutated.append(req)

        outbound: Any = mutated if isinstance(body, list) else mutated[0]
        upstream_path = path if path.startswith("/") else f"/{path}"
        upstream, status, _latency_ms, error = _post_json_any(
            f"{self.server_url}{upstream_path}",
            outbound,
            self.timeout,
            self.auth_token,
        )
        if error:
            first_id = requests[0].get("id") if requests and isinstance(requests[0], dict) else None
            return 502, sanitize_model_visible_payload({
                "jsonrpc": "2.0",
                "id": first_id,
                "error": {"code": -32000, "message": error},
            })

        responses = upstream if isinstance(upstream, list) else [upstream]
        for response in responses:
            if not isinstance(response, dict):
                continue
            if "error" in response:
                continue
            meta = metadata_by_id.get(response.get("id"))
            if not meta:
                continue
            name, _lifecycle, identity_generation = meta
            self.process_tool_response(
                name=name,
                slot=slot,
                raw=response,
                identity_generation=identity_generation,
            )
        return status, sanitize_model_visible_payload(upstream)

    def turn_checkin(self, body: dict[str, Any], headers: Any) -> tuple[int, dict[str, Any]]:
        slot = self.slot_from(body, headers)
        ensured = self.ensure_session(slot)
        sid = ensured.get("client_session_id")
        if not isinstance(sid, str) or not sid.strip():
            return 502, sanitize_model_visible_payload({
                "success": False,
                "error": "sidecar could not establish client_session_id",
                "onboard": ensured,
            })
        session = self.read_session(slot)
        event = str(body.get("event") or "turn_stop")
        status = submit_checkin(
            event=event,
            response_text=str(body.get("response_text") or body.get("summary") or "Sidecar turn check-in"),
            complexity=float(body.get("complexity", 0.3)),
            # Pass through only what the caller actually stated. Defaulting a
            # missing confidence mints a tactical prediction the caller never
            # made; None omits the field and mints nothing.
            confidence=(
                float(body["confidence"])
                if body.get("confidence") is not None
                else None
            ),
            client_session_id=sid.strip(),
            slot=slot,
            uuid=str(session.get("uuid", "")),
            server_url=self.server_url,
            plugin_version=_plugin_version(),
            epistemic_class=body.get("epistemic_class") or "agent_report",
        )
        if status == "sent":
            self.stamp_session(slot)
        return 200 if status in {"sent", "skip_kill_switch"} else 502, sanitize_model_visible_payload({
            "success": status in {"sent", "skip_kill_switch"},
            "status": status,
            "slot": slot,
            "session_bound": True,
            "client_session_id": sid.strip(),
            "uuid": session.get("uuid", ""),
        })

    def audit(self, *, log_tail: int | None = None, since: str | None = None) -> dict[str, Any]:
        since_dt = parse_since(since)
        tail = self.audit_log_tail if log_tail is None else log_tail
        findings = audit_session_caches(self.workspace) + audit_checkin_log(
            self.log_path,
            log_tail=tail,
            since=since_dt,
        )
        return {
            "success": True,
            "workspace": str(self.workspace),
            "log": str(self.log_path),
            "log_tail": tail,
            "since": since_dt.isoformat() if since_dt else None,
            "errors": sum(1 for f in findings if f.severity == "error"),
            "warnings": sum(1 for f in findings if f.severity == "warning"),
            "findings": [f.as_dict() for f in findings],
        }

    def client_config(self, *, sidecar_url: str, slot: str) -> dict[str, Any]:
        base = sidecar_url.rstrip("/")
        slot = slot.strip() or self.default_slot
        mcp_url = f"{base}/mcp/"
        return {
            "success": True,
            "sidecar_url": base,
            "server_url": self.server_url,
            "workspace": str(self.workspace),
            "default_slot": self.default_slot,
            "slot": slot,
            "transport": {
                "mcp": {
                    "mode": "jsonrpc-http",
                    "url": mcp_url,
                    "slot_header": "X-UNITARES-Slot",
                    "intercepts": ["tools/call"],
                    "passes_through": ["tools/list", "initialize", "notifications/*"],
                },
                "rest_tool_call": f"{base}/v1/tools/call",
                "turn_checkin": f"{base}/turn/checkin",
                "turn_stop": f"{base}/turn/stop",
                "session": f"{base}/session",
                "audit": f"{base}/audit?log_tail=200",
            },
            "mcp_config": {
                "mcpServers": {
                    "unitares-governance-sidecar": {
                        "type": "url",
                        "url": mcp_url,
                        "headers": {"X-UNITARES-Slot": slot},
                    }
                }
            },
            "environment": {
                "UNITARES_SIDECAR_URL": base,
                "UNITARES_SIDECAR_SLOT": slot,
                "UNITARES_SERVER_URL": self.server_url,
            },
            "notes": [
                "Use this sidecar MCP URL only for clients that send JSON request/response MCP over HTTP.",
                "Clients requiring streamable HTTP or SSE should use the upstream governance MCP endpoint until sidecar support is added.",
                "The sidecar stores slot-scoped client_session_id continuity and never persists continuity_token.",
            ],
        }


def make_handler(sidecar: IdentitySidecar) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "UNITARESIdentitySidecar/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout clean for agents
            if os.environ.get("UNITARES_SIDECAR_ACCESS_LOG", "off").lower() in {"1", "on", "true"}:
                super().log_message(fmt, *args)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            path = parsed.path
            if path == "/health":
                _json_response(self, 200, {
                    "success": True,
                    "server_url": sidecar.server_url,
                    "workspace": str(sidecar.workspace),
                    "default_slot": sidecar.default_slot,
                    "client_config": f"{_handler_base_url(self)}/client-config",
                    "mcp": f"{_handler_base_url(self)}/mcp/",
                })
                return
            if path in {"/client-config", "/config"}:
                requested_slot = (query.get("slot") or [""])[0]
                body = {"slot": requested_slot} if requested_slot else {}
                slot = sidecar.slot_from(body, self.headers)
                _json_response(
                    self,
                    200,
                    sidecar.client_config(sidecar_url=_handler_base_url(self), slot=slot),
                )
                return
            if path == "/session":
                slot = sidecar.slot_from({}, self.headers)
                session = dict(sidecar.read_session(slot))
                session.pop("continuity_token", None)
                _json_response(self, 200, {"success": True, "slot": slot, "session": session})
                return
            if path == "/audit":
                raw_tail = (query.get("log_tail") or [None])[0]
                try:
                    log_tail = int(raw_tail) if raw_tail else None
                    _json_response(
                        self,
                        200,
                        sidecar.audit(
                            log_tail=log_tail,
                            since=(query.get("since") or [None])[0],
                        ),
                    )
                except ValueError as exc:
                    _json_response(self, 400, {"success": False, "error": str(exc)})
                return
            _json_response(self, 404, {"success": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            body = _read_request_json(self)
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path not in {"/mcp", "/mcp/"} and not isinstance(body, dict):
                _json_response(self, 400, {"success": False, "error": "JSON object required"})
                return
            if path == "/v1/tools/call":
                status, payload = sidecar.tool_call(body, self.headers)
                _json_response(self, status, payload)
                return
            if path in {"/mcp", "/mcp/"}:
                status, payload = sidecar.mcp_proxy(body, self.headers, path or "/mcp/")
                _json_response(self, status, payload)
                return
            if path in {"/turn/checkin", "/turn/stop"}:
                if path == "/turn/stop":
                    body.setdefault("event", "turn_stop")
                    body.setdefault("epistemic_class", "substrate_interpretation")
                status, payload = sidecar.turn_checkin(body, self.headers)
                _json_response(self, status, payload)
                return
            if path == "/session/start":
                slot = sidecar.slot_from(body, self.headers)
                result = run_onboard(
                    server_url=sidecar.server_url,
                    agent_name=str(body.get("agent_name") or sidecar.agent_name),
                    model_type=str(body.get("model_type") or sidecar.model_type),
                    workspace=sidecar.workspace,
                    slot=slot,
                    force_new=bool(body.get("ignore_lineage", False)),
                    auth_token=sidecar.auth_token,
                    timeout=sidecar.timeout,
                )
                status = 200 if result.get("status") == "ok" else 502
                _json_response(self, status, {"success": status == 200, "slot": slot, "result": result})
                return
            _json_response(self, 404, {"success": False, "error": "not found"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("UNITARES_SIDECAR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("UNITARES_SIDECAR_PORT", DEFAULT_PORT)))
    parser.add_argument("--server-url", default=os.environ.get("UNITARES_SERVER_URL", DEFAULT_SERVER_URL))
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--slot", default=os.environ.get("UNITARES_SIDECAR_SLOT", ""))
    parser.add_argument("--name", default=os.environ.get("UNITARES_AGENT_NAME", ""))
    parser.add_argument("--model-type", default=os.environ.get("UNITARES_MODEL_TYPE", "sidecar"))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("UNITARES_SIDECAR_TIMEOUT", DEFAULT_TIMEOUT)))
    parser.add_argument("--audit-log-tail", type=int, default=int(os.environ.get("UNITARES_SIDECAR_AUDIT_LOG_TAIL", "200")),
                        help="default number of check-in log lines included by GET /audit")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    default_slot = args.slot.strip() or f"sidecar-{_workspace_hash(workspace)}"
    sidecar = IdentitySidecar(
        server_url=args.server_url,
        workspace=workspace,
        agent_name=args.name.strip() or workspace.name,
        model_type=args.model_type,
        default_slot=default_slot,
        timeout=args.timeout,
        auth_token=os.environ.get("UNITARES_HTTP_API_TOKEN") or None,
        audit_log_tail=args.audit_log_tail,
    )
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(sidecar))
    print(json.dumps({
        "success": True,
        "sidecar_url": f"http://{args.host}:{args.port}",
        "server_url": sidecar.server_url,
        "workspace": str(workspace),
        "default_slot": default_slot,
    }), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
