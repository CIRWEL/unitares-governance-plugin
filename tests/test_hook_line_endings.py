from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = Path("hooks/run-hook.cmd")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )


def _tracked_extensionless_hooks() -> list[Path]:
    tracked = _git(ROOT, "ls-files", "hooks").stdout.splitlines()
    return sorted(Path(path) for path in tracked if not Path(path).suffix)


def _attributes(repo: Path, paths: list[Path]) -> dict[tuple[str, str], str]:
    result = _git(
        repo,
        "check-attr",
        "-z",
        "text",
        "eol",
        "--",
        *(path.as_posix() for path in paths),
    )
    fields = result.stdout.split("\0")
    assert fields.pop() == ""
    return {
        (fields[index], fields[index + 1]): fields[index + 2]
        for index in range(0, len(fields), 3)
    }


def test_autocrlf_checkout_preserves_polyglot_wrapper_lf(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "true")

    shutil.copyfile(ROOT / ".gitattributes", repo / ".gitattributes")
    (repo / "hooks").mkdir()
    shutil.copyfile(ROOT / WRAPPER, repo / WRAPPER)

    extensionless_hooks = _tracked_extensionless_hooks()
    hook_paths = [WRAPPER, *extensionless_hooks]
    attrs = _attributes(repo, hook_paths)
    assert extensionless_hooks
    for path in hook_paths:
        assert attrs[(path.as_posix(), "text")] == "set"
        assert attrs[(path.as_posix(), "eol")] == "lf"

    _git(repo, "add", ".gitattributes", WRAPPER.as_posix())
    (repo / WRAPPER).unlink()
    _git(repo, "checkout", "--", WRAPPER.as_posix())

    wrapper_bytes = (repo / WRAPPER).read_bytes()
    assert b"\n" in wrapper_bytes
    assert b"\r\n" not in wrapper_bytes
    subprocess.run(
        ["bash", "-n", str(repo / WRAPPER)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
