"""Integration coverage for the plain-CLI UNITARES process wrapper."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import u_run  # noqa: E402


SERVER_URL = "http://governance.test:8767"


class _ReusableServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _serve(handler: type[BaseHTTPRequestHandler]):
    server = _ReusableServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _stop_server(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


def _json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def _respond(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _sidecar_handler(workspace: Path, calls: list[tuple[str, dict[str, Any]]]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):  # pragma: no cover
            return

        def do_GET(self):  # noqa: N802
            assert self.path == "/health"
            _respond(
                self,
                {
                    "success": True,
                    "server_url": SERVER_URL,
                    "workspace": str(workspace.resolve()),
                    "default_slot": "fake",
                },
            )

        def do_POST(self):  # noqa: N802
            body = _json_body(self)
            calls.append((self.path, body))
            if self.path == "/session/start":
                _respond(self, {"success": True, "result": {"status": "ok"}})
            elif self.path == "/turn/stop":
                _respond(self, {"success": True, "status": "sent"})
            else:  # pragma: no cover
                self.send_error(404)

    return Handler


def test_reuses_sidecar_exports_context_and_preserves_child_exit(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    server, thread, sidecar_url = _serve(_sidecar_handler(tmp_path, calls))
    output = tmp_path / "child-env.json"
    child = (
        "import json, os, pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
        "'server': os.environ.get('UNITARES_SERVER_URL'), "
        "'sidecar': os.environ.get('UNITARES_SIDECAR_URL'), "
        "'slot': os.environ.get('UNITARES_SIDECAR_SLOT'), "
        "'class': os.environ.get('UNITARES_MODEL_TYPE')})); "
        "raise SystemExit(7)"
    )
    try:
        result = u_run.main(
            [
                "--class",
                "goose",
                "--server-url",
                SERVER_URL,
                "--sidecar-url",
                sidecar_url,
                "--workspace",
                str(tmp_path),
                "--slot",
                "plain-cli",
                "--",
                sys.executable,
                "-c",
                child,
                str(output),
            ]
        )
    finally:
        _stop_server(server, thread)

    assert result == 7
    assert json.loads(output.read_text()) == {
        "server": SERVER_URL,
        "sidecar": sidecar_url,
        "slot": "plain-cli",
        "class": "goose",
    }
    assert [path for path, _body in calls] == ["/session/start", "/turn/stop"]
    start = calls[0][1]
    assert start == {"slot": "plain-cli", "model_type": "goose"}
    stopped = calls[1][1]
    assert stopped["event"] == "u_run_exit"
    assert stopped["epistemic_class"] == "substrate_interpretation"
    assert "confidence" not in stopped
    assert "exit code 7" in stopped["response_text"]


class _GovernanceHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def log_message(self, _fmt, *_args):  # pragma: no cover
        return

    def do_POST(self):  # noqa: N802
        body = _json_body(self)
        self.__class__.calls.append(body)
        if body.get("name") == "onboard":
            result = {
                "success": True,
                "uuid": "11111111-2222-4333-8444-555555555555",
                "agent_id": "URun_Test",
                "client_session_id": "agent-u-run-111",
                "session_resolution_source": "force_new",
            }
        else:
            result = {"success": True, "verdict": {"value": "proceed"}}
        _respond(self, {"result": result})


def _unused_sidecar_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def test_starts_real_sidecar_writes_slot_cache_and_tears_it_down(tmp_path: Path) -> None:
    _GovernanceHandler.calls = []
    server, thread, governance_url = _serve(_GovernanceHandler)
    sidecar_url = _unused_sidecar_url()
    try:
        result = u_run.main(
            [
                "--server-url",
                governance_url,
                "--sidecar-url",
                sidecar_url,
                "--workspace",
                str(tmp_path),
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(0)",
            ]
        )
    finally:
        _stop_server(server, thread)

    assert result == 0
    caches = list((tmp_path / ".unitares").glob("session-u-run-*.json"))
    assert len(caches) == 1
    cache = json.loads(caches[0].read_text())
    assert cache["uuid"] == "11111111-2222-4333-8444-555555555555"
    assert [call["name"] for call in _GovernanceHandler.calls] == [
        "onboard",
        "process_agent_update",
    ]
    assert _GovernanceHandler.calls[0]["arguments"]["model_type"] == "u-run"
    checkin = _GovernanceHandler.calls[1]["arguments"]
    assert checkin["epistemic_class"] == "substrate_interpretation"
    assert "confidence" not in checkin
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"{sidecar_url}/health", timeout=0.2)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group signal semantics")
def test_forwards_termination_signal_to_child_group(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    server, thread, sidecar_url = _serve(_sidecar_handler(tmp_path, calls))
    pid_file = tmp_path / "child.pid"
    child = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    wrapper = subprocess.Popen(
        [
            str(SCRIPTS / "u-run"),
            "--server-url",
            SERVER_URL,
            "--sidecar-url",
            sidecar_url,
            "--workspace",
            str(tmp_path),
            "--slot",
            "signal-slot",
            "--",
            sys.executable,
            "-c",
            child,
            str(pid_file),
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid = 0
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pid_file.exists():
            if wrapper.poll() is not None:
                pytest.fail(f"wrapper exited early: {wrapper.stderr.read()}")
            time.sleep(0.02)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text())
        wrapper.send_signal(signal.SIGTERM)
        _stdout, stderr = wrapper.communicate(timeout=5)
        assert stderr == ""
    finally:
        wrapper_was_running = wrapper.poll() is None
        if wrapper_was_running:
            wrapper.kill()
            wrapper.wait(timeout=2)
        if wrapper_was_running and child_pid:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _stop_server(server, thread)

    assert wrapper.returncode == 128 + signal.SIGTERM
    assert [path for path, _body in calls] == ["/session/start", "/turn/stop"]
    stopped = calls[1][1]
    assert f"signal {signal.SIGTERM}" in stopped["response_text"]
    assert f"exit code {128 + signal.SIGTERM}" in stopped["response_text"]
    assert stopped["epistemic_class"] == "substrate_interpretation"


def test_cli_requires_separator_and_complexity_is_bounded() -> None:
    with pytest.raises(SystemExit) as caught:
        u_run.parse_args(["agent-cli"])
    assert caught.value.code == 2
    assert u_run.exit_complexity(0, 0) == 0.3
    assert u_run.exit_complexity(3600, 0) == 0.65
    assert u_run.exit_complexity(3600, 9) == 0.85
    assert u_run.exit_complexity(100_000, 9) == 0.85


@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1:8768", "http://127.evil:8768", "http://127.0.0.1:0"],
)
def test_sidecar_address_must_be_plain_http_ipv4_loopback(url: str) -> None:
    with pytest.raises(u_run.URunError):
        u_run._sidecar_bind(url)
