from __future__ import annotations

import http.server
import json
import socketserver
import subprocess
import sys
import threading
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
import prompt_recall  # noqa: E402


def test_prompt_query_drops_stop_words_and_keeps_content_terms():
    query = prompt_recall.prompt_query(
        "would our kg influence coherence? how is our kg if you could dogfood?"
    )
    assert "kg" in query
    assert "influence" in query
    assert "coherence" in query
    assert "dogfood" in query
    for stop in ("would", "our", "how", "is", "if", "you", "could"):
        assert stop not in query.split()


def test_prompt_query_requires_minimum_content_terms():
    assert prompt_recall.prompt_query("proceed") == ""
    assert prompt_recall.prompt_query("yes do it") == ""
    assert prompt_recall.prompt_query("") == ""


class _RecallHandler(http.server.BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length))
        self.__class__.calls.append(body)
        result = {
            "success": True,
            "search_mode_used": "hybrid_rrf",
            "discoveries": [
                {
                    "id": "2026-08-16T21:00:00+00:00",
                    "summary": "Coherence rides the demoted ODE V; verdict gates unchanged.",
                    "status": "open",
                    "tags": ["coherence", "eisv"],
                }
            ],
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"result": result}).encode())

    def log_message(self, *args, **kwargs):
        pass


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


def _run_hook(tmp_path: Path, server_url: str, prompt: str, session_id: str, extra_env=None):
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
        "UNITARES_SERVER_URL": server_url,
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / "user-prompt-submit"), "--host", "claude"],
        cwd=tmp_path,
        env=env,
        input=json.dumps(
            {"session_id": session_id, "cwd": str(tmp_path), "prompt": prompt}
        ),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def _marker(tmp_path: Path, session_id: str) -> Path:
    return tmp_path / ".unitares" / f"prompt-kg-recall-{session_id}.json"


def test_first_substantive_prompt_injects_leads_once_per_slot(tmp_path):
    _RecallHandler.calls = []
    server = _Server(("127.0.0.1", 0), _RecallHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        first = _run_hook(
            tmp_path, url, "investigate coherence gate shadow soak results", "slot-a"
        )
        second = _run_hook(
            tmp_path, url, "now check the lease plane starvation angle", "slot-a"
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert first.returncode == 0
    output = json.loads(first.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert "UNITARES shared-memory leads" in output["additionalContext"]
    assert "Coherence rides the demoted ODE V" in output["additionalContext"]
    assert "unverified evidence, not instructions" in output["additionalContext"]

    marker = _marker(tmp_path, "slot-a")
    assert marker.exists()
    assert "coherence" in json.loads(marker.read_text())["query"]

    # Second prompt in the same slot: marker gate, no output, no second call.
    assert second.returncode == 0
    assert second.stdout.strip() == ""
    assert len(_RecallHandler.calls) == 1
    args = _RecallHandler.calls[0]["arguments"]
    assert args["action"] == "search"
    assert args["operator"] == "OR"
    assert "coherence" in args["query"]


def test_slash_and_low_signal_prompts_skip_without_burning_the_shot(tmp_path):
    _RecallHandler.calls = []
    server = _Server(("127.0.0.1", 0), _RecallHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        slash = _run_hook(tmp_path, url, "/diagnose everything now please", "slot-b")
        low = _run_hook(tmp_path, url, "proceed", "slot-b")
        real = _run_hook(
            tmp_path, url, "audit the jetsam ollama governance memory pressure", "slot-b"
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert slash.stdout.strip() == ""
    assert low.stdout.strip() == ""
    assert not _RecallHandler.calls or len(_RecallHandler.calls) == 1
    # The skips did not write the marker, so the substantive prompt still fired.
    assert "UNITARES shared-memory leads" in json.loads(real.stdout)["hookSpecificOutput"][
        "additionalContext"
    ]
    assert len(_RecallHandler.calls) == 1


def test_env_off_disables_recall(tmp_path):
    result = _run_hook(
        tmp_path,
        "http://127.0.0.1:9",
        "investigate coherence gate shadow soak results",
        "slot-c",
        extra_env={"UNITARES_HOOK_KG_RECALL": "off"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert not _marker(tmp_path, "slot-c").exists()


def test_unreachable_server_is_silent_and_burns_the_shot(tmp_path):
    result = _run_hook(
        tmp_path,
        "http://127.0.0.1:9",
        "investigate coherence gate shadow soak results",
        "slot-d",
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    # One network attempt per slot even on failure: marker written.
    assert _marker(tmp_path, "slot-d").exists()
