#!/usr/bin/env python3
"""Shared transactions for UNITARES session-cache writers."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

DEFAULT_SESSION_CACHE_LOCK_TIMEOUT_S = 2.0


def cache_generation_path(path: Path) -> Path:
    """Return the persistent generation/tombstone path for one cache slot."""
    return path.with_suffix(".generation")


def read_cache_generation_unlocked(path: Path) -> int:
    """Read a generation while the caller holds ``session_cache_lock``."""
    generation_path = cache_generation_path(path)
    if generation_path.is_symlink():
        return 0
    try:
        generation = int(generation_path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return 0
    return max(0, generation)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def advance_cache_generation_unlocked(path: Path) -> int:
    """Advance one generation before mutating its JSON cache."""
    generation = read_cache_generation_unlocked(path) + 1
    _atomic_write_bytes(
        cache_generation_path(path),
        f"{generation}\n".encode("ascii"),
    )
    return generation


def read_json_dict_unlocked(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_json_dict_unlocked(path: Path, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write_bytes(path, data)


def _lock_timeout_s() -> float:
    raw = os.environ.get(
        "UNITARES_SESSION_CACHE_LOCK_TIMEOUT_S",
        str(DEFAULT_SESSION_CACHE_LOCK_TIMEOUT_S),
    )
    try:
        return min(4.0, max(0.05, float(raw)))
    except ValueError:
        return DEFAULT_SESSION_CACHE_LOCK_TIMEOUT_S


@contextmanager
def session_cache_lock(
    path: Path,
    *,
    timeout_s: float | None = None,
) -> Iterator[None]:
    """Lock one cache slot through a stable sidecar across atomic replaces.

    Cache JSON files are replaced atomically, so locking the JSON inode would
    not serialize a later writer that opens the replacement. Every writer uses
    ``session-<slot>.lock`` instead and leaves that sidecar in place.
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
    lock_timeout = _lock_timeout_s() if timeout_s is None else max(0.0, timeout_s)
    deadline = time.monotonic() + lock_timeout
    try:
        if os.name == "nt":
            import msvcrt

            # msvcrt locks a byte range from the current offset. Keep one byte
            # in the sidecar so every process locks the same region.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while not acquired:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"session cache lock timed out: {lock_path}"
                        )
                    time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
        else:
            import fcntl

            while not acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"session cache lock timed out: {lock_path}"
                        )
                    time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
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


def session_cache_snapshot(path: Path) -> tuple[dict[str, Any], int]:
    """Read one cache and generation atomically."""
    path, _ = _normalized_cache_paths(path, None)
    with session_cache_lock(path):
        return read_json_dict_unlocked(path), read_cache_generation_unlocked(path)


def _normalized_cache_paths(
    path: Path,
    home_path: Path | None,
) -> tuple[Path, Path | None]:
    """Resolve directory aliases without following the cache file itself."""
    path = _normalized_final_path(path)
    if home_path is None:
        return path, None
    home_path = _normalized_final_path(home_path)
    return path, None if home_path == path else home_path


def _normalized_final_path(path: Path) -> Path:
    """Resolve directory aliases while preserving final-file symlink handling."""
    path = path.expanduser()
    return path.parent.resolve() / path.name


def cache_authority_path(home_path: Path) -> Path:
    """Return the persistent slot authority outside the optional mirror dir."""
    raw = home_path.expanduser()
    home_root = raw.parent.parent.resolve()
    return home_root / ".unitares-cache-authority" / raw.name


def _transaction_paths(
    path: Path,
    home_path: Path | None,
) -> tuple[Path, Path | None, Path | None, bool]:
    raw_home = home_path
    path, home_path = _normalized_cache_paths(path, home_path)
    authority_path = (
        _normalized_final_path(cache_authority_path(raw_home))
        if raw_home is not None
        else None
    )
    if authority_path is not None and authority_path in (path, home_path):
        raise ValueError(
            "session cache path aliases its authority record; "
            "use distinct .unitares and .unitares-cache-authority directories"
        )
    return path, home_path, authority_path, raw_home is not None and home_path is None


def _read_authority_unlocked(
    authority_path: Path,
    home_cache_path: Path,
) -> dict[str, Any]:
    state = read_json_dict_unlocked(authority_path)
    if state.get("schema_version") == 1 and isinstance(state.get("generation"), int):
        return state
    return {
        "schema_version": 1,
        "generation": 0,
        "mirror_valid": home_cache_path.is_file() and not home_cache_path.is_symlink(),
    }


def _write_authority_unlocked(
    authority_path: Path,
    *,
    generation: int,
    mirror_valid: bool,
) -> None:
    write_json_dict_unlocked(
        authority_path,
        {
            "schema_version": 1,
            "generation": generation,
            "mirror_valid": mirror_valid,
        },
    )


def _advance_authority_unlocked(
    authority_path: Path,
    home_cache_path: Path,
    *,
    mirror_valid: bool | None = None,
) -> int:
    state = _read_authority_unlocked(authority_path, home_cache_path)
    generation = max(0, int(state.get("generation") or 0)) + 1
    _write_authority_unlocked(
        authority_path,
        generation=generation,
        mirror_valid=(
            bool(state.get("mirror_valid")) if mirror_valid is None else mirror_valid
        ),
    )
    return generation


def home_session_mirror_is_valid(home_path: Path) -> bool:
    """Reject stale mirrors invalidated by a failed write or explicit clear."""
    home_path = home_path.expanduser()
    authority_path = _normalized_final_path(cache_authority_path(home_path))
    home_path = _normalized_final_path(home_path)
    if home_path.is_symlink():
        return False
    if authority_path == home_path:
        return False
    if not authority_path.exists():
        return True  # Legacy mirror written before the authority contract.
    state = read_json_dict_unlocked(authority_path)
    return state.get("schema_version") == 1 and state.get("mirror_valid") is True


def _read_transaction_current_unlocked(
    path: Path,
    *,
    authority_path: Path | None,
    home_is_primary: bool,
) -> dict[str, Any]:
    if home_is_primary and authority_path is not None:
        authority = _read_authority_unlocked(authority_path, path)
        if authority.get("mirror_valid") is not True:
            return {}
    return read_json_dict_unlocked(path)


@contextmanager
def _session_cache_locks(
    path: Path,
    home_path: Path | None,
    *,
    authority_path: Path | None,
    lock_mirror: bool = True,
) -> Iterator[tuple[Path | None, Exception | None]]:
    """Acquire authority, primary, then optional mirror in fixed order."""
    authority_lock = session_cache_lock(authority_path) if authority_path else None
    if authority_lock is not None:
        authority_lock.__enter__()
    try:
        primary_lock = session_cache_lock(path)
        primary_lock.__enter__()
        try:
            if home_path is None or not lock_mirror:
                yield None, None
                return
            home_lock = session_cache_lock(home_path)
            try:
                home_lock.__enter__()
            except TimeoutError:
                raise
            except OSError as exc:
                yield None, exc
                return
            try:
                yield home_path, None
            finally:
                home_lock.__exit__(None, None, None)
        finally:
            primary_lock.__exit__(None, None, None)
    finally:
        if authority_lock is not None:
            authority_lock.__exit__(None, None, None)


def reserve_session_cache_snapshot(
    path: Path,
    *,
    home_path: Path | None = None,
) -> tuple[dict[str, Any], int, int | None]:
    """Reserve invocation order and return the cache plus generation token."""
    path, home_path, authority_path, home_is_primary = _transaction_paths(
        path,
        home_path,
    )
    home_cache_path = path if home_is_primary else home_path
    with _session_cache_locks(
        path,
        home_path,
        authority_path=authority_path,
        lock_mirror=False,
    ):
        current = _read_transaction_current_unlocked(
            path,
            authority_path=authority_path,
            home_is_primary=home_is_primary,
        )
        authority_generation = None
        if authority_path is not None and home_cache_path is not None:
            authority_generation = _advance_authority_unlocked(
                authority_path,
                home_cache_path,
            )
        generation = advance_cache_generation_unlocked(path)
        return current, generation, authority_generation


def _replace_locked(
    path: Path,
    payload: dict[str, Any],
    *,
    home_path: Path | None,
    home_cache_path: Path | None,
    home_is_primary: bool,
    authority_path: Path | None,
    on_mirror_error: Callable[[Path, Exception], None] | None,
) -> None:
    """Replace primary and mirror while all available transaction locks are held."""
    authority_generation = None
    if authority_path is not None and home_cache_path is not None:
        authority_generation = _advance_authority_unlocked(
            authority_path,
            home_cache_path,
            mirror_valid=False,
        )

    mirror_ready = home_path is not None
    if mirror_ready:
        # A missing fallback is safer than a stale identity. Invalidate the
        # mirror before the authoritative primary changes.
        try:
            advance_cache_generation_unlocked(home_path)
            home_path.unlink(missing_ok=True)
        except Exception as exc:
            mirror_ready = False
            try:
                home_path.unlink(missing_ok=True)
            except OSError:
                pass
            if on_mirror_error is not None:
                on_mirror_error(home_path, exc)

    advance_cache_generation_unlocked(path)
    write_json_dict_unlocked(path, payload)

    mirror_written = home_is_primary
    if mirror_ready:
        try:
            write_json_dict_unlocked(home_path, payload)
            mirror_written = True
        except Exception as exc:
            home_path.unlink(missing_ok=True)
            if on_mirror_error is not None:
                on_mirror_error(home_path, exc)

    if authority_path is not None and authority_generation is not None and mirror_written:
        try:
            _write_authority_unlocked(
                authority_path,
                generation=authority_generation,
                mirror_valid=True,
            )
        except OSError as exc:
            if on_mirror_error is not None and home_cache_path is not None:
                on_mirror_error(home_cache_path, exc)


def replace_session_cache(
    path: Path,
    payload: dict[str, Any],
    *,
    home_path: Path | None = None,
    on_mirror_error: Callable[[Path, Exception], None] | None = None,
) -> None:
    """Replace primary and optional HOME mirror in one ordered transaction."""
    path, home_path, authority_path, home_is_primary = _transaction_paths(
        path,
        home_path,
    )
    home_cache_path = path if home_is_primary else home_path
    with _session_cache_locks(
        path,
        home_path,
        authority_path=authority_path,
    ) as (locked_home, home_error):
        if home_error is not None and on_mirror_error is not None and home_path is not None:
            on_mirror_error(home_path, home_error)
        _replace_locked(
            path,
            payload,
            home_path=locked_home,
            home_cache_path=home_cache_path,
            home_is_primary=home_is_primary,
            authority_path=authority_path,
            on_mirror_error=on_mirror_error,
        )


def update_session_cache(
    path: Path,
    mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    *,
    home_path: Path | None = None,
    expected_generation: int | None = None,
    expected_authority_generation: int | None = None,
    on_mirror_error: Callable[[Path, Exception], None] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Run one generation-aware read-modify-write transaction.

    Returns ``(authoritative_payload, committed)``. A generation mismatch or a
    mutator returning ``None`` preserves current state and reports ``False``.
    """
    path, home_path, authority_path, home_is_primary = _transaction_paths(
        path,
        home_path,
    )
    home_cache_path = path if home_is_primary else home_path
    with _session_cache_locks(
        path,
        home_path,
        authority_path=authority_path,
    ) as (locked_home, home_error):
        if home_error is not None and on_mirror_error is not None and home_path is not None:
            on_mirror_error(home_path, home_error)
        current = _read_transaction_current_unlocked(
            path,
            authority_path=authority_path,
            home_is_primary=home_is_primary,
        )
        if (
            expected_generation is not None
            and read_cache_generation_unlocked(path) != expected_generation
        ):
            return current, False
        if expected_authority_generation is not None:
            if authority_path is None or home_cache_path is None:
                return current, False
            authority_state = _read_authority_unlocked(authority_path, home_cache_path)
            if authority_state.get("generation") != expected_authority_generation:
                return current, False
        replacement = mutator(dict(current))
        if replacement is None:
            return current, False
        _replace_locked(
            path,
            replacement,
            home_path=locked_home,
            home_cache_path=home_cache_path,
            home_is_primary=home_is_primary,
            authority_path=authority_path,
            on_mirror_error=on_mirror_error,
        )
        return replacement, True


def clear_session_cache(path: Path, *, home_path: Path | None = None) -> None:
    """Clear a cache while retaining a generation tombstone."""
    path, home_path, authority_path, home_is_primary = _transaction_paths(
        path,
        home_path,
    )
    home_cache_path = path if home_is_primary else home_path
    with _session_cache_locks(
        path,
        home_path,
        authority_path=authority_path,
    ) as (locked_home, _home_error):
        if authority_path is not None and home_cache_path is not None:
            _advance_authority_unlocked(
                authority_path,
                home_cache_path,
                mirror_valid=False,
            )
        if locked_home is not None:
            try:
                advance_cache_generation_unlocked(locked_home)
            except OSError:
                pass
            try:
                locked_home.unlink(missing_ok=True)
            except OSError:
                pass
        advance_cache_generation_unlocked(path)
        path.unlink(missing_ok=True)
