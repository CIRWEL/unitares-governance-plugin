#!/usr/bin/env python3
"""One-shot task-aware KG recall on the first substantive user prompt.

SessionStart recall is task-blind: its query is repo + branch, and from a
non-repo working directory it degenerates to a near-constant string. The
task only exists once the user has typed something, so this helper re-runs
the same bounded, read-only, precision-or-silence retrieval with the
prompt's content terms — at most one network attempt per session slot.

Gating (cheap checks first, no network):
  - UNITARES_HOOK_KG_RECALL off disables this hook and SessionStart recall alike.
  - Slash-command prompts and prompts with fewer than MIN_TASK_TERMS content
    terms are skipped WITHOUT writing the marker, so a later substantive
    prompt still gets its shot.
  - A fresh marker (written after any network attempt, success or failure)
    ends recall for the slot: one attempt total, never one per prompt.

KG text is framed as unverified evidence, never as host instructions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
from pathlib import Path

import kg_recall

MIN_TASK_TERMS = 3
DEFAULT_TTL_S = 3600.0

# _safe_terms() was built for repo/branch text, which carries no prose. A
# prompt is prose, and with operator=OR every stop word matches broadly, so
# they must not reach the query.
STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has
    have how i if in into is it its just me my no not of on or our out over
    please she he should so than that the their them then there these they
    this to under up was we were what when where which who why will with
    would you your
    """.split()
)


def prompt_query(prompt: str) -> str:
    # _safe_terms drops any token with punctuation attached ("coherence?"),
    # which is fine for branch names but discards most of a prose sentence.
    prose = re.sub(r"[^\w\s./-]", " ", prompt)
    terms = [t for t in kg_recall._safe_terms(prose, limit=24) if t not in STOPWORDS]
    if len(terms) < MIN_TASK_TERMS:
        return ""
    return " ".join(terms[:12])


def _safe_slot(session_id: str) -> str:
    # Match session_cache.py _slot_suffix: alnum + [-_], cap at 64 chars.
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in session_id)
    return safe[:64]


def marker_path(cwd: Path, session_id: str) -> Path | None:
    slot = _safe_slot(session_id)
    if not slot:
        return None
    return cwd / ".unitares" / f"prompt-kg-recall-{slot}.json"


def marker_fresh(marker: Path | None, ttl_s: float) -> bool:
    if marker is None:
        return False
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        return False
    return 0 <= age < ttl_s


def write_marker(marker: Path | None, query: str) -> None:
    if marker is None:
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "query": query,
                }
            )
            + "\n"
        )
    except OSError:
        pass


def hook_output(context: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Task-aware one-shot KG recall")
    parser.add_argument("--server-url", default=os.environ.get("UNITARES_SERVER_URL") or kg_recall.DEFAULT_SERVER_URL)
    parser.add_argument("--limit", type=int, default=kg_recall.DEFAULT_LIMIT)
    parser.add_argument("--timeout", type=float, default=kg_recall.DEFAULT_TIMEOUT_S)
    parser.add_argument("--ttl", type=float, default=None)
    args = parser.parse_args(argv)

    mode = (os.environ.get("UNITARES_HOOK_KG_RECALL") or "on").strip().lower()
    if mode in {"0", "off", "false", "no"}:
        return 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    prompt = str(payload.get("prompt") or "")
    if not prompt.strip() or prompt.lstrip().startswith("/"):
        return 0
    # A harness-injected background-task notification rides the same
    # UserPromptSubmit event as a real prompt, but is not user-typed prose --
    # its literal task-id/tool-use-id/path tokens survive _safe_terms()
    # untouched and dominate the query, producing leads with no connection to
    # anything relevant. Observed 2026-08-22: two such notifications in one
    # session both queried on their own id strings. Recognized by the
    # harness's own stable marker, the same way a slash-command is
    # recognized by its leading "/" above -- skip without burning the shot,
    # so the next real prompt in the slot still gets one.
    if prompt.lstrip().startswith("[SYSTEM NOTIFICATION"):
        return 0

    cwd = Path(str(payload.get("cwd") or "") or os.getcwd())
    session_id = str(payload.get("session_id") or "")
    marker = marker_path(cwd, session_id)
    ttl_s = args.ttl
    if ttl_s is None:
        try:
            ttl_s = float(os.environ.get("UNITARES_HOOK_KG_RECALL_TTL_S", DEFAULT_TTL_S))
        except ValueError:
            ttl_s = DEFAULT_TTL_S
    if marker_fresh(marker, ttl_s):
        return 0

    query = prompt_query(prompt)
    if not query:
        # Low-signal prompt: free retry on the next one, no marker.
        return 0

    # One network attempt per slot, success or failure: mark before framing
    # output so a hung/killed search still ends recall for this session.
    write_marker(marker, query)
    try:
        result = kg_recall.search(
            query,
            server_url=args.server_url,
            limit=max(1, min(args.limit, 5)),
            timeout_s=max(0.1, min(args.timeout, 5.0)),
        )
        leads = kg_recall.select_leads(result, limit=max(1, min(args.limit, 5)))
    except (OSError, TimeoutError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        print(f"[PROMPT_KG_RECALL_SKIP] {type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr)
        return 0

    rendered = kg_recall.format_context(query, leads)
    if rendered:
        print(hook_output(rendered))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
