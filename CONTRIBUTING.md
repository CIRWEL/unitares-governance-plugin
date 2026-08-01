# Contributing

This repository ships hooks and guidance into live agent sessions. Every
change, including docs, skills, commands, and tests, goes through a branch and
pull request. Direct pushes to `master` are not part of the normal workflow.

## Preferred Workflow

Use this delivery flow:

1. Fetch the current default branch and create a fresh short-lived branch.
2. Claim any single-writer surface before editing it.
3. Keep the branch focused on one topic and run the relevant tests.
4. Commit only intentional files and push with an upstream.
5. Release the owned surface claim after the commit is visible upstream.
6. Run `git prepr`, open or update a draft PR, and run `git closeout --strict`.

The local `git prepr` gate rejects all active claims, so the implementable
order is push, release, pre-PR gate, PR. This differs from keeping a claim
through PR creation, but it avoids an impossible gate while keeping the edit
protected until its commit is durable upstream.

Never reuse a branch after its PR is merged or closed. A follow-up starts from
current `master` on a new branch even when it is only one line. This prevents a
valid commit from being pushed to a head that no open PR can deliver.

For unclaimed staged changes in an otherwise clean worktree, the repository
helper runs the commit, push, pre-PR, and draft-PR steps:

```bash
SHIP_BRANCH_PREFIX=codex/auto ./scripts/dev/ship.sh "fix: describe the change"
```

For claimed changes, commit and push manually while the claim remains active,
then release it and run the pre-PR/PR steps. The helper creates a branch only
from an up-to-date `master`; `SHIP_BRANCH_PREFIX` makes author attribution
explicit. It refuses merged or closed PR heads. Re-running it on a clean
upstream branch resumes a failed delivery. It never merges or enables
auto-merge. Set `SHIP_SCOPE` to a task or issue id when the pre-PR duplicate
scan is useful; do not use a broad commit-message phrase.

Recommended branch prefixes:

- `codex/` for Codex-authored changes
- `claude/` for Claude-authored changes
- `ops/` for operational changes
- `docs/` for documentation-only changes

Examples:

- `codex/codex-plugin-manifest`
- `docs/skill-refresh`
- `ops/session-start-hardening`

## What Belongs Together

Good PR scope:

- one plugin packaging change
- one skill refresh batch
- one hook behavior change
- one README or docs cleanup pass

Bad PR scope:

- hooks + skills + Discord bridge + repo restructuring all mixed together

If a change touches both behavior and docs, keep them together only if the docs explain that exact behavior change.

## Practical Review Standard

Before opening or merging a PR, check:

- the README still matches the runtime story
- commands match current UNITARES semantics
- skills do not point at stale server paths
- hooks do not create noisy or misleading governance behavior
- Claude and Codex manifests each point at host-compatible MCP and hook files
- host-dependent hook commands select a host explicitly, with payload fixtures
  covering that host's edit and Stop wire formats
- synchronous Codex PostToolUse hooks remain bounded and do not make edit-level
  governance check-ins

Run the full local suite before delivery:

```bash
./scripts/dev/lint-command-cache-contract.sh
./scripts/dev/lint-doc-drift.sh
python3 scripts/dev/lint-doc-command-examples.py
./scripts/check-skill-freshness.sh
python3 -m pytest
```

## Current Principle

Prefer:

- meaningful check-ins over per-edit check-ins
- live runtime diagnostics over hardcoded thresholds
- adapter-specific behavior in adapter files
- shared guidance in shared skills and commands

## Worktree And Branch Cleanup

Cleanup is a reconciliation task, not a branch-age heuristic. This repository
uses squash merges, so ancestry-only commands such as `git branch --merged`
can misclassify a delivered branch as unmerged.

1. Inventory every worktree, local branch, remote-tracking ref, stash, and PR
   state before pruning any evidence.
2. Record locked worktrees and repo-rooted processes; do not remove a worktree
   that is locked, active, or dirty.
3. Treat dirty worktrees, branches without upstreams, and commits created after
   a PR merged as recoverable work until reviewed.
4. Fetch updates, then inspect the proposed `--prune` result before pruning
   remote-tracking refs.
5. Remove only clean, inactive worktrees whose branch has no post-merge commits.
6. Run `git branch-hygiene --include-gone` as a dry run before deleting merged
   local branches; use GitHub PR state for squash-merged heads.
7. Run `git worktree prune --dry-run --verbose` before pruning stale metadata.

Do not force-delete a branch merely because its upstream is gone. Preserve the
commit on a fresh branch and PR, or document why the work is intentionally
retired first.
