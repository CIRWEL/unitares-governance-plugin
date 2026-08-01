"""S15-c — _fetch_skills.py adapter coverage.

Spec: §9 step 3 + §8.6.

The helper fetches canonical skill content from the server's `skills`
MCP tool and falls back to the bundled mirror on offline / failure.
Tests pin: REST + native MCP response shapes, cache TTL behavior,
fallback ordering (cache → server → cache → bundled), breadcrumb
emission, and the §4.5 identity-blindness invariant (cache key is
content-addressed, never identity-derived).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "_fetch_skills.py"


# ---------------------------------------------------------------------------
# Tiny mock /v1/tools/call server. Records every request so tests can
# assert on call shape (or absence — for cache-hit cases).
# ---------------------------------------------------------------------------


class MockHandler(BaseHTTPRequestHandler):
    response_payload: dict[str, Any] = {}
    response_status: int = 200
    delay_before_response: float = 0.0
    requests: list[dict[str, Any]] = []

    def log_message(self, *args, **kwargs) -> None:  # silence noisy server log
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"_raw": body}
        type(self).requests.append(
            {
                "path": self.path,
                "body": parsed,
                "headers": {key.lower(): value for key, value in self.headers.items()},
            }
        )

        if type(self).delay_before_response:
            time.sleep(type(self).delay_before_response)

        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(type(self).response_payload).encode("utf-8"))


@pytest.fixture
def mock_server():
    MockHandler.response_payload = {}
    MockHandler.response_status = 200
    MockHandler.delay_before_response = 0.0
    MockHandler.requests = []

    server = HTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"port": port, "url": f"http://127.0.0.1:{port}", "handler": MockHandler}
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Each test runs against its own cache directory. Without isolation
    a prior test's cache would leak into this test's TTL math and the
    fallback ordering becomes non-deterministic."""
    cache = tmp_path / "skills-cache"
    monkeypatch.setenv("UNITARES_SKILLS_CACHE_DIR", str(cache))
    return cache


@pytest.fixture
def fake_plugin_root(tmp_path):
    """Layout: <tmp>/plugin/skills/<name>/SKILL.md (the bundled mirror)."""
    root = tmp_path / "plugin"
    (root / "skills" / "governance-fundamentals").mkdir(parents=True)
    (root / "skills" / "governance-fundamentals" / "SKILL.md").write_text(
        "---\nname: governance-fundamentals\nlast_verified: \"2026-04-25\"\n---\n\n"
        "# Bundled mirror content (offline fallback)\n",
        encoding="utf-8",
    )
    return root


def _server_response(content: str = "# fresh server content\n") -> dict[str, Any]:
    """Shape that mirrors what /v1/tools/call returns from the live server."""
    return {
        "name": "skills",
        "result": {
            "success": True,
            "server_time": "2026-04-27T00:00:00",
            "skills": [
                {
                    "name": "governance-fundamentals",
                    "description": "Use when an agent needs to understand UNITARES governance.",
                    "version": "2026-04-25",
                    "last_verified": "2026-04-25",
                    "freshness_days": 14,
                    "source_files": ["unitares/src/mcp_handlers/core.py"],
                    "triggers": None,
                    "stale": False,
                    "content": content,
                    "content_hash": "sha256:abc",
                }
            ],
            "registry_version": "2026-04-25",
            "registry_hash": "sha256:reg-abc",
        },
        "success": True,
    }


def _run(
    *,
    plugin_root: Path,
    server_url: str,
    name: str = "governance-fundamentals",
    cache_ttl: int = 300,
    timeout: float = 3.0,
    force_refresh: bool = False,
) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--name",
        name,
        "--plugin-root",
        str(plugin_root),
        "--server-url",
        server_url,
        "--cache-ttl-secs",
        str(cache_ttl),
        "--fetch-timeout-secs",
        str(timeout),
    ]
    if force_refresh:
        cmd.append("--force-refresh")
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


# ---------------------------------------------------------------------------
# 1. Fresh server fetch — happy path
# ---------------------------------------------------------------------------


def test_fresh_server_fetch_renders_frontmatter_and_body(
    mock_server, isolated_cache, fake_plugin_root
):
    MockHandler.response_payload = _server_response("# server body\n")
    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])

    yaml = pytest.importorskip("yaml")
    assert result.returncode == 0
    assert "[FETCH_SKILLS_FRESH]" in result.stderr
    end = result.stdout.find("\n---\n", 4)
    fm = yaml.safe_load(result.stdout[4:end])
    assert fm["name"] == "governance-fundamentals"
    assert fm["last_verified"] == "2026-04-25"
    assert "# server body" in result.stdout
    # Bundled-mirror sentinel must NOT be present — we got the server copy.
    assert "Bundled mirror content" not in result.stdout

    # Exactly one POST was made.
    assert len(MockHandler.requests) == 1
    req = MockHandler.requests[0]
    assert req["path"] == "/v1/tools/call"
    assert req["body"]["name"] == "skills"
    assert req["body"]["arguments"]["name"] == "governance-fundamentals"


def test_fresh_server_fetch_forwards_http_api_token(
    mock_server, isolated_cache, fake_plugin_root, monkeypatch
):
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "skills-secret")
    MockHandler.response_payload = _server_response()

    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])

    assert result.returncode == 0
    assert len(MockHandler.requests) == 1
    assert MockHandler.requests[0]["headers"]["authorization"] == (
        "Bearer skills-secret"
    )


def test_fresh_fetch_writes_cache_at_mode_0600(
    mock_server, isolated_cache, fake_plugin_root, tmp_path
):
    MockHandler.response_payload = _server_response()
    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert result.returncode == 0

    cache_file = isolated_cache / "governance-fundamentals.json"
    assert cache_file.exists()
    import stat as _stat
    assert _stat.S_IMODE(os.stat(cache_file).st_mode) == 0o600

    cached = json.loads(cache_file.read_text())
    assert cached["registry_hash"] == "sha256:reg-abc"
    assert "rendered" in cached
    assert isinstance(cached["fetched_at"], int)


# ---------------------------------------------------------------------------
# 2. Cache TTL — within TTL replays without HTTP
# ---------------------------------------------------------------------------


def test_within_ttl_replays_cache_without_http(
    mock_server, isolated_cache, fake_plugin_root
):
    # Seed cache via one fresh fetch.
    MockHandler.response_payload = _server_response("# v1\n")
    first = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert first.returncode == 0
    assert len(MockHandler.requests) == 1

    # Second call within TTL — must NOT hit the server again.
    MockHandler.response_payload = _server_response("# v2 (would only show if fetched)\n")
    second = _run(
        plugin_root=fake_plugin_root, server_url=mock_server["url"], cache_ttl=300
    )
    assert second.returncode == 0
    assert "[FETCH_SKILLS_CACHE]" in second.stderr
    assert "# v1" in second.stdout
    assert "# v2" not in second.stdout
    # Still only one server hit total.
    assert len(MockHandler.requests) == 1


def test_force_refresh_bypasses_ttl(
    mock_server, isolated_cache, fake_plugin_root
):
    MockHandler.response_payload = _server_response("# v1\n")
    first = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert first.returncode == 0

    MockHandler.response_payload = _server_response("# v2-after-refresh\n")
    second = _run(
        plugin_root=fake_plugin_root,
        server_url=mock_server["url"],
        cache_ttl=300,
        force_refresh=True,
    )
    assert second.returncode == 0
    assert "[FETCH_SKILLS_FRESH]" in second.stderr
    assert "# v2-after-refresh" in second.stdout
    assert len(MockHandler.requests) == 2


# ---------------------------------------------------------------------------
# 3. Server failure paths — fall back through cache then bundled
# ---------------------------------------------------------------------------


def test_server_500_falls_back_to_bundled_when_no_cache(
    mock_server, isolated_cache, fake_plugin_root
):
    MockHandler.response_status = 500
    MockHandler.response_payload = {"error": "boom"}

    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert result.returncode == 0
    assert "[FETCH_SKILLS_HTTP_ERROR]" in result.stderr
    assert "[FETCH_SKILLS_OFFLINE_BUNDLED]" in result.stderr
    assert "Bundled mirror content" in result.stdout


def test_server_unreachable_falls_back_to_bundled(
    isolated_cache, fake_plugin_root
):
    # Pick a port that is virtually guaranteed to be closed.
    closed_url = "http://127.0.0.1:1"  # port 1 is reserved/unbound for HTTP
    result = _run(plugin_root=fake_plugin_root, server_url=closed_url, timeout=1.0)
    assert result.returncode == 0
    assert "[FETCH_SKILLS_HTTP_ERROR]" in result.stderr
    assert "[FETCH_SKILLS_OFFLINE_BUNDLED]" in result.stderr
    assert "Bundled mirror content" in result.stdout


def test_server_failure_after_cache_uses_offline_cache(
    mock_server, isolated_cache, fake_plugin_root
):
    """When a prior fetch seeded the cache and the server later fails,
    the helper prefers the cached render over the bundled mirror —
    cached content is at least as fresh as bundled and tagged with a
    real registry_hash."""
    # Seed cache.
    MockHandler.response_payload = _server_response("# cached server content\n")
    first = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert first.returncode == 0

    # Now break the server and force-refresh past the TTL window.
    MockHandler.response_status = 500
    MockHandler.response_payload = {"error": "down"}
    second = _run(
        plugin_root=fake_plugin_root,
        server_url=mock_server["url"],
        force_refresh=True,
    )
    assert second.returncode == 0
    assert "[FETCH_SKILLS_OFFLINE_CACHE]" in second.stderr
    assert "# cached server content" in second.stdout
    # Bundled mirror was NOT used — cache wins.
    assert "Bundled mirror content" not in second.stdout


def test_total_failure_returns_nonzero(isolated_cache, tmp_path):
    """No server, no cache, no bundled mirror → exit 1, empty stdout.
    The hook's fallback chain treats this as 'no Fundamentals excerpt
    this fire' — same as pre-S15-c with a missing SKILL.md."""
    bare_plugin = tmp_path / "bare-plugin"
    bare_plugin.mkdir()
    result = _run(
        plugin_root=bare_plugin,
        server_url="http://127.0.0.1:1",
        timeout=1.0,
    )
    assert result.returncode == 1
    assert "[FETCH_SKILLS_NOT_FOUND]" in result.stderr
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# 4. Response-shape robustness (REST + native-MCP wrapping)
# ---------------------------------------------------------------------------


def test_response_shape_native_mcp_content_array(
    mock_server, isolated_cache, fake_plugin_root
):
    """Some MCP transports wrap tool results in `content: [{type:"text",
    text: "<json>"}]`. The helper must unwrap that shape too — see
    CLAUDE.md "MCP response shapes" memory."""
    inner = {
        "skills": [
            {
                "name": "governance-fundamentals",
                "description": "wrapped",
                "last_verified": "2026-04-25",
                "freshness_days": 14,
                "source_files": [],
                "content": "# wrapped body\n",
                "content_hash": "sha256:wrapped",
            }
        ],
        "registry_version": "2026-04-25",
        "registry_hash": "sha256:wrapped-reg",
    }
    MockHandler.response_payload = {
        "content": [{"type": "text", "text": json.dumps(inner)}]
    }
    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert result.returncode == 0
    assert "[FETCH_SKILLS_FRESH]" in result.stderr
    assert "# wrapped body" in result.stdout


def test_response_shape_direct_skills_at_top_level(
    mock_server, isolated_cache, fake_plugin_root
):
    """Some endpoints return skills/registry_version at the top level
    without the `result:` wrapper."""
    MockHandler.response_payload = {
        "skills": [
            {
                "name": "governance-fundamentals",
                "description": "direct",
                "last_verified": "2026-04-25",
                "freshness_days": 14,
                "source_files": [],
                "content": "# top-level body\n",
                "content_hash": "sha256:top",
            }
        ],
        "registry_version": "2026-04-25",
        "registry_hash": "sha256:top-reg",
    }
    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert result.returncode == 0
    assert "# top-level body" in result.stdout


def test_response_with_no_matching_skill_falls_back(
    mock_server, isolated_cache, fake_plugin_root
):
    """The server may return an empty `skills` array (e.g. since_version
    no-deltas, or the requested skill was renamed). Helper must treat
    this as a fetch-miss and fall back to bundled mirror."""
    payload = _server_response()
    payload["result"]["skills"] = []
    MockHandler.response_payload = payload

    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert result.returncode == 0
    assert "[FETCH_SKILLS_NOT_IN_RESPONSE]" in result.stderr
    assert "[FETCH_SKILLS_OFFLINE_BUNDLED]" in result.stderr
    assert "Bundled mirror content" in result.stdout


def test_malformed_json_response_falls_back(
    mock_server, isolated_cache, fake_plugin_root
):
    """Garbage bytes from the server (e.g. truncated due to tunnel
    hiccup) must not crash the helper — fall back gracefully."""
    # Mock can't easily emit garbage via response_payload (json.dumps
    # serializes everything); use a custom response_status to trigger
    # the parse-error path. A 200 with a JSON-incompatible string
    # requires a different handler — easier to use a closed port.
    MockHandler.response_payload = "not-json-at-all"  # type: ignore[assignment]
    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    # The mock will json.dumps this and it serializes as a JSON string
    # ("not-json-at-all"), so it parses but lacks `skills` — exercises
    # the not-in-response path.
    assert result.returncode == 0
    assert "Bundled mirror content" in result.stdout


# ---------------------------------------------------------------------------
# 5. Identity-blindness invariant (§4.5)
# ---------------------------------------------------------------------------


def test_cache_filename_is_identity_blind(
    mock_server, isolated_cache, fake_plugin_root
):
    """§4.5: the skills cache must NOT be keyed by agent_uuid or any
    identity-derived value. Cache filename is content-addressed by
    skill name; running the helper with different shell environment
    (UUID env vars) must produce the same cache file path."""
    MockHandler.response_payload = _server_response()

    env_a = os.environ.copy()
    env_a["UNITARES_AGENT_UUID"] = "uuid-A"
    env_b = os.environ.copy()
    env_b["UNITARES_AGENT_UUID"] = "uuid-B"

    cmd = [
        sys.executable, str(SCRIPT),
        "--name", "governance-fundamentals",
        "--plugin-root", str(fake_plugin_root),
        "--server-url", mock_server["url"],
        "--cache-ttl-secs", "0",  # always re-fetch
    ]
    subprocess.run(cmd, env=env_a, capture_output=True, check=True)
    subprocess.run(cmd, env=env_b, capture_output=True, check=True)

    files = sorted(p.name for p in isolated_cache.iterdir())
    assert files == ["governance-fundamentals.json"], (
        f"Cache directory must contain exactly one file (identity-blind); "
        f"got {files}"
    )


def test_helper_does_not_send_identity_in_request(
    mock_server, isolated_cache, fake_plugin_root
):
    """Defense in depth: even if the server is identity-blind on its
    side, the helper itself must not send agent_uuid/client_session_id
    in the tool arguments. Anything in `arguments` becomes telemetry the
    server may store; identity-blindness is a wire-level invariant too."""
    MockHandler.response_payload = _server_response()
    _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])

    assert len(MockHandler.requests) == 1
    args = MockHandler.requests[0]["body"]["arguments"]
    forbidden = {"agent_uuid", "uuid", "client_session_id", "continuity_token"}
    leaked = forbidden & set(args.keys())
    assert not leaked, f"helper leaked identity into request: {leaked}"


# ---------------------------------------------------------------------------
# 6. Frontmatter rendering details (regression — downstream parsers depend
#    on the shape)
# ---------------------------------------------------------------------------


def test_rendered_frontmatter_round_trips_through_yaml(
    mock_server, isolated_cache, fake_plugin_root
):
    """`_freshness_warning.py` parses `last_verified` from the rendered
    frontmatter. The reconstructed YAML must be valid (parseable) so
    downstream consumers don't silently see no-frontmatter."""
    yaml = pytest.importorskip("yaml")

    MockHandler.response_payload = _server_response()
    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert result.returncode == 0

    text = result.stdout
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    assert end > 0, "rendered frontmatter must have closing ---"
    fm = yaml.safe_load(text[4:end])
    assert fm["name"] == "governance-fundamentals"
    assert fm["last_verified"] == "2026-04-25"
    assert fm["freshness_days"] == 14
    assert fm["source_files"] == ["unitares/src/mcp_handlers/core.py"]


def test_unknown_server_fields_pass_through_to_rendered_frontmatter(
    mock_server, isolated_cache, fake_plugin_root
):
    """A future v2-ontology field added on the server side must reach
    Claude Code consumers without an adapter edit. Allow-list-only
    rendering would silently drop new fields — the §6 authority-
    hierarchy failure mode in the rendering direction."""
    yaml = pytest.importorskip("yaml")
    payload = _server_response()
    payload["result"]["skills"][0].update({
        "triggers": {"keywords": ["onboard"], "tool_calls": ["onboard"]},
        "stale": False,
        "version": "2026-04-25",
        "speculative_future_field": "v2-ontology-extension",
    })
    MockHandler.response_payload = payload

    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert result.returncode == 0
    end = result.stdout.find("\n---\n", 4)
    fm = yaml.safe_load(result.stdout[4:end])
    assert fm.get("triggers") == {
        "keywords": ["onboard"],
        "tool_calls": ["onboard"],
    }
    assert fm.get("stale") is False
    assert fm.get("speculative_future_field") == "v2-ontology-extension"


def test_multiline_description_renders_as_literal_block(
    mock_server, isolated_cache, fake_plugin_root
):
    """A description containing paragraph breaks must round-trip
    losslessly. Folded scalars (`>`) collapse blank-line-separated
    paragraphs into a single line — the helper must use a literal
    block scalar (`|`) instead."""
    yaml = pytest.importorskip("yaml")
    payload = _server_response()
    payload["result"]["skills"][0]["description"] = (
        "First paragraph with a colon: still here.\n"
        "\n"
        "Second paragraph after a blank line.\n"
        "Leading dash - should also survive."
    )
    MockHandler.response_payload = payload

    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert result.returncode == 0
    end = result.stdout.find("\n---\n", 4)
    fm = yaml.safe_load(result.stdout[4:end])
    assert "Second paragraph after a blank line." in fm["description"]
    assert "Leading dash - should also survive." in fm["description"]
    assert "still here." in fm["description"]


def test_source_files_with_special_chars_round_trip(
    mock_server, isolated_cache, fake_plugin_root
):
    """source_files entries are operator-controlled paths and may
    legitimately contain `:`, `#`, leading `-`, or other YAML reserved
    chars (e.g. `path/file.py:L42`). Bare unquoted scalars would break
    the YAML round-trip; entries must be quoted."""
    yaml = pytest.importorskip("yaml")
    tricky = [
        "src/path:with:colons.py",
        "#leading-hash.py",
        "-leading-dash.py",
        "src/with spaces.py",
    ]
    payload = _server_response()
    payload["result"]["skills"][0]["source_files"] = tricky
    MockHandler.response_payload = payload

    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert result.returncode == 0
    end = result.stdout.find("\n---\n", 4)
    fm = yaml.safe_load(result.stdout[4:end])
    assert fm["source_files"] == tricky


def test_concurrent_writes_do_not_corrupt_cache(
    mock_server, isolated_cache, fake_plugin_root
):
    """Two parallel SessionStart fires (e.g. a `--resume` and a second
    tab opening simultaneously) must not collide on a shared `<name>.tmp`
    write target. mkstemp gives each writer a unique tmp path; the final
    `os.replace` is atomic; the cache stays valid JSON regardless of who
    won the race."""
    MockHandler.response_payload = _server_response("# concurrent body\n")
    cmd = [
        sys.executable, str(SCRIPT),
        "--name", "governance-fundamentals",
        "--plugin-root", str(fake_plugin_root),
        "--server-url", mock_server["url"],
        "--cache-ttl-secs", "0",  # force every fire to hit the server
        "--force-refresh",
    ]
    procs = [
        subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(8)
    ]
    rcs = [p.wait(timeout=20) for p in procs]
    assert all(rc == 0 for rc in rcs), f"some procs failed: {rcs}"

    cache_file = isolated_cache / "governance-fundamentals.json"
    assert cache_file.exists()
    cached = json.loads(cache_file.read_text())  # must parse, no corruption
    assert "rendered" in cached
    assert "registry_hash" in cached

    # No leftover tmp files in the cache dir — every mkstemp tmp must
    # have been replaced or unlinked.
    leftovers = [p.name for p in isolated_cache.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"orphaned tmp files: {leftovers}"


def test_server_stale_flag_emits_fresh_stale_breadcrumb(
    mock_server, isolated_cache, fake_plugin_root
):
    """A skill the server itself flagged stale (drifted past
    freshness_days since last_verified) must surface a distinct
    [FETCH_SKILLS_FRESH_STALE] breadcrumb so operators reading the
    log can tell server-vouched-fresh from server-vouched-stale."""
    payload = _server_response()
    payload["result"]["skills"][0]["stale"] = True
    MockHandler.response_payload = payload

    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert result.returncode == 0
    assert "[FETCH_SKILLS_FRESH_STALE]" in result.stderr
    # The plain FRESH crumb must NOT also fire — the two states are
    # mutually exclusive on a single fetch.
    assert "[FETCH_SKILLS_FRESH]" not in result.stderr.replace(
        "[FETCH_SKILLS_FRESH_STALE]", ""
    )


def test_print_source_label_round_trips(
    mock_server, isolated_cache, fake_plugin_root
):
    """The session-start hook parses `source=<label>` from stderr to
    pick the agent-facing excerpt suffix. Labels for fresh / cache /
    offline_bundled must reach stderr in a stable, greppable form."""
    MockHandler.response_payload = _server_response()
    cmd = [
        sys.executable, str(SCRIPT),
        "--name", "governance-fundamentals",
        "--plugin-root", str(fake_plugin_root),
        "--server-url", mock_server["url"],
        "--print-source",
    ]
    fresh = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "source=fresh\n" in fresh.stderr

    # Second invocation hits the cache (within 300s default TTL).
    cached = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "source=cache\n" in cached.stderr


# ---------------------------------------------------------------------------
# Defensive-cache tests — clock skew, replace failures, hostile nesting.
# Each one pins a specific failure mode that the council review surfaced;
# regression coverage so a future refactor can't silently re-introduce it.
# ---------------------------------------------------------------------------


def test_future_fetched_at_does_not_pin_cache_eternally(
    mock_server, isolated_cache, fake_plugin_root
):
    """If `fetched_at` lands in the future (NFS clock skew, an operator
    setting the clock back, a corrupt write), the unsigned `now -
    fetched_at < ttl` check would otherwise return True forever. The
    fix floors the delta at zero so future timestamps trigger a
    refetch instead of pinning the cache as eternally fresh."""
    cache_file = isolated_cache / "governance-fundamentals.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "rendered": "---\nname: governance-fundamentals\n---\n\n# stale poison\n",
                "registry_hash": "sha256:poison",
                "registry_version": "stale",
                "content_hash": "sha256:poison",
                # 9999s in the future — without the floor, this would satisfy
                # `delta < 300` and replay the poisoned content forever.
                "fetched_at": int(time.time()) + 9999,
            }
        )
    )
    MockHandler.response_payload = _server_response("# fresh server content\n")

    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    assert result.returncode == 0
    # The fresh-fetch breadcrumb fires — i.e. the script went to the server,
    # it did not trust the future-dated cache.
    assert "[FETCH_SKILLS_FRESH]" in result.stderr
    assert "[FETCH_SKILLS_CACHE]" not in result.stderr
    # Body is the server copy, not the poisoned cache.
    assert "stale poison" not in result.stdout
    assert "fresh server content" in result.stdout
    # And exactly one HTTP call landed.
    assert len(MockHandler.requests) == 1


def test_write_cache_unlinks_tmp_on_replace_failure(
    isolated_cache, monkeypatch
):
    """`os.replace` can fail (cross-device move, permission flip,
    target-dir vanished). The fix wraps it so the mkstemp tmp file is
    unlinked before bubbling — otherwise the cache dir grows hidden
    `.governance-fundamentals-XXXX.tmp` files on every retry."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import importlib

        mod = importlib.import_module("_fetch_skills")
        importlib.reload(mod)  # pick up latest source if previously imported
    finally:
        sys.path.pop(0)

    monkeypatch.setenv("UNITARES_SKILLS_CACHE_DIR", str(isolated_cache))

    real_replace = os.replace

    def boom(src, dst):  # noqa: ANN001
        raise PermissionError("simulated replace failure")

    monkeypatch.setattr(mod.os, "replace", boom)

    with pytest.raises(PermissionError):
        mod._write_cache("governance-fundamentals", {"rendered": "x"})

    # No leftover tmp files. Without the fix, mkstemp left a
    # `.governance-fundamentals-XXXX.tmp` in the cache dir.
    monkeypatch.setattr(mod.os, "replace", real_replace)
    if isolated_cache.exists():
        leftovers = [p.name for p in isolated_cache.iterdir() if p.suffix == ".tmp"]
        assert leftovers == [], f"orphaned tmp files: {leftovers}"


def test_normalize_response_caps_recursion_depth(
    mock_server, isolated_cache, fake_plugin_root
):
    """A hostile or malformed server could chain `content[0].text` →
    JSON → `content[0].text` indefinitely. Without a depth cap, this
    blows Python's recursion limit and propagates a RecursionError out
    of the hook. Confirm the cap kicks in and the script falls back to
    the bundled mirror cleanly (exit 0, OFFLINE_BUNDLED breadcrumb)."""
    # Build a nested payload deeper than _NORMALIZE_MAX_DEPTH (=3).
    # Each layer wraps the previous one in `{content: [{text: <json>}]}`.
    inner = {"some_other_shape": True}
    for _ in range(8):
        inner = {"content": [{"text": json.dumps(inner)}]}
    MockHandler.response_payload = inner

    result = _run(plugin_root=fake_plugin_root, server_url=mock_server["url"])
    # Bundled mirror has the fake content from the fixture — the script
    # must fall through to it cleanly rather than crash.
    assert result.returncode == 0
    assert "[FETCH_SKILLS_OFFLINE_BUNDLED]" in result.stderr
    assert "Bundled mirror content" in result.stdout
    # And the parse-error breadcrumb names the depth cap explicitly so an
    # operator reading the log can tell this apart from a JSON-parse miss.
    assert "PARSE_ERROR" in result.stderr
    assert "normalize depth" in result.stderr
