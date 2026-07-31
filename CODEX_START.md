# Start in Codex

Use this path if you are working from Codex or ChatGPT and want the cleanest UNITARES workflow without depending on Claude-only hooks.

## Install In Codex

Add this repository as a marketplace source:

```bash
codex plugin marketplace add cirwel/unitares-governance-plugin
```

Start Codex, open `/plugins`, install and enable `UNITARES Governance`, then
start a new session. The bundle registers the local server at
`http://localhost:8767/mcp/` and loads a Codex-specific synchronous hook file.
Open `/hooks` to review and trust those command hooks; Codex skips untrusted
plugin hooks by design.

Claude uses `.mcp.json`, including its `UNITARES_SERVER_URL` expansion, and the
async handlers in `hooks/hooks.json`. Codex uses the concrete local transport
in `.codex-mcp.json` and synchronous `hooks/codex-hooks.json`. The separate
files keep each host's configuration and execution contracts explicit.

For a non-local server, disable the bundled local transport and register a
separate URL whose server name still contains `unitares` so the lifecycle
matchers apply:

```toml
# ~/.codex/config.toml
[plugins."unitares-governance@unitares-governance".mcp_servers.unitares-governance]
enabled = false
```

```bash
codex mcp add unitares-governance-remote --url "${UNITARES_SERVER_URL%/}/mcp/"
```

## Goal

Connect to a running UNITARES governance server, preserve continuity cleanly,
and check in once per assistant turn as a behavioral baseline. A new user
message is not a new process identity: call `start_session(force_new=true)` once
per fresh process, then continue with `client_session_id`-backed check-ins. Add
milestone check-ins for substantial work; avoid per-tool or per-edit noise.

## Recommended Default

Use `explicit` mode unless you are deliberately dogfooding tighter automation.
When Codex lifecycle hooks are configured and trusted, Codex also has a light
native hook path: `SessionStart` shows the governance nudge, `PostToolUse`
records identity/check-in cache updates, `PreToolUse` injects the cached
`client_session_id` into later governance calls, and `Stop` emits one
turn-level substrate check-in. This does **not** turn every edit or tool call
into a check-in.

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

1. Run `/governance-start`
2. Keep continuity in slot-scoped `.unitares/session-<slot>.json` caches
3. Do real work
4. Run `/checkin` once per assistant turn, and after meaningful milestones
5. Run `/diagnose` when continuity or governance state looks wrong
6. Use `/dialectic` when you need structured review

With Codex lifecycle hooks configured/trusted, step 4 becomes a baseline rather
than the only safety net: the Stop hook emits one turn-stop check-in, while
manual `/checkin` remains the right tool for meaningful milestones and
agent-authored state.

If you are not using commands directly, the equivalent raw tool flow is:

1. First run of a fresh process: `start_session(force_new=true)` (`onboard(...)` is the canonical equivalent)
2. Fresh process continuing finished prior work: `start_session(force_new=true, parent_agent_id=<saved uuid>, spawn_reason="new_session")`
3. Same still-running process: do **not** call `start_session` again; use `sync_state(..., client_session_id=<current session id>)`
4. `sync_state()` once per assistant turn, and after meaningful work (`process_agent_update(...)` is the canonical equivalent)
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

- start or declare lineage with `/governance-start`
- do meaningful work
- check in once per assistant turn as a baseline
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
- `session_resolution_source`: if this falls back to a weak source, rerun `/governance-start`
- `identity_assurance`: strong is better than implicit

Use the local audit when continuity looks suspicious:

```bash
python3 scripts/audit_identity_contract.py --workspace "$PWD" --log-tail 200
```

It checks the neutral cache and check-in log for token-at-rest violations, empty
identity stubs, weak resolution sources, and floor/failure log statuses.

## Commands

- `/governance-start` to create or declare lineage and refresh local continuity state
- `/checkin` for the turn baseline and meaningful milestones
- `/diagnose` for identity, state, and operator diagnostics
- `/dialectic` for structured review

## Claude Note

Claude hooks remain supported in this repo, but they are an adapter convenience, not the canonical UNITARES workflow. The server is the source of truth; the client should stay thin.
