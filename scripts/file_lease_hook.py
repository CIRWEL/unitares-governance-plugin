#!/usr/bin/env python3
"""Host-neutral edit-hook helper for BEAM file leases.

This script is intentionally stdlib-only so the installed plugin can run in
any workspace without importing the UNITARES server repo. It talks to the
lease-plane HTTP API, acquires `file://` leases before a host edit, releases
the edited files after PostToolUse, and keeps session-end release as a
best-effort cleanup path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:
    from .edit_hook_event import EditHookEvent, EditHookPayloadError, normalize_edit_hook
except ImportError:  # Executed directly from an installed plugin.
    from edit_hook_event import EditHookEvent, EditHookPayloadError, normalize_edit_hook

try:
    from ._http_auth import authorization_safe_urlopen
except ImportError:  # Executed directly from an installed plugin.
    from _http_auth import authorization_safe_urlopen

try:
    from ._session_cache_io import session_cache_lock
except ImportError:  # Executed directly from an installed plugin.
    from _session_cache_io import session_cache_lock

DEFAULT_BASE_URL = "http://127.0.0.1:8788"
# Leases are released in PostToolUse right after each edit, so this TTL is now
# only a backstop for the crash-mid-edit window (acquire fired, the session
# died before release-edit ran). A short TTL means even that orphan self-heals
# fast via the reaper. Overridable via UNITARES_FILE_LEASE_TTL_S.
DEFAULT_TTL_S = 300
STATE_VERSION = 1
DEFAULT_BATCH_TIMEOUT_S = 3.5


class UnsupportedLeaseStateError(RuntimeError):
    """Existing local lease state cannot be mutated by this plugin version."""


def _safe_slot(slot: str) -> str:
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)
    return safe[:64]


def _slot_key(slot: str) -> str:
    """Readable, collision-resistant filename component for a raw host slot."""
    prefix = _safe_slot(slot)[:40] or "session"
    digest = hashlib.sha256(slot.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _session_id(stdin_text: str) -> str:
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("session_id") or "").strip()


def _edit_key(tool_use_id: str) -> str:
    if not tool_use_id:
        return ""
    return hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()[:16]


def _legacy_state_path(workspace: Path, slot: str, tool_use_id: str = "") -> Path:
    edit_suffix = f"-edit-{_edit_key(tool_use_id)}" if tool_use_id else ""
    return workspace / ".unitares" / f"file-leases-{_safe_slot(slot)}{edit_suffix}.json"


def _state_path(workspace: Path, slot: str, tool_use_id: str = "") -> Path:
    edit_suffix = f"-edit-{_edit_key(tool_use_id)}" if tool_use_id else ""
    return workspace / ".unitares" / f"file-leases-{_slot_key(slot)}{edit_suffix}.json"


def _state_paths(
    workspace: Path,
    slot: str,
    *,
    deadline: float | None = None,
) -> list[Path]:
    directory = workspace / ".unitares"
    if not directory.is_dir():
        return []
    paths: list[Path] = []
    seen: set[Path] = set()

    def add_if_owned(path: Path) -> bool:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if path in paths or not path.is_file():
            return True
        # Legacy sanitized names can collide. Trust only the exact raw slot
        # persisted inside the state, never the filename prefix alone.
        if _read_json(path).get("slot") == slot:
            paths.append(path)
            seen.add(path)
        return True

    for path in (
        _state_path(workspace, slot),
        _legacy_state_path(workspace, slot),
    ):
        if not add_if_owned(path):
            _debug("state discovery deadline reached; TTL cleanup remains")
            return paths

    edit_prefixes = (
        f"file-leases-{_slot_key(slot)}-edit-",
        f"file-leases-{_safe_slot(slot)}-edit-",
    )
    try:
        entries = os.scandir(directory)
    except OSError:
        return paths
    with entries:
        for entry in entries:
            if deadline is not None and time.monotonic() >= deadline:
                _debug("state discovery deadline reached; TTL cleanup remains")
                break
            name = entry.name
            if not name.endswith(".json") or not name.startswith(edit_prefixes):
                continue
            path = Path(entry.path)
            if path in seen or not add_if_owned(path):
                continue
    return paths


def _session_cache_path(workspace: Path, slot: str) -> Path:
    return workspace / ".unitares" / f"session-{_safe_slot(slot)}.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, data)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _debug(message: str) -> None:
    if os.environ.get("UNITARES_HOOK_DEBUG") != "1":
        return
    log_path = os.path.expanduser(os.environ.get("UNITARES_HOOK_DEBUG_LOG", "~/.unitares/hook-skips.log"))
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} | hook=file-lease | {message}\n")
    except OSError:
        pass


def _load_env_file() -> None:
    """Load a simple KEY=VALUE env file without overriding existing env."""
    env_path = os.path.expanduser(
        os.environ.get("UNITARES_SECRETS_ENV", "~/.config/cirwel/secrets.env")
    )
    path = Path(env_path)
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _required() -> bool:
    return os.environ.get("UNITARES_FILE_LEASES_REQUIRED", "0").strip().lower() in {
        "1",
        "true",
        "on",
        "yes",
    }


def _enabled() -> bool:
    # Required mode is a fail-closed policy, so it must win over an inherited
    # or stale opt-out. This also keeps acquire and cleanup commands aligned.
    return _required() or os.environ.get(
        "UNITARES_FILE_LEASES_ENABLED", "1"
    ).strip().lower() not in {
        "0",
        "false",
        "off",
    }


def _bearer_token() -> str:
    _load_env_file()
    return (
        os.environ.get("LEASE_PLANE_BEARER_TOKEN")
        or os.environ.get("UNITARES_LEASE_PLANE_BEARER_TOKEN")
        or os.environ.get("GOVERNANCE_TOKEN")
        or ""
    ).strip()


def _base_url() -> str:
    return (
        os.environ.get("LEASE_PLANE_BASE_URL")
        or os.environ.get("UNITARES_LEASE_PLANE_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")


def _timeout_s() -> float:
    raw = os.environ.get("UNITARES_FILE_LEASE_TIMEOUT_S", "1.0")
    try:
        return max(0.05, float(raw))
    except ValueError:
        return 1.0


def _batch_timeout_s() -> float:
    raw = os.environ.get("UNITARES_FILE_LEASE_BATCH_TIMEOUT_S", str(DEFAULT_BATCH_TIMEOUT_S))
    try:
        return min(4.0, max(0.1, float(raw)))
    except ValueError:
        return DEFAULT_BATCH_TIMEOUT_S


def _ttl_s() -> int:
    raw = os.environ.get("UNITARES_FILE_LEASE_TTL_S", str(DEFAULT_TTL_S))
    try:
        return min(3600, max(1, int(raw)))
    except ValueError:
        return DEFAULT_TTL_S


def _http_json(
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        _base_url() + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with authorization_safe_urlopen(
            request,
            timeout=_timeout_s() if timeout_s is None else max(0.05, timeout_s),
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    except Exception as exc:
        return {"ok": False, "error": "service_unavailable", "reason": type(exc).__name__}

    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return {"ok": False, "error": "schema_invalid", "detail": "response was not JSON"}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "schema_invalid"}


def _deadline_timeout(deadline: float | None, cap: float) -> float | None:
    if deadline is None:
        return cap
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(cap, remaining)


def _worktree_roots(
    start: Path,
    *,
    timeout_s: float = 0.75,
) -> tuple[Path, Path] | None:
    """Return (worktree root, repository-unique file namespace root)."""
    import subprocess

    if timeout_s <= 0:
        return None
    probe = start if start.is_dir() else start.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=min(0.75, max(0.01, timeout_s)),
        )
        if out.returncode != 0:
            return None
        lines = out.stdout.splitlines()
        if len(lines) < 2:
            return None
        worktree_root = Path(lines[0].strip()).resolve()
        common_dir = Path(lines[1].strip())
        if not common_dir.is_absolute():
            common_dir = probe / common_dir
        common_dir = common_dir.resolve()

        # Ordinary and linked worktrees share <main>/.git, so the real main
        # checkout remains the clearest namespace. Separate-git-dir and
        # submodule layouts do not have that relationship; use a synthetic
        # path under their unique common dir rather than collapsing unrelated
        # repositories onto common_dir.parent.
        main_candidate = common_dir.parent
        main_dotgit = main_candidate / ".git"
        if main_dotgit.is_dir() and main_dotgit.resolve() == common_dir:
            namespace_root = main_candidate
        else:
            namespace_root = common_dir / "unitares-worktree-files"
        return worktree_root, namespace_root
    except Exception:
        return None


def _canonicalize_worktree_path(
    p: Path,
    roots: tuple[Path, Path] | None = None,
    root_cache: list[tuple[Path, Path]] | None = None,
    deadline: float | None = None,
) -> Path:
    """Map a path inside a git worktree to the equivalent path in the repo's
    main/shared checkout, so the SAME logical file across different worktrees
    collapses to ONE ``file://`` surface.

    Why this matters: the lease-plane ``file:`` scheme canonicalizes surfaces by
    realpath (server-side), so two worktrees of the same repo produce two
    DISTINCT surfaces for the same logical file. Concurrent agents editing that
    file from different worktrees — the dominant multi-agent pattern here — then
    acquire independent leases and never see each other's claim. (That is the
    exact dual-writer collision the file lease is meant to prevent.) Mapping
    every worktree path into one repository-unique namespace collapses them to
    one surface, so the second agent's acquire conflicts.

    Fail-open: any git error returns ``p`` unchanged. A coordination nicety must
    never break an edit, so we degrade to the prior per-worktree behavior rather
    than raising.
    """
    try:
        resolved_path = p.resolve()
        candidates: list[tuple[Path, Path]] = []
        if roots is not None:
            candidates.append(roots)
        if root_cache is not None:
            candidates.extend(item for item in root_cache if item not in candidates)
        for worktree_root, namespace_root in candidates:
            try:
                return namespace_root / resolved_path.relative_to(worktree_root)
            except ValueError:
                continue

        timeout_s = _deadline_timeout(deadline, 0.75)
        if timeout_s is None:
            return p
        resolved_roots = _worktree_roots(resolved_path, timeout_s=timeout_s)
        if resolved_roots is None:
            return p
        if root_cache is not None and resolved_roots not in root_cache:
            root_cache.append(resolved_roots)
        worktree_root, namespace_root = resolved_roots
        return namespace_root / resolved_path.relative_to(worktree_root)
    except Exception:
        return p


def _surface_id(
    path: str,
    workspace: Path,
    roots: tuple[Path, Path] | None = None,
    root_cache: list[tuple[Path, Path]] | None = None,
    deadline: float | None = None,
) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = workspace / p
    # Collapse worktrees of the same repo to a single surface so concurrent
    # agents in different worktrees see each other's lease (see
    # _canonicalize_worktree_path). Fail-open to the raw absolute path.
    p = _canonicalize_worktree_path(p, roots, root_cache, deadline).resolve()
    return f"file://{p}"


def _load_state(workspace: Path, slot: str, tool_use_id: str = "") -> dict[str, Any]:
    path = _state_path(workspace, slot, tool_use_id)
    legacy_path = _legacy_state_path(workspace, slot, tool_use_id)
    data: dict[str, Any] = {}
    source: Path | None = None
    if path.is_file():
        source = path
        data = _read_json(path)
    elif legacy_path.is_file():
        legacy = _read_json(legacy_path)
        if legacy.get("slot") == slot:
            source = legacy_path
            data = legacy
    if source is None:
        return {
            "version": STATE_VERSION,
            "slot": slot,
            "workspace": str(workspace),
            "tool_use_id": tool_use_id,
            "holder_uuid": "",
            "leases": {},
        }
    if not data or data.get("version") != STATE_VERSION:
        version = data.get("version") if isinstance(data, dict) else None
        raise UnsupportedLeaseStateError(
            f"unsupported or unreadable lease state at {source} (version={version!r})"
        )
    leases = data.get("leases")
    if not isinstance(leases, dict):
        data["leases"] = {}
    return data


def _holder_uuid(workspace: Path, event: EditHookEvent, state: dict[str, Any]) -> str:
    if event.tool_use_id:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"unitares:file-lease:{event.host}:{event.session_id}:{event.tool_use_id}",
            )
        )
    existing = str(state.get("holder_uuid") or "").strip()
    if existing:
        return existing
    cache = _read_json(_session_cache_path(workspace, event.session_id))
    cached = str(cache.get("uuid") or cache.get("agent_uuid") or "").strip()
    if cached:
        return cached
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"unitares:file-lease:{workspace}:{event.session_id}")
    )


def _save_state(
    workspace: Path,
    slot: str,
    state: dict[str, Any],
    tool_use_id: str = "",
) -> None:
    path = _state_path(workspace, slot, tool_use_id)
    _write_json(path, state)
    legacy = _legacy_state_path(workspace, slot, tool_use_id)
    if legacy != path:
        legacy.unlink(missing_ok=True)


def _block(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def _fail_open_or_block(message: str) -> int:
    _debug(message)
    if _required():
        return _block(f"BLOCKED: file lease required but unavailable: {message}")
    return 0


def _heartbeat(
    token: str,
    lease_id: str,
    *,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    return _http_json(
        "POST",
        "/v1/lease/heartbeat",
        token=token,
        body={"lease_id": lease_id},
        timeout_s=timeout_s,
    )


def _release(
    token: str,
    lease_id: str,
    *,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    return _http_json(
        "POST",
        "/v1/lease/release",
        token=token,
        body={"lease_id": lease_id, "release_reason": "normal"},
        timeout_s=timeout_s,
    )


def _self_heal_eta(expires_at: Any) -> str:
    """Human phrase for when a `file://` lease auto-clears.

    File leases take the `remote_heartbeat` path: a pure TTL row with no
    auto-renewing holder, swept by the reaper at `expires_at`. So a lease held
    by a *dead* session is not permanent — it clears on its own. This phrase
    tells the operator that, so they don't reflexively force-release a lease
    that would have cleared by itself.
    """
    if not isinstance(expires_at, str) or not expires_at:
        return ""
    try:
        from datetime import datetime, timezone

        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        remaining = (exp - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return ""
    if remaining <= 30:
        return " (expiring now — should clear within a reaper cycle)"
    minutes = int(remaining // 60) or 1
    return f" (auto-clears in ~{minutes} min if the holder is gone — no action needed)"


def _remaining_timeout(deadline: float) -> float | None:
    return _deadline_timeout(deadline, _timeout_s())


def _find_lease_key(
    leases: dict[str, Any],
    *,
    file_path: str,
    surface_id: str,
) -> str | None:
    return next(
        (
            key
            for key, row in leases.items()
            if isinstance(row, dict)
            and row.get("lease_id")
            and (row.get("path") == file_path or key == surface_id)
        ),
        None,
    )


def _lease_owned_by_event(
    row: Any,
    *,
    holder_uuid: str,
    tool_use_id: str,
) -> bool:
    if not isinstance(row, dict) or row.get("holder_uuid") != holder_uuid:
        return False
    return not tool_use_id or row.get("tool_use_id") == tool_use_id


def _persist_or_remove_state(
    workspace: Path,
    slot: str,
    state: dict[str, Any],
    tool_use_id: str = "",
) -> None:
    leases = state.get("leases")
    if isinstance(leases, dict) and leases:
        _save_state(workspace, slot, state, tool_use_id)
    else:
        _state_path(workspace, slot, tool_use_id).unlink(missing_ok=True)
        _legacy_state_path(workspace, slot, tool_use_id).unlink(missing_ok=True)


def _rollback_batch_leases(
    *,
    token: str,
    workspace: Path,
    slot: str,
    state: dict[str, Any],
    rollback_keys: list[str],
    deadline: float,
    tool_use_id: str = "",
) -> None:
    """Release every target lease owned by a batch that will not run."""
    leases = state.get("leases")
    if not isinstance(leases, dict):
        return
    for key in reversed(rollback_keys):
        row = leases.get(key)
        if not isinstance(row, dict) or not row.get("lease_id"):
            continue
        timeout_s = _remaining_timeout(deadline)
        if timeout_s is None:
            _debug(f"rollback deadline reached for {key}; TTL/session cleanup remains")
            break
        result = _release(token, str(row["lease_id"]), timeout_s=timeout_s)
        if result.get("ok") is True or result.get("error") in {"not_found", "expired"}:
            leases.pop(key, None)
        else:
            _debug(f"rollback release failed for {key}: {result.get('error')}")
    _persist_or_remove_state(workspace, slot, state, tool_use_id)


def _cmd_pre_edit_locked(args: argparse.Namespace, stdin_text: str) -> int:
    if not _enabled():
        return 0

    workspace = Path(args.workspace).resolve()
    try:
        payload = normalize_edit_hook(stdin_text, host=args.host)
    except EditHookPayloadError as exc:
        return _fail_open_or_block(f"invalid {args.host} edit payload: {exc}")
    if not payload.session_id:
        return _fail_open_or_block("missing session_id")
    if not payload.tool_use_id:
        return _fail_open_or_block(
            f"missing tool_use_id in {args.host} edit payload; lease ownership "
            "cannot be correlated safely"
        )

    token = _bearer_token()
    if not token:
        return _fail_open_or_block("missing LEASE_PLANE_BEARER_TOKEN")

    try:
        state = _load_state(workspace, payload.session_id, payload.tool_use_id)
    except UnsupportedLeaseStateError as exc:
        return _fail_open_or_block(str(exc))
    holder_uuid = _holder_uuid(workspace, payload, state)
    leases = state.setdefault("leases", {})
    if not isinstance(leases, dict):
        leases = {}
        state["leases"] = leases

    batch_budget = _batch_timeout_s()
    deadline = time.monotonic() + batch_budget
    rollback_reserve = min(0.75, batch_budget / 3.0)
    operation_deadline = deadline - rollback_reserve
    rollback_keys: list[str] = []
    for file_path in payload.paths:
        existing_key = _find_lease_key(
            leases,
            file_path=file_path,
            surface_id="",
        )
        if (
            existing_key is not None
            and _lease_owned_by_event(
                leases[existing_key],
                holder_uuid=holder_uuid,
                tool_use_id=payload.tool_use_id,
            )
            and existing_key not in rollback_keys
        ):
            rollback_keys.append(existing_key)

    def rollback() -> None:
        try:
            _rollback_batch_leases(
                token=token,
                workspace=workspace,
                slot=payload.session_id,
                state=state,
                rollback_keys=rollback_keys,
                deadline=deadline,
                tool_use_id=payload.tool_use_id,
            )
        except Exception as exc:
            # A local persistence failure must not replace the original lease
            # decision. Remote releases were attempted first; TTL remains the
            # final backstop for any row that could not be recorded locally.
            _debug(f"batch rollback persistence failed: {exc}")

    roots_timeout = _deadline_timeout(operation_deadline, 0.75)
    if roots_timeout is None:
        rollback()
        return _fail_open_or_block("batch lease deadline reached during path setup")
    roots = _worktree_roots(workspace, timeout_s=roots_timeout)
    root_cache: list[tuple[Path, Path]] = []
    targets_by_surface: dict[str, str] = {}
    for file_path in payload.paths:
        if _deadline_timeout(operation_deadline, 1.0) is None:
            rollback()
            return _fail_open_or_block("batch lease deadline reached during path setup")
        surface_id = _surface_id(
            file_path,
            workspace,
            roots,
            root_cache,
            operation_deadline,
        )
        targets_by_surface[surface_id] = file_path
    if _deadline_timeout(operation_deadline, 1.0) is None:
        rollback()
        return _fail_open_or_block("batch lease deadline reached during path setup")
    targets = sorted(targets_by_surface.items())
    for surface_id, file_path in targets:
        existing_key = _find_lease_key(
            leases,
            file_path=file_path,
            surface_id=surface_id,
        )
        if (
            existing_key is not None
            and _lease_owned_by_event(
                leases[existing_key],
                holder_uuid=holder_uuid,
                tool_use_id=payload.tool_use_id,
            )
            and existing_key not in rollback_keys
        ):
            rollback_keys.append(existing_key)

    for surface_id, file_path in targets:
        timeout_s = _remaining_timeout(operation_deadline)
        if timeout_s is None:
            rollback()
            return _fail_open_or_block("batch lease deadline reached")

        existing_key = _find_lease_key(
            leases,
            file_path=file_path,
            surface_id=surface_id,
        )
        if existing_key is not None and not _lease_owned_by_event(
            leases[existing_key],
            holder_uuid=holder_uuid,
            tool_use_id=payload.tool_use_id,
        ):
            existing_key = None
        if existing_key is not None:
            existing = leases[existing_key]
            heartbeat = _heartbeat(token, str(existing["lease_id"]), timeout_s=timeout_s)
            if heartbeat.get("ok") is True:
                existing["last_heartbeat_at"] = time.time()
                _save_state(workspace, payload.session_id, state, payload.tool_use_id)
                continue
            if heartbeat.get("error") not in {"not_found", "expired"}:
                rollback()
                return _fail_open_or_block(
                    f"heartbeat failed for {surface_id}: {heartbeat.get('error')}"
                )
            leases.pop(existing_key, None)

        body = {
            "surface_id": surface_id,
            "holder_agent_uuid": holder_uuid,
            "holder_class": "process_instance",
            "holder_kind": "remote_heartbeat",
            "ttl_s": _ttl_s(),
            "holder_pid": str(os.getpid()),
            "intent": f"{payload.host} plugin {payload.tool_name}",
            "audit_session": payload.session_id,
        }
        timeout_s = _remaining_timeout(operation_deadline)
        if timeout_s is None:
            result: dict[str, Any] = {"ok": False, "error": "batch_timeout"}
        else:
            result = _http_json(
                "POST",
                "/v1/lease/acquire",
                token=token,
                body=body,
                timeout_s=timeout_s,
            )
        if result.get("ok") is True:
            lease = result.get("lease") if isinstance(result.get("lease"), dict) else {}
            lease_id = str(lease.get("lease_id") or "")
            if not lease_id:
                rollback()
                return _fail_open_or_block("acquire response missing lease_id")
            key = str(lease.get("surface_id") or surface_id)
            idempotent = bool(result.get("idempotent"))
            state["holder_uuid"] = holder_uuid
            leases[key] = {
                "lease_id": lease_id,
                "path": file_path,
                "surface_id": key,
                "expires_at": lease.get("expires_at"),
                "acquired_at": time.time(),
                "idempotent": idempotent,
                "holder_uuid": holder_uuid,
                "tool_use_id": payload.tool_use_id,
                "host": payload.host,
            }
            if key not in rollback_keys:
                rollback_keys.append(key)
            try:
                _save_state(workspace, payload.session_id, state, payload.tool_use_id)
            except Exception as exc:
                rollback()
                return _fail_open_or_block(
                    f"lease state persistence failed after acquire: {exc}"
                )
            continue

        rollback()
        if result.get("error") == "held_by_other":
            expires_at = result.get("expires_at", "?")
            return _block(
                "BLOCKED: file lease held by another agent\n"
                f"  Path: {file_path}\n"
                f"  Surface: {result.get('surface_id') or surface_id}\n"
                f"  Blocking lease: {result.get('blocking_lease_id', '?')}\n"
                f"  Held by: {result.get('held_by_uuid', '?')}\n"
                f"  Expires: {expires_at}{_self_heal_eta(expires_at)}\n"
                "This lease self-heals: file leases auto-expire at the time above even if the "
                "holding session died without releasing.\n"
                "Best action: wait for it to clear or edit a different file. Force-release "
                "(operator) is only for when you genuinely can't wait."
            )
        return _fail_open_or_block(
            f"acquire failed for {surface_id}: {result.get('error') or result}"
        )

    return 0


def cmd_pre_edit(args: argparse.Namespace, stdin_text: str) -> int:
    """Run one pre-edit state transaction under its stable tool lock."""
    if not _enabled():
        return 0
    workspace = Path(args.workspace).resolve()
    try:
        payload = normalize_edit_hook(stdin_text, host=args.host)
    except EditHookPayloadError:
        return _cmd_pre_edit_locked(args, stdin_text)
    if not payload.session_id or not payload.tool_use_id:
        return _cmd_pre_edit_locked(args, stdin_text)
    if not _bearer_token():
        return _cmd_pre_edit_locked(args, stdin_text)
    path = _state_path(workspace, payload.session_id, payload.tool_use_id)
    try:
        with session_cache_lock(path):
            return _cmd_pre_edit_locked(args, stdin_text)
    except TimeoutError as exc:
        return _fail_open_or_block(str(exc))


def cmd_heartbeat_session(args: argparse.Namespace, stdin_text: str) -> int:
    # File leases are scoped to one edit and released by PostToolUse. Retain
    # this command as a compatibility no-op so an old external caller cannot
    # accidentally keep residual leases alive indefinitely.
    _debug("heartbeat-session is deprecated; edit leases are not renewed")
    return 0


def _release_state_file(
    state_path: Path,
    *,
    token: str,
    deadline: float,
    expected_slot: str | None = None,
    expected_tool_use_id: str | None = None,
) -> bool:
    """Release one state file under its stable lock.

    Returns whether the aggregate deadline was reached. Unsupported or
    ownership-mismatched state is preserved for a newer client or TTL cleanup.
    """
    if not state_path.is_file():
        return False
    lock_timeout = deadline - time.monotonic()
    if lock_timeout <= 0:
        _debug("release deadline reached before state lock; TTL cleanup remains")
        return True
    try:
        with session_cache_lock(state_path, timeout_s=lock_timeout):
            if not state_path.is_file():
                return False
            state = _read_json(state_path)
            if state.get("version") != STATE_VERSION:
                _debug(f"release preserved unsupported state: {state_path}")
                return False
            if expected_slot is not None and state.get("slot") != expected_slot:
                _debug(f"release preserved slot-mismatched state: {state_path}")
                return False
            if (
                expected_tool_use_id is not None
                and state.get("tool_use_id") != expected_tool_use_id
            ):
                _debug(f"release preserved tool-mismatched state: {state_path}")
                return False
            leases = state.get("leases")
            deadline_reached = False
            if isinstance(leases, dict):
                for surface, row in list(leases.items()):
                    if not isinstance(row, dict) or not row.get("lease_id"):
                        continue
                    timeout_s = _remaining_timeout(deadline)
                    if timeout_s is None:
                        _debug("release deadline reached; TTL cleanup remains")
                        deadline_reached = True
                        break
                    result = _release(token, str(row["lease_id"]), timeout_s=timeout_s)
                    if result.get("ok") is True or result.get("error") in {
                        "not_found",
                        "expired",
                    }:
                        leases.pop(surface, None)
                    else:
                        _debug(f"release failed for {surface}: {result.get('error')}")
            if isinstance(leases, dict) and leases:
                _write_json(state_path, state)
            else:
                state_path.unlink(missing_ok=True)
            return deadline_reached
    except TimeoutError:
        _debug(f"release lock deadline reached; TTL cleanup remains: {state_path}")
        return True


def cmd_release_session(args: argparse.Namespace, stdin_text: str) -> int:
    if not _enabled():
        return 0
    workspace = Path(args.workspace).resolve()
    session_id = _session_id(stdin_text)
    if not session_id:
        return 0
    token = _bearer_token()
    if not token:
        _debug("release skipped: missing token")
        return 0
    if args.budget is not None:
        release_budget = min(args.budget, 60.0) if args.budget > 0 else 0.05
    else:
        release_budget = (
            min(_batch_timeout_s(), 1.5)
            if args.host == "codex"
            else _batch_timeout_s()
        )
    deadline = time.monotonic() + release_budget
    state_paths = _state_paths(workspace, session_id, deadline=deadline)
    for state_path in state_paths:
        if time.monotonic() >= deadline:
            _debug("release-session deadline reached; TTL cleanup remains")
            break
        if _release_state_file(
            state_path,
            token=token,
            deadline=deadline,
            expected_slot=session_id,
        ):
            break
    return 0


def _cmd_release_edit_locked(args: argparse.Namespace, stdin_text: str) -> int:
    """Release leases for the just-completed edit event (PostToolUse).

    A `file://` lease only needs to exist for the duration of the write that
    holds it. Releasing it right after the edit — instead of heartbeating it
    until SessionEnd — means a file is leased only while actively being
    mutated, so a session that dies cannot strand a held lease (the immortal-
    lease class). The TTL remains a backstop for the crash-mid-edit window
    (acquire fired in pre-edit, the session died before this released).

    Fire-and-forget: always returns 0; never blocks the PostToolUse chain.
    """
    if not _enabled():
        return 0
    workspace = Path(args.workspace).resolve()
    try:
        payload = normalize_edit_hook(stdin_text, host=args.host)
    except EditHookPayloadError as exc:
        _debug(f"release-edit invalid {args.host} payload: {exc}")
        return 0
    if not payload.session_id:
        return 0
    if not payload.tool_use_id:
        _debug(
            f"release-edit skipped: {args.host} payload has no tool_use_id; "
            "session/TTL cleanup remains"
        )
        return 0
    token = _bearer_token()
    if not token:
        _debug("release-edit skipped: missing token")
        return 0
    state_tool_use_id = payload.tool_use_id
    try:
        state = _load_state(workspace, payload.session_id, state_tool_use_id)
    except UnsupportedLeaseStateError as exc:
        _debug(f"release-edit preserved state: {exc}")
        return 0
    leases = state.get("leases")
    if not isinstance(leases, dict) or not leases:
        # Hot-upgrade compatibility: an edit acquired under the old
        # session-wide state contract may complete after this version installs.
        for legacy_path in (
            _state_path(workspace, payload.session_id),
            _legacy_state_path(workspace, payload.session_id),
        ):
            legacy = _read_json(legacy_path)
            legacy_leases = legacy.get("leases")
            if (
                legacy.get("version") == STATE_VERSION
                and legacy.get("slot") == payload.session_id
                and isinstance(legacy_leases, dict)
                and any(
                    isinstance(row, dict) and row.get("path") in payload.paths
                    for row in legacy_leases.values()
                )
            ):
                state = legacy
                leases = legacy_leases
                state_tool_use_id = ""
                break
    if not isinstance(leases, dict) or not leases:
        return 0
    release_budget = min(_batch_timeout_s(), 1.5) if args.host == "codex" else _batch_timeout_s()
    deadline = time.monotonic() + release_budget
    roots: tuple[Path, Path] | None = None
    roots_loaded = False
    root_cache: list[tuple[Path, Path]] = []
    keys: list[str] = []
    for file_path in payload.paths:
        # New state preserves the raw event path, so the common path needs no
        # Git subprocess at all. Canonicalization is only a compatibility
        # fallback for older rows that lack or changed that field.
        key = _find_lease_key(leases, file_path=file_path, surface_id="")
        if key is None:
            roots_timeout = _deadline_timeout(deadline, 0.75)
            if roots_timeout is None:
                _debug("release-edit path setup deadline reached; session/TTL cleanup remains")
                break
            if not roots_loaded:
                roots = _worktree_roots(workspace, timeout_s=roots_timeout)
                roots_loaded = True
            surface_id = _surface_id(
                file_path,
                workspace,
                roots,
                root_cache,
                deadline,
            )
            key = _find_lease_key(
                leases,
                file_path=file_path,
                surface_id=surface_id,
            )
        if key is not None and key not in keys:
            keys.append(key)
    for key in keys:
        timeout_s = _remaining_timeout(deadline)
        if timeout_s is None:
            _debug("release-edit batch deadline reached; session/TTL cleanup remains")
            break
        result = _release(token, str(leases[key]["lease_id"]), timeout_s=timeout_s)
        if result.get("ok") is not True and result.get("error") not in {"not_found", "expired"}:
            # Keep failures in state so session-end can retry; TTL is the final backstop.
            _debug(f"release-edit failed for {key}: {result.get('error')}")
            continue
        leases.pop(key, None)
    _persist_or_remove_state(
        workspace,
        payload.session_id,
        state,
        state_tool_use_id,
    )
    return 0


def cmd_release_edit(args: argparse.Namespace, stdin_text: str) -> int:
    """Release one completed edit under its exact tool-state lock."""
    if not _enabled():
        return 0
    workspace = Path(args.workspace).resolve()
    try:
        payload = normalize_edit_hook(stdin_text, host=args.host)
    except EditHookPayloadError:
        return _cmd_release_edit_locked(args, stdin_text)
    if not payload.session_id or not payload.tool_use_id:
        return _cmd_release_edit_locked(args, stdin_text)
    if not _bearer_token():
        return _cmd_release_edit_locked(args, stdin_text)
    path = _state_path(workspace, payload.session_id, payload.tool_use_id)
    try:
        with session_cache_lock(path):
            return _cmd_release_edit_locked(args, stdin_text)
    except TimeoutError:
        _debug(f"release-edit lock timed out; TTL cleanup remains: {path}")
        return 0


def cmd_release_batch(args: argparse.Namespace, stdin_text: str) -> int:
    """Release only edit tool IDs proven complete by Claude PostToolBatch."""
    if not _enabled() or args.host != "claude":
        return 0
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    session_id = str(payload.get("session_id") or "").strip()
    tool_calls = payload.get("tool_calls")
    if not session_id or not isinstance(tool_calls, list):
        return 0
    completed_ids: list[str] = []
    for call in tool_calls:
        if not isinstance(call, dict) or call.get("tool_name") not in {
            "Edit",
            "Write",
            "MultiEdit",
        }:
            continue
        tool_use_id = str(call.get("tool_use_id") or "").strip()
        if tool_use_id and tool_use_id not in completed_ids:
            completed_ids.append(tool_use_id)
    if not completed_ids:
        return 0
    token = _bearer_token()
    if not token:
        _debug("release-batch skipped: missing token")
        return 0
    release_budget = args.budget if args.budget is not None else _batch_timeout_s()
    deadline = time.monotonic() + max(0.05, min(release_budget, 60.0))
    workspace = Path(args.workspace).resolve()
    for tool_use_id in completed_ids:
        if time.monotonic() >= deadline:
            _debug("release-batch deadline reached; SessionEnd/TTL cleanup remains")
            break
        for state_path in (
            _state_path(workspace, session_id, tool_use_id),
            _legacy_state_path(workspace, session_id, tool_use_id),
        ):
            if _release_state_file(
                state_path,
                token=token,
                deadline=deadline,
                expected_slot=session_id,
                expected_tool_use_id=tool_use_id,
            ):
                return 0
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "pre-edit",
            "release-edit",
            "release-batch",
            "heartbeat-session",
            "release-session",
        ],
    )
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--host", choices=("claude", "codex"), default="claude")
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        help="Optional aggregate release-session deadline in seconds.",
    )
    return parser


def main(argv: list[str] | None = None, stdin_text: str | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    text = sys.stdin.read() if stdin_text is None else stdin_text
    if args.command == "pre-edit":
        return cmd_pre_edit(args, text)
    if args.command == "release-edit":
        return cmd_release_edit(args, text)
    if args.command == "release-batch":
        return cmd_release_batch(args, text)
    if args.command == "heartbeat-session":
        return cmd_heartbeat_session(args, text)
    if args.command == "release-session":
        return cmd_release_session(args, text)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
