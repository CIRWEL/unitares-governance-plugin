# Repository Agent Contract

- Use a fresh named branch for every change. Never push directly to `master`.
- Never append work to a branch whose pull request is merged or closed.
- Claim single-writer surfaces with `git surface claim` before editing them.
- Run the relevant lints and `python3 -m pytest` before delivery.
- Commit only intentional files and push the branch with an upstream while any
  owned surface claim remains active.
- Release owned surface claims after push and before `git prepr`; the local
  pre-PR gate rejects active claims.
- Run `git prepr` before opening or updating a draft pull request, then run
  `git closeout --strict` before reporting done.
- Treat dirty worktrees, stashes, local-only branches, and post-merge commits as
  recoverable until they have been inspected. Cleanup must be dry-run first.
