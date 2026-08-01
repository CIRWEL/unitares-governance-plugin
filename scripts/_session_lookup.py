"""Slot-aware session-cache lookup for governance plugin hooks.

The plugin writes the session cache to a slotted file so concurrent
Claude Code sessions don't collide on one ``.unitares/session.json``.
Hooks fired from Claude Code receive the slot identifier on stdin
(the ``session_id`` field of the hook payload); this helper mirrors
the slot-hashing logic from ``onboard_helper.py`` so every hook
reads the same file the onboard helper wrote.

Public API:
    resolve_session_file(workspace, slot) -> Path | None
    load_session_for_hook(workspace, stdin_payload) -> dict

``load_session_for_hook`` is the one-liner most hooks want: pass the
current working directory plus whatever came in on stdin, get back
a dict with ``uuid``, ``client_session_id``, ``continuity_token``,
``slot`` (and whatever else the cache wrote). Returns an empty dict
if nothing matches. Claude supplies ``session_id``. Codex may expose only
stable conversation/transcript fields, so those are hashed into a slot key
rather than collapsing onto the retired flat cache.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from _session_cache_io import home_session_mirror_is_valid

CACHE_FILE = "session.json"


def _slot_filename(slot: Optional[str]) -> str:
    """Mirror onboard_helper._slot_filename. Must stay byte-identical.

    Without a slot, returns the legacy shared "session.json". With a slot
    (typically the Claude Code session_id from the hook input JSON), returns
    "session-<safe-slot>.json" using the same safe-char replacement that
    onboard_helper uses — NOT md5. The safe-char rule is: keep alphanumeric,
    hyphen, and underscore; replace everything else with underscore; truncate
    to 64 chars.
    """
    if not slot:
        return CACHE_FILE
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)
    safe = safe[:64]  # keep file names sane
    return f"session-{safe}.json"


def _codex_fallback_slot(data: dict[str, Any]) -> Optional[str]:
    for field in ("thread_id", "conversation_id", "transcript_path"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            digest = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:16]
            return f"codex-{field}-{digest}"
    return None


def resolve_session_file(workspace: str | Path, slot: Optional[str]) -> Optional[Path]:
    """Return the matching session file path.

    With a slot, the slot-scoped cache is the only eligible target. The
    workspace `.unitares/` directory is checked first; if missing, fall back
    to ``$HOME/.unitares/session-<slot>.json`` (PWD-mismatch fallback, see
    below). Without a slot, returns None — workspace-flat legacy files are
    lineage candidates for ``session_cache.py list``, not hook read targets
    (S20 §3d).
    Returns None if no matching file exists.

    Identity-honesty note (2026-04-18): the prior unslotted
    ``$HOME/.unitares/session.json`` last-resort fallback was removed because
    it was an axiom-violating *shared* cache — any same-UID Claude Code /
    Codex / CLI session whose own slotted or workspace-unslotted cache was
    empty would silently adopt whatever identity the most recent writer left
    there, siphoning one UUID across parallel agents (invariant #3:
    per-instance isolation).

    PWD-mismatch fallback (2026-05-10): a *slotted* HOME read is structurally
    distinct from the deleted unslotted fallback. The slot key is the Claude
    Code ``session_id`` — globally unique per Claude session. Parallel agents
    have different session_ids, so they read different filenames at HOME and
    cannot collapse onto each other's cache. This fallback addresses the
    specific failure mode where ``post-identity`` runs with PWD=X (writing
    ``X/.unitares/session-<slot>.json``) and later ``post-edit`` runs with
    PWD=Y (looking in ``Y/.unitares/...``, miss) — empirically the dominant
    cause of dark sessions per ~/.unitares/hook-skips.log evidence. The HOME
    file becomes findable regardless of PWD because the slot anchors it.

    Callers that previously depended on the *unslotted* HOME fallback still
    must pass ``workspace=Path.home()`` explicitly; silent collapse onto the
    shared ``session.json`` is still impossible.
    """
    if not slot:
        return None
    unitares_dir = Path(workspace) / ".unitares"
    slotted = unitares_dir / _slot_filename(slot)
    home_slotted = Path.home() / ".unitares" / _slot_filename(slot)
    if slotted.exists():
        normalized_slotted = slotted.parent.resolve() / slotted.name
        normalized_home = home_slotted.parent.resolve() / home_slotted.name
        if normalized_slotted == normalized_home and not home_session_mirror_is_valid(
            home_slotted
        ):
            return None
        return slotted
    # Slotted HOME fallback. Only fires when a slot key is present — the
    # slot is per-Claude-session unique, so cross-agent siphoning is
    # structurally precluded (cf. unslotted-HOME removal above).
    if home_slotted.exists() and home_session_mirror_is_valid(home_slotted):
        return home_slotted
    return None


def _extract_slot(stdin_payload: str) -> Optional[str]:
    """Extract the slot identifier from hook stdin JSON.

    Prefer raw ``session_id`` to preserve the existing Claude convention. If
    absent, use stable Codex fields hashed into safe slot names.
    """
    if not stdin_payload:
        return None
    try:
        data = json.loads(stdin_payload)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    sid = data.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return sid
    return _codex_fallback_slot(data)


def load_session_for_hook(workspace: str | Path, stdin_payload: str) -> dict[str, Any]:
    """Read the slot-scoped session file matching this hook invocation.
    Returns {} if no cache file is found or the file isn't JSON."""
    slot = _extract_slot(stdin_payload)
    path = resolve_session_file(workspace, slot)
    if path is None:
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def _cli() -> int:
    """Emit resolved session fields as shell-sourceable vars on stdout.

    Usage: `eval "$(python3 _session_lookup.py --workspace "$PWD" <<<"$stdin")"`

    Prints lines like:
        UUID="86ae619f-87e0-4040-8f29-eacece0c7904"
        CSID="agent-test1234"
        TOK="v1.faketoken"
        SLOT="test-slot"
    """
    import argparse
    import sys as _sys

    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    args = ap.parse_args()

    payload = _sys.stdin.read()
    data = load_session_for_hook(args.workspace, payload)

    def _esc(value: str) -> str:
        # Escape for double-quoted bash string: backslash, double-quote, dollar, backtick
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "\\$")
            .replace("`", "\\`")
        )

    print(f'UUID="{_esc(data.get("uuid", ""))}"')
    print(f'CSID="{_esc(data.get("client_session_id", ""))}"')
    print(f'TOK="{_esc(data.get("continuity_token", ""))}"')
    # Empty fallback (S20.1a): the previous `"default"` literal collapsed
    # every slotless cache onto a shared `session-default.json` target when
    # the SLOT was used as a `--slot` arg downstream. Callers that need a
    # non-empty SLOT (e.g. for diagnostic logging in checkin.py) handle the
    # empty case explicitly; callers using SLOT to scope cache writes must
    # skip the write when SLOT is empty — see hooks/post-edit S20.1a notes.
    print(f'SLOT="{_esc(data.get("slot") or "")}"')
    # RAW_SLOT is the raw Claude session_id from the hook stdin — present even
    # when no onboard cache exists. The substrate FLOOR (post-stop, un-onboarded
    # case) keys on this so a session that never onboarded is still measurable.
    print(f'RAW_SLOT="{_esc(_extract_slot(payload) or "")}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
