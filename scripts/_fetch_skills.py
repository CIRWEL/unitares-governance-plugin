#!/usr/bin/env python3
"""S15-c — Claude Code skill adapter (server fetch with offline fallback).

Per §9 step 3 + §8.6 (MCP-liveness
coupling acknowledgment): the plugin's hook-based skill loader fetches
canonical content from the server's `skills` MCP tool on session start,
caches the response keyed by `registry_hash`, and falls back to the bundled
mirror at ``${PLUGIN_ROOT}/skills/<name>/SKILL.md`` when MCP is unreachable
or the server response is malformed. Bundled fallback teaches *known-stale*
content during outages (cloudflare hiccup, governance-mcp restart, anyio-
asyncio deadlock); staleness is bounded by plugin install age.

Output: a SKILL.md-shaped markdown blob (reconstructed YAML frontmatter
followed by the body) on stdout. Session-start consumes the first 80
lines of this blob, matching the pre-S15-c contract where it sed-read
the bundled mirror directly. Reconstruction is necessary because the
server's `skills` tool returns body-only `content` (frontmatter parsed
into structured fields), and downstream consumers like
``_freshness_warning.py`` parse `last_verified` from the rendered
frontmatter.

Identity-blindness invariant (§4.5): cache files are content-addressed
by skill name + registry_hash. They are NOT keyed by agent_uuid or any
identity-derived value. The cache surface is process-instance-local
and identity-blind by design.

State-legibility per axiom #14: every fallback path emits a
``[FETCH_SKILLS_<state>]`` breadcrumb on stderr so operators can tell
fresh-fetch from cache-replay from bundled-fallback. Three ontologically
distinct success paths are kept distinguishable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from _http_auth import authorization_safe_urlopen, governance_json_headers

DEFAULT_SERVER_URL = "http://localhost:8767"
DEFAULT_CACHE_DIR = "~/.unitares/skills-cache"
# Session-start fires often (every Claude Code resume); fetching every
# fire couples session-start latency to MCP latency. Within this TTL we
# replay the cache without an HTTP call. Well under the typical drift
# horizon (skill edits land weekly); operator sees fresh content within
# 5 minutes.
DEFAULT_CACHE_TTL_SECS = 300
# Skills bundle is < 100KB total; per-skill is well under 50KB. 3s
# matches the SessionStart hook's existing health-check budget.
DEFAULT_FETCH_TIMEOUT_SECS = 3.0


def _crumb(state: str, detail: str = "") -> None:
    msg = f"[FETCH_SKILLS_{state}]"
    if detail:
        msg += f" {detail}"
    print(msg, file=sys.stderr)


def _cache_dir() -> Path:
    raw = os.environ.get("UNITARES_SKILLS_CACHE_DIR", DEFAULT_CACHE_DIR)
    return Path(raw).expanduser()


def _cache_path_for(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)
    return _cache_dir() / f"{safe[:64]}.json"


def _read_cache(name: str) -> Optional[dict[str, Any]]:
    path = _cache_path_for(name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_cache(name: str, payload: dict[str, Any]) -> None:
    path = _cache_path_for(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Cache file is content-addressed and identity-blind, but skills cache
    # may include source_files paths from the operator's checkout — 0600
    # to match the rest of the plugin's local-cache contract.
    #
    # mkstemp gives a unique tmp filename per writer; two concurrent
    # SessionStart fires (e.g. a `--resume` and a second tab opening at
    # the same time) cannot collide on a shared `<name>.tmp` path the
    # way `path.with_suffix(".tmp")` would. Each writer wins or loses
    # the final `os.replace`; never partially overwrites the other's
    # in-flight buffer.
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp"
    )
    try:
        os.write(fd, data)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    try:
        os.replace(tmp_name, str(path))
    except Exception:
        # Cross-device replace, permission flip, or any other os.replace
        # failure leaves the tmp file behind. The mkstemp path is hidden
        # (dot-prefixed) but accumulates on retries — clean up before
        # bubbling so the cache dir doesn't grow leaks.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


_NORMALIZE_MAX_DEPTH = 3


def _normalize_response(
    raw: dict[str, Any], _depth: int = 0
) -> Optional[dict[str, Any]]:
    """Unwrap REST and native-MCP response shapes to the inner payload.

    REST `/v1/tools/call` returns ``{name, result: {success, skills, ...}, success}``;
    native MCP wraps tool results in ``content[0].text`` (per CLAUDE.md
    "MCP response shapes" memory). Some endpoints return data directly.
    Try the most-common shapes in order; first one that yields ``{skills:
    [...]}`` wins.

    Depth-capped: a hostile or malformed server could chain ``content[0].text``
    → JSON → ``content[0].text`` indefinitely and exhaust Python's recursion
    limit. Real responses unwrap in 0 or 1 levels; cap at 3.
    """
    if _depth > _NORMALIZE_MAX_DEPTH:
        _crumb("PARSE_ERROR", f"normalize depth > {_NORMALIZE_MAX_DEPTH}")
        return None
    if "skills" in raw and isinstance(raw["skills"], list):
        return raw
    if isinstance(raw.get("result"), dict) and isinstance(
        raw["result"].get("skills"), list
    ):
        return raw["result"]
    content = raw.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            try:
                inner = json.loads(first["text"])
            except Exception:
                return None
            return (
                _normalize_response(inner, _depth + 1)
                if isinstance(inner, dict)
                else None
            )
    return None


def _fetch_from_server(
    server_url: str, skill_name: str, timeout: float
) -> Optional[dict[str, Any]]:
    payload = json.dumps(
        {"name": "skills", "arguments": {"name": skill_name}}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/v1/tools/call",
        data=payload,
        headers=governance_json_headers(),
    )
    try:
        with authorization_safe_urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _crumb("HTTP_ERROR", f"{type(exc).__name__}: {exc}")
        return None
    except Exception as exc:
        _crumb("HTTP_ERROR", f"{type(exc).__name__}: {exc}")
        return None
    try:
        raw = json.loads(body)
    except Exception:
        _crumb("PARSE_ERROR", "non-JSON response")
        return None
    if not isinstance(raw, dict):
        _crumb("PARSE_ERROR", "response not an object")
        return None
    return _normalize_response(raw)


def _extract_skill(
    response: dict[str, Any], skill_name: str
) -> Optional[dict[str, Any]]:
    skills = response.get("skills")
    if not isinstance(skills, list):
        return None
    for entry in skills:
        if isinstance(entry, dict) and entry.get("name") == skill_name:
            return entry
    return None


def _render_skill_md(entry: dict[str, Any]) -> str:
    """Reconstruct a SKILL.md-shaped markdown blob from the structured fields.

    Pre-S15-c, the hook ``sed``-read the bundled mirror's first 80 lines —
    frontmatter + body. The server's ``skills`` tool returns parsed
    frontmatter as structured fields and ``content`` as body-only. Render
    the same shape back so downstream parsers (``_freshness_warning.py``,
    the SessionStart context formatter) keep working unchanged.

    Pass-through stance: every field on the server response (except
    ``content``/``content_hash``, which aren't frontmatter) is rendered
    into the YAML block. A future v2-ontology field added on the server
    side (e.g. richer ``triggers``) reaches Claude Code without an
    adapter edit. An allow-list-only renderer would silently drop new
    fields — the §6 authority-hierarchy failure mode in the rendering
    direction.

    Field ordering is canonical-first (``name``, ``description``,
    ``last_verified``, ``freshness_days``, ``source_files``) to match
    the existing SKILL.md files — minimizes diff for operators
    debugging fetch-vs-bundled rendering — and any extra fields follow
    in stable insertion order.
    """
    body = entry.get("content", "") or ""
    canonical_order = (
        "name",
        "description",
        "last_verified",
        "freshness_days",
        "source_files",
    )
    skip_fields = {"content", "content_hash"}

    lines = ["---"]
    for key in canonical_order:
        if key in entry and entry[key] is not None:
            lines.extend(_yaml_field(key, entry[key]))
    for key, value in entry.items():
        if key in canonical_order or key in skip_fields:
            continue
        if value is None:
            continue
        lines.extend(_yaml_field(key, value))
    lines.append("---")
    rendered = "\n".join(lines) + "\n"
    if not body.startswith("\n"):
        rendered += "\n"
    rendered += body
    return rendered


def _yaml_field(key: str, value: Any) -> list[str]:
    """Render one frontmatter field as one or more YAML lines.

    Strings with newlines emit as literal block scalars (``|``) — this
    preserves multi-paragraph descriptions losslessly, where folded
    scalars (``>``) would collapse paragraph breaks. List values emit
    one entry per line, each JSON-quoted so source_files paths
    containing ``:``, ``#``, leading ``-``, or other YAML reserved
    chars round-trip cleanly. Scalars are JSON-quoted for the same
    reason — JSON strings are a strict subset of YAML strings.
    """
    if isinstance(value, str):
        if "\n" in value:
            out = [f"{key}: |"]
            for line in value.splitlines():
                out.append(f"  {line}")
            return out
        return [f"{key}: {json.dumps(value)}"]
    if isinstance(value, bool):
        return [f"{key}: {'true' if value else 'false'}"]
    if isinstance(value, (int, float)):
        return [f"{key}: {value}"]
    if isinstance(value, list):
        out = [f"{key}:"]
        for item in value:
            if isinstance(item, str):
                out.append(f"  - {json.dumps(item)}")
            else:
                # Non-string list items (numbers, bools) are unlikely in the
                # current schema but tolerated; fall back to JSON which is
                # YAML-compatible for these primitives.
                out.append(f"  - {json.dumps(item)}")
        return out
    # Dict / nested values — emit as inline JSON. YAML accepts JSON-flow
    # syntax for objects, so nested triggers/etc. remain parseable.
    return [f"{key}: {json.dumps(value)}"]


def _read_bundled(skill_name: str, plugin_root: Path) -> Optional[str]:
    bundled = plugin_root / "skills" / skill_name / "SKILL.md"
    if not bundled.is_file():
        return None
    try:
        return bundled.read_text(encoding="utf-8")
    except Exception:
        return None


def fetch_skill_content(
    *,
    name: str,
    plugin_root: Path,
    server_url: str,
    cache_ttl_secs: int,
    fetch_timeout_secs: float,
    force_refresh: bool = False,
) -> tuple[str, str]:
    """Resolve skill content via cache → server → bundled.

    Returns ``(content, source_label)`` where ``source_label`` is one of
    ``fresh``, ``cache``, ``offline_cache``, ``offline_bundled``, or
    ``not_found``. Caller can render the label or just consume the
    content. ``content`` is empty string on ``not_found``.
    """
    now = int(time.time())
    cached = _read_cache(name)

    # 1. Cache-hit path: TTL-fresh content replays without HTTP.
    # Floor delta at 0 so a future-dated `fetched_at` (NFS clock skew, an
    # operator dragging the system clock back, or a bad write) cannot
    # produce a negative delta that satisfies the unsigned `< ttl` check
    # and pins the cache as eternally fresh.
    if (
        not force_refresh
        and cached
        and isinstance(cached.get("fetched_at"), int)
        and 0 <= (now - cached["fetched_at"]) < cache_ttl_secs
        and isinstance(cached.get("rendered"), str)
    ):
        _crumb("CACHE", f"age={now - cached['fetched_at']}s ttl={cache_ttl_secs}s")
        return cached["rendered"], "cache"

    # 2. Fetch path: try the server.
    response = _fetch_from_server(server_url, name, fetch_timeout_secs)
    if response is not None:
        entry = _extract_skill(response, name)
        if entry is not None:
            rendered = _render_skill_md(entry)
            try:
                _write_cache(
                    name,
                    {
                        "rendered": rendered,
                        "registry_hash": response.get("registry_hash"),
                        "registry_version": response.get("registry_version"),
                        "content_hash": entry.get("content_hash"),
                        "fetched_at": now,
                    },
                )
            except Exception as exc:
                _crumb("CACHE_WRITE_ERROR", f"{type(exc).__name__}: {exc}")
            # Split on the server's `stale` flag: a fresh fetch of a
            # skill the server itself flagged stale is teaching content
            # that has drifted past `freshness_days` since `last_verified`.
            # The agent should see the Fundamentals excerpt regardless,
            # but operators reading the breadcrumb need to distinguish
            # "fresh and the server vouches for it" from "fresh but the
            # server says it's drifted." The `stale` field rides in the
            # rendered frontmatter via the generic pass-through, so the
            # agent's downstream `_freshness_warning.py` can also act on it.
            label = "fresh_stale" if entry.get("stale") else "fresh"
            crumb_state = "FRESH_STALE" if entry.get("stale") else "FRESH"
            _crumb(crumb_state, f"registry={response.get('registry_version')}")
            return rendered, label
        _crumb("NOT_IN_RESPONSE", f"name={name}")

    # 3. Stale-cache fallback: server failed but we have a previous render.
    if cached and isinstance(cached.get("rendered"), str):
        age = now - int(cached.get("fetched_at") or 0)
        _crumb("OFFLINE_CACHE", f"age={age}s")
        return cached["rendered"], "offline_cache"

    # 4. Bundled-mirror fallback: no cache, server unreachable. The mirror
    # is generated by `scripts/dev/sync-plugin-skills.sh` (S15-d), known-
    # stale by definition during the install window — but legible.
    bundled = _read_bundled(name, plugin_root)
    if bundled is not None:
        _crumb("OFFLINE_BUNDLED", f"path={plugin_root / 'skills' / name / 'SKILL.md'}")
        return bundled, "offline_bundled"

    _crumb("NOT_FOUND", f"name={name}")
    return "", "not_found"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Skill name to fetch")
    parser.add_argument(
        "--plugin-root",
        required=True,
        help="Plugin root directory (resolves bundled fallback path)",
    )
    parser.add_argument(
        "--server-url",
        default=os.environ.get("UNITARES_SERVER_URL", DEFAULT_SERVER_URL),
    )
    parser.add_argument(
        "--cache-ttl-secs",
        type=int,
        default=int(
            os.environ.get("UNITARES_SKILLS_CACHE_TTL", DEFAULT_CACHE_TTL_SECS)
        ),
    )
    parser.add_argument(
        "--fetch-timeout-secs",
        type=float,
        default=float(
            os.environ.get(
                "UNITARES_SKILLS_FETCH_TIMEOUT", DEFAULT_FETCH_TIMEOUT_SECS
            )
        ),
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Skip cache TTL check; always attempt server fetch first",
    )
    parser.add_argument(
        "--print-source",
        action="store_true",
        help="Print resolution source label to stderr after content",
    )
    args = parser.parse_args(argv)

    content, source = fetch_skill_content(
        name=args.name,
        plugin_root=Path(args.plugin_root).expanduser().resolve(),
        server_url=args.server_url,
        cache_ttl_secs=args.cache_ttl_secs,
        fetch_timeout_secs=args.fetch_timeout_secs,
        force_refresh=args.force_refresh,
    )
    sys.stdout.write(content)
    if args.print_source:
        print(f"source={source}", file=sys.stderr)
    # Exit non-zero on the truly-missing case so the hook can fall back
    # to its hardcoded reference text rather than emitting an empty
    # excerpt block.
    return 0 if content else 1


if __name__ == "__main__":
    raise SystemExit(main())
