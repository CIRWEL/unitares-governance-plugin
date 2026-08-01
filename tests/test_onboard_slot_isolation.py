"""Tests that two slots in the same workspace get distinct identities.

The regression fixed here: commit ``29a3a77`` made the plugin's client cache
per-slot, but the plugin still sent the same ``name`` on onboard. The server
resolves identity by label in ``resolve_by_name_claim`` without session-key
scoping, so both slots got bound to the same existing agent.

The fix appends a short slot fingerprint to the agent name when a slot is
provided. These tests pin that behavior — both the client-side mechanics
(what the plugin sends) and, when the governance server is reachable, the
end-to-end promise that different slots yield different UUIDs.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from onboard_helper import (  # noqa: E402
    BOOTSTRAP_RESPONSE_TEXT,
    _default_bootstrap_state,
    _post_json,
    _scope_name_by_slot,
    run_onboard,
)


# ---- Unit tests on the scoping helper itself -------------------------------


def test_scope_name_noop_when_no_slot() -> None:
    assert _scope_name_by_slot("cirwel", None) == "cirwel"
    assert _scope_name_by_slot("cirwel", "") == "cirwel"


def test_scope_name_appends_slot_fingerprint() -> None:
    scoped = _scope_name_by_slot("cirwel", "1d118271-7384-42cb-adce-c4f4b314e089")
    assert scoped.startswith("cirwel#")
    # Fingerprint is an 8-hex-char hash of the full slot — enough entropy
    # to avoid collisions even when two slots share a prefix. Exact value
    # is pinned so we catch accidental changes to the hashing scheme.
    assert scoped == "cirwel#74608bf3"


def test_scope_name_is_collision_resistant_on_prefix_overlap() -> None:
    """Regression: slots with identical first-8 chars must still produce
    distinct fingerprints. Earlier iteration used slot[:8] and collapsed
    all slots sharing a prefix (e.g. CI runners stamping "runner-N-*")
    into the same label, which put us right back at the original bug."""
    a = _scope_name_by_slot("w", "itest-slot-aaaa1111")
    b = _scope_name_by_slot("w", "itest-slot-bbbb2222")
    assert a != b


def test_scope_name_stable_across_calls() -> None:
    a = _scope_name_by_slot("w", "50346241-6659")
    b = _scope_name_by_slot("w", "50346241-6659")
    assert a == b


def test_different_slots_produce_different_scoped_names() -> None:
    a = _scope_name_by_slot("w", "1d118271-aaa")
    b = _scope_name_by_slot("w", "50346241-bbb")
    assert a != b


# ---- Behavior tests on run_onboard with injected transport -----------------


class _FakeTransport:
    """Record every outbound request so tests can assert on what was sent."""

    def __init__(self, response: dict[str, Any]):
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, payload: dict, timeout: float, token: str | None) -> dict:
        self.calls.append({"url": url, "payload": payload, "token": token})
        return self._response


def _onboard_ok_response(uuid: str, display_name: str) -> dict:
    return {
        "result": {
            "success": True,
            "uuid": uuid,
            "agent_id": f"Claude_Code_{uuid[:8]}",
            "client_session_id": f"agent-{uuid[:12]}",
            "continuity_token": f"token-{uuid}",
            "session_resolution_source": "explicit_client_session_id_scoped",
            "continuity_token_supported": True,
            "display_name": display_name,
        }
    }


def test_transport_error_returns_structured_failure(monkeypatch: Any) -> None:
    def deny_network(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.URLError(PermissionError(1, "Operation not permitted"))

    monkeypatch.setattr("onboard_helper.authorization_safe_urlopen", deny_network)

    result = _post_json(
        "http://unit-test/v1/tools/call",
        {"name": "onboard", "arguments": {"force_new": True}},
        timeout=1,
        token=None,
    )

    assert result["success"] is False
    assert result["error"] == "onboard transport request failed"
    assert result["recovery"]["reason"] == "transport_error"
    assert "sandboxed clients may need network permission" in result["recovery"]["hint"]
    assert "Operation not permitted" in result["detail"]


def test_unslotted_onboard_sends_bare_name_and_force_new(tmp_path: Path) -> None:
    """Codex / stdio flows must not have their agent names rewritten — only
    slotted callers need the scoping, and changing the name silently for
    single-process flows would be a surprise regression."""
    transport = _FakeTransport(_onboard_ok_response("aaaa1111-0000-0000-0000-000000000000", "cirwel"))

    result = run_onboard(
        server_url="http://unit-test",
        agent_name="cirwel",
        model_type="claude-code",
        workspace=tmp_path,
        slot=None,
        post_json=transport,
    )

    assert result["status"] == "ok"
    sent_args = transport.calls[0]["payload"]["arguments"]
    assert sent_args["name"] == "cirwel"
    assert sent_args["force_new"] is True
    assert "parent_agent_id" not in sent_args


def test_cache_file_is_mode_0600_and_omits_continuity_token(tmp_path: Path) -> None:
    """S20.3: cache file must be owner-only (0600) and must not persist
    continuity_token / continuity_token_supported.

    Default Path.write_text inherits umask 022 → mode 0644 (world-readable
    on a typical macOS setup). Even after S1-a narrows continuity_token,
    client_session_id is still process-instance identity, so same-UID
    readability remains a siphon surface — atomic 0600 write closes it.

    The token fields stay in the in-process return value (transient) so a
    caller can use them within the same process; lineage across
    process-instances is declared via parent_agent_id (S1-a / identity.md
    v2 ontology), not resumed via cached token.
    """
    transport = _FakeTransport(_onboard_ok_response("cccc3333-0000-0000-0000-000000000000", "cirwel"))

    result = run_onboard(
        server_url="http://unit-test",
        agent_name="cirwel",
        model_type="claude-code",
        workspace=tmp_path,
        slot=None,
        post_json=transport,
    )

    assert result["status"] == "ok"
    cache_file = tmp_path / ".unitares" / "session.json"
    assert cache_file.exists()
    assert cache_file.with_suffix(".lock").exists()
    mode = stat.S_IMODE(os.stat(cache_file).st_mode)
    assert mode == 0o600, f"expected mode 0600, got {oct(mode)}"

    written = json.loads(cache_file.read_text())
    assert "continuity_token" not in written
    assert "continuity_token_supported" not in written
    # Result still carries the transient token from the server response.
    assert result["continuity_token"]
    assert result["continuity_token_supported"] is True


def test_write_failure_does_not_leave_tmp_file(tmp_path: Path, monkeypatch: Any) -> None:
    """S20.3: a failed atomic write unlinks the temp file rather than
    leaving a .tmp turd in ``.unitares/``."""
    import _session_cache_io
    import onboard_helper

    def boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(_session_cache_io.os, "replace", boom)
    with pytest.raises(OSError):
        onboard_helper._write_cache(tmp_path, {"uuid": "x"}, slot=None)

    cache_dir = tmp_path / ".unitares"
    if cache_dir.exists():
        stragglers = [p for p in cache_dir.iterdir() if p.suffix == ".tmp"]
        assert stragglers == [], f"temp file leaked: {stragglers}"


def test_onboard_writer_mirrors_slot_and_removes_stale_mirror_on_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    import _session_cache_io
    import onboard_helper

    workspace = tmp_path / "workspace"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    slot = "mirrored-onboard"
    old = {"uuid": "old", "client_session_id": "agent-old"}
    new = {"uuid": "new", "client_session_id": "agent-new"}
    onboard_helper._write_cache(workspace, old, slot)
    home_path = fake_home / ".unitares" / f"session-{slot}.json"
    assert json.loads(home_path.read_text(encoding="utf-8")) == old

    real_write = _session_cache_io.write_json_dict_unlocked

    def fail_home(path: Path, payload: dict[str, Any]) -> None:
        if path == home_path:
            raise OSError("simulated mirror failure")
        real_write(path, payload)

    monkeypatch.setattr(_session_cache_io, "write_json_dict_unlocked", fail_home)
    onboard_helper._write_cache(workspace, new, slot)

    workspace_path = workspace / ".unitares" / f"session-{slot}.json"
    assert json.loads(workspace_path.read_text(encoding="utf-8")) == new
    assert not home_path.exists()


def test_clear_from_empty_supersedes_inflight_lazy_onboard(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    import onboard_helper
    import session_cache

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    called = threading.Event()
    release = threading.Event()
    result: dict[str, Any] = {}
    slot = "clear-empty-race"

    def blocked_transport(*_args: Any) -> dict:
        called.set()
        assert release.wait(timeout=5)
        return _onboard_ok_response(
            "aaaaaaaa-0000-4000-8000-000000000099",
            "late",
        )

    def lazy_onboard() -> None:
        result.update(
            run_onboard(
                server_url="http://unit-test",
                agent_name="late",
                model_type="claude-code",
                workspace=tmp_path,
                slot=slot,
                post_json=blocked_transport,
            )
        )

    worker = threading.Thread(target=lazy_onboard)
    worker.start()
    assert called.wait(timeout=5)
    assert session_cache.cmd_clear(
        argparse.Namespace(kind="session", workspace=str(tmp_path), slot=slot)
    ) == 0
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert result["status"] == "onboard_superseded"
    assert onboard_helper._read_cache(tmp_path, slot) == {}
    assert not (fake_home / ".unitares" / f"session-{slot}.json").exists()
    generation = tmp_path / ".unitares" / f"session-{slot}.generation"
    assert int(generation.read_text(encoding="ascii")) >= 1


def test_inflight_lazy_onboard_preserves_newer_explicit_identity(tmp_path: Path) -> None:
    """An async lazy onboard must not overwrite an identity hook's later choice."""
    import onboard_helper

    called = threading.Event()
    release = threading.Event()
    result: dict[str, Any] = {}
    slot = "race-slot"

    def blocked_transport(*_args: Any) -> dict:
        called.set()
        assert release.wait(timeout=5)
        return _onboard_ok_response(
            "aaaaaaaa-0000-4000-8000-000000000001",
            "lazy",
        )

    def lazy_onboard() -> None:
        result.update(
            run_onboard(
                server_url="http://unit-test",
                agent_name="lazy",
                model_type="claude-code",
                workspace=tmp_path,
                slot=slot,
                post_json=blocked_transport,
            )
        )

    worker = threading.Thread(target=lazy_onboard)
    worker.start()
    assert called.wait(timeout=5)
    explicit = {
        "uuid": "bbbbbbbb-0000-4000-8000-000000000002",
        "agent_id": "explicit-agent",
        "client_session_id": "agent-explicit",
        "display_name": "explicit",
    }
    onboard_helper._write_cache(tmp_path, explicit, slot)
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    cached = onboard_helper._read_cache(tmp_path, slot)
    assert cached == explicit
    assert result["status"] == "ok"
    assert result["uuid"] == explicit["uuid"]
    assert result["client_session_id"] == explicit["client_session_id"]
    assert result["continuity_token"] == ""


def test_slotted_onboard_sends_scoped_name(tmp_path: Path) -> None:
    transport = _FakeTransport(_onboard_ok_response("bbbb2222-0000-0000-0000-000000000000", "cirwel#74608bf3"))

    result = run_onboard(
        server_url="http://unit-test",
        agent_name="cirwel",
        model_type="claude-code",
        workspace=tmp_path,
        slot="1d118271-7384-42cb-adce-c4f4b314e089",
        post_json=transport,
    )

    assert result["status"] == "ok"
    sent_args = transport.calls[0]["payload"]["arguments"]
    assert sent_args["name"] == "cirwel#74608bf3"
    assert sent_args["force_new"] is True


def test_two_slots_receive_distinct_server_calls(tmp_path: Path) -> None:
    """Different slots → different names sent → server's name-claim can no
    longer bind both slots to the same label. This is the behavior that was
    broken before the fix: both slots were sending an identical ``name``."""
    response_a = _onboard_ok_response("1111aaaa-0000-0000-0000-000000000000", "cirwel#slot-aaa")
    transport_a = _FakeTransport(response_a)
    run_onboard(
        server_url="http://unit-test",
        agent_name="cirwel",
        model_type="claude-code",
        workspace=tmp_path,
        slot="slot-aaaaaaaa",
        post_json=transport_a,
    )

    response_b = _onboard_ok_response("2222bbbb-0000-0000-0000-000000000000", "cirwel#slot-bbb")
    transport_b = _FakeTransport(response_b)
    run_onboard(
        server_url="http://unit-test",
        agent_name="cirwel",
        model_type="claude-code",
        workspace=tmp_path,
        slot="slot-bbbbbbbb",
        post_json=transport_b,
    )

    name_a = transport_a.calls[0]["payload"]["arguments"]["name"]
    name_b = transport_b.calls[0]["payload"]["arguments"]["name"]
    assert name_a != name_b
    assert name_a.startswith("cirwel#")
    assert name_b.startswith("cirwel#")


def test_cached_uuid_declares_lineage_on_fresh_onboard(tmp_path: Path) -> None:
    """When the slot cache already has a UUID, startup must mint a fresh
    process identity and declare the cached UUID as lineage. It must not
    resume the UUID via ``identity()``."""
    # Seed the slot cache with an existing UUID, as if a prior run onboarded.
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / "session-existing.json").write_text(
        json.dumps({"uuid": "cccc3333-0000-0000-0000-000000000000"}),
        encoding="utf-8",
    )

    onboard_response = {
        "result": {
            "success": True,
            "uuid": "ffff6666-0000-0000-0000-000000000000",
            "agent_id": "Claude_Code_ffff6666",
            "client_session_id": "agent-ffff6666-000",
            "continuity_token": "token-ffff6666",
            "session_resolution_source": "force_new",
            "continuity_token_supported": True,
            "display_name": "cirwel",
        }
    }
    transport = _FakeTransport(onboard_response)

    result = run_onboard(
        server_url="http://unit-test",
        agent_name="cirwel",
        model_type="claude-code",
        workspace=tmp_path,
        slot="existing",
        post_json=transport,
    )

    assert result["status"] == "ok"
    assert result["uuid"] == "ffff6666-0000-0000-0000-000000000000"
    sent = transport.calls[0]["payload"]
    assert sent["name"] == "onboard"
    assert sent["arguments"]["force_new"] is True
    assert sent["arguments"]["name"] == "cirwel#f4e0ac58"
    assert sent["arguments"]["parent_agent_id"] == "cccc3333-0000-0000-0000-000000000000"
    assert sent["arguments"]["spawn_reason"] == "new_session"
    assert "agent_uuid" not in sent["arguments"]


def test_cached_token_is_not_sent_on_startup(tmp_path: Path) -> None:
    """S1-b: startup lineage comes from UUID, not cached token/session
    material. Tokens may still be used for in-process proof-owned calls
    outside this helper."""
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / "session-existing.json").write_text(
        json.dumps({
            "uuid": "dddd4444-0000-0000-0000-000000000000",
            "continuity_token": "v1.signedtokenpayload.signature",
        }),
        encoding="utf-8",
    )

    onboard_response = {
        "result": {
            "success": True,
            "uuid": "eeee7777-0000-0000-0000-000000000000",
            "agent_id": "Claude_Code_eeee7777",
            "client_session_id": "agent-eeee7777-000",
            "continuity_token": "v1.refreshed.token",
            "session_resolution_source": "force_new",
            "continuity_token_supported": True,
            "display_name": "cirwel",
        }
    }
    transport = _FakeTransport(onboard_response)

    run_onboard(
        server_url="http://unit-test",
        agent_name="cirwel",
        model_type="claude-code",
        workspace=tmp_path,
        slot="existing",
        post_json=transport,
    )

    sent_args = transport.calls[0]["payload"]["arguments"]
    assert sent_args["parent_agent_id"] == "dddd4444-0000-0000-0000-000000000000"
    assert sent_args["force_new"] is True
    assert "continuity_token" not in sent_args
    assert "client_session_id" not in sent_args


def test_force_new_flag_ignores_cached_lineage(tmp_path: Path) -> None:
    """The legacy --force-new flag remains a way to ignore cached lineage."""
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / "session-existing.json").write_text(
        json.dumps({"uuid": "eeee5555-0000-0000-0000-000000000000"}),
        encoding="utf-8",
    )

    onboard_response = {
        "result": {
            "success": True,
            "uuid": "ffff8888-0000-0000-0000-000000000000",
            "agent_id": "Claude_Code_ffff8888",
        }
    }
    transport = _FakeTransport(onboard_response)

    run_onboard(
        server_url="http://unit-test",
        agent_name="cirwel",
        model_type="claude-code",
        workspace=tmp_path,
        slot="existing",
        force_new=True,
        post_json=transport,
    )

    sent_args = transport.calls[0]["payload"]["arguments"]
    assert sent_args["force_new"] is True
    assert "parent_agent_id" not in sent_args
    assert "continuity_token" not in sent_args


# ---- Genesis bootstrap (name-claim ghost fix) ------------------------------


def test_onboard_omits_initial_state_by_default(tmp_path: Path) -> None:
    """The genesis seed is OPT-IN. A bare onboard must send no initial_state —
    seeding does not clear an 'uninitialized / 0 real updates' status (bootstrap
    rows are excluded from real-check-in counts), so it is not the default."""
    transport = _FakeTransport(_onboard_ok_response("aaaa1111-0000-0000-0000-000000000000", "cirwel"))

    run_onboard(
        server_url="http://unit-test",
        agent_name="cirwel",
        model_type="claude-code",
        workspace=tmp_path,
        slot=None,
        post_json=transport,
    )

    assert "initial_state" not in transport.calls[0]["payload"]["arguments"]


def test_onboard_bootstrap_true_attaches_genesis_seed(tmp_path: Path) -> None:
    """The per-call opt-in (mirrors the --bootstrap CLI flag) attaches the
    default genesis seed."""
    transport = _FakeTransport(_onboard_ok_response("bbbb2222-0000-0000-0000-000000000000", "cirwel"))

    run_onboard(
        server_url="http://unit-test",
        agent_name="cirwel",
        model_type="claude-code",
        workspace=tmp_path,
        slot=None,
        bootstrap=True,
        post_json=transport,
    )

    sent_args = transport.calls[0]["payload"]["arguments"]
    assert sent_args["initial_state"]["response_text"] == BOOTSTRAP_RESPONSE_TEXT
    # The seed mirrors the canonical check-in fields and must not pre-tag a
    # source — the server stamps source='bootstrap' itself.
    assert set(sent_args["initial_state"]) == {"response_text", "complexity", "confidence"}
    assert _default_bootstrap_state()["response_text"] == BOOTSTRAP_RESPONSE_TEXT


def test_explicit_initial_state_overrides_default_seed(tmp_path: Path) -> None:
    """A caller-supplied initial_state always wins over the default genesis."""
    transport = _FakeTransport(_onboard_ok_response("cccc3333-0000-0000-0000-000000000000", "cirwel"))
    custom = {"response_text": "custom genesis", "complexity": 0.4, "confidence": 0.9}

    run_onboard(
        server_url="http://unit-test",
        agent_name="cirwel",
        model_type="claude-code",
        workspace=tmp_path,
        slot=None,
        initial_state=custom,
        post_json=transport,
    )

    assert transport.calls[0]["payload"]["arguments"]["initial_state"] == custom


def test_env_opt_in_enables_bootstrap(tmp_path: Path, monkeypatch: Any) -> None:
    """UNITARES_ONBOARD_BOOTSTRAP=1 enables the (otherwise off) genesis seed globally."""
    monkeypatch.setenv("UNITARES_ONBOARD_BOOTSTRAP", "1")
    transport = _FakeTransport(_onboard_ok_response("dddd4444-0000-0000-0000-000000000000", "cirwel"))

    run_onboard(
        server_url="http://unit-test",
        agent_name="cirwel",
        model_type="claude-code",
        workspace=tmp_path,
        slot=None,
        post_json=transport,
    )

    assert "initial_state" in transport.calls[0]["payload"]["arguments"]


# ---- Integration: real server, real distinct UUIDs -------------------------


SERVER_URL = "http://127.0.0.1:8767"


def _server_reachable() -> bool:
    try:
        urllib.request.urlopen(f"{SERVER_URL}/health", timeout=1)
        return True
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return False


def _archive_agent(uuid: str) -> None:
    """Best-effort archive of a UUID created by this test.

    This test hits the real governance server and creates two identities
    per run. Without teardown they pile up as ``itest-plugin#*`` ghosts in
    production (operator caught a pair-per-run accumulation 2026-04-17).
    The server's periodic test-agent sweep is a backstop, not a substitute
    for the test cleaning up its own state.
    """
    if not uuid:
        return
    payload = {
        "name": "archive_agent",
        "arguments": {"agent_id": uuid, "reason": "itest teardown"},
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{SERVER_URL}/v1/tools/call", data=body, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        # Teardown is best-effort — if the server is down or the UUID was
        # already swept by Vigil, we don't want to fail a passing test.
        pass


@pytest.mark.skipif(not _server_reachable(), reason="governance server on :8767 unreachable")
def test_integration_two_slots_get_distinct_uuids(tmp_path: Path) -> None:
    """End-to-end: ask the real server to onboard two slots. Verify they
    actually resolve to different UUIDs. This is the regression test that
    would have caught the original bug before shipping."""
    slot_a = "itest-slot-aaaa1111"
    slot_b = "itest-slot-bbbb2222"
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()

    result_a = run_onboard(
        server_url=SERVER_URL,
        agent_name="itest-plugin",
        model_type="claude-code",
        workspace=ws_a,
        slot=slot_a,
    )
    result_b = run_onboard(
        server_url=SERVER_URL,
        agent_name="itest-plugin",
        model_type="claude-code",
        workspace=ws_b,
        slot=slot_b,
    )

    try:
        assert result_a["status"] == "ok", result_a
        assert result_b["status"] == "ok", result_b
        assert result_a["uuid"] != result_b["uuid"], (
            f"slot isolation broken: both slots resolved to the same UUID {result_a['uuid']}"
        )
    finally:
        _archive_agent(result_a.get("uuid", ""))
        _archive_agent(result_b.get("uuid", ""))
