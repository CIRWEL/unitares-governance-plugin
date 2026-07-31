from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SHIP = ROOT / "scripts" / "dev" / "ship.sh"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _seed_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)],
        check=True,
        text=True,
        capture_output=True,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")

    (repo / "README.md").write_text("seed\n")
    script_dir = repo / "scripts" / "dev"
    script_dir.mkdir(parents=True)
    shutil.copy2(SHIP, script_dir / "ship.sh")

    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-qu", "origin", "master")
    return repo, remote


def _fake_gh(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/bin/bash
printf '%s\n' "$*" >>"$FAKE_GH_LOG"

if [ "${1:-}" = "auth" ] && [ "${2:-}" = "status" ]; then
    exit 0
fi

if [ "${1:-}" = "pr" ] && [ "${2:-}" = "list" ]; then
    if [ "${FAKE_GH_LIST_FAIL:-}" = "1" ]; then
        exit 9
    fi
    shift 2
    head=""
    while [ "$#" -gt 0 ]; do
        if [ "$1" = "--head" ] && [ "$#" -ge 2 ]; then
            head="$2"
            shift 2
        else
            shift
        fi
    done
    if [ -n "${FAKE_GH_MERGED_HEAD:-}" ] && [ "$head" = "$FAKE_GH_MERGED_HEAD" ]; then
        printf 'MERGED\thttps://example.test/pull/1\n'
    elif [ -n "${FAKE_GH_OPEN_HEAD:-}" ] && [ "$head" = "$FAKE_GH_OPEN_HEAD" ]; then
        printf 'OPEN\thttps://example.test/pull/2\n'
    fi
    exit 0
fi

if [ "${1:-}" = "pr" ] && [ "${2:-}" = "create" ]; then
    if [ "${FAKE_GH_CREATE_FAIL:-}" = "1" ]; then
        exit 8
    fi
    printf 'https://example.test/pull/new\n'
    exit 0
fi

exit 1
"""
    )
    gh.chmod(0o755)

    git_surface = bin_dir / "git-surface"
    git_surface.write_text(
        """#!/bin/bash
if [ "${FAKE_SURFACE_ACTIVE:-}" = "1" ]; then
    printf 'active\tarea:test\ttest-owner\ttest-branch\n'
else
    printf 'no matching surface claims\n'
fi
"""
    )
    git_surface.chmod(0o755)

    for gate in ("git-prepr", "git-closeout"):
        gate_path = bin_dir / gate
        gate_path.write_text("#!/bin/bash\nexit 0\n")
        gate_path.chmod(0o755)
    return bin_dir, log


def _run_ship(
    repo: Path,
    tmp_path: Path,
    message: str = "fix: deliver safely",
    **extra_env: str,
) -> subprocess.CompletedProcess[str]:
    bin_dir, log = _fake_gh(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
        "FAKE_GH_LOG": str(log),
        "SHIP_BRANCH_PREFIX": "codex/auto",
        **extra_env,
    }
    result = subprocess.run(
        [str(repo / "scripts" / "dev" / "ship.sh"), message],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    result.gh_log = log.read_text() if log.exists() else ""  # type: ignore[attr-defined]
    return result


def _stage_change(repo: Path, name: str = "change.txt") -> None:
    (repo / name).write_text("change\n")
    _git(repo, "add", name)


def test_ship_rotates_default_branch_and_opens_draft_pr(tmp_path):
    repo, _remote = _seed_repo(tmp_path)
    _stage_change(repo)

    result = _run_ship(repo, tmp_path)

    assert result.returncode == 0, result.stderr
    branch = _git(repo, "branch", "--show-current")
    assert branch.startswith("codex/auto/")
    assert _git(repo, "rev-parse", "--abbrev-ref", "@{u}") == f"origin/{branch}"
    assert _git(repo, "rev-parse", "HEAD^") == _git(repo, "rev-parse", "origin/master")
    assert _git(repo, "status", "--porcelain") == ""
    assert "pr create" in result.gh_log  # type: ignore[attr-defined]
    assert "--draft" in result.gh_log  # type: ignore[attr-defined]
    assert "pr merge" not in result.gh_log  # type: ignore[attr-defined]


def test_ship_refuses_branch_with_merged_pr_before_committing(tmp_path):
    repo, _remote = _seed_repo(tmp_path)
    old_branch = "codex/already-merged"
    _git(repo, "switch", "-qc", old_branch)
    _git(repo, "push", "-qu", "origin", old_branch)
    old_remote_head = _git(repo, "rev-parse", f"origin/{old_branch}")
    _stage_change(repo)

    result = _run_ship(repo, tmp_path, FAKE_GH_MERGED_HEAD=old_branch)

    assert result.returncode == 2
    assert "belongs to a MERGED PR" in result.stderr
    assert _git(repo, "branch", "--show-current") == old_branch
    assert _git(repo, "status", "--porcelain") == "A  change.txt"
    assert _git(repo, "rev-parse", f"origin/{old_branch}") == old_remote_head


def test_ship_updates_open_pr_branch_without_creating_another(tmp_path):
    repo, _remote = _seed_repo(tmp_path)
    branch = "codex/open-work"
    _git(repo, "switch", "-qc", branch)
    _git(repo, "push", "-qu", "origin", branch)
    _stage_change(repo)

    result = _run_ship(repo, tmp_path, FAKE_GH_OPEN_HEAD=branch)

    assert result.returncode == 0, result.stderr
    assert _git(repo, "branch", "--show-current") == branch
    assert "updated draft/open PR: https://example.test/pull/2" in result.stdout
    assert "pr create" not in result.gh_log  # type: ignore[attr-defined]


def test_ship_refuses_mixed_dirty_state_before_commit(tmp_path):
    repo, _remote = _seed_repo(tmp_path)
    _stage_change(repo)
    (repo / "README.md").write_text("unrelated\n")
    old_head = _git(repo, "rev-parse", "HEAD")

    result = _run_ship(repo, tmp_path)

    assert result.returncode == 2
    assert "unstaged tracked changes remain" in result.stderr
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert _git(repo, "branch", "--show-current") == "master"


def test_ship_refuses_active_surface_claim_before_commit(tmp_path):
    repo, _remote = _seed_repo(tmp_path)
    _stage_change(repo)
    old_head = _git(repo, "rev-parse", "HEAD")

    result = _run_ship(repo, tmp_path, FAKE_SURFACE_ACTIVE="1")

    assert result.returncode == 2
    assert "active surface claims remain" in result.stderr
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert _git(repo, "branch", "--show-current") == "master"


def test_ship_fails_closed_when_pr_lookup_fails(tmp_path):
    repo, _remote = _seed_repo(tmp_path)
    branch = "codex/lookup-failure"
    _git(repo, "switch", "-qc", branch)
    _git(repo, "push", "-qu", "origin", branch)
    _stage_change(repo)
    old_head = _git(repo, "rev-parse", "HEAD")

    result = _run_ship(repo, tmp_path, FAKE_GH_LIST_FAIL="1")

    assert result.returncode == 2
    assert "cannot determine pull request state" in result.stderr
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert _git(repo, "status", "--porcelain") == "A  change.txt"


def test_ship_resumes_after_pr_creation_failure(tmp_path):
    repo, _remote = _seed_repo(tmp_path)
    _stage_change(repo)

    first = _run_ship(repo, tmp_path / "first", FAKE_GH_CREATE_FAIL="1")

    assert first.returncode != 0
    branch = _git(repo, "branch", "--show-current")
    assert branch.startswith("codex/auto/")
    assert _git(repo, "status", "--porcelain") == ""
    pushed_head = _git(repo, "rev-parse", "HEAD")

    second = _run_ship(repo, tmp_path / "second")

    assert second.returncode == 0, second.stderr
    assert "resuming delivery for clean branch" in second.stdout
    assert "pr create" in second.gh_log  # type: ignore[attr-defined]
    assert _git(repo, "rev-parse", "HEAD") == pushed_head


def test_ship_requires_explicit_author_prefix_on_default_branch(tmp_path):
    repo, _remote = _seed_repo(tmp_path)
    _stage_change(repo)

    result = _run_ship(repo, tmp_path, SHIP_BRANCH_PREFIX="")

    assert result.returncode == 2
    assert "SHIP_BRANCH_PREFIX is required" in result.stderr
    assert _git(repo, "branch", "--show-current") == "master"
