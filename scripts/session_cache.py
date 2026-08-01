#!/usr/bin/env python3
"""Transport-neutral local cache helper for UNITARES client adapters.

Stores lightweight continuity state in:

    .unitares/session-<slot>.json
    .unitares/last-milestone.json

The flat `.unitares/session.json` path exists only as a legacy/shared cache
surface. New session writes must be slot-scoped unless the caller explicitly
opts into the substrate-earned single-tenant escape hatch.

This helper is intentionally small and dependency-free so Claude hooks, Codex
commands, and other thin clients can share one cache format.

Session-cache schema versions
-----------------------------

* v1 (pre-S11): ``continuity_token`` was written by ``hooks/post-identity``
  and treated by ``hooks/session-start`` as a resume credential. Under the
 identity ontology (````), this
  performatively claimed cross-process-instance continuity without earning
  it. v1 caches may still exist on disk; the token field is treated as
  read-only legacy — downstream readers must not promote it back into a
  resume suggestion.
* v2 (post-S11): ``hooks/post-identity`` writes
  ``schema_version: 2`` and empties ``continuity_token``. The cache's UUID
  is surfaced by the next session's ``session-start`` hook as a
  ``parent_agent_id`` *lineage candidate* — a predecessor the fresh
  process-instance declares it inherits from, not an identity it resumes.

This helper itself is schema-agnostic (it marshals any JSON dict). The
schema contract lives at the hook layer; this docstring records it for
readers who grep for ``schema_version`` or ``continuity_token`` here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _session_cache_io import (  # noqa: E402
    clear_session_cache,
    reserve_session_cache_snapshot,
    update_session_cache,
)

CACHE_DIR = ".unitares"
CACHE_FILES = {
    "session": "session.json",
    "milestone": "last-milestone.json",
}
DEFAULT_MILESTONE_LOCK_TIMEOUT_S = 2.0
DEFAULT_MILESTONE_DELIVERY_CLAIM_TTL_S = 30.0
MIN_MILESTONE_DELIVERY_CLAIM_TTL_S = 30.0
DEFAULT_MILESTONE_SNAPSHOT_MAX_AGE_S = 900.0
DEFAULT_SESSION_SNAPSHOT_MAX_AGE_S = 900.0

# Mirrors the post-sanitization shape produced by `_slot_suffix`. Used by
# `_parse_session_filename` to reject filenames that bypassed the writer
# (a same-UID actor can drop arbitrary `session-*.json` directly on disk;
# the parsed slot is later reflected into agent context, where backticks
# or whitespace would break the surrounding markdown code-span).
_SLOT_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _workspace_path(raw: str | None) -> Path:
    base = raw or os.getcwd()
    return Path(base).expanduser().resolve()


def _slot_suffix(slot: str | None) -> str:
    """Safe-filename slot suffix. Matches onboard_helper/_session_lookup."""
    if not slot:
        return ""
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)
    return safe[:64]


def _cache_path(kind: str, workspace: Path, slot: str | None = None) -> Path:
    try:
        filename = CACHE_FILES[kind]
    except KeyError as exc:
        raise ValueError(f"unknown cache kind: {kind}") from exc
    # Only the session cache is slot-scoped — milestone accumulator is
    # workspace-level (per the auto-checkin design).
    safe_slot = _slot_suffix(slot) if kind == "session" else ""
    if safe_slot:
        stem, _, ext = filename.rpartition(".")
        filename = f"{stem}-{safe_slot}.{ext}"
    return workspace / CACHE_DIR / filename


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic write with mode 0600.

    Legacy v1 session caches may contain continuity tokens; v2 cache writes
    intentionally do not. A world-readable legacy cache (the default when using
    Path.write_text, which inherits umask 022) lets any same-UID process
    impersonate the cached identity against the governance API. Inlined rather
    than imported from unitares_sdk because this helper is intentionally
    dependency-free — shared by thin plugin clients that don't pull in the SDK.

    On any write/chmod/replace failure, the temp file is unlinked rather
    than left as a turd in the cache directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, data)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _bounded_lock_timeout_s(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name, str(default))
    try:
        return min(4.0, max(0.05, float(raw)))
    except ValueError:
        return default


def _milestone_lock_timeout_s() -> float:
    return _bounded_lock_timeout_s(
        "UNITARES_MILESTONE_LOCK_TIMEOUT_S",
        DEFAULT_MILESTONE_LOCK_TIMEOUT_S,
    )


@contextmanager
def _cache_lock(
    path: Path,
    *,
    timeout_s: float,
    label: str,
) -> Iterator[None]:
    """Serialize read-modify-write cycles across client processes.

    The lock lives in a stable sidecar because ``_write_json`` atomically
    replaces the JSON inode. Locking the JSON file itself would let a second
    process lock the replacement while the first still holds the unlinked
    predecessor.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    acquired = False
    deadline = time.monotonic() + timeout_s
    try:
        if os.name == "nt":
            import msvcrt

            # msvcrt locks a byte range from the current offset. Keep one byte
            # in the sidecar so the same region exists for every process.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while not acquired:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"{label} lock timed out: {lock_path}")
                    time.sleep(0.025)
        else:
            import fcntl

            while not acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"{label} lock timed out: {lock_path}")
                    time.sleep(0.025)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def _milestone_lock(path: Path) -> Iterator[None]:
    with _cache_lock(
        path,
        timeout_s=_milestone_lock_timeout_s(),
        label="milestone",
    ):
        yield


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    raw = args.json
    if raw is None and not sys.stdin.isatty():
        raw = sys.stdin.read()
    if raw is None:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    return data


def cmd_path(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    print(_cache_path(args.kind, workspace, getattr(args, "slot", None)))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    payload = _read_json(_cache_path(args.kind, workspace, getattr(args, "slot", None)))
    if args.key:
        value = payload.get(args.key)
        if value is None:
            return 0
        if isinstance(value, (dict, list)):
            print(json.dumps(value))
        else:
            print(value)
        return 0
    print(json.dumps(payload))
    return 0


_SESSION_IDENTITY_FIELDS = ("uuid", "client_session_id")


def cmd_set(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    slot = getattr(args, "slot", None)
    allow_shared = bool(getattr(args, "allow_shared", False))

    if args.kind == "session" and not slot and not allow_shared:
        # Slotless session writes produce flat `session.json`, the workspace-
        # shared "current owner" file every same-UID process can read. Hook
        # layer (PR #19) refuses to read it; helper now refuses to write it.
        # Convention-level: a determined caller can still write JSON directly
        # to the path (axiom #14). The earned defense lives in S1-A′ + S19.
        print(
            "session_cache.py: refusing slotless session write — pass --slot <id> "
            "(substrate-earned single-tenant: --allow-shared; convention-level — "
            "direct file writes still bypass)",
            file=sys.stderr,
        )
        return 2

    path = _cache_path(args.kind, workspace, slot)
    payload = _load_payload(args)
    if args.kind == "milestone":
        with _milestone_lock(path):
            if args.merge:
                existing = _read_json(path)
                existing.update(payload)
                payload = existing
            if args.stamp:
                payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            _write_json(path, payload)
        if args.echo:
            print(json.dumps(payload))
        return 0

    result_code = 0
    expected_generation: int | None = None
    expected_authority_generation: int | None = None
    snapshot_path: Path | None = None
    snapshot_id = str(getattr(args, "snapshot_id", "") or "").strip()
    if snapshot_id:
        if not slot:
            print(
                "session_cache.py: --snapshot-id requires a slotted session write",
                file=sys.stderr,
            )
            return 2
        snapshot_path = _session_snapshot_path(workspace, snapshot_id)
        snapshot = _read_json(snapshot_path)
        if (
            snapshot.get("schema_version") != 1
            or snapshot.get("event_id") != snapshot_id
            or _slot_suffix(str(snapshot.get("slot") or "")) != _slot_suffix(slot)
            or not isinstance(snapshot.get("generation"), int)
            or not isinstance(snapshot.get("authority_generation"), int)
        ):
            print(
                "session_cache.py: identity generation snapshot missing or invalid; "
                "session cache unchanged",
                file=sys.stderr,
            )
            return 3
        expected_generation = snapshot["generation"]
        expected_authority_generation = snapshot.get("authority_generation")

    def update_session(existing: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal payload, result_code
        if args.merge:
            # Auto-migrate v1 legacy tokens during merge: a pre-existing
            # slot file from before S11/S20 may carry a real continuity_token
            # at rest. Without this strip, the token-rejection check below
            # would fire on every merge against such a cache and brick
            # callers like the post-edit auto-checkin stamp (errors swallowed
            # via `|| true`). The strip is one-way: we never re-introduce a
            # legacy token; we only let new writes (whose own continuity_token
            # is checked below) succeed. Stderr breadcrumb keeps the
            # migration legible per axiom #14.
            stale_token = existing.get("continuity_token")
            if isinstance(stale_token, str) and stale_token.strip():
                existing.pop("continuity_token", None)
                print(
                    f"session_cache.py: [V1_LEGACY_STRIP] dropped pre-existing "
                    f"continuity_token from {path} during merge",
                    file=sys.stderr,
                )
            existing.update(payload)
            payload = existing

        token = payload.get("continuity_token")
        # Literal empty string is the v2 hook erasure path (passes). Any
        # non-empty string is rejected — including whitespace-only values,
        # since `bool(" ")` is True in Python and downstream readers that
        # test `if continuity_token:` would treat it as a credential.
        if isinstance(token, str) and token:
            print(
                "session_cache.py: refusing session payload with non-empty "
                "continuity_token — v2 ontology stores lineage, not resume "
                "credentials (write empty string to erase, or omit the field; "
                "to recover a legacy slot file, run: clear session --slot <id>)",
                file=sys.stderr,
            )
            result_code = 2
            return None
        if not any(k in payload for k in _SESSION_IDENTITY_FIELDS):
            # A session cache with neither uuid nor client_session_id
            # is a stub: subsequent hooks read it, find no addressable identity,
            # and silently no-op. Refuse so the failure is visible (caller
            # ignores via `|| true`) instead of silently bricking the next
            # hook's identity lookup.
            print(
                "session_cache.py: refusing to write session cache without any identity field "
                f"(need at least one of {list(_SESSION_IDENTITY_FIELDS)})",
                file=sys.stderr,
            )
            result_code = 1
            return None

        if args.stamp:
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        return payload

    def mirror_error(home_path: Path, exc: Exception) -> None:
        print(
            f"session_cache.py: home-mirror write failed ({exc!r}); "
            f"invalidated stale fallback at {home_path}",
            file=sys.stderr,
        )

    home_path = _cache_path("session", Path.home(), slot) if slot else None
    try:
        authoritative, committed = update_session_cache(
            path,
            update_session,
            home_path=home_path,
            expected_generation=expected_generation,
            expected_authority_generation=expected_authority_generation,
            on_mirror_error=mirror_error,
        )
    finally:
        if snapshot_path is not None:
            _discard_session_snapshot_file(
                snapshot_path,
                event_id=snapshot_id,
                generation=expected_generation,
            )
    if result_code:
        return result_code
    if snapshot_id and not committed:
        print(
            "session_cache.py: identity generation changed while the tool call "
            "was in flight; stale response ignored",
            file=sys.stderr,
        )
        return 4
    if committed:
        payload = authoritative

    if args.echo:
        print(json.dumps(payload))
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    slot = getattr(args, "slot", None)
    path = _cache_path(args.kind, workspace, slot)

    if args.kind == "milestone":
        with _milestone_lock(path):
            path.unlink(missing_ok=True)
        return 0

    home_path = _cache_path("session", Path.home(), slot) if slot else None
    clear_session_cache(path, home_path=home_path)
    return 0


def _parse_session_filename(name: str) -> str | None:
    """Recover the slot suffix from a session-*.json filename.

    Returns the slot string (the segment between ``session-`` and ``.json``),
    or ``None`` for the flat ``session.json`` (no slot) and for any name that
    does not match the pattern. Slot strings here are pre-sanitized (the
    writer ran them through ``_slot_suffix``) so callers receive the safe
    form already on disk — but a same-UID actor can write filenames
    directly, bypassing the writer, so the parsed slot is re-validated
    against ``_SLOT_PATTERN`` before being returned.
    """
    if not name.startswith("session-") or not name.endswith(".json"):
        return None
    raw = name[len("session-") : -len(".json")]
    if not raw or not _SLOT_PATTERN.fullmatch(raw):
        return None
    return raw


def cmd_list(args: argparse.Namespace) -> int:
    """List slot inventory for the session cache, newest first.

    Emits a JSON array of ``{slot, parent_agent_id, prior_client_session_id,
    updated_at, path}`` objects. Field names are deliberately the v2
    declared-lineage parameters of ``onboard()`` so consumers naturally
    flow into ``onboard(force_new=true, parent_agent_id=entry["parent_agent_id"])``
    — declared lineage, not resume. The scan-newest fallback (S20 §2b) is
    a *lineage candidate surface*, never a resume credential.

    Entries without a shape-valid UUID are filtered: they have no actionable
    lineage hint and would silently mis-rank the scan-newest pick if sorted to
    the top by ``updated_at``. The UUID check is also a trust boundary: cache
    files live in the workspace, and their values may later be reflected into
    host-provided developer context. Malformed JSON is skipped silently — this
    is a discovery surface, not a validator.
    """
    workspace = _workspace_path(args.workspace)
    cache_dir = workspace / CACHE_DIR
    entries: list[dict[str, Any]] = []
    if cache_dir.is_dir():
        for path in cache_dir.iterdir():
            if not path.is_file():
                continue
            slot = _parse_session_filename(path.name)
            # path.name == "session.json" → slot is None (the flat fallback).
            # Surface legacy/--allow-shared files alongside slotted ones so
            # operators can see them; consumers decide whether to use them.
            if path.name != "session.json" and slot is None:
                continue
            data = _read_json(path)
            if not data:
                continue
            uuid = data.get("uuid")
            if not isinstance(uuid, str) or not _UUID_PATTERN.fullmatch(uuid):
                continue
            sid = data.get("client_session_id")
            entries.append({
                "slot": slot,
                "parent_agent_id": uuid,
                "prior_client_session_id": sid,
                "updated_at": data.get("updated_at"),
                "path": str(path),
            })
    # Sort by parsed UTC datetime, not raw ISO string. Mixed-offset
    # timestamps (e.g., +05:30 vs +00:00) sort incorrectly by string
    # comparison even though Python's `fromisoformat` normalizes them.
    # Entries that fail to parse fall back to a sentinel that sorts last
    # under reverse=True, so they don't displace real entries.
    _MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)

    def _sort_ts(entry: dict[str, Any]) -> datetime:
        raw = entry.get("updated_at")
        if not isinstance(raw, str) or not raw:
            return _MIN_UTC
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return _MIN_UTC
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    entries.sort(key=_sort_ts, reverse=True)
    print(json.dumps(entries))
    return 0


# Per-workspace cap on how many distinct file paths we remember in the
# milestone accumulator. The accumulator exists so auto-checkin can report a
# concrete file list; beyond ~20 entries the summary becomes noise and the
# cache starts growing unbounded in long-running sessions.
MILESTONE_FILE_CAP = 20


def _milestone_snapshot_path(workspace: Path, event_id: str) -> Path:
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]
    return workspace / CACHE_DIR / "milestone-snapshots" / f"{digest}.json"


def _session_snapshot_path(workspace: Path, event_id: str) -> Path:
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]
    return workspace / CACHE_DIR / "session-snapshots" / f"{digest}.json"


def _milestone_delivery_claim_path(workspace: Path) -> Path:
    return workspace / CACHE_DIR / "milestone-delivery-claim.json"


def _snapshot_created_epoch(path: Path, payload: dict[str, Any]) -> float:
    raw = payload.get("created_at")
    if isinstance(raw, str) and raw:
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return created.timestamp()
        except (ValueError, OverflowError):
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _prune_session_snapshots(
    workspace: Path,
    *,
    now: float | None = None,
) -> int:
    directory = workspace / CACHE_DIR / "session-snapshots"
    if not directory.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - DEFAULT_SESSION_SNAPSHOT_MAX_AGE_S
    removed = 0
    for path in directory.glob("*.json"):
        if _snapshot_created_epoch(path, _read_json(path)) >= cutoff:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def _discard_session_snapshot_file(
    path: Path,
    *,
    event_id: str,
    generation: int | None = None,
) -> bool:
    snapshot = _read_json(path)
    if snapshot.get("event_id") != event_id:
        return False
    if generation is not None and snapshot.get("generation") != generation:
        return False
    path.unlink(missing_ok=True)
    return True


def cmd_snapshot_session(args: argparse.Namespace) -> int:
    """Capture a cache generation before an identity MCP call starts."""
    slot = _slot_suffix(args.slot.strip())
    if not slot:
        raise ValueError("snapshot-session requires a non-empty --slot")
    workspace = _workspace_path(args.workspace)
    path = _cache_path("session", workspace, slot)
    home_path = _cache_path("session", Path.home(), slot)
    _, generation, authority_generation = reserve_session_cache_snapshot(
        path,
        home_path=home_path,
    )
    _prune_session_snapshots(workspace)
    snapshot = {
        "schema_version": 1,
        "event_id": args.event_id,
        "slot": slot,
        "generation": generation,
        "authority_generation": authority_generation,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(_session_snapshot_path(workspace, args.event_id), snapshot)
    if args.echo:
        print(json.dumps(snapshot))
    return 0


def cmd_discard_session_snapshot(args: argparse.Namespace) -> int:
    """Discard an identity-call snapshot after a non-cacheable response."""
    workspace = _workspace_path(args.workspace)
    _discard_session_snapshot_file(
        _session_snapshot_path(workspace, args.event_id),
        event_id=args.event_id,
    )
    return 0


def _prune_milestone_snapshots(
    workspace: Path,
    *,
    now: float | None = None,
) -> int:
    """Remove stale tool snapshots without touching another live session."""
    directory = workspace / CACHE_DIR / "milestone-snapshots"
    if not directory.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - DEFAULT_MILESTONE_SNAPSHOT_MAX_AGE_S
    removed = 0
    for path in directory.glob("*.json"):
        if _snapshot_created_epoch(path, _read_json(path)) >= cutoff:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def cmd_claim_milestone_delivery(args: argparse.Namespace) -> int:
    """Claim the single in-flight auto-checkin delivery slot for a workspace."""
    workspace = _workspace_path(args.workspace)
    milestone_path = _cache_path("milestone", workspace)
    claim_path = _milestone_delivery_claim_path(workspace)
    now = time.time()
    # checkin.py's edit delivery can use its full 20-second HTTP timeout. Keep
    # the claim alive beyond that transport budget so a slow accepted response
    # cannot overlap a replacement claimant.
    ttl = min(120.0, max(MIN_MILESTONE_DELIVERY_CLAIM_TTL_S, float(args.ttl)))

    with _milestone_lock(milestone_path):
        existing = _read_json(claim_path)
        try:
            expires_at = float(existing.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        existing_owner = existing.get("owner")
        active = isinstance(existing_owner, str) and bool(existing_owner) and expires_at > now

        if active and existing_owner != args.owner:
            result = {
                "claimed": False,
                "owner": args.owner,
                "held_revision": existing.get("revision"),
                "expires_at": expires_at,
            }
        else:
            result = {
                "schema_version": 1,
                "claimed": True,
                "owner": args.owner,
                "revision": args.revision,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": now + ttl,
            }
            _write_json(claim_path, result)

    if args.echo:
        print(json.dumps(result))
    return 0


def cmd_release_milestone_delivery(args: argparse.Namespace) -> int:
    """Release an auto-checkin delivery claim only when its owner matches."""
    workspace = _workspace_path(args.workspace)
    milestone_path = _cache_path("milestone", workspace)
    claim_path = _milestone_delivery_claim_path(workspace)

    with _milestone_lock(milestone_path):
        existing = _read_json(claim_path)
        released = existing.get("owner") == args.owner
        if released:
            claim_path.unlink(missing_ok=True)

    if args.echo:
        print(json.dumps({"released": released}))
    return 0


def cmd_snapshot_milestone(args: argparse.Namespace) -> int:
    """Capture the pre-tool revision a later PostToolUse may acknowledge."""
    if not args.slot.strip():
        raise ValueError("snapshot-milestone requires a non-empty --slot")
    workspace = _workspace_path(args.workspace)
    milestone_path = _cache_path("milestone", workspace)
    snapshot_path = _milestone_snapshot_path(workspace, args.event_id)
    with _milestone_lock(milestone_path):
        _prune_milestone_snapshots(workspace)
        existing = _read_json(milestone_path)
        snapshot = {
            "schema_version": 1,
            "event_id": args.event_id,
            "slot": args.slot,
            "revision": int(existing.get("revision") or 0),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(snapshot_path, snapshot)
    if args.echo:
        print(json.dumps(snapshot))
    return 0


def cmd_discard_milestone_snapshot(args: argparse.Namespace) -> int:
    """Discard a tool-scoped snapshot without acknowledging its edits."""
    workspace = _workspace_path(args.workspace)
    snapshot_path = _milestone_snapshot_path(workspace, args.event_id)
    snapshot = _read_json(snapshot_path)
    if snapshot.get("event_id") == args.event_id:
        snapshot_path.unlink(missing_ok=True)
    return 0


def cmd_discard_milestone_snapshots(args: argparse.Namespace) -> int:
    """Discard only snapshots owned by one exact host session slot."""
    workspace = _workspace_path(args.workspace)
    milestone_path = _cache_path("milestone", workspace)
    directory = workspace / CACHE_DIR / "milestone-snapshots"
    removed = 0
    with _milestone_lock(milestone_path):
        if directory.is_dir():
            for path in directory.glob("*.json"):
                if _read_json(path).get("slot") != args.slot:
                    continue
                path.unlink(missing_ok=True)
                removed += 1
        _prune_milestone_snapshots(workspace)
    if args.echo:
        print(json.dumps({"removed": removed}))
    return 0


def cmd_bump_edit(args: argparse.Namespace) -> int:
    """Append an edit event to the milestone accumulator.

    Increments edit_count once, merges all event paths into files_touched (capped),
    stamps first_edit_ts on the first bump since reset, and always refreshes
    last_edit_ts + updated_at. Backwards-compatible keys (event, file_path,
    timestamp) are preserved so existing readers keep working.
    """
    workspace = _workspace_path(args.workspace)
    path = _cache_path("milestone", workspace)
    with _milestone_lock(path):
        existing = _read_json(path)

        now_epoch = int(datetime.now(timezone.utc).timestamp())
        now_iso = datetime.now(timezone.utc).isoformat()

        existing["revision"] = int(existing.get("revision") or 0) + 1
        existing["edit_count"] = int(existing.get("edit_count") or 0) + 1
        if not existing.get("first_edit_ts"):
            existing["first_edit_ts"] = now_epoch
        existing["last_edit_ts"] = now_epoch
        existing["updated_at"] = now_iso

        files = existing.get("files_touched")
        if not isinstance(files, list):
            files = []
        raw_paths = args.file_path or []
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        if args.file_paths_json:
            decoded = json.loads(args.file_paths_json)
            if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
                raise ValueError("--file-paths-json must be a JSON array of strings")
            raw_paths.extend(decoded)
        event_paths = list(dict.fromkeys(path.strip() for path in raw_paths if path.strip()))
        for file_path in event_paths:
            if file_path not in files:
                files.append(file_path)
        if len(files) > MILESTONE_FILE_CAP:
            files = files[-MILESTONE_FILE_CAP:]
        existing["files_touched"] = files

        # Legacy shape — keep for readers that predate the accumulator.
        existing.setdefault("event", "edit")
        if event_paths:
            existing["file_path"] = event_paths[-1]
        existing["timestamp"] = now_epoch

        _write_json(path, existing)
    if args.echo:
        print(json.dumps(existing))
    return 0


def cmd_reset_milestone(args: argparse.Namespace) -> int:
    """Reset edits acknowledged by a check-in unless a newer bump landed."""
    workspace = _workspace_path(args.workspace)
    path = _cache_path("milestone", workspace)
    with _milestone_lock(path):
        existing = _read_json(path)
        expected_revision = getattr(args, "expected_revision", None)
        snapshot_id = getattr(args, "snapshot_id", None)
        if snapshot_id:
            snapshot_path = _milestone_snapshot_path(workspace, snapshot_id)
            snapshot = _read_json(snapshot_path)
            snapshot_path.unlink(missing_ok=True)
            if snapshot.get("event_id") == snapshot_id and isinstance(
                snapshot.get("revision"), int
            ):
                expected_revision = snapshot["revision"]
            else:
                if args.echo:
                    output = dict(existing)
                    output["reset_skipped"] = True
                    output["reset_skip_reason"] = "snapshot_missing"
                    print(json.dumps(output))
                return 0
        current_revision = int(existing.get("revision") or 0)
        if expected_revision is not None and current_revision != expected_revision:
            if args.echo:
                output = dict(existing)
                output["reset_skipped"] = True
                print(json.dumps(output))
            return 0
        existing["revision"] = current_revision + 1
        existing["edit_count"] = 0
        existing["files_touched"] = []
        existing["first_edit_ts"] = None
        existing["last_edit_ts"] = None
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(path, existing)
    if args.echo:
        print(json.dumps(existing))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_path = sub.add_parser("path", help="Print the absolute cache path")
    p_path.add_argument("kind", choices=sorted(CACHE_FILES))
    p_path.add_argument("--workspace")
    p_path.add_argument("--slot", help="Claude Code session_id for slotted cache")
    p_path.set_defaults(func=cmd_path)

    p_get = sub.add_parser("get", help="Read cached JSON")
    p_get.add_argument("kind", choices=sorted(CACHE_FILES))
    p_get.add_argument("--workspace")
    p_get.add_argument("--slot", help="Claude Code session_id for slotted cache")
    p_get.add_argument("--key")
    p_get.set_defaults(func=cmd_get)

    p_set = sub.add_parser("set", help="Write cached JSON")
    p_set.add_argument("kind", choices=sorted(CACHE_FILES))
    p_set.add_argument("--workspace")
    p_set.add_argument("--slot", help="Claude Code session_id for slotted cache")
    p_set.add_argument(
        "--allow-shared",
        action="store_true",
        help=(
            "Permit slotless session writes for substrate-earned single-tenant "
            "deployments (e.g., Lumen on dedicated Pi). Operator-asserted — no "
            "runtime substrate-claim attestation here; the principled gate "
            "lives with S19 substrate attestation (see "
 " §6)."
        ),
    )
    p_set.add_argument("--json")
    p_set.add_argument("--merge", action="store_true")
    p_set.add_argument("--stamp", action="store_true")
    p_set.add_argument(
        "--snapshot-id",
        default="",
        help="Commit a session write only if this pre-tool generation still matches.",
    )
    p_set.add_argument("--echo", action="store_true")
    p_set.set_defaults(func=cmd_set)

    p_clear = sub.add_parser("clear", help="Delete a cache file")
    p_clear.add_argument("kind", choices=sorted(CACHE_FILES))
    p_clear.add_argument("--workspace")
    p_clear.add_argument("--slot", help="Claude Code session_id for slotted cache")
    p_clear.set_defaults(func=cmd_clear)

    p_list = sub.add_parser(
        "list",
        help="List session slot inventory (newest first) as JSON",
    )
    p_list.add_argument("--workspace")
    p_list.set_defaults(func=cmd_list)

    p_snapshot_session = sub.add_parser(
        "snapshot-session",
        help="Capture the current session generation before an identity tool call",
    )
    p_snapshot_session.add_argument("--workspace")
    p_snapshot_session.add_argument("--event-id", required=True)
    p_snapshot_session.add_argument("--slot", required=True)
    p_snapshot_session.add_argument("--echo", action="store_true")
    p_snapshot_session.set_defaults(func=cmd_snapshot_session)

    p_discard_session_snapshot = sub.add_parser(
        "discard-session-snapshot",
        help="Discard one identity-call generation snapshot",
    )
    p_discard_session_snapshot.add_argument("--workspace")
    p_discard_session_snapshot.add_argument("--event-id", required=True)
    p_discard_session_snapshot.set_defaults(func=cmd_discard_session_snapshot)

    p_bump = sub.add_parser(
        "bump-edit",
        help="Append an edit event to the milestone accumulator",
    )
    p_bump.add_argument("--workspace")
    p_bump.add_argument("--file-path", action="append", default=[])
    p_bump.add_argument("--file-paths-json", default="")
    p_bump.add_argument("--echo", action="store_true")
    p_bump.set_defaults(func=cmd_bump_edit)

    p_claim_delivery = sub.add_parser(
        "claim-milestone-delivery",
        help="Claim the workspace auto-checkin delivery slot",
    )
    p_claim_delivery.add_argument("--workspace")
    p_claim_delivery.add_argument("--revision", type=int, required=True)
    p_claim_delivery.add_argument("--owner", required=True)
    p_claim_delivery.add_argument(
        "--ttl",
        type=float,
        default=DEFAULT_MILESTONE_DELIVERY_CLAIM_TTL_S,
    )
    p_claim_delivery.add_argument("--echo", action="store_true")
    p_claim_delivery.set_defaults(func=cmd_claim_milestone_delivery)

    p_release_delivery = sub.add_parser(
        "release-milestone-delivery",
        help="Release the matching workspace auto-checkin delivery claim",
    )
    p_release_delivery.add_argument("--workspace")
    p_release_delivery.add_argument("--owner", required=True)
    p_release_delivery.add_argument("--echo", action="store_true")
    p_release_delivery.set_defaults(func=cmd_release_milestone_delivery)

    p_snapshot = sub.add_parser(
        "snapshot-milestone",
        help="Capture the current revision before a check-in tool call",
    )
    p_snapshot.add_argument("--workspace")
    p_snapshot.add_argument("--event-id", required=True)
    p_snapshot.add_argument("--slot", required=True)
    p_snapshot.add_argument("--echo", action="store_true")
    p_snapshot.set_defaults(func=cmd_snapshot_milestone)

    p_discard_snapshot = sub.add_parser(
        "discard-milestone-snapshot",
        help="Discard a failed check-in's revision snapshot without resetting edits",
    )
    p_discard_snapshot.add_argument("--workspace")
    p_discard_snapshot.add_argument("--event-id", required=True)
    p_discard_snapshot.set_defaults(func=cmd_discard_milestone_snapshot)

    p_discard_snapshots = sub.add_parser(
        "discard-milestone-snapshots",
        help="Discard revision snapshots owned by one host session slot",
    )
    p_discard_snapshots.add_argument("--workspace")
    p_discard_snapshots.add_argument("--slot", required=True)
    p_discard_snapshots.add_argument("--echo", action="store_true")
    p_discard_snapshots.set_defaults(func=cmd_discard_milestone_snapshots)

    p_reset = sub.add_parser(
        "reset-milestone",
        help="Reset the milestone accumulator after a check-in",
    )
    p_reset.add_argument("--workspace")
    p_reset.add_argument(
        "--expected-revision",
        type=int,
        default=None,
        help="Reset only when no newer edit event has changed this revision.",
    )
    p_reset.add_argument(
        "--snapshot-id",
        default="",
        help="Consume a pre-tool revision snapshot keyed by tool_use_id.",
    )
    p_reset.add_argument("--echo", action="store_true")
    p_reset.set_defaults(func=cmd_reset_milestone)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"session_cache.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
