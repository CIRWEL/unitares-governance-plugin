"""Tests for the local UNITARES identity sidecar."""

from __future__ import annotations

import argparse
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer

import pytest

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import identity_sidecar  # noqa: E402
import session_cache  # noqa: E402
from identity_sidecar import IdentitySidecar, make_handler  # noqa: E402


class _ReusableTCPServer(TCPServer):
    allow_reuse_address = True


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class FakeGovernanceHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, _fmt, *_args):  # pragma: no cover
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.calls.append(data)
        if self.path.startswith("/mcp"):
            if data.get("method") == "tools/call":
                params = data.get("params") or {}
                name = params.get("name")
                if name in {"process_agent_update", "sync_state"}:
                    result = {
                        "content": [{"type": "text", "text": json.dumps({
                            "success": True,
                            "verdict": {"value": "proceed"},
                            "identity_assurance": {"tier": "strong"},
                        })}]
                    }
                elif name in {"onboard", "start_session"}:
                    result = {
                        "content": [{"type": "text", "text": json.dumps({
                            "success": True,
                            "uuid": "11111111-2222-4333-8444-555555555555",
                            "agent_id": "Sidecar_Test",
                            "client_session_id": "agent-sidecar-111",
                            "continuity_token": "v1.transientpayloadpayloadpayload.signaturesignaturesignature",
                            "raw_governance": {
                                "continuity_token": "v1.transientpayloadpayloadpayload.signaturesignaturesignature",
                                "client_session_id": "agent-sidecar-111",
                            },
                        })}]
                    }
                else:
                    result = {
                        "content": [{"type": "text", "text": json.dumps({
                            "success": True,
                            "echo": params.get("arguments", {}),
                        })}]
                    }
            else:
                result = {"tools": [{"name": "knowledge"}]}
            body = json.dumps({
                "jsonrpc": "2.0",
                "id": data.get("id"),
                "result": result,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        name = data.get("name")
        if name in {"onboard", "start_session"}:
            payload = {
                "success": True,
                "uuid": "11111111-2222-4333-8444-555555555555",
                "agent_id": "Sidecar_Test",
                "client_session_id": "agent-sidecar-111",
                "continuity_token": "v1.transient",
                "continuity_token_supported": True,
                "raw_governance": {
                    "continuity_token": "v1.transientpayloadpayloadpayload.signaturesignaturesignature",
                    "client_session_id": "agent-sidecar-111",
                },
                "session_resolution_source": "force_new",
                "display_name": "sidecar-test",
            }
        elif name in {"process_agent_update", "sync_state"}:
            payload = {
                "success": True,
                "verdict": {"value": "proceed"},
                "identity_assurance": {"tier": "strong"},
            }
        else:
            payload = {"success": True, "echo": data.get("arguments", {})}

        body = json.dumps({"result": payload}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake_server():
    FakeGovernanceHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), FakeGovernanceHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        thread.join(timeout=2)
        srv.server_close()


def _sidecar(tmp_path: Path, fake_server: str) -> IdentitySidecar:
    return IdentitySidecar(
        server_url=fake_server,
        workspace=tmp_path,
        agent_name="sidecar-agent",
        model_type="sidecar-test",
        default_slot="slot-a",
        log_path=tmp_path / "checkins.log",
    )


def _serve_sidecar(sidecar: IdentitySidecar):
    srv = _ReusableThreadingHTTPServer(("127.0.0.1", 0), make_handler(sidecar))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread, f"http://127.0.0.1:{srv.server_address[1]}"


def _stop_server(srv: TCPServer, thread: threading.Thread) -> None:
    srv.shutdown()
    thread.join(timeout=2)
    srv.server_close()


def _http_post_json(url: str, payload: object, *, headers: dict[str, str] | None = None) -> tuple[int, object]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return int(resp.status), json.loads(resp.read().decode("utf-8"))


def _http_get_json(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, object]:
    req = urllib.request.Request(url, method="GET")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return int(resp.status), json.loads(resp.read().decode("utf-8"))


def _json_text(payload: object) -> str:
    return json.dumps(payload, sort_keys=True)


def _mcp_text_payload(payload: dict) -> dict:
    text = payload["result"]["content"][0]["text"]
    return json.loads(text)


def test_tool_proxy_lazy_onboards_and_injects_client_session_id(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.tool_call(
        {"name": "knowledge", "arguments": {"action": "search", "query": "x"}},
        headers={},
    )

    assert status == 200
    assert payload["result"]["success"] is True
    assert "client_session_id" not in _json_text(payload)
    assert "continuity_token" not in _json_text(payload)
    assert "raw_governance" not in _json_text(payload)
    assert [call["name"] for call in FakeGovernanceHandler.calls] == ["onboard", "knowledge"]
    knowledge_args = FakeGovernanceHandler.calls[1]["arguments"]
    assert knowledge_args["client_session_id"] == "agent-sidecar-111"
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert cache["client_session_id"] == "agent-sidecar-111"
    assert "continuity_token" not in cache


def test_concurrent_ensure_session_is_single_flight(
    tmp_path: Path,
    fake_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    sidecar = _sidecar(tmp_path, fake_server)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked_onboard(**kwargs):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=5)
        payload = {
            "uuid": "11111111-2222-4333-8444-555555555555",
            "client_session_id": "agent-single-flight",
        }
        sidecar.write_session("slot-a", payload)
        return {"status": "ok", **payload}

    monkeypatch.setattr(identity_sidecar, "run_onboard", blocked_onboard)
    results: list[dict] = []
    workers = [
        threading.Thread(target=lambda: results.append(sidecar.ensure_session("slot-a")))
        for _ in range(2)
    ]
    workers[0].start()
    assert started.wait(timeout=5)
    workers[1].start()
    release.set()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert calls == 1
    assert len(results) == 2
    assert {result["client_session_id"] for result in results} == {
        "agent-single-flight"
    }


def test_clear_supersedes_inflight_sidecar_identity_response(
    tmp_path: Path,
    fake_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    sidecar = _sidecar(tmp_path, fake_server)
    started = threading.Event()
    release = threading.Event()
    result: list[tuple[int, dict]] = []

    def blocked_post(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=5)
        return (
            {
                "result": {
                    "success": True,
                    "uuid": "99999999-2222-4333-8444-555555555555",
                    "client_session_id": "agent-too-late",
                }
            },
            1,
            None,
        )

    monkeypatch.setattr(identity_sidecar, "_post_json", blocked_post)
    worker = threading.Thread(
        target=lambda: result.append(
            sidecar.tool_call({"name": "onboard", "arguments": {}}, headers={})
        )
    )
    worker.start()
    assert started.wait(timeout=5)
    assert session_cache.cmd_clear(
        argparse.Namespace(kind="session", workspace=str(tmp_path), slot="slot-a")
    ) == 0
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert result[0][0] == 200
    assert sidecar.read_session("slot-a") == {}
    assert not (fake_home / ".unitares" / "session-slot-a.json").exists()


def test_turn_checkin_lazy_onboards_then_sends_real_checkin(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.turn_checkin(
        {"response_text": "did work", "complexity": 0.2, "confidence": 0.8},
        headers={},
    )

    assert status == 200
    assert payload["success"] is True
    assert payload["session_bound"] is True
    assert "client_session_id" not in payload
    assert [call["name"] for call in FakeGovernanceHandler.calls] == ["onboard", "process_agent_update"]
    checkin_args = FakeGovernanceHandler.calls[1]["arguments"]
    assert checkin_args["client_session_id"] == "agent-sidecar-111"
    assert checkin_args["response_text"] == "did work"
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert "last_checkin_ts" in cache


def test_bare_onboard_through_proxy_gets_force_new_and_updates_cache(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.tool_call({"name": "onboard", "arguments": {}}, headers={})

    assert status == 200
    assert payload["result"]["success"] is True
    assert "client_session_id" not in _json_text(payload)
    assert "continuity_token" not in _json_text(payload)
    assert "raw_governance" not in _json_text(payload)
    sent = FakeGovernanceHandler.calls[0]["arguments"]
    assert sent["force_new"] is True
    assert sent["name"] == "sidecar-agent"
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert cache["uuid"] == "11111111-2222-4333-8444-555555555555"
    assert "continuity_token" not in cache


def test_explicit_proof_field_skips_lazy_onboard_and_injection(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, _payload = sidecar.tool_call(
        {"name": "knowledge", "arguments": {"action": "search", "client_session_id": "agent-explicit"}},
        headers={},
    )

    assert status == 200
    assert [call["name"] for call in FakeGovernanceHandler.calls] == ["knowledge"]
    assert FakeGovernanceHandler.calls[0]["arguments"]["client_session_id"] == "agent-explicit"


def test_audit_endpoint_reports_local_contract_findings(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / "session-bad.json").write_text(json.dumps({"continuity_token": "v1.bad"}))

    payload = sidecar.audit()

    assert payload["success"] is True
    assert payload["errors"] == 2
    assert {f["code"] for f in payload["findings"]} == {
        "session_cache_token_at_rest",
        "session_cache_missing_identity",
    }


def test_audit_endpoint_uses_bounded_log_tail(tmp_path: Path, fake_server: str) -> None:
    log = tmp_path / "checkins.log"
    log.write_text(
        "\n".join([
            "2026-06-16T00:00:00Z | slot=s1 | event=turn_stop | uuid=u1 | status=fail",
            "2026-06-16T00:01:00Z | slot=s1 | event=turn_stop | uuid=u1 | status=sent",
        ]) + "\n",
        encoding="utf-8",
    )
    sidecar = IdentitySidecar(
        server_url=fake_server,
        workspace=tmp_path,
        agent_name="sidecar-agent",
        model_type="sidecar-test",
        default_slot="slot-a",
        log_path=log,
        audit_log_tail=1,
    )

    payload = sidecar.audit()

    assert payload["log_tail"] == 1
    assert payload["warnings"] == 0


def test_mcp_tools_call_lazy_onboards_and_injects(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.mcp_proxy(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "knowledge", "arguments": {"action": "search"}},
        },
        headers={},
        path="/mcp/",
    )

    assert status == 200
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 7
    assert [call.get("name") or call.get("method") for call in FakeGovernanceHandler.calls] == [
        "onboard",
        "tools/call",
    ]
    mcp_call = FakeGovernanceHandler.calls[1]
    assert mcp_call["params"]["arguments"]["client_session_id"] == "agent-sidecar-111"
    visible = _mcp_text_payload(payload)
    assert visible["echo"]["action"] == "search"
    assert "client_session_id" not in visible["echo"]
    assert "agent-sidecar-111" not in _json_text(payload)
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert cache["client_session_id"] == "agent-sidecar-111"
    assert "continuity_token" not in cache


def test_mcp_start_session_response_hides_proof_material(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.mcp_proxy(
        {
            "jsonrpc": "2.0",
            "id": 70,
            "method": "tools/call",
            "params": {"name": "start_session", "arguments": {}},
        },
        headers={},
        path="/mcp/",
    )

    assert status == 200
    visible = _mcp_text_payload(payload)
    assert visible["success"] is True
    assert visible["uuid"] == "11111111-2222-4333-8444-555555555555"
    assert "client_session_id" not in visible
    assert "continuity_token" not in visible
    assert "raw_governance" not in visible
    assert "agent-sidecar-111" not in _json_text(payload)


def test_mcp_non_tool_call_passes_through_without_onboard(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.mcp_proxy(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}},
        headers={},
        path="/mcp/",
    )

    assert status == 200
    assert payload["result"]["tools"] == [{"name": "knowledge"}]
    assert [call.get("method") for call in FakeGovernanceHandler.calls] == ["tools/list"]
    assert not (tmp_path / ".unitares").exists()


def test_mcp_checkin_stamps_existing_session(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / "session-slot-a.json").write_text(json.dumps({
        "uuid": "11111111-2222-4333-8444-555555555555",
        "client_session_id": "agent-sidecar-111",
        "schema_version": 2,
    }))

    status, _payload = sidecar.mcp_proxy(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "sync_state", "arguments": {"response_text": "did work"}},
        },
        headers={},
        path="/mcp/",
    )

    assert status == 200
    assert [call.get("method") for call in FakeGovernanceHandler.calls] == ["tools/call"]
    sent_args = FakeGovernanceHandler.calls[0]["params"]["arguments"]
    assert sent_args["client_session_id"] == "agent-sidecar-111"
    cache = json.loads((cache_dir / "session-slot-a.json").read_text())
    assert "last_checkin_ts" in cache


def test_http_client_config_returns_slot_scoped_mcp_snippet(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    srv, thread, base_url = _serve_sidecar(sidecar)
    try:
        status, payload = _http_get_json(f"{base_url}/client-config?slot=codex-local")
    finally:
        _stop_server(srv, thread)

    assert status == 200
    assert isinstance(payload, dict)
    assert payload["success"] is True
    assert payload["sidecar_url"] == base_url
    assert payload["slot"] == "codex-local"
    assert payload["transport"]["mcp"]["mode"] == "jsonrpc-http"
    assert payload["transport"]["mcp"]["url"] == f"{base_url}/mcp/"
    config = payload["mcp_config"]["mcpServers"]["unitares-governance-sidecar"]
    assert config == {
        "type": "url",
        "url": f"{base_url}/mcp/",
        "headers": {"X-UNITARES-Slot": "codex-local"},
    }


def test_http_mcp_route_lazy_onboards_and_injects_client_session_id(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    srv, thread, base_url = _serve_sidecar(sidecar)
    try:
        status, payload = _http_post_json(
            f"{base_url}/mcp/",
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "knowledge", "arguments": {"action": "search"}},
            },
            headers={"X-UNITARES-Slot": "codex-http"},
        )
    finally:
        _stop_server(srv, thread)

    assert status == 200
    assert isinstance(payload, dict)
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 10
    assert [call.get("name") or call.get("method") for call in FakeGovernanceHandler.calls] == [
        "onboard",
        "tools/call",
    ]
    mcp_args = FakeGovernanceHandler.calls[1]["params"]["arguments"]
    assert mcp_args["client_session_id"] == "agent-sidecar-111"
    visible = _mcp_text_payload(payload)
    assert visible["echo"]["action"] == "search"
    assert "client_session_id" not in visible["echo"]
    cache = json.loads((tmp_path / ".unitares" / "session-codex-http.json").read_text())
    assert cache["client_session_id"] == "agent-sidecar-111"
    assert "continuity_token" not in cache


def test_http_session_start_hides_onboard_proof_material(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    srv, thread, base_url = _serve_sidecar(sidecar)
    try:
        status, payload = _http_post_json(
            f"{base_url}/session/start",
            {"agent_name": "sidecar-agent", "slot": "codex-start"},
        )
    finally:
        _stop_server(srv, thread)

    assert status == 200
    assert isinstance(payload, dict)
    assert payload["success"] is True
    assert payload["result"]["status"] == "ok"
    assert "client_session_id" not in _json_text(payload)
    assert "continuity_token" not in _json_text(payload)
    assert "raw_governance" not in _json_text(payload)
