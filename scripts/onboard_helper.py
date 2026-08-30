#!/usr/bin/env python3
"""Onboard helper for UNITARES client hooks.

Owns the flow:

1. Read the existing slot-scoped session cache (if any).
2. Call ``onboard(force_new=true, onboard_origin="harness_backstop")``. When
   the cache has a UUID, pass it as ``parent_agent_id`` with
   ``spawn_reason="new_session"``. Orchestrated anchor resumes instead report
   ``onboard_origin="orchestrated_resume"``.
3. If the server reports ``trajectory_required`` (identity exists but lacks
   a verifiable signal), return status=``trajectory_required`` with the
   server's recovery hint. We do NOT auto-retry with ``force_new=true``;
   that is an explicit operator decision, not an automatic one (see commit
   718ccd3 and the identity "never silently substitute" invariant).
4. ``force_new=true`` is always sent on startup. ``--force-new`` only means
   "ignore cached lineage".
5. Only write the cache when onboard succeeded and produced a usable uuid.

Emits a JSON line on stdout with the resolved fields for the shell hook to
consume. Never raises — always returns a dict on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from _http_auth import authorization_safe_urlopen
from _session_cache_io import (
    home_session_mirror_is_valid,
    replace_session_cache,
    reserve_session_cache_snapshot,
    update_session_cache,
)

DEFAULT_SERVER_URL = "http://localhost:8767"
DEFAULT_TIMEOUT = 10.0
CACHE_DIR = ".unitares"
CACHE_FILE = "session.json"
CacheGenerationToken = Tuple[int, Optional[int]]

# Genesis bootstrap — an OPTIONAL trajectory anchor (OFF by default).
#
# Passing ``initial_state`` makes the server write a synthetic
# ``source='bootstrap'`` state row immediately after identity creation. This is
# NOT a fix for the "uninitialized, 0 updates" symptom: bootstrap rows are
# excluded server-side from calibration, outcome correlation, trust-tier
# observation counts, and **real-check-in counts**, so an agent with only a
# genesis row still reads as uninitialized / 0 real updates. Only a genuine
# ``process_agent_update`` (``sync_state``) clears that — see the lazy-onboard
# guidance in skills/governance-lifecycle and the session-start hook.
#
# The seed's sole benefit is giving the agent's FIRST real check-in a baseline,
# so it registers as a trajectory *delta* instead of a lone point. That is a
# minor nicety, so it is opt-in: per-call ``bootstrap=True`` / ``--bootstrap``,
# or globally via ``UNITARES_ONBOARD_BOOTSTRAP=1``. An explicit caller-supplied
# ``initial_state`` is always honored regardless of the flag.
BOOTSTRAP_RESPONSE_TEXT = "Genesis: identity created via plugin onboard (trajectory seed)."
BOOTSTRAP_COMPLEXITY = 0.1
BOOTSTRAP_CONFIDENCE = 0.5
HARNESS_BACKSTOP_ORIGIN = "harness_backstop"
ORCHESTRATED_RESUME_ORIGIN = "orchestrated_resume"


def _env_truthy(value: str | None) -> bool:
    """Return True only for explicit affirmative env values."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _default_bootstrap_state() -> dict:
    """Minimal genesis check-in payload for ``onboard(initial_state=...)``.

    Mirrors the canonical check-in fields (``response_text``/``complexity``/
    ``confidence``) so the server can persist it through the same path as a
    real ``process_agent_update`` — the server tags the row ``source='bootstrap'``
    itself, so we deliberately do not set a source key here.
    """
    return {
        "response_text": BOOTSTRAP_RESPONSE_TEXT,
        "complexity": BOOTSTRAP_COMPLEXITY,
        "confidence": BOOTSTRAP_CONFIDENCE,
    }


def _bootstrap_enabled() -> bool:
    """Global opt-in for genesis seeding (``UNITARES_ONBOARD_BOOTSTRAP=1``). Off by default."""
    return _env_truthy(os.environ.get("UNITARES_ONBOARD_BOOTSTRAP"))


def _slot_filename(slot: str | None) -> str:
    """Return the cache filename, optionally namespaced by a slot key.

    Without a slot, returns the legacy shared "session.json". With a slot
    (typically the Claude Code session_id from the hook input JSON), returns
    "session-<safe-slot>.json". This lets N parallel ``claude`` processes in
    the SAME workspace each maintain their own identity rather than racing
    on a single cache file. See KG note 2026-04-14: "multiple claude agents
    sharing UUID" — that was per-workspace cache + multiple processes.
    """
    if not slot:
        return CACHE_FILE
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)
    safe = safe[:64]  # keep file names sane
    return f"session-{safe}.json"


def _scope_name_by_slot(agent_name: str, slot: str | None) -> str:
    """Append a short, stable slot fingerprint to the agent name.

    Why this exists: the server's onboard handler runs a name-claim lookup
    (``resolve_by_name_claim`` in src/mcp_handlers/identity/resolution.py)
    that matches an existing agent purely by label. Two parallel Claude
    processes in the same workspace would send the same ``name`` (the
    workspace basename) and both get bound to whichever agent already owns
    that label — even though each has its own slot-isolated cache.

    Scoping the name by slot defeats the name-claim at the client. Each
    conversation (slot) gets its own label, its own UUID, its own
    trajectory. Existing slot caches keep working as lineage anchors:
    ``run_onboard`` passes the cached UUID as ``parent_agent_id`` on a
    forced fresh onboard.

    Unslotted callers (Codex stdio, single-process flows) keep the legacy
    naming — this only scopes when a slot is actually provided.

    The architectural fix (remove name-claim, or seed trajectory at
    creation so the trajectory_required guard always fires) is tracked
    separately; see the project memory entry on name-claim ghosts.
    """
    if not slot:
        return agent_name
    # Hash the full slot so fingerprints collide only on a genuine hash
    # clash (~1 in 4 billion for 8 hex chars), not when two slots happen to
    # share a prefix. An earlier version used slot[:8] directly, which
    # broke for workloads where slots share a common prefix (e.g. tests
    # using "itest-slot-*" or CI runners that stamp a pipeline prefix on
    # every session id).
    fingerprint = hashlib.md5(slot.encode("utf-8")).hexdigest()[:8]
    return f"{agent_name}#{fingerprint}"


# --- IO primitives (separable for tests) -----------------------------------

def _failure_response(
    *,
    error: str,
    reason: str,
    hint: str = "",
    detail: str = "",
    status_code: int | None = None,
) -> dict[str, Any]:
    """Return a parsed-response-shaped failure for helper diagnostics."""
    response: dict[str, Any] = {
        "success": False,
        "error": error,
        "recovery": {
            "reason": reason,
            "hint": hint,
        },
    }
    if detail:
        response["detail"] = detail
    if status_code is not None:
        response["status_code"] = status_code
    return response


def _post_json(url: str, payload: dict, timeout: float, token: str | None) -> dict:
    """POST JSON to ``url`` and return the parsed response or a structured failure."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with authorization_safe_urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return _failure_response(
            error=f"onboard HTTP request failed: {exc}",
            reason="http_error",
            hint="check UNITARES server health and authentication",
            status_code=exc.code,
        )
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return _failure_response(
            error="onboard transport request failed",
            reason="transport_error",
            hint="check that the UNITARES server is reachable; sandboxed clients may need network permission",
            detail=str(exc),
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        return _failure_response(
            error="onboard returned invalid JSON",
            reason="invalid_json_response",
            hint="check the UNITARES server endpoint and logs",
            detail=str(exc),
        )


def _cache_path(workspace: Path, slot: str | None = None) -> Path:
    return workspace / CACHE_DIR / _slot_filename(slot)


def _home_cache_path(slot: str | None) -> Path | None:
    if not slot:
        return None
    return Path.home() / CACHE_DIR / _slot_filename(slot)


def _read_cache(workspace: Path, slot: str | None = None) -> dict:
    """Read the cache for this slot. No cross-slot fallback.

    Each slot (Claude Code session) gets its own identity. When no slot is
    provided, reads the legacy unslotted file for backward compat.
    A slotted session that has no cache yet returns {} — fresh onboard,
    not inheritance from another session's identity.
    """
    path = _cache_path(workspace, slot)
    if not path.exists():
        return {}
    home_path = _home_cache_path(slot)
    if home_path is not None:
        normalized_path = path.parent.resolve() / path.name
        normalized_home = home_path.parent.resolve() / home_path.name
        if normalized_path == normalized_home and not home_session_mirror_is_valid(
            home_path
        ):
            return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _mirror_error(path: Path, exc: Exception) -> None:
    print(
        f"onboard_helper.py: home-mirror write failed ({exc!r}); "
        f"invalidated stale fallback at {path}",
        file=sys.stderr,
    )


def _read_cache_snapshot(
    workspace: Path,
    slot: str | None = None,
) -> tuple[dict[str, Any], CacheGenerationToken]:
    """Read identity state while reserving this network call's commit order."""
    payload, generation, authority_generation = reserve_session_cache_snapshot(
        _cache_path(workspace, slot),
        home_path=_home_cache_path(slot),
    )
    return payload, (generation, authority_generation)


def _write_cache(workspace: Path, payload: dict, slot: str | None = None) -> None:
    """Write one cache and its slotted HOME fallback transactionally."""
    path = _cache_path(workspace, slot)
    replace_session_cache(
        path,
        payload,
        home_path=_home_cache_path(slot),
        on_mirror_error=_mirror_error,
    )


def _update_cache_transaction(
    workspace: Path,
    mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    slot: str | None = None,
    *,
    expected_generation: int | CacheGenerationToken | None = None,
) -> tuple[dict[str, Any], bool]:
    expected_authority_generation: int | None = None
    if isinstance(expected_generation, tuple):
        expected_generation, expected_authority_generation = expected_generation
    return update_session_cache(
        _cache_path(workspace, slot),
        mutator,
        home_path=_home_cache_path(slot),
        expected_generation=expected_generation,
        expected_authority_generation=expected_authority_generation,
        on_mirror_error=_mirror_error,
    )


def _update_cache(
    workspace: Path,
    mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    slot: str | None = None,
) -> dict[str, Any]:
    """Apply one read-modify-write transaction under the shared slot lock.

    Returning ``None`` from ``mutator`` preserves the current cache without a
    rewrite. The returned dict is always the payload that remains authoritative.
    """
    authoritative, _committed = _update_cache_transaction(workspace, mutator, slot)
    return authoritative


# --- Response unwrap -------------------------------------------------------

def unwrap_tool_response(raw: dict) -> dict:
    """Unwrap the REST ``/v1/tools/call`` envelope.

    Handles two shapes:

    * Native MCP: ``{"result": {"content": [{"text": "<json>"}]}}``
    * REST-direct: ``{"result": {...fields...}}``

    Returns the inner dict, or ``{}`` if unrecognizable.
    """
    if not isinstance(raw, dict):
        return {}
    result = raw.get("result", raw)
    if not isinstance(result, dict):
        return {}
    content = result.get("content")
    if isinstance(content, list) and content:
        item = content[0]
        if isinstance(item, dict) and "text" in item:
            try:
                return json.loads(item["text"])
            except (json.JSONDecodeError, TypeError):
                return {}
    return result


def is_successful_onboard(parsed: dict) -> bool:
    """Onboard is successful iff the response has ``success != False`` and a uuid."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("success") is False:
        return False
    return bool(parsed.get("uuid"))


def trajectory_required(parsed: dict) -> bool:
    """Detect the ``trajectory_required`` recovery reason."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("success") is not False:
        return False
    recovery = parsed.get("recovery") or {}
    return isinstance(recovery, dict) and recovery.get("reason") == "trajectory_required"


def _identity_fingerprint(payload: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(payload.get(field) or "").strip()
        for field in ("uuid", "agent_uuid", "agent_id", "client_session_id")
    )


# --- Core flow -------------------------------------------------------------

def run_onboard(
    *,
    server_url: str,
    agent_name: str,
    model_type: str,
    workspace: Path,
    slot: str | None = None,
    force_new: bool = False,
    client_session_id: str | None = None,
    orchestrated: bool = False,
    auth_token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    initial_state: dict | None = None,
    bootstrap: bool = False,
    post_json: Callable[[str, dict, float, str | None], dict] = _post_json,
    read_cache: Callable[..., dict] = _read_cache,
    write_cache: Callable[..., None] = _write_cache,
) -> dict:
    """Run the onboard flow. Returns a dict with status info.

    ``slot`` namespaces the cache file so multiple processes in the same
    workspace can each own their own identity (typically the Claude Code
    session_id from hook input). When omitted, falls back to the legacy
    shared session.json — preserves single-process behavior.

    ``client_session_id`` is a stable per-conversation **resume anchor**
    (typically ``UNITARES_CLIENT_SESSION_ID``, e.g. ``"agent:/thread-<id>"``).
    It is honored only when ``orchestrated`` is true. That fail-closed marker
    distinguishes headless orchestrated turn-children (the Discord bridge /
    dispatch_beam case, where each user turn is a fresh ``claude -p`` process)
    from normal interactive sessions. A leaked bare anchor on an interactive
    shell must be inert: it falls back to the default fresh-mint path, never
    silently resume-sharing multiple subjects onto one governance UUID.
    ``force_new`` still overrides the anchor as a deliberate break.
    """
    url = f"{server_url.rstrip('/')}/v1/tools/call"
    native_cache_io = read_cache is _read_cache and write_cache is _write_cache
    initial_generation: CacheGenerationToken | None = None
    if native_cache_io:
        cache, initial_generation = _read_cache_snapshot(workspace, slot)
    else:
        cache = read_cache(workspace, slot)

    # Resume-by-anchor vs. fresh-mint. Resume is continuity, NOT lineage: the
    # turns are the *same* agent resumed, so we send ``client_session_id``, OMIT
    # ``force_new``, and declare no parent. ``force_new`` (explicit operator
    # break) suppresses resume.
    anchor = (client_session_id or "").strip()
    resume = bool(anchor) and orchestrated and not force_new

    parent_agent_id = ""
    if not resume and not force_new:
        parent_agent_id = (cache.get("uuid") or cache.get("agent_uuid") or "").strip()

    if resume:
        # Stable name (NOT slot-scoped): one identity per anchor across turns.
        # The server resolves by the anchor, so name-claim can't cross-bind.
        arguments: dict[str, Any] = {
            "name": agent_name,
            "model_type": model_type,
            "client_session_id": anchor,
            "orchestrated": True,
            "onboard_origin": ORCHESTRATED_RESUME_ORIGIN,
        }
    else:
        # Scope the name by slot so the server's name-claim lookup doesn't bind
        # this slot's onboard to an agent owned by another slot. This is also
        # the path for a bare anchor without the orchestration marker: mint, do
        # not resume-share.
        scoped_name = _scope_name_by_slot(agent_name, slot)
        arguments = {
            "name": scoped_name,
            "model_type": model_type,
            "force_new": True,
            "onboard_origin": HARNESS_BACKSTOP_ORIGIN,
        }
        if parent_agent_id:
            arguments["parent_agent_id"] = parent_agent_id
            arguments["spawn_reason"] = "new_session"

        # Optional genesis anchor (OFF by default — see BOOTSTRAP_* above; it is
        # NOT a fix for the "uninitialized" symptom). An explicit caller-supplied
        # ``initial_state`` always wins; otherwise a default seed is attached only
        # when opted in per-call (``bootstrap=True``) or globally
        # (``UNITARES_ONBOARD_BOOTSTRAP=1``). Genesis is for a fresh identity, so
        # it does not apply on resume.
        if initial_state is None and (bootstrap or _bootstrap_enabled()):
            initial_state = _default_bootstrap_state()
        if initial_state:
            arguments["initial_state"] = initial_state

    raw = post_json(url, {"name": "onboard", "arguments": arguments}, timeout, auth_token)
    parsed = unwrap_tool_response(raw)

    if not is_successful_onboard(parsed):
        # Per 718ccd3: never auto-retry with a weaker/different identity
        # posture. Surface the error so the operator can decide.
        recovery = parsed.get("recovery") or {}
        return {
            "status": "trajectory_required" if trajectory_required(parsed) else "onboard_failed",
            "error": parsed.get("error", "onboard returned no uuid"),
            "recovery_reason": recovery.get("reason", ""),
            "recovery_hint": recovery.get("hint", ""),
        }

    # Build fresh cache payload — never preserve stale fields.
    # continuity_token / continuity_token_supported are intentionally NOT
    # persisted: per identity.md v2 ontology and S1-a, lineage across
    # process-instances is declared via parent_agent_id, not resumed via
    # cached token. The fields stay in the in-process return value so a
    # caller can use them transiently within the same process if needed.
    # S20.3 — mirrors the v2 cache schema enforced by session_cache.py.
    new_cache = {
        "server_url": server_url,
        "agent_name": agent_name,
        "slot": slot or "",
        "uuid": parsed.get("uuid"),
        "agent_id": parsed.get("agent_id") or parsed.get("resolved_agent_id") or "",
        "client_session_id": parsed.get("client_session_id", ""),
        "session_resolution_source": parsed.get("session_resolution_source", ""),
        "display_name": parsed.get("display_name", ""),
    }
    reported_origin = parsed.get("onboard_origin")
    if isinstance(reported_origin, str) and reported_origin.strip():
        # Cache only what the server reports, not what this helper requested.
        # The marker is descriptive observability, never an identity proof.
        new_cache["onboard_origin"] = reported_origin.strip()
        reported_origin_basis = parsed.get("onboard_origin_basis")
        if isinstance(reported_origin_basis, str) and reported_origin_basis.strip():
            new_cache["onboard_origin_basis"] = reported_origin_basis.strip()
    if parent_agent_id:
        new_cache["parent_agent_id"] = parent_agent_id
        new_cache["spawn_reason"] = "new_session"

    committed_fresh_identity = True
    if native_cache_io:
        initial_identity = _identity_fingerprint(cache)

        def commit_if_unchanged(current: dict[str, Any]) -> dict[str, Any] | None:
            if _identity_fingerprint(current) != initial_identity:
                # Another host hook selected or cleared an identity while the
                # network request was in flight. That newer local decision wins.
                return None
            return new_cache

        authoritative, committed_fresh_identity = _update_cache_transaction(
            workspace,
            commit_if_unchanged,
            slot,
            expected_generation=initial_generation,
        )
        new_cache = authoritative
    else:
        # Preserve the injectable IO contract used by tests and embedders.
        write_cache(workspace, new_cache, slot)

    if not any(_identity_fingerprint(new_cache)):
        return {
            "status": "onboard_superseded",
            "error": "session cache changed while onboarding; newer local state preserved",
        }

    result = {
        "status": "ok",
        "uuid": new_cache.get("uuid", ""),
        "agent_id": new_cache.get("agent_id", ""),
        "client_session_id": new_cache.get("client_session_id", ""),
        "continuity_token": (
            parsed.get("continuity_token", "") if committed_fresh_identity else ""
        ),
        "session_resolution_source": new_cache.get("session_resolution_source", ""),
        "continuity_token_supported": (
            parsed.get("continuity_token_supported", False)
            if committed_fresh_identity
            else False
        ),
        "display_name": new_cache.get("display_name", ""),
    }
    authoritative_origin = new_cache.get("onboard_origin")
    if isinstance(authoritative_origin, str) and authoritative_origin.strip():
        result["onboard_origin"] = authoritative_origin.strip()
        authoritative_origin_basis = new_cache.get("onboard_origin_basis")
        if (
            isinstance(authoritative_origin_basis, str)
            and authoritative_origin_basis.strip()
        ):
            result["onboard_origin_basis"] = authoritative_origin_basis.strip()
    return result


# --- CLI -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=os.environ.get("UNITARES_SERVER_URL", DEFAULT_SERVER_URL))
    parser.add_argument("--name", required=True, help="Agent display name")
    parser.add_argument("--model-type", default="claude-code")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--force-new", action="store_true",
                        help="Create a fresh identity without declaring cached lineage")
    parser.add_argument("--bootstrap", action="store_true",
                        help="Attach an optional trajectory genesis seed "
                             "(initial_state) at onboard. OFF by default. This "
                             "does NOT clear an 'uninitialized / 0 real updates' "
                             "status — bootstrap rows are excluded from "
                             "real-check-in counts; only a real sync_state does. "
                             "Its only benefit is giving the first real check-in "
                             "a trajectory baseline. UNITARES_ONBOARD_BOOTSTRAP=1 "
                             "enables it globally.")
    parser.add_argument(
        "--slot",
        default=os.environ.get("UNITARES_SESSION_SLOT", ""),
        help="Per-process slot key (typically Claude Code session_id) so "
             "parallel processes in the same workspace don't collide on "
             "the same cache file.",
    )
    parser.add_argument(
        "--client-session-id",
        default=os.environ.get("UNITARES_CLIENT_SESSION_ID", ""),
        help="Stable per-conversation resume anchor (e.g. 'agent:/thread-<id>'). "
             "Honored only with --orchestrated / UNITARES_ORCHESTRATED=1, so a "
             "leaked anchor in an interactive shell cannot resume-share. Empty "
             "= legacy fresh-mint; --force-new overrides (clean break).",
    )
    parser.add_argument(
        "--orchestrated",
        action="store_true",
        default=_env_truthy(os.environ.get("UNITARES_ORCHESTRATED")),
        help="Declare this process as an orchestrated headless turn-child. "
             "Required for --client-session-id to trigger resume; without it "
             "the anchor is ignored and onboarding mints normally.",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    auth_token = os.environ.get("UNITARES_HTTP_API_TOKEN") or None
    workspace = Path(args.workspace).expanduser().resolve()
    slot = (args.slot or "").strip() or None
    client_session_id = (args.client_session_id or "").strip() or None
    result = run_onboard(
        server_url=args.server_url,
        agent_name=args.name,
        model_type=args.model_type,
        workspace=workspace,
        slot=slot,
        force_new=args.force_new,
        client_session_id=client_session_id,
        orchestrated=args.orchestrated,
        auth_token=auth_token,
        timeout=args.timeout,
        bootstrap=args.bootstrap,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
