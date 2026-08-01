#!/usr/bin/env python3
"""PreToolUse injector — ensure governance MCP calls carry client_session_id.

Claude Code's streamable HTTP transport is stateless (no Mcp-Session-Id), so
any governance call that omits identity arguments resolves server-side via
the `recent_onboard:<ip:ua>` Redis pin — a heuristic that any parallel
non-subagent onboard on the same host can legitimately displace (the
2026-06-10 driver-capture incident class; server PR #604 closed the subagent
displacement vector, this hook removes the driver's dependence on the pin
entirely). Calls carrying client_session_id resolve at step 2 with
identity_assurance tier=strong and are immune to the pin.

This hook also normalizes the `tags` list on knowledge-graph calls (casing,
separators, de-dup) before they reach the server, so the shared graph does
not fragment on `Postgres`/`postgres`/`PostgreSQL`. That pass is
formatting-only, runs independently of (and never disables) identity
injection, and lives in scripts/tag_normalize.py — see docs/ontology-need.md.
Codex does not rewrite multi-action knowledge calls because applying any input
rewrite there would also pre-approve the call.

Contract:
- Only fires for MCP tools whose case-normalized server segment exactly matches
  a configured UNITARES alias. Lookalike names receive no rewrite or implicit
  Codex approval.
- Only fires for an explicit suffix allowlist of attribution-relevant tools
  verified to accept client_session_id (schemas inherit AgentIdentityMixin
  server-side). Unknown/new tools get NO injection — they degrade to today's
  pin behavior rather than risking an extra-field validation error.
- For identity minting tools, only fires for orchestrated anchored bare
  onboard/start_session calls: UNITARES_CLIENT_SESSION_ID and a truthy
  UNITARES_ORCHESTRATED marker must both be present, and force_new must be
  absent. Explicit force_new and explicit proof fields still win. Never
  injects into bind_session/identity.
- Skips when the call already carries any identity proof field
  (client_session_id, continuity_token, agent_uuid, agent_id) — explicit
  caller intent always wins.
- Reads ONLY the slot-scoped session cache (slot = Claude session_id via
  _session_lookup; no workspace-flat fallback). With the post-identity
  subagent guard, the slot cache holds the driver's identity.
- Claude emits hookSpecificOutput.updatedInput without a permission decision,
  so its normal permission flow is unchanged. Codex requires
  permissionDecision="allow" whenever updatedInput is returned. Codex rewrites
  only anchored lifecycle, check-in, and read-only diagnostic calls; admin-
  capable tools keep their normal permission flow and require explicit proof.
- Fails open: any error or missing cache → no output, tool runs untouched.

Known residual (KNOWINGLY ACCEPTED, council-reviewed 2026-06-12): both this
guard and server #604 key on spawn_reason=="subagent" being DECLARED. A
non-declaring subagent is invisible to both: its onboard still overwrites
the slot cache (post-identity can't see it), and this injector would then
propagate that poisoned identity into the driver's later argless calls at
tier=strong — overriding the otherwise-protective server pin. For DECLARED
subagents the no-widening claim holds: their argless calls already resolve
to the driver via the standing SET-NX pin, so injection makes the same
attribution explicit and lintable. The deferred close-path for the
non-declaring residual is subagent discrimination from hook context
(e.g. transcript_path), rejected for now because misclassifying a
succession onboard would dark-session the driver — the inverse failure.

Epistemic note: rows resolved through an injected client_session_id read
server-side as identity_assurance tier=strong / explicit_client_session_id.
That tier here reflects HARNESS-MAINTAINED process continuity, not an
agent-asserted proof — the agent never typed the token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from governance_tool_name import governance_tool_suffix

# Tag normalization (formatting-only, fail-open) is folded into this single
# hook on purpose: a second PreToolUse hook emitting its own updatedInput for
# the same call would risk clobbering the client_session_id injected here. The
# import is guarded so a tag_normalize failure can never disable the
# identity-injection path below. See scripts/tag_normalize.py and
# docs/ontology-need.md.
try:
    from tag_normalize import TAG_BEARING_SUFFIXES, normalize_call_tags
except Exception:  # pragma: no cover - identity injection must work alone
    TAG_BEARING_SUFFIXES = frozenset()

    def normalize_call_tags(_tool_input: dict) -> bool:  # type: ignore[misc]
        return False

# Attribution-relevant tools whose server schemas accept client_session_id
# (all inherit AgentIdentityMixin), plus their friendly-workflow aliases.
# Deliberately EXCLUDES every identity-minting/resuming/binding tool from the
# general slot-cache injector. onboard/start_session have a narrower anchored
# exception below: inject UNITARES_CLIENT_SESSION_ID only when force_new is
# absent. identity/bind_session remain excluded because injecting proof there
# would silently flip assert/bind semantics (council finding 2026-06-12).
# Also excludes any tool not verified against the server schemas.
INJECT_SUFFIXES = frozenset({
    "process_agent_update", "sync_state",
    "get_governance_metrics", "check_working_state",
    "outcome_event", "record_result",
    "knowledge", "search_shared_memory", "leave_note",
    "dialectic", "request_review",
    "observe", "calibration", "export", "config",
    "agent", "self_recovery", "archive_orphan_agents",
    "list_tools", "describe_tool", "health_check",
})

# Codex can only apply updatedInput by also allowing the tool call. Keep that
# implicit approval narrowly scoped to lifecycle/check-in and read-only
# diagnostics. Administrative and multi-action tools must carry identity
# explicitly so their normal permission flow remains intact.
CODEX_REWRITE_SUFFIXES = frozenset({
    "process_agent_update", "sync_state",
    "get_governance_metrics", "check_working_state",
    "list_tools", "describe_tool", "health_check",
})

ANCHORED_MINT_SUFFIXES = frozenset({"onboard", "start_session"})
PROOF_FIELDS = ("client_session_id", "continuity_token", "agent_uuid", "agent_id")


def _hook_output(updated: dict, host: str) -> dict:
    """Build the host-specific PreToolUse rewrite envelope."""
    specific: dict[str, object] = {
        "hookEventName": "PreToolUse",
        "updatedInput": updated,
    }
    if host == "codex":
        # Codex ignores updatedInput unless the hook explicitly allows it.
        specific["permissionDecision"] = "allow"
    return {"hookSpecificOutput": specific}


def _has_proof_field(tool_input: dict) -> bool:
    """True if the caller supplied a non-empty identity proof field."""
    for field in PROOF_FIELDS:
        value = tool_input.get(field)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        # Any non-empty proof field: the caller declared identity intent.
        return True
    return False


def _env_anchor() -> str:
    orchestrated = os.environ.get("UNITARES_ORCHESTRATED", "").strip().lower()
    if orchestrated not in {"1", "true", "yes", "on"}:
        return ""
    value = os.environ.get("UNITARES_CLIENT_SESSION_ID", "")
    return value.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize a governance PreToolUse call")
    parser.add_argument("--host", choices=("claude", "codex"), default="claude")
    args = parser.parse_args(argv)

    try:
        raw = sys.stdin.read()
        data = json.loads(raw or "{}")
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0

    suffix = governance_tool_suffix(data.get("tool_name"), host=args.host)
    if suffix is None:
        return 0
    if suffix not in INJECT_SUFFIXES and suffix not in ANCHORED_MINT_SUFFIXES:
        return 0
    if (
        args.host == "codex"
        and suffix not in CODEX_REWRITE_SUFFIXES
        and suffix not in ANCHORED_MINT_SUFFIXES
    ):
        return 0

    tool_input = data.get("tool_input")
    if tool_input is None:
        tool_input = {}
    if not isinstance(tool_input, dict):
        return 0

    updated = dict(tool_input)
    changed = False

    if suffix in ANCHORED_MINT_SUFFIXES:
        # Anchored dispatch/beam conversations provide a stable per-thread
        # resume proof through the environment. A manual bare start_session()
        # should resume through that anchor just like the Stop hook's lazy
        # onboarding path. The exception is intentionally narrow: if the agent
        # explicitly names force_new or any proof field, pass the call through.
        anchor = _env_anchor()
        if anchor and "force_new" not in tool_input and not _has_proof_field(tool_input):
            updated["client_session_id"] = anchor
            changed = True
        if not changed:
            return 0
        print(json.dumps(_hook_output(updated, args.host)))
        return 0

    # Tag normalization runs independently of identity injection: it applies
    # whenever a tag-bearing knowledge call carries a `tags` list, regardless
    # of cache state or proof fields. Failures are swallowed and the partial
    # edit discarded so the identity path below is never affected.
    if suffix in TAG_BEARING_SUFFIXES:
        try:
            if normalize_call_tags(updated):
                changed = True
        except Exception:
            updated = dict(tool_input)

    # Identity injection: add the slot-cached client_session_id only when the
    # caller declared no identity intent of its own. A present proof field
    # means explicit caller intent always wins — skip injection, but still
    # honor any tag normalization above.
    if not _has_proof_field(tool_input):
        try:
            from _session_lookup import load_session_for_hook
            sess = load_session_for_hook(Path.cwd(), raw)
        except Exception:
            sess = None
        sid = (sess or {}).get("client_session_id")
        if isinstance(sid, str) and sid.strip():
            updated["client_session_id"] = sid.strip()
            changed = True

    if not changed:
        return 0
    print(json.dumps(_hook_output(updated, args.host)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
