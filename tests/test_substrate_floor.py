"""Unit tests for scripts/substrate_floor.py — the identity-free check-in floor.

The floor must: refuse to send without a slot, post the right identity-free
payload to /v1/substrate/observe (never process_agent_update), and never raise.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import substrate_floor  # noqa: E402


def test_empty_slot_skips_without_post():
    """No slot → no network call. A floor with no disambiguator is meaningless."""
    with patch("substrate_floor._post") as mock_post:
        result = substrate_floor.submit_floor(slot="   ", event="turn_stop")
    assert result == "skip_no_slot"
    mock_post.assert_not_called()


def test_kill_switch_skips(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITARES_CHECKINS", "off")
    monkeypatch.setenv("UNITARES_CHECKIN_LOG", str(tmp_path / "checkins.log"))
    with patch("substrate_floor._post") as mock_post:
        result = substrate_floor.submit_floor(slot="claude-session-xyz")
    assert result == "skip_kill_switch"
    mock_post.assert_not_called()


def test_payload_is_identity_free_and_slot_keyed():
    """The posted body carries slot_key and NO identity fields, to the floor URL."""
    captured = {}

    def fake_post(url, payload, timeout=5.0):
        captured["url"] = url
        captured["payload"] = payload
        return True, 12, None

    with patch("substrate_floor._post", side_effect=fake_post):
        result = substrate_floor.submit_floor(
            slot="claude-session-xyz",
            event="turn_stop",
            tool_count=3,
            summary="did some work",
            server_url="http://localhost:8767",
        )

    assert result == "floor_sent"
    assert captured["url"] == "http://localhost:8767"
    body = captured["payload"]
    assert body["slot_key"] == "claude-session-xyz"
    assert body["tool_count"] == 3
    # Identity-free: none of the check-in/identity fields may appear.
    for forbidden in ("client_session_id", "agent_id", "uuid", "continuity_token", "confidence"):
        assert forbidden not in body


def test_post_failure_returns_floor_fail():
    with patch("substrate_floor._post", return_value=(False, 5, "connection refused")):
        result = substrate_floor.submit_floor(slot="s1", server_url="http://localhost:8767")
    assert result == "floor_fail"


def test_post_targets_observe_endpoint_with_http_api_token(monkeypatch):
    """_post must hit /v1/substrate/observe, never /v1/tools/call."""
    seen = {}
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "floor-secret")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout=5.0):
        seen["full_url"] = req.full_url
        seen["headers"] = {
            key.lower(): value for key, value in req.header_items()
        }
        return _Resp()

    with patch("substrate_floor.authorization_safe_urlopen", side_effect=fake_urlopen):
        ok, _latency, err = substrate_floor._post("http://localhost:8767", {"slot_key": "s"})
    assert ok is True
    assert err is None
    assert seen["full_url"].endswith("/v1/substrate/observe")
    assert seen["headers"]["authorization"] == "Bearer floor-secret"
    assert seen["headers"]["content-type"] == "application/json"


def test_session_lookup_emits_raw_slot():
    """_session_lookup CLI must surface RAW_SLOT from the stdin session_id even
    when no onboard cache exists (the floor's only disambiguator)."""
    payload = '{"session_id": "claude-floor-abc123"}'
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "_session_lookup.py"), "--workspace", "/tmp/nonexistent-ws"],
        input=payload,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert 'RAW_SLOT="claude-floor-abc123"' in proc.stdout
    # No cache at that workspace → CSID empty, which is exactly the floor case.
    assert 'CSID=""' in proc.stdout
