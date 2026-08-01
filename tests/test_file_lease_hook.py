from __future__ import annotations

import json
import shutil
import socketserver
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from scripts import file_lease_hook


class _ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


class LeaseHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    acquire_response: dict = {}

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        self.__class__.calls.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
                "body": body,
            }
        )
        if self.path == "/v1/lease/acquire":
            payload = self.__class__.acquire_response
            status = 409 if payload.get("error") == "held_by_other" else 200
        elif self.path in {"/v1/lease/heartbeat", "/v1/lease/release"}:
            payload = {"ok": True, "protocol_version": "v1.0"}
            status = 200
        else:
            payload = {"ok": False, "error": "not_found"}
            status = 404
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):  # noqa: A002
        return


@pytest.fixture
def lease_server():
    LeaseHandler.calls = []
    LeaseHandler.acquire_response = {
        "ok": True,
        "lease": {
            "lease_id": "11111111-1111-4111-8111-111111111111",
            "surface_id": "file:///tmp/example.py",
            "expires_at": "2026-05-28T00:00:00Z",
        },
        "idempotent": False,
        "protocol_version": "v1.0",
    }
    srv = _ReusableTCPServer(("127.0.0.1", 0), LeaseHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        thread.join(timeout=2)


def _payload(
    slot: str = "slot-1",
    path: str = "a.py",
    tool_use_id: str = "",
) -> str:
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": slot,
        "tool_name": "Edit",
        "tool_input": {"file_path": path},
    }
    if tool_use_id:
        payload["tool_use_id"] = tool_use_id
    return json.dumps(payload)


def _codex_payload(
    *,
    slot: str = "codex-slot",
    tool_use_id: str = "call_1",
    paths: tuple[str, ...] = ("a.py",),
) -> str:
    directives = "\n".join(f"*** Update File: {path}" for path in paths)
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": slot,
            "tool_name": "apply_patch",
            "tool_use_id": tool_use_id,
            "tool_input": {"command": f"*** Begin Patch\n{directives}\n*** End Patch"},
        }
    )


def _lease_env(monkeypatch, lease_server):
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", f"http://127.0.0.1:{lease_server.server_address[1]}")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")


def test_pre_edit_acquires_file_lease_and_records_state(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    tool_use_id = "toolu_acquire"

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id=tool_use_id),
    )

    assert rc == 0
    acquire = LeaseHandler.calls[0]
    assert acquire["path"] == "/v1/lease/acquire"
    assert acquire["authorization"] == "Bearer lease-token"
    assert acquire["body"]["surface_id"] == f"file://{tmp_path / 'a.py'}"
    assert acquire["body"]["holder_kind"] == "remote_heartbeat"

    state = json.loads(
        file_lease_hook._state_path(tmp_path, "slot-1", tool_use_id).read_text()
    )
    assert state["version"] == 1
    assert state["leases"]["file:///tmp/example.py"]["lease_id"] == "11111111-1111-4111-8111-111111111111"


def test_pre_edit_blocks_on_held_by_other(tmp_path, monkeypatch, lease_server, capsys):
    _lease_env(monkeypatch, lease_server)
    LeaseHandler.acquire_response = {
        "ok": False,
        "error": "held_by_other",
        "surface_id": f"file://{tmp_path / 'a.py'}",
        "blocking_lease_id": "22222222-2222-4222-8222-222222222222",
        "held_by_uuid": "33333333-3333-4333-8333-333333333333",
        "expires_at": "2026-05-28T00:01:00Z",
        "retry_after_hint_ms": 1000,
        "protocol_version": "v1.0",
    }

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id="toolu_conflict"),
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "BLOCKED: file lease held by another agent" in err
    # The block message must tell the operator the lease self-heals, so they
    # don't reflexively force-release a lease that would clear on its own.
    assert "self-heals" in err
    assert not list((tmp_path / ".unitares").glob("file-leases-*.json"))


def test_pre_edit_missing_token_fails_open_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("UNITARES_LEASE_PLANE_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("GOVERNANCE_TOKEN", raising=False)
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(tmp_path / "missing.env"))

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id="toolu_missing_token"),
    )

    assert rc == 0
    assert not (tmp_path / ".unitares").exists()


def test_disabled_leases_remain_disabled_when_not_required(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITARES_FILE_LEASES_ENABLED", "0")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "0")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")

    def unexpected_http(*args, **kwargs):
        pytest.fail("disabled lease guard attempted an HTTP request")

    monkeypatch.setattr(file_lease_hook, "_http_json", unexpected_http)

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id="toolu_disabled"),
    )

    assert rc == 0
    assert not (tmp_path / ".unitares").exists()


def test_required_leases_override_disabled_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UNITARES_FILE_LEASES_ENABLED", "0")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "YES")
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("UNITARES_LEASE_PLANE_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("GOVERNANCE_TOKEN", raising=False)
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(tmp_path / "missing.env"))

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id="toolu_required_disabled"),
    )

    assert rc == 2
    assert "missing LEASE_PLANE_BEARER_TOKEN" in capsys.readouterr().err


def test_heartbeat_session_is_compatibility_noop(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    lease_dir = tmp_path / ".unitares"
    lease_dir.mkdir()
    (lease_dir / "file-leases-slot-1.json").write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "slot-1",
                "workspace": str(tmp_path),
                "holder_uuid": "33333333-3333-4333-8333-333333333333",
                "leases": {
                    "file:///tmp/example.py": {
                        "lease_id": "11111111-1111-4111-8111-111111111111",
                        "path": "a.py",
                        "surface_id": "file:///tmp/example.py",
                    }
                },
            }
        )
    )

    rc = file_lease_hook.main(["heartbeat-session", "--workspace", str(tmp_path)], stdin_text=_payload())

    assert rc == 0
    assert LeaseHandler.calls == []
    assert (lease_dir / "file-leases-slot-1.json").exists()


def test_release_session_releases_and_removes_state(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    lease_dir = tmp_path / ".unitares"
    lease_dir.mkdir()
    state_path = lease_dir / "file-leases-slot-1.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "slot-1",
                "workspace": str(tmp_path),
                "holder_uuid": "33333333-3333-4333-8333-333333333333",
                "leases": {
                    "file:///tmp/example.py": {
                        "lease_id": "11111111-1111-4111-8111-111111111111",
                        "path": "a.py",
                        "surface_id": "file:///tmp/example.py",
                    }
                },
            }
        )
    )

    rc = file_lease_hook.main(["release-session", "--workspace", str(tmp_path)], stdin_text=_payload())

    assert rc == 0
    assert [call["path"] for call in LeaseHandler.calls] == ["/v1/lease/release"]
    assert not state_path.exists()


def test_release_edit_releases_only_edited_file_and_keeps_others(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    lease_dir = tmp_path / ".unitares"
    lease_dir.mkdir()
    tool_use_id = "toolu_release"
    state_path = file_lease_hook._state_path(tmp_path, "slot-1", tool_use_id)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "slot-1",
                "tool_use_id": tool_use_id,
                "workspace": str(tmp_path),
                "holder_uuid": "33333333-3333-4333-8333-333333333333",
                "leases": {
                    "file:///tmp/a.py": {
                        "lease_id": "11111111-1111-4111-8111-111111111111",
                        "path": "a.py",
                        "surface_id": "file:///tmp/a.py",
                        "tool_use_id": tool_use_id,
                    },
                    "file:///tmp/b.py": {
                        "lease_id": "22222222-2222-4222-8222-222222222222",
                        "path": "b.py",
                        "surface_id": "file:///tmp/b.py",
                        "tool_use_id": tool_use_id,
                    },
                },
            }
        )
    )

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(path="a.py", tool_use_id=tool_use_id),
    )

    assert rc == 0
    # Only the edited file's lease is released.
    assert [call["path"] for call in LeaseHandler.calls] == ["/v1/lease/release"]
    assert LeaseHandler.calls[0]["body"]["lease_id"] == "11111111-1111-4111-8111-111111111111"
    # State keeps the still-held b.py lease, drops a.py.
    state = json.loads(state_path.read_text())
    assert "file:///tmp/a.py" not in state["leases"]
    assert "file:///tmp/b.py" in state["leases"]


def test_release_edit_noop_when_file_not_leased(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    lease_dir = tmp_path / ".unitares"
    lease_dir.mkdir()
    tool_use_id = "toolu_unleased"
    file_lease_hook._state_path(tmp_path, "slot-1", tool_use_id).write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "slot-1",
                "tool_use_id": tool_use_id,
                "workspace": str(tmp_path),
                "holder_uuid": "33333333-3333-4333-8333-333333333333",
                "leases": {
                    "file:///tmp/b.py": {
                        "lease_id": "22222222-2222-4222-8222-222222222222",
                        "path": "b.py",
                        "surface_id": "file:///tmp/b.py",
                        "tool_use_id": tool_use_id,
                    }
                },
            }
        )
    )

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(path="a.py", tool_use_id=tool_use_id),
    )

    assert rc == 0
    assert LeaseHandler.calls == []  # nothing released for an unleased file


def test_release_edit_adopts_exact_legacy_session_state(
    tmp_path, monkeypatch, lease_server
):
    _lease_env(monkeypatch, lease_server)
    legacy_path = file_lease_hook._state_path(tmp_path, "slot-1")
    legacy_path.parent.mkdir()
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "slot-1",
                "workspace": str(tmp_path),
                "leases": {
                    "file:///tmp/a.py": {
                        "lease_id": "11111111-1111-4111-8111-111111111111",
                        "path": "a.py",
                        "surface_id": "file:///tmp/a.py",
                    }
                },
            }
        )
    )

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=_payload(path="a.py", tool_use_id="toolu_after_upgrade"),
    )

    assert rc == 0
    assert [call["path"] for call in LeaseHandler.calls] == ["/v1/lease/release"]
    assert not legacy_path.exists()


def test_unknown_state_version_is_preserved(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    tool_use_id = "toolu_future_state"
    state_path = file_lease_hook._state_path(tmp_path, "slot-1", tool_use_id)
    state_path.parent.mkdir()
    original = {
        "version": 99,
        "slot": "slot-1",
        "leases": {
            "file:///tmp/a.py": {"lease_id": "future-lease", "path": "a.py"}
        },
    }
    state_path.write_text(json.dumps(original, sort_keys=True))
    before = state_path.read_text()

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=_payload(path="a.py", tool_use_id=tool_use_id),
    )

    assert rc == 0
    assert LeaseHandler.calls == []
    assert state_path.read_text() == before


def test_claude_missing_tool_id_never_acquires_or_releases_shared_state(
    tmp_path, monkeypatch, lease_server
):
    _lease_env(monkeypatch, lease_server)
    args = ["pre-edit", "--workspace", str(tmp_path), "--host", "claude"]
    payload = _payload()

    assert file_lease_hook.main(args, stdin_text=payload) == 0
    assert file_lease_hook.main(args, stdin_text=payload) == 0
    assert file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=payload,
    ) == 0

    assert LeaseHandler.calls == []
    assert not (tmp_path / ".unitares").exists()


def test_claude_missing_tool_id_blocks_when_leases_are_required(
    tmp_path, monkeypatch, lease_server, capsys
):
    _lease_env(monkeypatch, lease_server)
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=_payload(),
    )

    assert rc == 2
    assert "missing tool_use_id" in capsys.readouterr().err
    assert LeaseHandler.calls == []


def test_codex_patch_acquires_all_paths_in_deterministic_order(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    calls = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        calls.append((path, body, timeout_s))
        surface = body["surface_id"]
        return {
            "ok": True,
            "lease": {
                "lease_id": f"lease-{len(calls)}",
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    payload = _codex_payload(paths=("z.py", "a.py"))

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=payload,
    )

    assert rc == 0
    acquired = [body["surface_id"] for path, body, _ in calls if path.endswith("/acquire")]
    assert acquired == [f"file://{tmp_path / 'a.py'}", f"file://{tmp_path / 'z.py'}"]
    state_path = file_lease_hook._state_path(tmp_path, "codex-slot", "call_1")
    state = json.loads(state_path.read_text())
    assert len(state["leases"]) == 2
    assert {row["tool_use_id"] for row in state["leases"].values()} == {"call_1"}
    assert len({row["holder_uuid"] for row in state["leases"].values()}) == 1


def test_codex_batch_conflict_rolls_back_new_leases(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    calls = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        calls.append((path, body))
        if path.endswith("/release"):
            return {"ok": True}
        surface = body["surface_id"]
        if surface.endswith("/b.py"):
            return {
                "ok": False,
                "error": "held_by_other",
                "surface_id": surface,
                "blocking_lease_id": "other-lease",
                "held_by_uuid": "other-agent",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        return {
            "ok": True,
            "lease": {
                "lease_id": "new-lease-a",
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    payload = _codex_payload(paths=("a.py", "b.py"), tool_use_id="call_conflict")

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=payload,
    )

    assert rc == 2
    assert "held by another agent" in capsys.readouterr().err
    assert [path for path, _ in calls] == [
        "/v1/lease/acquire",
        "/v1/lease/acquire",
        "/v1/lease/release",
    ]
    assert calls[-1][1]["lease_id"] == "new-lease-a"
    assert not file_lease_hook._state_path(
        tmp_path, "codex-slot", "call_conflict"
    ).exists()


def test_acquire_persistence_failure_releases_and_applies_required_policy(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    calls = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        calls.append((path, body))
        if path.endswith("/release"):
            return {"ok": True}
        return {
            "ok": True,
            "lease": {
                "lease_id": "lease-without-state",
                "surface_id": body["surface_id"],
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    def fail_save(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    monkeypatch.setattr(file_lease_hook, "_save_state", fail_save)

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=_codex_payload(tool_use_id="call_save_failure"),
    )

    assert rc == 2
    assert "lease state persistence failed after acquire" in capsys.readouterr().err
    assert [path for path, _ in calls] == [
        "/v1/lease/acquire",
        "/v1/lease/release",
    ]
    assert calls[-1][1]["lease_id"] == "lease-without-state"
    assert not file_lease_hook._state_path(
        tmp_path, "codex-slot", "call_save_failure"
    ).exists()


def test_codex_batch_conflict_rolls_back_persisted_event_lease(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    calls = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        calls.append((path, body))
        if path.endswith("/heartbeat"):
            return {"ok": True}
        if path.endswith("/release"):
            return {"ok": True}
        surface = body["surface_id"]
        if surface.endswith("/b.py"):
            return {
                "ok": False,
                "error": "held_by_other",
                "surface_id": surface,
                "blocking_lease_id": "other-lease",
                "held_by_uuid": "other-agent",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        return {
            "ok": True,
            "lease": {
                "lease_id": "persisted-lease-a",
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    args = ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"]
    tool_use_id = "call_retry"

    assert file_lease_hook.main(
        args,
        stdin_text=_codex_payload(paths=("a.py",), tool_use_id=tool_use_id),
    ) == 0
    calls.clear()

    rc = file_lease_hook.main(
        args,
        stdin_text=_codex_payload(paths=("a.py", "b.py"), tool_use_id=tool_use_id),
    )

    assert rc == 2
    assert "held by another agent" in capsys.readouterr().err
    assert [path for path, _ in calls] == [
        "/v1/lease/heartbeat",
        "/v1/lease/acquire",
        "/v1/lease/release",
    ]
    assert calls[-1][1]["lease_id"] == "persisted-lease-a"
    assert not file_lease_hook._state_path(
        tmp_path, "codex-slot", tool_use_id
    ).exists()


def test_codex_tool_use_ids_are_distinct_lease_holders(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    holder_by_surface = {}
    acquire_holders = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        if path.endswith("/release"):
            return {"ok": True}
        surface = body["surface_id"]
        holder = body["holder_agent_uuid"]
        acquire_holders.append(holder)
        if surface in holder_by_surface and holder_by_surface[surface] != holder:
            return {
                "ok": False,
                "error": "held_by_other",
                "surface_id": surface,
                "blocking_lease_id": "first-lease",
                "held_by_uuid": holder_by_surface[surface],
                "expires_at": "2099-01-01T00:00:00Z",
            }
        holder_by_surface[surface] = holder
        return {
            "ok": True,
            "lease": {
                "lease_id": "first-lease",
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    args = ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"]

    assert file_lease_hook.main(args, stdin_text=_codex_payload(tool_use_id="call_a")) == 0
    assert file_lease_hook.main(args, stdin_text=_codex_payload(tool_use_id="call_b")) == 2
    assert len(set(acquire_holders)) == 2
    assert file_lease_hook._state_path(tmp_path, "codex-slot", "call_a").exists()
    assert not file_lease_hook._state_path(tmp_path, "codex-slot", "call_b").exists()


def test_claude_sync_release_allows_next_tool_holder(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    holder_by_surface = {}
    lease_surface = {}
    acquire_holders = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        if path.endswith("/release"):
            surface = lease_surface.pop(body["lease_id"], "")
            holder_by_surface.pop(surface, None)
            return {"ok": True}
        surface = body["surface_id"]
        holder = body["holder_agent_uuid"]
        acquire_holders.append(holder)
        prior = holder_by_surface.get(surface)
        if prior is not None and prior != holder:
            return {
                "ok": False,
                "error": "held_by_other",
                "surface_id": surface,
                "blocking_lease_id": "prior-lease",
                "held_by_uuid": prior,
                "expires_at": "2099-01-01T00:00:00Z",
            }
        holder_by_surface[surface] = holder
        lease_id = f"lease-{len(acquire_holders)}"
        lease_surface[lease_id] = surface
        return {
            "ok": True,
            "lease": {
                "lease_id": lease_id,
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    args = ["pre-edit", "--workspace", str(tmp_path), "--host", "claude"]

    assert file_lease_hook.main(
        args,
        stdin_text=_payload(tool_use_id="toolu_1"),
    ) == 0
    assert file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=_payload(tool_use_id="toolu_1"),
    ) == 0
    assert file_lease_hook.main(
        args,
        stdin_text=_payload(tool_use_id="toolu_2"),
    ) == 0
    assert len(set(acquire_holders)) == 2


def test_session_end_discovers_and_releases_per_tool_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    released = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        if path.endswith("/release"):
            released.append(body["lease_id"])
            return {"ok": True}
        surface = body["surface_id"]
        return {
            "ok": True,
            "lease": {
                "lease_id": f"lease-{Path(surface).name}",
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    args = ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"]
    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(tool_use_id="call_a", paths=("a.py",))
    ) == 0
    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(tool_use_id="call_b", paths=("b.py",))
    ) == 0

    rc = file_lease_hook.main(
        ["release-session", "--workspace", str(tmp_path)],
        stdin_text=json.dumps({"session_id": "codex-slot"}),
    )

    assert rc == 0
    assert sorted(released) == ["lease-a.py", "lease-b.py"]
    assert file_lease_hook._state_paths(tmp_path, "codex-slot") == []


def test_claude_batch_releases_only_completed_edit_tool_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    slot = "shared-driver-slot"

    def seed(tool_use_id: str, lease_id: str, path: str) -> Path:
        state_path = file_lease_hook._state_path(tmp_path, slot, tool_use_id)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "slot": slot,
                    "workspace": str(tmp_path),
                    "tool_use_id": tool_use_id,
                    "leases": {
                        f"file://{tmp_path / path}": {
                            "lease_id": lease_id,
                            "path": path,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return state_path

    completed = seed("toolu_completed", "lease-completed", "done.py")
    still_running = seed("toolu_background", "lease-background", "active.py")
    released: list[str] = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        assert method == "POST"
        assert path == "/v1/lease/release"
        released.append(body["lease_id"])
        return {"ok": True}

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    payload = json.dumps(
        {
            "hook_event_name": "PostToolBatch",
            "session_id": slot,
            "tool_calls": [
                {"tool_name": "Write", "tool_use_id": "toolu_completed"},
                # A sibling may share the session id but is not proven complete
                # by this batch's edit-call set.
                {"tool_name": "Read", "tool_use_id": "toolu_background"},
            ],
        }
    )

    rc = file_lease_hook.main(
        ["release-batch", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=payload,
    )

    assert rc == 0
    assert released == ["lease-completed"]
    assert not completed.exists()
    assert still_running.exists()
    assert json.loads(still_running.read_text(encoding="utf-8"))["leases"]


def test_state_discovery_rejects_sanitized_slot_collision(tmp_path):
    slot_a = "session?one"
    slot_b = "session!one"
    assert file_lease_hook._safe_slot(slot_a) == file_lease_hook._safe_slot(slot_b)
    assert file_lease_hook._state_path(tmp_path, slot_a) != file_lease_hook._state_path(
        tmp_path, slot_b
    )

    legacy = file_lease_hook._legacy_state_path(tmp_path, slot_b, "call_b")
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"version": 1, "slot": slot_b, "leases": {}}))

    assert legacy not in file_lease_hook._state_paths(tmp_path, slot_a)
    assert legacy in file_lease_hook._state_paths(tmp_path, slot_b)


def test_codex_post_edit_releases_every_patch_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    tool_use_id = "call_release_all"
    state_path = file_lease_hook._state_path(tmp_path, "codex-slot", tool_use_id)
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "codex-slot",
                "workspace": str(tmp_path),
                "tool_use_id": tool_use_id,
                "leases": {
                    f"file://{tmp_path / 'a.py'}": {
                        "lease_id": "lease-a",
                        "path": "a.py",
                        "surface_id": f"file://{tmp_path / 'a.py'}",
                    },
                    f"file://{tmp_path / 'b.py'}": {
                        "lease_id": "lease-b",
                        "path": "b.py",
                        "surface_id": f"file://{tmp_path / 'b.py'}",
                    },
                },
            }
        )
    )
    released = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        assert path == "/v1/lease/release"
        released.append(body["lease_id"])
        return {"ok": True}

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=_codex_payload(tool_use_id=tool_use_id, paths=("b.py", "a.py")),
    )

    assert rc == 0
    assert released == ["lease-b", "lease-a"]
    assert not state_path.exists()


def test_hooks_json_wires_pretooluse_edit_guard():
    config = json.loads((Path(__file__).parent.parent / "hooks" / "hooks.json").read_text())

    pre_hooks = config["hooks"]["PreToolUse"]
    assert pre_hooks[0]["matcher"] == "Edit|Write|MultiEdit"
    assert "pre-edit" in pre_hooks[0]["hooks"][0]["command"]


# ---------------------------------------------------------------------------
# _surface_id: worktrees of one repo must collapse to ONE surface so concurrent
# agents in different worktrees see each other's file lease.
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_surface_id_collapses_worktrees_to_one_surface(tmp_path):
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "src").mkdir()
    (main / "src" / "f.py").write_text("x\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", str(wt), "HEAD", cwd=main)
    (wt / "src" / "f.py").write_text("x\n")

    s_main = file_lease_hook._surface_id("src/f.py", main)
    s_wt = file_lease_hook._surface_id("src/f.py", wt)

    # Same logical file in two worktrees -> identical surface (the collision fix)
    assert s_main == s_wt
    # And it canonicalizes onto the main checkout, not the worktree dir
    assert str(main.resolve()) in s_main
    assert "/wt/" not in s_wt


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_surface_id_collapses_nonexistent_add_path_across_worktrees(tmp_path):
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "seed.txt").write_text("seed\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", str(wt), "HEAD", cwd=main)

    relative = "not-created-yet/deep/new.py"
    s_main = file_lease_hook._surface_id(relative, main)
    s_wt = file_lease_hook._surface_id(relative, wt)

    assert s_main == s_wt
    assert str(main.resolve()) in s_main


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_surface_id_uses_target_roots_for_absolute_sibling_worktree_path(tmp_path):
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "src").mkdir()
    (main / "src" / "f.py").write_text("x\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    sibling = tmp_path / "sibling-wt"
    _git("worktree", "add", "-q", str(sibling), "HEAD", cwd=main)

    cached_roots = file_lease_hook._worktree_roots(main)
    s_main = file_lease_hook._surface_id(str(main / "src" / "f.py"), main, cached_roots)
    s_sibling = file_lease_hook._surface_id(
        str(sibling / "src" / "f.py"), main, cached_roots
    )

    assert s_main == s_sibling
    assert str(main.resolve()) in s_sibling
    assert str(sibling.resolve()) not in s_sibling


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_surface_id_keeps_separate_git_dirs_distinct(tmp_path):
    surfaces = []
    for name in ("one", "two"):
        checkout = tmp_path / name
        metadata = tmp_path / f"{name}.gitdata"
        _git(
            "init",
            "-q",
            "--separate-git-dir",
            str(metadata),
            str(checkout),
            cwd=tmp_path,
        )
        (checkout / "src").mkdir()
        (checkout / "src" / "f.py").write_text(name)
        surfaces.append(file_lease_hook._surface_id("src/f.py", checkout))

    assert surfaces[0] != surfaces[1]
    assert "one.gitdata" in surfaces[0]
    assert "two.gitdata" in surfaces[1]


def test_pre_edit_path_setup_obeys_shared_batch_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    clock = [0.0]
    root_calls = []

    def monotonic():
        return clock[0]

    def slow_roots(start, *, timeout_s=0.75):
        root_calls.append((Path(start), timeout_s))
        clock[0] += timeout_s
        return None

    def unexpected_http(*args, **kwargs):
        raise AssertionError("lease HTTP must not start after path setup exhausts its budget")

    monkeypatch.setattr(file_lease_hook.time, "monotonic", monotonic)
    monkeypatch.setattr(file_lease_hook, "_worktree_roots", slow_roots)
    monkeypatch.setattr(file_lease_hook, "_http_json", unexpected_http)
    paths = tuple(f"/outside/repo-{index}/f.py" for index in range(256))

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=_codex_payload(paths=paths, tool_use_id="call_deadline"),
    )

    assert rc == 0
    assert clock[0] <= file_lease_hook.DEFAULT_BATCH_TIMEOUT_S
    assert len(root_calls) < 10


def test_release_edit_path_setup_obeys_codex_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    tool_use_id = "call_release_deadline"
    paths = tuple(f"/outside/repo-{index}/f.py" for index in range(256))
    state_path = file_lease_hook._state_path(tmp_path, "codex-slot", tool_use_id)
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "codex-slot",
                "tool_use_id": tool_use_id,
                "leases": {
                    f"file://legacy/{index}": {"lease_id": f"lease-{index}"}
                    for index in range(256)
                },
            }
        )
    )
    clock = [0.0]
    root_calls = []

    def monotonic():
        return clock[0]

    def slow_roots(start, *, timeout_s=0.75):
        root_calls.append((Path(start), timeout_s))
        clock[0] += timeout_s
        return None

    def unexpected_http(*args, **kwargs):
        raise AssertionError("release HTTP must not start after path setup exhausts its budget")

    monkeypatch.setattr(file_lease_hook.time, "monotonic", monotonic)
    monkeypatch.setattr(file_lease_hook, "_worktree_roots", slow_roots)
    monkeypatch.setattr(file_lease_hook, "_http_json", unexpected_http)

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=_codex_payload(paths=paths, tool_use_id=tool_use_id),
    )

    assert rc == 0
    assert clock[0] <= 1.5
    assert len(root_calls) <= 2


def test_release_session_discovery_obeys_codex_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASE_BATCH_TIMEOUT_S", "0.3")
    slot = "codex-many-states"
    state_dir = tmp_path / ".unitares"
    state_dir.mkdir()
    for index in range(50):
        file_lease_hook._state_path(tmp_path, slot, f"call-{index}").write_text(
            json.dumps(
                {
                    "version": 1,
                    "slot": slot,
                    "tool_use_id": f"call-{index}",
                    "leases": {
                        f"file://repo/f-{index}.py": {"lease_id": f"lease-{index}"}
                    },
                }
            )
        )

    clock = [0.0]
    reads = []
    original_read_json = file_lease_hook._read_json

    def monotonic():
        return clock[0]

    def slow_read_json(path):
        reads.append(path)
        clock[0] += 0.11
        return original_read_json(path)

    def unexpected_release(*args, **kwargs):
        raise AssertionError("release HTTP must not start after discovery exhausts its budget")

    monkeypatch.setattr(file_lease_hook.time, "monotonic", monotonic)
    monkeypatch.setattr(file_lease_hook, "_read_json", slow_read_json)
    monkeypatch.setattr(file_lease_hook, "_release", unexpected_release)

    rc = file_lease_hook.main(
        ["release-session", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=json.dumps({"session_id": slot}),
    )

    assert rc == 0
    assert len(reads) < 50
    assert clock[0] <= 0.4
    assert len(list(state_dir.glob("file-leases-*.json"))) == 50


def test_release_state_lock_wait_is_bounded_by_cleanup_deadline(tmp_path, monkeypatch):
    state_path = file_lease_hook._state_path(tmp_path, "slot-lock", "call-lock")
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "slot-lock",
                "tool_use_id": "call-lock",
                "leases": {"file://repo/a.py": {"lease_id": "lease-lock"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        file_lease_hook,
        "_release",
        lambda *args, **kwargs: pytest.fail("HTTP release started while state was locked"),
    )

    with file_lease_hook.session_cache_lock(state_path):
        started = time.monotonic()
        deadline_reached = file_lease_hook._release_state_file(
            state_path,
            token="lease-token",
            deadline=started + 0.08,
            expected_slot="slot-lock",
        )
        elapsed = time.monotonic() - started

    assert deadline_reached is True
    assert elapsed < 0.25
    assert state_path.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_surface_id_fail_open_outside_git(tmp_path):
    # Not a git repo -> degrade to the raw absolute path (never break an edit).
    s = file_lease_hook._surface_id("a/b.py", tmp_path)
    assert s == f"file://{tmp_path / 'a' / 'b.py'}"
