"""Refuse a push to a branch whose pull request has already merged or closed.

The failure this prevents is silent and total. Work continues on a branch after
its pull request squash-merged; the commits land where nothing will merge them
again, and the next "prune merged branches" sweep deletes them.

Nothing in the normal loop catches it:

* ``git push`` succeeds. Git has no idea a pull request exists.
* ``gh pr checks`` is green. It reports the merged state.
* ``gh pr view --state`` says MERGED, which reads as success.
* ``git merge-base --is-ancestor`` returns true against a squash merge even
  when the change is absent, so ancestry checks pass.

Only a content diff against the base branch finds it, and only if someone
thinks to look. This moves detection to the moment the orphan is created.

Fail-open by design. An unauthenticated, offline, or slow ``gh`` allows the
push: a guard that blocks delivery when the forge is unreachable costs more
than the bug it prevents. A quiet pass is therefore NOT proof the branch is
open. This narrows the window; it does not close it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

ALLOW = 0
BLOCK = 2

#: Branches a push to is never an orphan risk.
PROTECTED_BRANCHES = frozenset({"master", "main", "trunk", "develop", "HEAD"})

_PUSH_RE = re.compile(r"(^|[;&|\s])git\s+([^;&|]*\s)?push(\s|$)")
_DASH_C_RE = re.compile(r"git\s+-C\s+([^\s;&|]+)")
# `cd <dir> && git push` is the other way an agent names the repo. Without
# this the guard read the SESSION cwd instead: PreToolUse hooks run before
# the shell, so the `cd` has not happened when the guard looks. That misread
# a worktree push as a push to whatever branch the session directory
# happened to be on, and refused it naming an unrelated branch.
_CD_RE = re.compile(r"(?:^|[;&|]|&&)\s*cd\s+([^\s;&|]+)")
_REFSPEC_RE = re.compile(
    r"git[^;&|]*push\s+[^;&|]*origin\s+(?:-u\s+|--set-upstream\s+)?"
    r"([A-Za-z0-9._/-]+)"
)

_TRUTHY = {"1", "true", "yes", "on"}


def guard_enabled(env: Mapping[str, str]) -> bool:
    """The guard is on unless explicitly disabled.

    The toggle must live in the hook's own environment. A command prefix such
    as ``VAR=0 git push`` never reaches a PreToolUse hook, because the hook
    runs before the shell that would have applied it.
    """
    raw = env.get("UNITARES_MERGED_PR_GUARD", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def extract_command(payload: Mapping[str, Any] | None) -> str:
    """The shell command a host is about to run, or an empty string."""
    if not isinstance(payload, Mapping):
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return ""
    command = tool_input.get("command")
    return command if isinstance(command, str) else ""


def is_push(command: str) -> bool:
    """True when the command runs ``git push`` in any position."""
    return bool(_PUSH_RE.search(command or ""))


def repo_dir(command: str, cwd: str) -> str:
    """Resolve the repo the push targets, not merely where the shell started.

    Precedence: an explicit ``git -C <dir>``, then the last ``cd <dir>`` that
    precedes the push in the chain, then the working directory. The middle
    case matters because this hook runs BEFORE the shell, so for
    ``cd <worktree> && git push`` the process cwd is still the session's
    directory — and reading the branch there refuses a legitimate push while
    naming a branch the author never touched.

    Fails open by design: a directory that does not resolve, or is not a git
    repository, falls back rather than guessing. A guard that blocks on its
    own confusion is worse than one that misses.
    """
    command = command or ""
    match = _DASH_C_RE.search(command)
    if match:
        return _expand(match.group(1)) or cwd

    push = _PUSH_RE.search(command)
    limit = push.start() if push else len(command)
    candidates = [m for m in _CD_RE.finditer(command) if m.start() < limit]
    if candidates:
        resolved = _expand(candidates[-1].group(1))
        if resolved and os.path.isdir(resolved):
            return resolved
    return cwd


def _expand(raw: str) -> str:
    """Expand ~ and quoting on a path lifted out of a command string."""
    if not raw:
        return ""
    return os.path.expanduser(raw.strip().strip("\"'"))


def branch_from_command(command: str) -> str:
    """The branch named in an explicit refspec, or an empty string."""
    match = _REFSPEC_RE.search(command or "")
    return match.group(1) if match else ""


def _run(args: Sequence[str], cwd: str, timeout: float) -> str:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def current_branch(cwd: str, timeout: float = 5.0) -> str:
    try:
        return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd, timeout)
    except (OSError, subprocess.SubprocessError):
        return ""


def is_git_repo(cwd: str, timeout: float = 5.0) -> bool:
    try:
        return bool(_run(["git", "rev-parse", "--git-dir"], cwd, timeout))
    except (OSError, subprocess.SubprocessError):
        return False


def lookup_pull_request(branch: str, cwd: str, timeout: float = 8.0) -> dict[str, Any]:
    """Ask the forge about the newest pull request for this branch.

    Returns an empty mapping on every failure path, which the caller reads as
    "allow". The timeout is short on purpose: this runs on every push and must
    not become the slow part of a delivery.
    """
    if shutil.which("gh") is None:
        return {}
    try:
        raw = _run(
            [
                "gh", "pr", "list",
                "--head", branch,
                "--state", "all",
                "--limit", "1",
                "--json", "number,state,mergedAt,title",
            ],
            cwd,
            timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], Mapping):
        return dict(parsed[0])
    return {}


def refusal_text(
    number: Any,
    branch: str,
    state: str,
    merged_at: Any,
    target: str = "",
) -> str:
    verb = "closed" if str(state).upper() == "CLOSED" else "merged"
    when = f" (at {merged_at})" if merged_at else ""
    # Name the directory the branch was read FROM. Without it a false positive
    # is nearly undiagnosable: the message asserts a branch the author never
    # checked out, with no clue that the guard looked somewhere else.
    where = f"\nBranch read from: {target}\n" if target else ""
    return (
        f"BLOCKED: pull request #{number} for branch `{branch}` is already "
        f"{verb}{when}.\n{where}\n"
        "Pushing here ORPHANS the commit. Nothing will merge this branch again, "
        "and the next merged-branch sweep will delete it.\n\n"
        "Move the work onto a fresh branch instead:\n\n"
        "  git -C <repo> fetch origin <base>\n"
        "  git -C <repo> worktree add <path> -b <new-branch> origin/<base>\n"
        "  cd <path> && git cherry-pick <sha>\n"
        "  git diff --stat origin/<base>   # confirm ONLY your change is there\n\n"
        "Verify by CONTENT, never by ancestry: `git merge-base --is-ancestor` "
        "returns true against a squash merge even when your change is absent. "
        "Use `git diff origin/<base> <sha>`, or grep for the change itself.\n\n"
        "To push to a closed pull request's branch deliberately, set "
        "UNITARES_MERGED_PR_GUARD=0 in the hook environment. A command prefix "
        "will not work: PreToolUse hooks run before the shell."
    )


def evaluate(
    payload: Mapping[str, Any] | None,
    *,
    cwd: str,
    env: Mapping[str, str],
    lookup: Callable[[str, str], Mapping[str, Any]] | None = None,
    branch_resolver: Callable[[str], str] | None = None,
    repo_check: Callable[[str], bool] | None = None,
) -> tuple[int, str]:
    """Return ``(exit_code, message)``. Every uncertain path allows the push."""
    if not guard_enabled(env):
        return ALLOW, ""

    command = extract_command(payload)
    if not is_push(command):
        return ALLOW, ""

    target = repo_dir(command, cwd)
    check = repo_check or is_git_repo
    if not check(target):
        return ALLOW, ""

    resolve = branch_resolver or current_branch
    branch = branch_from_command(command) or resolve(target)
    if not branch or branch in PROTECTED_BRANCHES or branch.startswith("-"):
        return ALLOW, ""

    query = lookup or lookup_pull_request
    pull_request = query(branch, target)
    state = str(pull_request.get("state") or "").upper()
    if state not in {"MERGED", "CLOSED"}:
        return ALLOW, ""

    return BLOCK, refusal_text(
        pull_request.get("number"),
        branch,
        state,
        pull_request.get("mergedAt"),
        target,
    )


def main(argv: Iterable[str] | None = None, stdin_text: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog="merged_pr_guard")
    parser.add_argument("hook", nargs="?", default="pre-push")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--host", default="claude")
    args = parser.parse_args(list(argv) if argv is not None else None)

    raw = stdin_text if stdin_text is not None else sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return ALLOW

    code, message = evaluate(
        payload,
        cwd=args.workspace or os.getcwd(),
        env=os.environ,
    )
    if message:
        sys.stderr.write(message + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
