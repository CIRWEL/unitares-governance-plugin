#!/usr/bin/env bash
# Commit staged work, push it on a review branch, and open or update a draft
# pull request. A second invocation can resume delivery after a push or PR API
# failure when the branch is clean and already has an upstream.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

usage() {
    cat <<'EOF'
usage: ./scripts/dev/ship.sh "commit message"

Requirements:
  - exactly the intended files are staged, or this is a clean delivery retry
  - no unstaged or untracked files remain
  - no active single-writer surface claims remain
  - gh, git-surface, git-prepr, and git-closeout are installed

Set SHIP_SCOPE to a task or issue id when duplicate-PR scanning is useful.
Set SHIP_BRANCH_PREFIX to the author prefix (for example, codex/auto or
claude/auto) when shipping staged work from the default branch.

From an up-to-date default branch, the helper creates a fresh branch under the
explicit author prefix. It refuses merged or closed PR heads instead of
replaying their stale history. It never merges or enables auto-merge.
EOF
}

die() {
    echo "ship: $*" >&2
    exit 2
}

# Fleet flag compatibility (2026-08-01): agents carry the unitares ship.sh
# flag vocabulary across repos. This helper's only delivery mode IS a draft
# PR, so the draft flags are accepted as no-ops; modes it cannot honor are
# refused loudly instead of being swallowed into the commit message (the
# bridge repo's pre-sync script titled a PR literally "--draft-pr").
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --draft-pr|--draft)
            shift ;;
        --open-pr|--pr|--auto-merge|--direct|--stage-all|--all|--plan|--dry-run|--classify)
            die "this repo's ship.sh only delivers draft PRs; unsupported mode: $1" ;;
        --help|-h)
            usage
            exit 0 ;;
        --*)
            die "unknown option: $1" ;;
        *)
            break ;;
    esac
done

if [[ "$#" -ne 1 || -z "${1:-}" ]]; then
    usage >&2
    exit 2
fi

MESSAGE="$1"

command -v gh >/dev/null 2>&1 || die "gh is required"
command -v git-surface >/dev/null 2>&1 || die "git-surface is required"
command -v git-prepr >/dev/null 2>&1 || die "git-prepr is required"
command -v git-closeout >/dev/null 2>&1 || die "git-closeout is required"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated"

# git prepr rejects all active claims. Claimed work must be committed and
# pushed manually while protected, then released before this delivery helper.
if ! SURFACE_CLAIMS="$(git surface list --repo --active 2>/dev/null)"; then
    die "cannot inspect active surface claims"
fi
if ! grep -qx 'no matching surface claims' <<<"$SURFACE_CLAIMS"; then
    printf '%s\n' "$SURFACE_CLAIMS" >&2
    die "active surface claims remain; commit/push claimed work, then release before delivery"
fi

if ! git diff --quiet; then
    die "unstaged tracked changes remain; stage or park them intentionally"
fi
UNTRACKED="$(git ls-files --others --exclude-standard)"
if [[ -n "$UNTRACKED" ]]; then
    die "untracked files remain; stage or park them intentionally"
fi

HAS_STAGED="true"
if git diff --cached --quiet; then
    HAS_STAGED="false"
else
    git diff --cached --check
fi

BRANCH="$(git branch --show-current)"
[[ -n "$BRANCH" ]] || die "detached HEAD; create a named branch first"

DEFAULT_REF="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
DEFAULT_BRANCH="${DEFAULT_REF#origin/}"
if [[ -z "$DEFAULT_BRANCH" || "$DEFAULT_BRANCH" == "$DEFAULT_REF" ]]; then
    if git show-ref --verify --quiet refs/heads/main; then
        DEFAULT_BRANCH="main"
    elif git show-ref --verify --quiet refs/heads/master; then
        DEFAULT_BRANCH="master"
    else
        die "cannot determine the default branch"
    fi
fi

git fetch --quiet origin "$DEFAULT_BRANCH" || die "cannot fetch origin/${DEFAULT_BRANCH}"

pr_info() {
    local branch="$1"
    gh pr list \
        --head "$branch" \
        --state all \
        --limit 100 \
        --json state,url,updatedAt \
        --jq '. as $all | (($all | map(select(.state == "OPEN")) | .[0]) // $all[0]) | select(. != null) | [.state, .url] | @tsv'
}

PR_INFO=""
if [[ "$BRANCH" != "$DEFAULT_BRANCH" ]]; then
    if ! PR_INFO="$(pr_info "$BRANCH")"; then
        die "cannot determine pull request state for ${BRANCH}"
    fi
fi
PR_STATE="$(printf '%s' "$PR_INFO" | cut -f1)"
PR_URL="$(printf '%s' "$PR_INFO" | cut -f2)"

if [[ "$PR_STATE" == "MERGED" || "$PR_STATE" == "CLOSED" ]]; then
    die "${BRANCH} belongs to a ${PR_STATE} PR; move the intended diff to a fresh branch from origin/${DEFAULT_BRANCH}"
fi

if [[ "$BRANCH" == "$DEFAULT_BRANCH" ]]; then
    [[ "$HAS_STAGED" == "true" ]] || die "nothing staged on ${DEFAULT_BRANCH}"
    BRANCH_PREFIX="${SHIP_BRANCH_PREFIX%/}"
    [[ -n "$BRANCH_PREFIX" ]] \
        || die "SHIP_BRANCH_PREFIX is required on ${DEFAULT_BRANCH} (for example, codex/auto)"
    git check-ref-format --branch "${BRANCH_PREFIX}/probe" >/dev/null 2>&1 \
        || die "invalid SHIP_BRANCH_PREFIX: ${BRANCH_PREFIX}"
    LOCAL_DEFAULT="$(git rev-parse HEAD)"
    REMOTE_DEFAULT="$(git rev-parse "origin/${DEFAULT_BRANCH}")"
    [[ "$LOCAL_DEFAULT" == "$REMOTE_DEFAULT" ]] \
        || die "${DEFAULT_BRANCH} is not at current origin/${DEFAULT_BRANCH}; update it before shipping"

    SLUG="$(printf '%s' "$MESSAGE" \
        | tr '[:upper:] ' '[:lower:]-' \
        | tr -cd 'a-z0-9-' \
        | sed 's/--*/-/g; s/^-//; s/-$//' \
        | cut -c1-40)"
    [[ -n "$SLUG" ]] || SLUG="change"
    BRANCH="${BRANCH_PREFIX}/$(date -u +%Y%m%d-%H%M%S)-${SLUG}"
    echo "[ship] creating fresh branch ${BRANCH} from current ${DEFAULT_BRANCH}"
    git switch -c "$BRANCH"
    PR_STATE=""
    PR_URL=""
elif [[ "$HAS_STAGED" == "false" ]]; then
    git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1 \
        || die "nothing staged and ${BRANCH} has no upstream to resume"
fi

if [[ "$HAS_STAGED" == "true" ]]; then
    git commit -m "$MESSAGE"
else
    echo "[ship] resuming delivery for clean branch ${BRANCH}"
fi
git push --set-upstream origin "HEAD:${BRANCH}"

if [[ "$PR_STATE" == "OPEN" || -z "${SHIP_SCOPE:-}" ]]; then
    git prepr
else
    git prepr --scope "$SHIP_SCOPE"
fi

if [[ "$PR_STATE" == "OPEN" && -n "$PR_URL" ]]; then
    echo "[ship] updated draft/open PR: $PR_URL"
else
    PR_URL="$(gh pr create \
        --draft \
        --base "$DEFAULT_BRANCH" \
        --head "$BRANCH" \
        --title "$MESSAGE" \
        --body "Delivered by scripts/dev/ship.sh. Verification should be recorded before the PR is marked ready.")"
    echo "[ship] opened draft PR: $PR_URL"
fi

git closeout --strict
