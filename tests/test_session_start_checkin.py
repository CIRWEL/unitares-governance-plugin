"""Contract tests for the SessionStart hook.

After the identity-honesty Part C refactor (2026-04-17) and the identity-
hijack hardening of 2026-04-20, the hook does NOT create an identity on
the agent's behalf AND does NOT surface other instances' UUIDs as a
resume menu. It only:

  1. Confirms governance is reachable.
  2. Suggests start_session(force_new=true) with onboard(...) as canonical fallback.
  3. If THIS workspace has slot-scoped continuity state, surfaces a lineage
     candidate for parent_agent_id, not a resume credential.
  4. Never enumerates ~/.unitares/session-*.json — those are other instances'
     identities, and surfacing them as an unfiltered "Recent session UUIDs"
     menu invited cross-instance hijack (KG bug 2026-04-20T00:09:51).

These tests lock in the post-2026-04-20 contract:
  - Hook emits ZERO HTTP tool calls on SessionStart.
  - Online context describes the provisional-free state.
  - Online context surfaces ONLY the workspace-local continuity cache, if any.
  - Online context never lists ~/.unitares/session-*.json contents.
  - Offline context reports OFFLINE and does not reference a fake identity.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import subprocess
import threading
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent.parent


class RecordingHandler(http.server.BaseHTTPRequestHandler):
    """HTTP test double that records every POST (tool call) for inspection."""

    calls: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            data = {"raw": body.decode(errors="replace")}
        RecordingHandler.calls.append(data)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"result": {"success": True}}).encode())

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"alive"}')

    def log_message(self, *a, **k):
        pass


class _ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def _run_hook(tmp_path, server_url, extra_env=None, cwd=None, claude_session_id=None):
    """Run session-start with a given server URL and return (stdout, tool_calls).

    cwd defaults to tmp_path. Pass a different cwd to test workspace-local
    continuity cache discovery independently of HOME. claude_session_id is
    placed on stdin in the Claude Code hook envelope so the hook can find
    the slot-scoped workspace cache.
    """
    RecordingHandler.calls = []
    workdir = cwd if cwd is not None else tmp_path
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "UNITARES_SERVER_URL": server_url,
        "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
        "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
        "PWD": str(workdir),
        "USER": "testuser",
    }
    if extra_env:
        env.update(extra_env)

    stdin_payload = {"session_id": claude_session_id} if claude_session_id else {}

    hook = PLUGIN_ROOT / "hooks" / "session-start"
    result = subprocess.run(
        [str(hook)],
        env=env,
        cwd=str(workdir),
        input=json.dumps(stdin_payload),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return result.stdout, list(RecordingHandler.calls)


def _serve_and_run(tmp_path, **run_kwargs):
    srv = _ReusableTCPServer(("127.0.0.1", 0), RecordingHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        return _run_hook(tmp_path, f"http://127.0.0.1:{port}", **run_kwargs)
    finally:
        srv.shutdown()
        thread.join(timeout=2)


class TestSessionStartMakesNoToolCalls:
    """The hook's load-bearing invariant: no governance state-mutation on start.

    S15-c (2026-04-27) added a single read-only, identity-blind introspection
    call to the `skills` tool to fetch canonical skill content from the server
    (with offline fallback to the bundled mirror). `skills` is in the
    rate_limit_exempt read-only set on the server side and does not consume
    or mutate identity per §4.5 of the s15-server-side-skills.md design.
    The Identity Honesty Part C invariant (no auto-onboard, no auto-resume)
    is unchanged."""

    # Anything in this set is considered identity-mutating or state-changing
    # and must NEVER be called from SessionStart. `skills` is deliberately
    # excluded — it is the S15-c introspection fetch.
    FORBIDDEN_TOOLS = {
        "onboard",
        "identity",
        "bind_session",
        "process_agent_update",
        "self_recovery",
        "knowledge",
        "leave_note",
        "outcome_event",
        "calibration",
        "agent",
        "archive_orphan_agents",
        "config",
        "dialectic",
        "observe",
    }

    def _assert_no_state_mutations(self, calls):
        tool_calls = [c.get("name") for c in calls if isinstance(c, dict)]
        forbidden = [t for t in tool_calls if t in self.FORBIDDEN_TOOLS]
        assert not forbidden, (
            f"SessionStart must not invoke identity-mutating governance tools; "
            f"saw forbidden: {forbidden} (full call list: {tool_calls})"
        )

    def test_online_path_makes_no_state_mutations(self, tmp_path):
        _, calls = _serve_and_run(tmp_path)
        self._assert_no_state_mutations(calls)

    def test_online_path_only_fetches_skills_introspection(self, tmp_path):
        """Positive bound: the only tool call SessionStart should make is
        the S15-c skills fetch. Surface any new call so a future hook
        addition is reviewed against the no-state-mutation invariant."""
        _, calls = _serve_and_run(tmp_path)
        tool_calls = [c.get("name") for c in calls if isinstance(c, dict)]
        unexpected = [t for t in tool_calls if t != "skills"]
        assert not unexpected, (
            f"SessionStart made unexpected tool calls: {unexpected}. "
            f"Only 'skills' (S15-c introspection) is allowed; any other "
            f"addition needs explicit review."
        )

    def test_offline_path_emits_zero_tool_calls(self, tmp_path):
        """When MCP is unreachable, the helper falls back to the bundled
        mirror without any successful tool call landing on the server."""
        _, calls = _run_hook(tmp_path, "http://127.0.0.1:1")
        tool_calls = [c.get("name") for c in calls if isinstance(c, dict)]
        assert tool_calls == []


class TestSessionStartContext:
    """Context wording teaches the agent how to bind its own identity."""

    def test_online_context_offers_start_session_with_lazy_onboard_fallback(self, tmp_path):
        stdout, _ = _serve_and_run(tmp_path)
        ctx = json.loads(stdout).get("additional_context", "")
        assert "UNITARES Governance: ONLINE" in ctx
        assert "Identity attribution" in ctx
        assert "SessionStart has not created an identity yet" in ctx
        assert "start_session(" in ctx
        assert "onboard(" in ctx
        assert "Stop hook will lazily create a slot-scoped identity" in ctx

    def test_online_context_avoids_pressure_framing(self, tmp_path):
        stdout, _ = _serve_and_run(tmp_path)
        ctx = json.loads(stdout).get("additional_context", "")
        assert "ACTION REQUIRED" not in ctx
        assert "onboard now" not in ctx
        assert "invisible to governance" not in ctx
        assert "goes ungoverned" not in ctx
        assert "uninitialized, 0-update" not in ctx
        assert "Do not manufacture a check-in" in ctx
        assert "Substrate hooks may record observations automatically" in ctx
        assert "harness-managed attribution" in ctx

    def test_online_context_instructs_force_new_on_fresh_onboard(self, tmp_path):
        """Regression guard: a bare `onboard()` / `start_session()` suggestion lets the server
        pin-resume a prior agent's UUID by IP:UA fingerprint alone on shared
        hosts (PATH 2 bleed — server emits `identity_hijack_suspected` with
        path='path2_ipua_pin'). The default fresh-mint suggestion must pass
        `force_new=true` so the server cannot silently adopt an unrelated
        identity.

        See KG council follow-up to #83 (server-side PATH 2 observation)
        and the companion server PR #92.
        """
        stdout, _ = _serve_and_run(tmp_path)
        ctx = json.loads(stdout).get("additional_context", "")
        assert "force_new=true" in ctx, (
            "Fresh-onboard suggestion must include force_new=true to avoid "
            "silent pin-resume. Context was: " + ctx[:500]
        )
        assert "purpose=" not in ctx

    def test_online_context_does_not_offer_agent_uuid_resume_by_default(self, tmp_path):
        """agent_uuid resume is a hijack vector when paired with cross-instance
        UUID enumeration. Surfacing it in the default menu invites fresh agents
        to pick someone else's UUID. Recovery via known UUID still works as an
        explicit operator action via /diagnose, but the hook must not advertise
        it as a first-call option.

        See KG bug 2026-04-20T00:09:51.
        """
        stdout, _ = _serve_and_run(tmp_path)
        ctx = json.loads(stdout).get("additional_context", "")
        assert "identity(agent_uuid=" not in ctx
        assert "bind_session(agent_uuid=" not in ctx

    def test_online_context_names_agent_experience_response_fields(self, tmp_path):
        """The banner should teach agents what to inspect after the first
        governance call, not only which call to make."""
        stdout, _ = _serve_and_run(tmp_path)
        ctx = json.loads(stdout).get("additional_context", "")

        assert "next_action" in ctx
        assert "state_summary" in ctx
        assert "risk_summary" in ctx
        assert "memory_suggestions" in ctx
        assert "recovery_hint" in ctx
        assert "retrieval prompts" in ctx
        assert "first recovery route" in ctx

    def test_anchored_session_does_not_nudge_force_new(self, tmp_path):
        """When a stable resume anchor is set by an orchestrator — the Discord /
        dispatch_beam case where each turn is a fresh `claude -p` — the banner
        must NOT push `force_new=true`. force_new mints a new uuid per turn and
        defeats the anchor's resume-one-identity-per-conversation guarantee,
        which is the whole point of the anchor. Lazy onboarding at Stop carries
        the anchor to the server and resumes the same identity.

        Regression guard for the "every BEAM turn mints a new id" bug.
        """
        stdout, _ = _serve_and_run(
            tmp_path,
            extra_env={
                "UNITARES_CLIENT_SESSION_ID": "agent:/thread-123",
                "UNITARES_ORCHESTRATED": "1",
            },
        )
        ctx = json.loads(stdout).get("additional_context", "")
        assert "UNITARES Governance: ONLINE" in ctx
        assert "anchored session" in ctx
        # force_new must appear ONLY inside an explicit prohibition, never as an
        # affirmative bind instruction. The non-anchored branches lead with an
        # "Identity attribution: bind this process" nudge + a bare
        # `start_session(force_new=true)` call line — both must be absent here.
        assert "Do NOT call `start_session(force_new=true)`" in ctx
        assert "Identity attribution: bind this process" not in ctx
        assert "SessionStart has not created an identity yet" not in ctx
        # The lineage hint also nudges force_new / a concrete parent declaration;
        # it must be suppressed on anchored sessions.
        assert "parent_agent_id=\"" not in ctx

    def test_bare_anchor_without_orchestrated_marker_uses_normal_onboard_nudge(self, tmp_path):
        """A leaked anchor alone must not suppress fresh-process onboarding guidance."""
        stdout, _ = _serve_and_run(
            tmp_path,
            extra_env={"UNITARES_CLIENT_SESSION_ID": "agent:/leaked-global-anchor"},
        )
        ctx = json.loads(stdout).get("additional_context", "")
        assert "UNITARES Governance: ONLINE" in ctx
        assert "anchored session" not in ctx
        assert "Identity attribution: bind this process" in ctx
        assert "SessionStart has not created an identity yet" in ctx
        assert "start_session(force_new=true)" in ctx

    def test_offline_context_reports_offline_without_fake_identity(self, tmp_path):
        stdout, _ = _run_hook(tmp_path, "http://127.0.0.1:1")
        ctx = json.loads(stdout).get("additional_context", "")
        assert "OFFLINE" in ctx
        assert "provisional identity" not in ctx.lower()
        assert "uuid:" not in ctx.lower()


class TestNoCrossInstanceUuidEnumeration:
    """Regression guard: the hook must NOT enumerate ~/.unitares/session-*.json.

    That file glob produced an unfiltered menu of every UUID that had ever
    onboarded from this host — across every Claude tab, Codex run, and
    subagent. Combined with an `identity(agent_uuid=..., resume=true)`
    suggestion in the same context block, fresh agents pattern-matched on
    model name and resumed into other instances' identities. KG bug
    2026-04-20T00:09:51. The fix removes the enumeration entirely.
    """

    def test_does_not_list_other_instances_session_files(self, tmp_path):
        unitares = tmp_path / ".unitares"
        unitares.mkdir()
        # Two session files belonging to two unrelated prior instances —
        # neither owned by the current workspace.
        (unitares / "session-aaaaaaaaaaaa.json").write_text(json.dumps({
            "uuid": "aaaaaaaa-1111-2222-3333-444444444444",
            "display_name": "Other-Instance-A",
            "updated_at": "2026-04-19T12:00:00+00:00",
        }))
        (unitares / "session-bbbbbbbbbbbb.json").write_text(json.dumps({
            "uuid": "bbbbbbbb-1111-2222-3333-444444444444",
            "display_name": "Other-Instance-B",
            "updated_at": "2026-04-19T13:00:00+00:00",
        }))

        stdout, _ = _serve_and_run(tmp_path)
        ctx = json.loads(stdout).get("additional_context", "")

        # Neither UUID nor label may appear — surfacing them is the hijack vector.
        assert "aaaaaaaa-1111-2222-3333-444444444444" not in ctx
        assert "bbbbbbbb-1111-2222-3333-444444444444" not in ctx
        assert "Other-Instance-A" not in ctx
        assert "Other-Instance-B" not in ctx
        # The misleading section header must be gone.
        assert "Recent session UUIDs on this host" not in ctx


class TestWorkspaceLocalLineage:
    """Under the identity ontology (S11),
    the workspace-local cache surfaces the prior process-instance's UUID as
    a *lineage candidate* — the predecessor the fresh process can declare
    via ``parent_agent_id`` — not as a resume credential.

    Slot-scoped only: the bare ``./.unitares/session.json`` is a
    cross-instance artifact when workspaces are shared (e.g. dispatch
    threads defaulting to cwd=HOME) and must not be read. Only
    ``./.unitares/session-<claude_session_id>.json`` — written by the
    post-identity hook with the current session's slot — is a trustworthy
    lineage anchor.
    """

    def test_surfaces_lineage_from_matching_slot_scoped_cache(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        slot = "claude-session-xyz-123"
        (workspace / ".unitares" / f"session-{slot}.json").write_text(json.dumps({
            "uuid": "ffffffff-1111-2222-3333-444444444444",
            "agent_id": "Claude_Workspace_X",
            "display_name": "Claude_Workspace_X",
            "continuity_token": "",
            "schema_version": 2,
            "updated_at": "2026-04-20T00:00:00+00:00",
        }))

        stdout, _ = _serve_and_run(tmp_path, cwd=workspace, claude_session_id=slot)
        ctx = json.loads(stdout).get("additional_context", "")

        # Workspace context is surfaced.
        assert "workspace" in ctx.lower()
        # The prior UUID appears — as a lineage anchor, not a resume target.
        assert "ffffffff-1111-2222-3333-444444444444" in ctx
        assert "parent_agent_id" in ctx
        # The retired resume-by-token framing must be gone.
        assert "onboard(continuity_token=" not in ctx
        assert "To resume that identity" not in ctx
        # UUID-resume framing remains absent (hijack vector).
        assert "identity(agent_uuid=" not in ctx
        assert "resume=true" not in ctx

    def test_bare_session_json_is_not_surfaced(self, tmp_path):
        """The bare ``./.unitares/session.json`` is a legacy/shared artifact.
        Dispatch threads that fall back to cwd=HOME share it, so surfacing
        its UUID as "workspace lineage" mis-claims continuity and funnels
        concurrent threads onto one predecessor. The hook must ignore it.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        (workspace / ".unitares" / "session.json").write_text(json.dumps({
            "uuid": "eeeeeeee-1111-2222-3333-555555555555",
            "agent_id": "Shared_Cache_Agent",
            "continuity_token": "v1.legacy-token-should-be-ignored",
            "updated_at": "2026-04-10T00:00:00+00:00",
        }))

        stdout, _ = _serve_and_run(tmp_path, cwd=workspace, claude_session_id="fresh-session")
        ctx = json.loads(stdout).get("additional_context", "")

        # Bare-cache UUID must NOT appear — it's not a trustworthy lineage
        # anchor for this specific process-instance.
        assert "eeeeeeee-1111-2222-3333-555555555555" not in ctx
        assert "Shared_Cache_Agent" not in ctx
        assert "v1.legacy-token-should-be-ignored" not in ctx

    def test_slot_mismatch_does_not_surface_as_same_session_lineage(self, tmp_path):
        """Slot scoping must actually scope. A cache file written by a
        different claude session in the same workspace must NOT be surfaced
        as if it were this session's slot match — the slot-match path's
        ``"A prior process-instance ran in this workspace"`` framing is
        false for cross-session caches.

        The scan-newest fallback (S20.2) surfaces other-slot caches
        DELIBERATELY as workspace lineage candidates, but only with the
        ``"scan-newest workspace lineage hint, not a slot match"`` honesty
        marker that distinguishes them. Both paths can fire on the same
        UUID — the slot-match-framing falsehood is the actual axiom
        violation here.

        For a TTL-stale slot, neither path fires — see ``test_scan_newest_filtered_by_ttl``.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        # Far older than the 30d scan-newest TTL — guarantees the
        # cross-test invariant holds against any "today" the suite runs on.
        from datetime import datetime, timezone, timedelta
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        (workspace / ".unitares" / "session-other-slot.json").write_text(json.dumps({
            "uuid": "dddddddd-1111-2222-3333-666666666666",
            "agent_id": "Other_Session",
            "schema_version": 2,
            "updated_at": stale_ts,
        }))

        stdout, _ = _serve_and_run(
            tmp_path, cwd=workspace, claude_session_id="my-slot-not-other"
        )
        ctx = json.loads(stdout).get("additional_context", "")

        # Slot-match framing must not appear at all (this is not a slot match
        # for the current session_id).
        assert "A prior process-instance ran in this workspace" not in ctx
        # And because the cache is >30d old, scan-newest also drops it.
        assert "dddddddd-1111-2222-3333-666666666666" not in ctx
        assert "Other_Session" not in ctx

    def test_no_session_id_means_no_lineage_hint(self, tmp_path):
        """Without a claude_session_id on stdin, the hook has no way to pick
        a trustworthy slot-scoped cache, so it must surface nothing.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        (workspace / ".unitares" / "session.json").write_text(json.dumps({
            "uuid": "cccccccc-1111-2222-3333-777777777777",
            "agent_id": "Bare_Cache",
            "updated_at": "2026-04-20T00:00:00+00:00",
        }))

        stdout, _ = _serve_and_run(tmp_path, cwd=workspace)  # no session_id
        ctx = json.loads(stdout).get("additional_context", "")

        assert "cccccccc-1111-2222-3333-777777777777" not in ctx

    def test_no_workspace_cache_means_no_resume_hint(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        stdout, _ = _serve_and_run(
            tmp_path, cwd=workspace, claude_session_id="fresh-session"
        )
        ctx = json.loads(stdout).get("additional_context", "")
        # No workspace cache → no resume hint block.
        # The phrase "continuity_token" may still appear in the always-on
        # copy that explains what the post-identity hook records; what must
        # NOT appear is the resume hint pointing at a workspace cache.
        assert "./.unitares/session.json" not in ctx
        assert "To resume that identity" not in ctx


class TestScanNewestLineageFallback:
    """S20.2 §3b: cross-`/clear` lineage discovery.

    When ``CLAUDE_SESSION_ID`` is set but its slot-scoped cache file does not
    exist (the harness minted a fresh session_id and prior work in this
    workspace lives under a different slot), the hook falls back to
    ``session_cache.py list --workspace "$PWD"`` and surfaces the *newest* prior slot's
    UUID as a lineage candidate. Strict guarantees:

    - One UUID, never a menu (KG bug 2026-04-20T00:09:51).
    - Filtered by ``UNITARES_HOOK_LINEAGE_MAX_AGE_DAYS`` (default 30).
    - Surfaced as lineage candidate, not resume credential.
    - Suppressed entirely when a slot match exists (the slot-match path
      is more specific and wins).
    """

    def _now_iso(self, *, days_ago: int = 0, hours_ago: int = 0) -> str:
        from datetime import datetime, timezone, timedelta
        ts = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)
        return ts.isoformat()

    def test_scan_newest_surfaces_when_slot_misses(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        # Two prior sessions; newest should win.
        (workspace / ".unitares" / "session-older.json").write_text(json.dumps({
            "uuid": "11111111-aaaa-bbbb-cccc-000000000001",
            "agent_id": "Older_Session",
            "display_name": "Older_Session",
            "client_session_id": "agent-older",
            "schema_version": 2,
            "updated_at": self._now_iso(hours_ago=5),
        }))
        (workspace / ".unitares" / "session-newer.json").write_text(json.dumps({
            "uuid": "22222222-aaaa-bbbb-cccc-000000000002",
            "agent_id": "Newer_Session",
            "display_name": "Newer_Session",
            "client_session_id": "agent-newer",
            "schema_version": 2,
            "updated_at": self._now_iso(hours_ago=1),
        }))

        # Fresh slot — slot-scoped cache does not exist for this session_id.
        stdout, _ = _serve_and_run(
            tmp_path, cwd=workspace, claude_session_id="post-clear-session"
        )
        ctx = json.loads(stdout).get("additional_context", "")

        # Newest UUID is surfaced; older is suppressed (one, not menu).
        assert "22222222-aaaa-bbbb-cccc-000000000002" in ctx
        assert "11111111-aaaa-bbbb-cccc-000000000001" not in ctx
        # Honesty marker — appears BEFORE the UUID (council finding) so a
        # pattern-matching agent that copies the surrounding sentence still
        # reads the disclaimer.
        marker_idx = ctx.find("Scan-newest workspace slot")
        uuid_idx = ctx.find("22222222-aaaa-bbbb-cccc-000000000002")
        assert marker_idx >= 0 and uuid_idx >= 0 and marker_idx < uuid_idx
        # Framing must NOT claim "different Claude session" — the workspace
        # can have Codex/dispatch/ad-hoc writers; over-claiming is the §9
        # axiom #3 violation the council flagged.
        assert "different Claude session" not in ctx or (
            "may have been a different Claude session" in ctx
        )  # only the hedged "may have been" form is allowed
        # Lineage framing, not resume.
        assert "parent_agent_id" in ctx
        assert "identity(agent_uuid=" not in ctx
        assert "resume=true" not in ctx
        # The UUID must appear ONLY inside the parent_agent_id="..." template
        # — not as a standalone backticked token. An agent that copies the
        # literal string then also copies the lineage-only intent.
        bare_backticked = f"`22222222-aaaa-bbbb-cccc-000000000002`"
        assert bare_backticked not in ctx

    def test_scan_newest_filtered_by_ttl(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        (workspace / ".unitares" / "session-old.json").write_text(json.dumps({
            "uuid": "33333333-aaaa-bbbb-cccc-000000000003",
            "agent_id": "Long_Dead",
            "client_session_id": "agent-long-dead",
            "schema_version": 2,
            "updated_at": self._now_iso(days_ago=60),
        }))

        env = {"UNITARES_HOOK_LINEAGE_MAX_AGE_DAYS": "30"}
        stdout, _ = _serve_and_run(
            tmp_path,
            cwd=workspace,
            claude_session_id="post-clear-session",
            extra_env=env,
        )
        ctx = json.loads(stdout).get("additional_context", "")

        # >30 day slot is filtered out; nothing surfaces.
        assert "33333333-aaaa-bbbb-cccc-000000000003" not in ctx
        assert "scan-newest" not in ctx

    def test_scan_newest_skipped_when_slot_match_exists(self, tmp_path):
        """Slot match wins. Never run scan-newest when we already have a
        same-session lineage anchor — that would surface a *second* UUID
        and reintroduce the menu pattern S20 forbids."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        slot = "current-slot"
        (workspace / ".unitares" / f"session-{slot}.json").write_text(json.dumps({
            "uuid": "44444444-aaaa-bbbb-cccc-000000000004",
            "agent_id": "Current_Slot_Match",
            "display_name": "Current_Slot_Match",
            "schema_version": 2,
            "updated_at": self._now_iso(hours_ago=2),
        }))
        (workspace / ".unitares" / "session-other.json").write_text(json.dumps({
            "uuid": "55555555-aaaa-bbbb-cccc-000000000005",
            "agent_id": "Should_Not_Appear",
            "schema_version": 2,
            "updated_at": self._now_iso(hours_ago=1),  # newer than slot match!
        }))

        stdout, _ = _serve_and_run(tmp_path, cwd=workspace, claude_session_id=slot)
        ctx = json.loads(stdout).get("additional_context", "")

        # Slot match surfaces.
        assert "44444444-aaaa-bbbb-cccc-000000000004" in ctx
        # Other slot — even though newer — must NOT appear.
        assert "55555555-aaaa-bbbb-cccc-000000000005" not in ctx
        assert "scan-newest" not in ctx

    def test_scan_newest_quiet_with_empty_unitares_dir(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()  # empty dir

        stdout, _ = _serve_and_run(
            tmp_path, cwd=workspace, claude_session_id="post-clear-session"
        )
        ctx = json.loads(stdout).get("additional_context", "")

        assert "scan-newest" not in ctx
        assert "lineage" not in ctx.lower() or "no" in ctx.lower()

    def test_scan_newest_rejects_future_timestamp_planted_entry(self, tmp_path):
        """C1 from the council review: a same-UID actor (Codex, dispatch
        worker, ad-hoc script) can drop ``session-attacker.json`` with
        ``updated_at`` set to a far-future date and a chosen UUID. Without
        an upper-bound check, that planted UUID would sort to the top of
        cmd_list and become the lineage candidate.

        The hook caps accepted timestamps at ``now + 5min`` (small clock-
        skew tolerance) and falls through to the next valid entry.
        """
        from datetime import datetime, timezone, timedelta
        future_ts = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        # Planted future-dated entry with a chosen UUID.
        (workspace / ".unitares" / "session-attacker.json").write_text(json.dumps({
            "uuid": "deadbeef-aaaa-bbbb-cccc-000000000999",
            "agent_id": "Attacker_Plant",
            "client_session_id": "agent-attacker",
            "schema_version": 2,
            "updated_at": future_ts,
        }))
        # Legitimate fresh entry behind it — should win after future is rejected.
        (workspace / ".unitares" / "session-real.json").write_text(json.dumps({
            "uuid": "77777777-aaaa-bbbb-cccc-000000000777",
            "agent_id": "Real_Predecessor",
            "client_session_id": "agent-real",
            "schema_version": 2,
            "updated_at": self._now_iso(hours_ago=2),
        }))

        stdout, _ = _serve_and_run(
            tmp_path, cwd=workspace, claude_session_id="post-clear-session"
        )
        ctx = json.loads(stdout).get("additional_context", "")

        # Planted UUID must NOT surface.
        assert "deadbeef-aaaa-bbbb-cccc-000000000999" not in ctx
        assert "Attacker_Plant" not in ctx
        # Real predecessor surfaces in its place — the loop fell through.
        assert "77777777-aaaa-bbbb-cccc-000000000777" in ctx

    def test_scan_newest_falls_through_malformed_updated_at(self, tmp_path):
        """T2 from the council review: when the newest-sorted entry has a
        malformed ``updated_at``, parse_ts returns None and the loop must
        fall through to the next valid entry — not give up after the first.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        # Malformed entry — cmd_list sinks unparseable timestamps to the
        # bottom under the new sort, so we use an explicit non-string
        # value to verify the hook's own parse_ts also rejects it cleanly
        # if cmd_list ever changes.
        (workspace / ".unitares" / "session-malformed.json").write_text(json.dumps({
            "uuid": "88888888-aaaa-bbbb-cccc-000000000888",
            "agent_id": "Malformed_Ts",
            "client_session_id": "agent-malformed",
            "schema_version": 2,
            "updated_at": "not-a-real-iso-timestamp",
        }))
        # Valid entry — should be chosen since malformed one is dropped.
        (workspace / ".unitares" / "session-valid.json").write_text(json.dumps({
            "uuid": "99999999-aaaa-bbbb-cccc-000000000999",
            "agent_id": "Valid_Predecessor",
            "client_session_id": "agent-valid",
            "schema_version": 2,
            "updated_at": self._now_iso(hours_ago=3),
        }))

        stdout, _ = _serve_and_run(
            tmp_path, cwd=workspace, claude_session_id="post-clear-session"
        )
        ctx = json.loads(stdout).get("additional_context", "")

        assert "88888888-aaaa-bbbb-cccc-000000000888" not in ctx
        assert "99999999-aaaa-bbbb-cccc-000000000999" in ctx

    def test_scan_newest_skips_legacy_flat_session_json(self, tmp_path):
        """Legacy flat ``session.json`` (slot=None in cmd_list output) must
        not be surfaced as lineage even when it is the only file present.
        Same axiom as ``test_bare_session_json_is_not_surfaced``: the flat
        file is a cross-instance shared artifact, not a trustworthy
        per-process lineage anchor."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        (workspace / ".unitares" / "session.json").write_text(json.dumps({
            "uuid": "66666666-aaaa-bbbb-cccc-000000000006",
            "agent_id": "Legacy_Flat",
            "client_session_id": "agent-legacy",
            "schema_version": 1,
            "updated_at": self._now_iso(hours_ago=1),
        }))

        stdout, _ = _serve_and_run(
            tmp_path, cwd=workspace, claude_session_id="post-clear-session"
        )
        ctx = json.loads(stdout).get("additional_context", "")

        assert "66666666-aaaa-bbbb-cccc-000000000006" not in ctx
        assert "Legacy_Flat" not in ctx

    def test_scan_newest_rejects_unsafe_slot_filename(self, tmp_path):
        """A same-UID actor can drop ``session-*.json`` files directly on
        disk, bypassing the writer's ``_slot_suffix`` sanitization. The
        parsed slot is later reflected into agent context as ``slot
        \\`{slot}\\```; a backtick or whitespace in the slot would
        terminate the markdown code-span and become a prompt-injection
        vector. ``_parse_session_filename`` re-validates the parsed slot
        against the same shape ``_slot_suffix`` produces, so unsafe
        filenames are skipped — the loop falls through to the next
        well-formed entry.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        # Filename with a backtick — bypasses _slot_suffix because the
        # writer would have replaced it with `_`. cmd_list must drop it.
        unsafe_name = "session-bad`tick.json"
        (workspace / ".unitares" / unsafe_name).write_text(json.dumps({
            "uuid": "bad00000-aaaa-bbbb-cccc-000000000bad",
            "agent_id": "Backtick_Plant",
            "client_session_id": "agent-backtick",
            "schema_version": 2,
            "updated_at": self._now_iso(hours_ago=1),  # newer than the valid one
        }))
        # Well-formed predecessor; should win after the unsafe one is dropped.
        (workspace / ".unitares" / "session-real.json").write_text(json.dumps({
            "uuid": "aaaaaaaa-aaaa-bbbb-cccc-000000000aaa",
            "agent_id": "Real_Predecessor",
            "client_session_id": "agent-real",
            "schema_version": 2,
            "updated_at": self._now_iso(hours_ago=2),
        }))

        stdout, _ = _serve_and_run(
            tmp_path, cwd=workspace, claude_session_id="post-clear-session"
        )
        ctx = json.loads(stdout).get("additional_context", "")

        # Unsafe filename must NOT surface — UUID and agent_id stay out
        # of agent context, and the literal backtick never appears in the
        # `slot \`{slot}\`` reflection.
        assert "bad00000-aaaa-bbbb-cccc-000000000bad" not in ctx
        assert "Backtick_Plant" not in ctx
        assert "bad`tick" not in ctx
        # Real predecessor wins — loop fell through.
        assert "aaaaaaaa-aaaa-bbbb-cccc-000000000aaa" in ctx


class TestSkillInjection:
    """Fundamentals skill content is injected on both paths (online/offline)."""

    def test_online_context_includes_skill(self, tmp_path):
        stdout, _ = _serve_and_run(tmp_path)
        ctx = json.loads(stdout).get("additional_context", "")
        assert "Governance Fundamentals" in ctx

    def test_offline_context_includes_skill(self, tmp_path):
        stdout, _ = _run_hook(tmp_path, "http://127.0.0.1:1")
        ctx = json.loads(stdout).get("additional_context", "")
        assert "Governance Fundamentals" in ctx


class TestCompactMode:
    """When the slot-scoped workspace cache is fresh (mtime within TTL), the
    hook collapses the full Fundamentals + onboard prose to a one-line
    prompt. Per-turn SessionStart fires legitimately under the v2 identity
    ontology — re-injecting ~3KB of unchanged context every fire is
    repetition without information.
    """

    def _make_fresh_cache(self, workspace, slot, uuid="aaaa1111-2222-3333-4444-555555555555"):
        (workspace / ".unitares").mkdir(exist_ok=True)
        cache = workspace / ".unitares" / f"session-{slot}.json"
        cache.write_text(json.dumps({
            "uuid": uuid,
            "agent_id": "Prior_Agent",
            "display_name": "Prior_Agent",
            "schema_version": 2,
            "updated_at": "2026-04-25T17:00:00+00:00",
        }))
        return cache

    def test_fresh_cache_triggers_compact_prose(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        slot = "claude-fresh-slot"
        self._make_fresh_cache(workspace, slot)

        stdout, _ = _serve_and_run(tmp_path, cwd=workspace, claude_session_id=slot)
        ctx = json.loads(stdout).get("additional_context", "")

        # Compact prose markers: no pressure nudge, but security and recovery
        # pointers remain available.
        assert "Slot-scoped governance state is present" in ctx
        assert "ACTION REQUIRED" not in ctx
        assert "onboard now" not in ctx
        assert "force_new=true" in ctx  # security regression guard still applies
        assert "/diagnose" in ctx  # operator escape hatch retained
        assert "next_action" in ctx
        assert "memory_suggestions" in ctx
        assert "recovery_hint" in ctx
        # The full-prose marker is gone
        assert "No identity has been created on your behalf" not in ctx
        # Fundamentals excerpt is suppressed in compact mode
        assert "Governance Fundamentals" not in ctx

    def test_fresh_cache_still_surfaces_lineage_hint(self, tmp_path):
        """Compact mode drops the boilerplate but keeps the per-instance
        lineage signal — that's specific information, not repetition."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        slot = "claude-fresh-slot-2"
        self._make_fresh_cache(workspace, slot, uuid="bbbb1111-2222-3333-4444-666666666666")

        stdout, _ = _serve_and_run(tmp_path, cwd=workspace, claude_session_id=slot)
        ctx = json.loads(stdout).get("additional_context", "")

        assert "bbbb1111-2222-3333-4444-666666666666" in ctx
        assert "parent_agent_id" in ctx

    def test_stale_cache_falls_back_to_full_prose(self, tmp_path):
        """When the cache mtime is beyond the TTL, the agent has likely
        rotated context out — re-inject the full Fundamentals and prose."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        slot = "claude-stale-slot"
        cache = self._make_fresh_cache(workspace, slot)
        # Force mtime to 2 hours ago
        old = subprocess.run(
            ["touch", "-t", "202604251400.00", str(cache)],
            check=True,
        )

        # Use a short TTL just to be unambiguous about the boundary.
        stdout, _ = _serve_and_run(
            tmp_path,
            cwd=workspace,
            claude_session_id=slot,
            extra_env={"UNITARES_HOOK_COMPACT_TTL": "60"},
        )
        ctx = json.loads(stdout).get("additional_context", "")

        # Full prose markers
        assert "SessionStart has not created an identity yet" in ctx
        assert "Stop hook will lazily create a slot-scoped identity" in ctx
        assert "Governance Fundamentals" in ctx

    def test_no_cache_means_full_prose(self, tmp_path):
        """A truly fresh workspace (no cache) must get the full prose —
        compact mode is for repeat sessions, not first-time onboarding."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        stdout, _ = _serve_and_run(
            tmp_path, cwd=workspace, claude_session_id="never-seen-before"
        )
        ctx = json.loads(stdout).get("additional_context", "")
        assert "SessionStart has not created an identity yet" in ctx
        assert "Stop hook will lazily create a slot-scoped identity" in ctx
        assert "Governance Fundamentals" in ctx

    def test_compact_ttl_is_configurable_via_env(self, tmp_path):
        """Operators can tune the TTL via UNITARES_HOOK_COMPACT_TTL.
        Setting it to 0 effectively disables compact mode."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        slot = "claude-ttl-slot"
        self._make_fresh_cache(workspace, slot)

        stdout, _ = _serve_and_run(
            tmp_path,
            cwd=workspace,
            claude_session_id=slot,
            extra_env={"UNITARES_HOOK_COMPACT_TTL": "0"},
        )
        ctx = json.loads(stdout).get("additional_context", "")
        # TTL=0 means age (>=0) is never less than TTL → full prose returns
        assert "SessionStart has not created an identity yet" in ctx
        assert "Stop hook will lazily create a slot-scoped identity" in ctx
        assert "Governance Fundamentals" in ctx

    def test_compact_mode_substantially_reduces_context_size(self, tmp_path):
        """The whole point of this mode — verify the compact path is
        materially smaller. Threshold is generous (compact must be at
        least 60% smaller) so future copy edits don't break the test."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        slot = "claude-size-slot"
        self._make_fresh_cache(workspace, slot)

        compact_stdout, _ = _serve_and_run(
            tmp_path, cwd=workspace, claude_session_id=slot
        )
        compact_len = len(json.loads(compact_stdout).get("additional_context", ""))

        full_stdout, _ = _serve_and_run(
            tmp_path,
            cwd=workspace,
            claude_session_id=slot,
            extra_env={"UNITARES_HOOK_COMPACT_TTL": "0"},
        )
        full_len = len(json.loads(full_stdout).get("additional_context", ""))

        assert compact_len < full_len * 0.4, (
            f"compact={compact_len}, full={full_len} — expected >=60% reduction"
        )


class TestOnboardingNudgeOnce:
    """Issue #735: the onboarding note must not repeat every turn before the
    stop hook has a chance to lazily onboard the session."""

    def test_no_cache_repeats_suppress_optional_note_for_same_session(self, tmp_path):
        slot = "same-session-no-cache"
        first_stdout, _ = _serve_and_run(tmp_path, claude_session_id=slot)
        first_ctx = json.loads(first_stdout).get("additional_context", "")
        assert "Identity attribution" in first_ctx
        assert "start_session(force_new=true)" in first_ctx
        assert "Governance Fundamentals" in first_ctx

        second_stdout, _ = _serve_and_run(tmp_path, claude_session_id=slot)
        second_ctx = json.loads(second_stdout).get("additional_context", "")
        assert "Onboarding context was already shown" in second_ctx
        assert "Identity attribution" not in second_ctx
        assert "start_session(" not in second_ctx
        assert "Governance Fundamentals" not in second_ctx
        assert "Do not manufacture a check-in" in second_ctx
        assert "Stop hook will lazily create a slot-scoped governance identity" in second_ctx


class TestOrchestratorProvisionedLineage:
    """Spawn-context env lineage source (UNITARES_PARENT_AGENT_ID).

    The BEAM agent orchestrator (unitares PR #648) provisions
    UNITARES_PARENT_AGENT_ID / UNITARES_SPAWN_REASON into spawned agents'
    env as candidate declarations. The hook surfaces them as the
    highest-confidence lineage candidate — ground truth from the spawner,
    outranking slot-file and scan-newest inference — while keeping the
    candidate-not-credential posture (declare via parent_agent_id, agent
    may decline).
    """

    ENV_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    def test_env_lineage_surfaces_with_template_and_spawn_reason(self, tmp_path):
        stdout, _ = _serve_and_run(
            tmp_path,
            extra_env={
                "UNITARES_PARENT_AGENT_ID": self.ENV_UUID,
                "UNITARES_SPAWN_REASON": "explicit",
            },
        )
        ctx = json.loads(stdout).get("additional_context", "")

        assert "Orchestrator-provisioned" in ctx
        assert f'parent_agent_id="{self.ENV_UUID}"' in ctx
        assert 'spawn_reason="explicit"' in ctx
        # Candidate-not-credential posture preserved.
        assert "candidate, not an obligation" in ctx
        assert "resume=true" not in ctx
        assert "identity(agent_uuid=" not in ctx

    def test_spawn_reason_defaults_to_subagent(self, tmp_path):
        stdout, _ = _serve_and_run(
            tmp_path,
            extra_env={"UNITARES_PARENT_AGENT_ID": self.ENV_UUID},
        )
        ctx = json.loads(stdout).get("additional_context", "")
        assert 'spawn_reason="subagent"' in ctx

    def test_env_lineage_outranks_slot_scoped_cache(self, tmp_path):
        """Spawn-context ground truth wins over workspace-file inference."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        slot = "claude-session-env-vs-slot"
        (workspace / ".unitares" / f"session-{slot}.json").write_text(json.dumps({
            "uuid": "ffffffff-1111-2222-3333-444444444444",
            "agent_id": "Claude_Workspace_X",
            "display_name": "Claude_Workspace_X",
            "continuity_token": "",
            "schema_version": 2,
            "updated_at": "2026-04-20T00:00:00+00:00",
        }))

        stdout, _ = _serve_and_run(
            tmp_path,
            cwd=workspace,
            claude_session_id=slot,
            extra_env={"UNITARES_PARENT_AGENT_ID": self.ENV_UUID},
        )
        ctx = json.loads(stdout).get("additional_context", "")

        assert self.ENV_UUID in ctx
        # The slot UUID must NOT also surface — one candidate, never a menu.
        assert "ffffffff-1111-2222-3333-444444444444" not in ctx

    def test_malformed_env_uuid_is_ignored_and_slot_still_works(self, tmp_path):
        """A polluted env var must not become a copy-pasteable suggestion."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".unitares").mkdir()
        slot = "claude-session-malformed-env"
        (workspace / ".unitares" / f"session-{slot}.json").write_text(json.dumps({
            "uuid": "ffffffff-1111-2222-3333-444444444444",
            "agent_id": "Claude_Workspace_X",
            "display_name": "Claude_Workspace_X",
            "continuity_token": "",
            "schema_version": 2,
            "updated_at": "2026-04-20T00:00:00+00:00",
        }))

        stdout, _ = _serve_and_run(
            tmp_path,
            cwd=workspace,
            claude_session_id=slot,
            extra_env={"UNITARES_PARENT_AGENT_ID": "not-a-uuid; rm -rf /"},
        )
        ctx = json.loads(stdout).get("additional_context", "")

        assert "not-a-uuid" not in ctx
        assert "Orchestrator-provisioned" not in ctx
        # Falls through to the slot-scoped source.
        assert "ffffffff-1111-2222-3333-444444444444" in ctx

    def test_malformed_spawn_reason_falls_back_to_subagent(self, tmp_path):
        """spawn_reason is embedded in a quoted template — no breakout."""
        stdout, _ = _serve_and_run(
            tmp_path,
            extra_env={
                "UNITARES_PARENT_AGENT_ID": self.ENV_UUID,
                "UNITARES_SPAWN_REASON": 'evil" ); rm -rf /; echo "',
            },
        )
        ctx = json.loads(stdout).get("additional_context", "")

        assert "rm -rf" not in ctx
        assert 'spawn_reason="subagent"' in ctx

    def test_env_lineage_makes_no_tool_calls(self, tmp_path):
        """The new source keeps the load-bearing no-mutation invariant."""
        _, calls = _serve_and_run(
            tmp_path,
            extra_env={"UNITARES_PARENT_AGENT_ID": self.ENV_UUID},
        )
        mutating = [
            c for c in calls
            if (c.get("tool") or c.get("name") or "") not in ("", "skills")
        ]
        assert mutating == []


def _git_q(repo, *args):
    """Run a quiet git command; identity is supplied inline so the test needs
    no global git config or author env."""
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class TestWorkspaceCoordinationBriefing:
    """Git-sourced collision briefing.

    The substrate can't answer "who else is live here" — that needs the very
    check-ins we're trying to elicit, and status='active' is never reaped. So
    the hook sources liveness from git worktree state (adoption-independent)
    and surfaces sibling worktrees carrying UNCOMMITTED edits — the collision
    hotspots. Precision-or-silence: clean siblings produce no briefing, and a
    non-git cwd produces none at all.
    """

    def _repo_with_sibling(self, tmp_path, *, dirty):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_q(repo, "init", "-q", "-b", "master")
        (repo / "README.md").write_text("seed\n")
        _git_q(repo, "add", ".")
        _git_q(repo, "commit", "-qm", "init")
        sib = tmp_path / "sib"
        _git_q(repo, "worktree", "add", "-q", "-b", "codex/test-sib", str(sib))
        (sib / "shared.py").write_text("change\n")  # uncommitted edit in sibling
        if not dirty:
            _git_q(sib, "add", ".")
            _git_q(sib, "commit", "-qm", "commit the sibling change")
        return repo, sib

    def test_briefing_surfaces_dirty_sibling(self, tmp_path):
        repo, _sib = self._repo_with_sibling(tmp_path, dirty=True)
        stdout, _ = _run_hook(tmp_path, "http://127.0.0.1:1", cwd=repo)
        ctx = json.loads(stdout).get("additional_context", "")
        assert "Workspace coordination" in ctx
        assert "codex/test-sib" in ctx
        assert "shared.py" in ctx
        # Honest framing: a heads-up sourced from git, not a governance lock.
        assert "NOT governance state" in ctx

    def test_briefing_silent_when_siblings_clean(self, tmp_path):
        repo, _sib = self._repo_with_sibling(tmp_path, dirty=False)
        stdout, _ = _run_hook(tmp_path, "http://127.0.0.1:1", cwd=repo)
        ctx = json.loads(stdout).get("additional_context", "")
        assert "Workspace coordination" not in ctx

    def test_briefing_absent_outside_git_repo(self, tmp_path):
        # tmp_path is not a git work tree -> no briefing, and the hook still
        # emits a well-formed envelope (fail-safe).
        stdout, _ = _run_hook(tmp_path, "http://127.0.0.1:1")
        payload = json.loads(stdout)
        assert "Workspace coordination" not in payload.get("additional_context", "")
        assert "additionalContext" in payload.get("hookSpecificOutput", {})

    def _seed_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_q(repo, "init", "-q", "-b", "master")
        (repo / "README.md").write_text("seed\n")
        _git_q(repo, "add", ".")
        _git_q(repo, "commit", "-qm", "init")
        return repo

    def _add_sibling(self, repo, tmp_path, branch, *, dirty_files=()):
        wt = tmp_path / branch.replace("/", "_")
        _git_q(repo, "worktree", "add", "-q", "-b", branch, str(wt))
        for dirty_file in dirty_files:
            (wt / dirty_file).write_text("change\n")
        return wt

    def test_self_on_agent_branch_not_reported_as_sibling(self, tmp_path):
        _repo, sibling = self._repo_with_sibling(tmp_path, dirty=True)
        stdout, _ = _run_hook(tmp_path, "http://127.0.0.1:1", cwd=sibling)
        ctx = json.loads(stdout).get("additional_context", "")
        assert "Workspace coordination" not in ctx

    def test_briefing_preserves_filenames_with_spaces(self, tmp_path):
        repo = self._seed_repo(tmp_path)
        self._add_sibling(
            repo,
            tmp_path,
            "codex/spaces",
            dirty_files=("my shared file.py",),
        )
        stdout, _ = _run_hook(tmp_path, "http://127.0.0.1:1", cwd=repo)
        ctx = json.loads(stdout).get("additional_context", "")
        assert "my shared file.py" in ctx

    def test_briefing_counts_dirty_siblings_and_marks_truncation(self, tmp_path):
        repo = self._seed_repo(tmp_path)
        self._add_sibling(
            repo,
            tmp_path,
            "codex/dirty-a",
            dirty_files=("a.py", "b.py", "c.py", "d.py"),
        )
        self._add_sibling(
            repo,
            tmp_path,
            "claude/dirty-b",
            dirty_files=("other.py",),
        )
        self._add_sibling(repo, tmp_path, "codex/clean-c")

        stdout, _ = _run_hook(tmp_path, "http://127.0.0.1:1", cwd=repo)
        ctx = json.loads(stdout).get("additional_context", "")

        assert "2 sibling worktree(s) carry uncommitted" in ctx
        assert "codex/clean-c" not in ctx
        assert "(+1 more)" in ctx

    def test_briefing_surfaces_dirty_detached_sibling(self, tmp_path):
        repo = self._seed_repo(tmp_path)
        sibling = self._add_sibling(repo, tmp_path, "codex/detached")
        _git_q(sibling, "switch", "--detach", "-q")
        (sibling / "detached work.py").write_text("change\n")

        stdout, _ = _run_hook(tmp_path, "http://127.0.0.1:1", cwd=repo)
        ctx = json.loads(stdout).get("additional_context", "")

        assert "DETACHED:" in ctx
        assert "detached work.py" in ctx

    def test_briefing_can_be_disabled(self, tmp_path):
        repo, _sibling = self._repo_with_sibling(tmp_path, dirty=True)
        stdout, _ = _run_hook(
            tmp_path,
            "http://127.0.0.1:1",
            cwd=repo,
            extra_env={"UNITARES_HOOK_SKIP_WORKSPACE_BRIEFING": "1"},
        )
        ctx = json.loads(stdout).get("additional_context", "")
        assert "Workspace coordination" not in ctx
