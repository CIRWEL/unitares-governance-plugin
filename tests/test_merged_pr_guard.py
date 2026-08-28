from __future__ import annotations

import json

import pytest

from scripts import merged_pr_guard as guard


def _payload(command: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "claude-slot",
        "tool_name": "Bash",
        "tool_use_id": "toolu_1",
        "tool_input": {"command": command},
    }


def _evaluate(command, *, pr=None, branch="feature/x", env=None, is_repo=True):
    return guard.evaluate(
        _payload(command),
        cwd="/repo",
        env={} if env is None else env,
        lookup=lambda _branch, _cwd: pr or {},
        branch_resolver=lambda _cwd: branch,
        repo_check=lambda _cwd: is_repo,
    )


MERGED = {"number": 1730, "state": "MERGED", "mergedAt": "2026-08-19T02:34:10Z"}
CLOSED = {"number": 91, "state": "CLOSED", "mergedAt": None}
OPEN = {"number": 92, "state": "OPEN", "mergedAt": None}


# --- the case the guard exists for -----------------------------------------

def test_push_to_merged_pull_request_is_refused():
    code, message = _evaluate("git push", pr=MERGED)
    assert code == guard.BLOCK
    assert "#1730" in message
    assert "feature/x" in message
    assert "ORPHANS" in message


def test_push_to_closed_pull_request_is_refused():
    code, _ = _evaluate("git push -u origin feature/x", pr=CLOSED)
    assert code == guard.BLOCK


def test_refusal_names_the_merge_time_when_known():
    _, message = _evaluate("git push", pr=MERGED)
    assert "2026-08-19T02:34:10Z" in message


def test_refusal_warns_against_ancestry_checks():
    """The check a reader would reach for next is the one that lies."""
    _, message = _evaluate("git push", pr=MERGED)
    assert "is-ancestor" in message


# --- everything else allows -------------------------------------------------

def test_open_pull_request_allows_the_push():
    assert _evaluate("git push", pr=OPEN)[0] == guard.ALLOW


def test_branch_with_no_pull_request_allows_the_push():
    assert _evaluate("git push", pr={})[0] == guard.ALLOW


def test_non_push_git_command_is_ignored():
    assert _evaluate("git status -sb", pr=MERGED)[0] == guard.ALLOW


def test_unrelated_command_mentioning_push_is_ignored():
    assert _evaluate("echo 'remember to push later'", pr=MERGED)[0] == guard.ALLOW


@pytest.mark.parametrize("branch", ["master", "main", "trunk", "develop", "HEAD"])
def test_protected_branches_are_never_blocked(branch):
    assert _evaluate("git push", pr=MERGED, branch=branch)[0] == guard.ALLOW


def test_outside_a_git_repository_allows_the_push():
    assert _evaluate("git push", pr=MERGED, is_repo=False)[0] == guard.ALLOW


def test_guard_can_be_disabled_by_environment():
    env = {"UNITARES_MERGED_PR_GUARD": "0"}
    assert _evaluate("git push", pr=MERGED, env=env)[0] == guard.ALLOW


def test_unreachable_forge_fails_open():
    """A guard that blocks delivery when the forge is down costs more than the bug."""
    assert _evaluate("git push", pr={})[0] == guard.ALLOW


# --- command parsing --------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin feature/x",
        "git push -u origin feature/x",
        "cd /repo && git push",
        "git fetch origin; git push --force-with-lease",
        "git -C /somewhere push",
    ],
)
def test_push_shapes_are_detected(command):
    assert guard.is_push(command)


@pytest.mark.parametrize(
    "command",
    ["git status", "git log --oneline", "gh pr list", "pushd /tmp", "git pull"],
)
def test_non_push_shapes_are_not_detected(command):
    assert not guard.is_push(command)


def test_explicit_refspec_wins_over_head():
    """A push naming its branch must not be judged against a different HEAD."""
    code, message = _evaluate(
        "git push -u origin claude/other", pr=MERGED, branch="feature/x"
    )
    assert code == guard.BLOCK
    assert "claude/other" in message


def test_dash_c_selects_the_target_repository():
    assert guard.repo_dir("git -C /elsewhere push", "/repo") == "/elsewhere"


def test_working_directory_is_the_default_repository():
    assert guard.repo_dir("git push", "/repo") == "/repo"


# --- entry point ------------------------------------------------------------

def test_malformed_stdin_allows_the_push():
    assert guard.main(["pre-push"], stdin_text="{not json") == guard.ALLOW


def test_empty_stdin_allows_the_push():
    assert guard.main(["pre-push"], stdin_text="") == guard.ALLOW


def test_payload_without_a_command_allows_the_push():
    body = json.dumps({"tool_name": "Bash", "tool_input": {}})
    assert guard.main(["pre-push"], stdin_text=body) == guard.ALLOW


# --- which repository the branch is read from -------------------------------
# This hook runs BEFORE the shell, so for `cd <worktree> && git push` the
# process cwd is still the session's directory. Reading the branch there
# refused a legitimate worktree push on 2026-08-28, naming a merged branch the
# author had never checked out — the session directory happened to be sitting
# on it. The guard has to resolve the repo the command targets.

def test_cd_before_push_selects_that_directory(tmp_path):
    target = tmp_path / "worktree"
    target.mkdir()
    resolved = guard.repo_dir(f"cd {target} && git commit -m x && git push", "/session")
    assert resolved == str(target)


def test_dash_c_still_wins_over_cd(tmp_path):
    target = tmp_path / "explicit"
    target.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    resolved = guard.repo_dir(f"cd {other} && git -C {target} push", "/session")
    assert resolved == str(target)


def test_cd_to_a_missing_directory_falls_back(tmp_path):
    """Fail open: a path that does not resolve is not evidence about a branch."""
    assert guard.repo_dir("cd /no/such/dir && git push", "/session") == "/session"


def test_cd_after_the_push_is_ignored(tmp_path):
    """Only a directory change that precedes the push can affect it."""
    target = tmp_path / "later"
    target.mkdir()
    assert guard.repo_dir(f"git push && cd {target}", "/session") == "/session"


def test_plain_push_still_uses_the_working_directory():
    assert guard.repo_dir("git push", "/session") == "/session"


def test_tilde_in_a_cd_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "wt").mkdir()
    assert guard.repo_dir("cd ~/wt && git push", "/session") == str(tmp_path / "wt")


def test_refusal_names_the_directory_it_read_from():
    """A false positive is undiagnosable when the message asserts a branch the
    author never touched and gives no hint where it looked."""
    message = guard.refusal_text(1730, "feature/x", "MERGED", None, "/some/worktree")
    assert "/some/worktree" in message
