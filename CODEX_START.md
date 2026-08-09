# Start in Codex

Use this path if you are working from Codex or ChatGPT and want the cleanest UNITARES workflow without depending on Claude-only hooks.

## Install In Codex

Codex CLI 0.146.0 is the minimum tested version.

Add this repository as a marketplace source:

```bash
codex plugin marketplace add cirwel/unitares-governance-plugin
```

Start Codex, open `/plugins`, install and enable `UNITARES Governance`, then
start a new session. The bundle registers an unauthenticated local server at
`http://localhost:8767/mcp/` and loads a Codex-specific synchronous hook file.
It does not read `UNITARES_HTTP_API_TOKEN` for this bundled MCP transport. Open
`/hooks` to review and trust those command hooks; Codex skips untrusted plugin
hooks by design.

On Windows, install Git for Windows with Git Bash and Python 3.12+ exposed to
Git Bash as `python3`. The hook wrapper fails visibly when Bash is unavailable;
with `UNITARES_FILE_LEASES_REQUIRED=1`, its PreToolUse path denies edits instead
of silently bypassing required leases.

Claude uses `.mcp.json`, including its `UNITARES_SERVER_URL` expansion and an
optional `Authorization` header sourced from `UNITARES_HTTP_API_TOKEN`, plus the
async handlers in `hooks/hooks.json`. Current Claude hook payloads scope its
bundled server as `plugin_unitares-governance_unitares-governance`, so a tool
arrives as
`mcp__plugin_unitares-governance_unitares-governance__<tool>`. Codex uses the
concrete unauthenticated local transport in `.codex-mcp.json`, plus synchronous
`hooks/codex-hooks.json`, and reports the bare
`mcp__unitares-governance__<tool>` form. The separate files keep each host's
configuration, naming, and execution contracts explicit.

The edit contract is also host-specific. Codex reports the canonical
`apply_patch` tool with a patch envelope in `tool_input.command`; it does not
send Claude's scalar `tool_input.file_path`. The adapter leases every path in
that envelope under the invocation's `tool_use_id`, records one local
milestone, and exits. It does not perform a synchronous per-edit governance
check-in. Codex Stop summaries read `last_assistant_message`; tool counts are
left unavailable rather than reported as zero. The synchronous Stop path caps
lazy onboarding at 5 seconds and its check-in at 15 seconds, leaving process
and cache overhead inside the 30-second hook deadline.

For a hosted, authenticated, or otherwise non-local server, disable the bundled
local transport and register the separate URL under the exact
`unitares-governance` alias. Exporting a token alone does not add authentication
to the bundled localhost entry. Runtime hook policy uses a case-normalized
alias allowlist; substring lookalikes and Claude's plugin-scoped alias are
deliberately ignored at the Codex boundary and never implicitly approved:

```toml
# ~/.codex/config.toml
[plugins."unitares-governance@unitares-governance".mcp_servers.unitares-governance]
enabled = false
```

```bash
export UNITARES_SERVER_URL=https://gov.example.org
export UNITARES_HTTP_API_TOKEN='replace-with-client-token'
codex mcp add unitares-governance \
  --url "${UNITARES_SERVER_URL%/}/mcp/" \
  --bearer-token-env-var UNITARES_HTTP_API_TOKEN
```

## Goal

Connect to a running UNITARES governance server, preserve continuity cleanly,
and check in when there is meaningful agent state to report — usually zero or
one `sync_state()` call per assistant turn. A new user message is not a new
process identity: call `start_session(force_new=true)` once per fresh process,
then continue with `client_session_id`-backed check-ins. The automatic Stop
summary is a non-agent-authored substrate interpretation, not a replacement
agent report. Avoid per-tool or per-edit check-in noise.

## Recommended Default

Use `explicit` mode unless you are deliberately dogfooding tighter automation.
When Codex lifecycle hooks are configured and trusted, Codex also has a light
native hook path: `SessionStart` shows the governance nudge, `PostToolUse`
records completed-tool receipts and identity/check-in cache updates for matching
governance calls, `PreToolUse` injects the cached `client_session_id` into later
governance calls, and `Stop` emits one turn-level substrate interpretation. This
does **not** turn every edit or tool call into a check-in or prove continuous
agent runtime.

### Modes

- `explicit`: manual onboarding/check-in/diagnosis; best default
- `dogfood-light`: explicit check-ins plus stronger milestone reminders
- `dogfood-heavy`: research mode for tighter automation and deterministic outcome capture

This plugin still optimizes for `explicit` agent-authored check-ins. If you
want adapter-like onboarding/check-in behavior from a client that cannot load
the Codex lifecycle hooks, run the sidecar and send governance REST tool calls
through it.

```bash
python3 scripts/identity_sidecar.py --server-url http://localhost:8767 --workspace "$PWD" --slot codex-local
```

Then read `http://127.0.0.1:8768/client-config?slot=codex-local` for the
slot-scoped MCP URL/header snippet to paste into clients that can use a URL MCP
server.

The sidecar wraps REST `/v1/tools/call` and minimal JSON-RPC MCP `/mcp/`
requests, lazily onboards when the slot has no cached `client_session_id`,
injects that session id into attribution-relevant governance calls, and provides
`/turn/checkin`, `/turn/stop`, and `/audit`. It is not a full streamable-MCP/SSE
transport proxy yet.

### Local and Non-Frontier Models

For local model runners, small hosted models, or any client that does not have a
trusted plugin/hook lifecycle, prefer the sidecar over raw direct MCP. Raw
MCP/REST is still the canonical server contract, but the runner should keep
UNITARES identity state out of the model prompt.

Recommended shape:

```text
local model
  -> runner/tool router
    -> sidecar REST or sidecar JSON-RPC MCP
      -> UNITARES server MCP/REST
```

Expose task-level operations to the model, such as `governance_checkin`,
`governance_state`, and `governance_review`. Let the sidecar or runner retain
the slot cache and inject process identity. Use raw direct MCP only when the
runner already has equivalent lifecycle handling or when an operator is
debugging the underlying protocol.

## Recommended Flow

1. Follow the bundled `unitares-governance:governance-lifecycle` skill and call `start_session(force_new=true)`
2. Keep continuity in slot-scoped `.unitares/session-<slot>.json` caches
3. Do real work
4. Call `sync_state(...)` when there is meaningful agent state to report (usually at most once per assistant turn)
5. Call `identity()`, `check_working_state()`, and `health_check()` when continuity or governance state looks wrong
6. Follow the bundled `unitares-governance:dialectic-reasoning` skill and call `dialectic(...)` when you need structured review

With Codex lifecycle hooks configured/trusted, the Stop hook emits one automatic
turn-stop interpretation. It is not agent-authored and does not make step 4
mandatory: manual `sync_state(...)` remains the right tool only for meaningful
agent-authored state. Lazy onboarding may also add a synthetic bootstrap row,
which is initialization rather than a real check-in. The repository's
`commands/` directory is a Claude Code surface; Codex plugins do not install
those custom slash commands.

The raw tool flow is:

1. First run of a fresh process: `start_session(force_new=true)` (`onboard(...)` is the canonical equivalent)
2. Fresh process continuing finished prior work: `start_session(force_new=true, parent_agent_id=<saved uuid>, spawn_reason="new_session")`
3. Same still-running process: do **not** call `start_session` again; use `sync_state(..., client_session_id=<current session id>)`
4. `sync_state()` when meaningful, usually at most once per assistant turn (`process_agent_update(...)` is the canonical equivalent)
5. Same live owner / proof-owned rebind only: `identity(agent_uuid=..., continuity_token=..., resume=true)`
6. `check_working_state()` for read-only state checks (`get_governance_metrics(...)` is the canonical equivalent)
7. `identity()` if continuity looks wrong
8. `health_check()` if the system itself may be part of the problem

On servers with the agent-experience envelope enabled, friendly aliases lift
`next_action`, `state_summary`, `risk_summary`, `memory_suggestions`, and
`recovery_hint` when present, plus the full canonical payload under
`raw_governance`. Treat `memory_suggestions` as optional retrieval prompts and
`recovery_hint` as the first recovery route when a response reports degraded or
paused state. Older compatibility surfaces may return the canonical payload
directly; in that case read the same fields where they already appear. If a
server does not know these aliases yet, use the canonical tool names shown in
parentheses.
When `search_shared_memory()` returns `low_confidence` or a `confidence_note`,
do not treat the surfaced rows as matches; open details or rephrase with better
terms before relying on them.

## Local Continuity Cache

Codex should treat continuity as local workspace state, not Claude-only adapter state.

Preferred cache path:

- `.unitares/session-<slot>.json`

Flat `.unitares/session.json` is a legacy/shared artifact. Use `scripts/session_cache.py list --workspace "$PWD"` to discover recent slots, then read a specific cache with `scripts/session_cache.py get session --slot=<slot>`.

Shared helper:

- `scripts/session_cache.py`

Treat this as local runtime state. It should not be used as a source of truth over the server, but it is the first place to look for:

- `client_session_id`
- `uuid`
- `agent_id`
- `display_name`
- `session_resolution_source`

Do not persist `continuity_token` in this cache. v2 slot caches are lineage and
transport-continuity hints only; a token belongs only to the live response that
returned it and to rare same-live-process proof-owned rebinds.

## Minimal Session Pattern

Typical session:

- start or declare lineage with `start_session(...)`
- do meaningful work
- make an agent-authored check-in only when meaningful
- let Stop's separately labeled substrate interpretation describe the turn boundary
- add a check-in after a milestone, completed step, or decision point
- diagnose only when needed

Do not treat every file edit, tool call, or user message as a governance start.
Turn-level baseline check-ins are useful; raw file churn and repeated fresh
identity mints are not.

## What to Watch

- `uuid`: identity anchor, not ownership proof
- `continuity_token`: short-lived ownership proof for same-owner rebinding, not indefinite cross-process resume
- `client_session_id`: in-session transport continuity metadata
- `parent_agent_id`: lineage declaration for a fresh process continuing prior work
- `session_resolution_source`: if this falls back to a weak source, inspect `identity()` and start a fresh session only when the process contract calls for one
- `identity_assurance`: strong is better than implicit

Use the local audit when continuity looks suspicious:

```bash
python3 scripts/audit_identity_contract.py --workspace "$PWD" --log-tail 200
```

It checks the neutral cache and check-in log for token-at-rest violations, empty
identity stubs, weak resolution sources, and floor/failure log statuses.

## Codex Skills And Tools

- `unitares-governance:governance-lifecycle` skill plus `start_session(...)` for onboarding and declared lineage
- `sync_state(...)` for the turn baseline and meaningful milestones
- `identity()`, `check_working_state()`, and `health_check()` for diagnosis
- `unitares-governance:dialectic-reasoning` skill plus `dialectic(...)` for structured review

## Claude Note

Claude hooks remain supported in this repo, but they are an adapter convenience, not the canonical UNITARES workflow. The server is the source of truth; the client should stay thin.
