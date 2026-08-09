# Check-In and Host-Evidence Triggers

The adapters emit canonical `process_agent_update` calls at semantic trigger
points. `session-start` is deliberately read-only: it checks server
reachability, fetches the governance fundamentals excerpt, and prompts the
agent to call `start_session(force_new=true)` / `onboard(force_new=true)` itself
only when no identity is cached for this fresh process. If the agent does not
do that before the turn ends, `post-stop` lazily onboards a slot-scoped identity
before emitting `turn_stop`; if that fails, it records an identity-free floor
observation instead. Once a process is bound, later turns continue via
`client_session_id` and `sync_state()` rather than fresh onboarding.

The ordinary Codex profile is intentionally sparse. An agent usually makes zero
or one meaningful `sync_state()` call during a turn. At Stop, the adapter may add
one automatic `substrate_interpretation` for the completed turn. If lazy
onboarding was needed, initialization may also create a synthetic bootstrap
row. Only `sync_state()` with `epistemic_class=agent_report` is an agent-authored
check-in; neither the Stop summary nor bootstrap is agent speech. An identity
with only those automatic rows must not be presented as a continuously running
agent.

| Trigger | Hook script | Frequency | `metadata.event` |
|---|---|---|---|
| Claude turn ends | `post-stop` | per Claude turn | `turn_stop` |
| Codex turn ends | `post-stop` | per Codex turn | `turn_stop` |
| Edit threshold crossed | `post-edit` | every N edits or T seconds | `auto_edit` |

Codex has distinct signal classes. They should not be counted as if they
were the same measurement:

| Signal | Producer | Storage path | What it measures |
|---|---|---|---|
| Explicit `sync_state` | agent, usually zero or one per turn | identity-bound state row (`agent_report` default) | agent-authored proprioceptive state |
| `turn_stop` | Stop hook, once per completed turn | identity-bound state row (`substrate_interpretation`) | automatic host interpretation of the completed turn |
| Bootstrap | lazy onboarding / explicit initial state | synthetic identity-bound state row | initialization only; excluded from real-check-in counts |
| PostToolUse activity | PostToolUse hook | local `~/.unitares/codex-activity/` ledger | completed-tool events received for one Codex slot |
| Bounded activity rollup | detached per-slot worker | identity-bound `audit.events` host observation | a counter/time-window summary of completed-tool receipts |
| Optional host heartbeat | detached per-slot worker | identity-bound `audit.events` host observation | only that the hook's parent PID was observed alive |

The local activity observer is on by default. SessionStart does not launch its
worker. The synchronous PostToolUse hook does no network I/O: after a completed
tool event, it updates the ledger, ensures one detached worker exists for the
slot, and returns. The worker emits an activity rollup after 25
completed tools or 30 active minutes, no more often than every 10 minutes. The
rollup writes only its factual counter window to the legacy runtime-named audit
sink. It never calls `process_agent_update`, increments governance check-in
counts, writes EISV, or proves that the agent is currently sampling.

Hook-parent heartbeats are disabled by default. Codex desktop can use one
long-lived app-server PID as the parent of hooks from multiple chats, so that
PID is not a per-chat or per-agent runtime witness. Operators may opt in with
`UNITARES_CODEX_HOST_HEARTBEATS=on`; those rows are explicitly scoped as
`hook_parent_process_liveness`, never extend an identity session, and never
mark an agent or slot active. A synchronous hook cannot truthfully identify a
model that is currently sampling or a tool that has not yet completed.

The worker is per-slot, PID-start guarded, detached from hook latency, uses a
neutral home working directory, and polls no more than once every five minutes
by default. It stops at SessionEnd, when the hook-parent PID exits, or after one
hour without a completed-tool receipt. That idle bound prevents a shared host
PID from keeping orphaned slot workers alive indefinitely. The synchronous
PostToolUse path uses a cached worker-token verification window instead of
spawning `ps` for every completed tool.

Host observations also carry the Codex model as descriptive context and an
execution-mode pair: `execution_mode` plus `execution_mode_source`. Codex's
documented hook payload does not currently identify scheduled tasks, and model
names such as `gpt-5.4` are not launch-mode proof. The worker therefore emits
`unknown` / `unspecified` unless an execution-specific environment sets
`UNITARES_CODEX_EXECUTION_MODE=interactive|automation|ephemeral`, or a future
hook payload provides an explicit mode. An environment classification is
labeled `explicit_env`; a host-provided classification is labeled
`hook_payload`.

Neither host signal writes to `/v1/substrate/observe`; that endpoint remains the
coverage-gap floor for sessions that never onboarded. Disable the whole bridge
with `UNITARES_CODEX_LIVENESS=off` or only network scheduling with
`UNITARES_CODEX_RUNTIME_OBSERVATIONS=off`. Tune the thresholds through
`UNITARES_CODEX_ROLLUP_TOOLS`, `UNITARES_CODEX_ROLLUP_SECS`,
`UNITARES_CODEX_ROLLUP_COOLDOWN_S`, and
`UNITARES_CODEX_RUNTIME_IDLE_EXIT_S`. Heartbeat opt-in uses
`UNITARES_CODEX_HOST_HEARTBEATS` and `UNITARES_CODEX_HEARTBEAT_SECS`.

This hook path is a diagnostic bridge for the packaged Codex client. If a
custom orchestrator owns Codex App Server or the Codex SDK, prefer its native
turn/item event stream and post the same identity-bound host observations to
`/v1/runtime/observe`. The route name is retained for compatibility; it is not
a claim that the observation represents continuous agent runtime.

Identity-bearing emissions share one helper (`scripts/checkin.py`) that:
- Applies secret-pattern redaction to `response_text` before POST
- Truncates `response_text` to 512 chars
- Logs one status line per attempt to `~/.unitares/checkins.log`
- Returns fire-and-forget: never raises or makes hook failure user-visible

Codex identity tools may arrive at the hook boundary with either
`mcp__unitares-governance__...` (native alias) or
`mcp__unitares_governance__...` (code-mode-normalized alias). Both exact names
are captured into the same slot-scoped cache, so a later Stop hook reuses the
explicit identity instead of lazily minting a second one.

`session-end` is deliberately not a third network trigger. Claude gives
plugin-provided SessionEnd hooks a shared 1.5-second budget, so that hook only
attempts bounded lease cleanup. The preceding `post-stop` call owns final
governance delivery; an abrupt shutdown can leave a lease only until its short
TTL expires.

## Kill switch

`UNITARES_CHECKINS=off` in the environment suppresses every plugin-emitted
check-in, identity-free substrate observation, and identity-bound host
observation. Disable a single trigger by removing its entry from
`hooks/hooks.json` or `hooks/codex-hooks.json`.

## Diagnosing check-in behavior

```bash
tail -f ~/.unitares/checkins.log
```

Expected line format:
```
2026-04-17T02:45:12Z | slot=abc12345 | event=turn_stop | uuid=86ae619f | status=sent | latency_ms=42
```

Statuses: `sent` (accepted by governance), `fail` (POST failed — see `err=...`),
`floor_sent`/`floor_fail` (identity-free Stop floor observation),
`skip_kill_switch` (suppressed by `UNITARES_CHECKINS=off`), and `error`
(client-side exception; caller passed garbage values).

## Protective audit

Run the local identity-contract audit when check-ins look wrong, before shipping
adapter changes, or from a lightweight monitor:

```bash
python3 scripts/audit_identity_contract.py --workspace "$PWD" --log-tail 200
```

The audit checks slot-scoped session caches and the hook diagnostic log without
contacting the governance server. Hard failures include non-empty
`continuity_token` at rest, unreadable session JSON, and session caches with no
`uuid` or `client_session_id`. Warnings include flat legacy `session.json`, weak
`session_resolution_source` values such as `ip_ua_fingerprint`, and log statuses
like `floor_sent`, `floor_fail`, `fail`, or `error`. Use `--log-tail N` or
`--since 24h` for operational monitoring, `--json` for monitor output, and
`--fail-on-warning` when warnings should break CI.

## Strict thread-anchor canary

Before changing the Discord/dispatch thread identity path or advancing a
`STRICT_IDENTITY_REQUIRED` rollout, run the thread-anchor contract canary:

```bash
python3 scripts/dev/strict_thread_anchor_contract.py --json
```

That local mode checks the plugin envelope only: a thread
`UNITARES_CLIENT_SESSION_ID` is forwarded only when the orchestrated marker is
present, and a bare anchor falls back to fresh minting. To probe a live strict
governance server, add `--live`:

```bash
python3 scripts/dev/strict_thread_anchor_contract.py \
  --live \
  --server-url "http://127.0.0.1:8767" \
  --json
```

Live mode writes a unique canary identity. It asserts the full boundary: a bare
`agent:/thread-*` resume miss returns `lineage_declaration_required`, while
`orchestrated=true` first-binds and a second turn resumes the same UUID.

## HTTP authentication

Set `UNITARES_HTTP_API_TOKEN` when the governance REST surface requires a
Bearer token. Check-ins, skill fetches, onboarding, the identity sidecar, and
identity-free floor observations forward the non-empty value in the
`Authorization` header. The bundled Claude MCP transport expands the same value
in its `headers` map. Current Claude hook payloads identify that plugin server as
`plugin_unitares-governance_unitares-governance`. Codex hook payloads use the
bare `unitares-governance` alias for native calls and its normalized
`unitares_governance` spelling for code-mode calls.

The bundled Codex transport is deliberately unauthenticated localhost and has
no `bearer_token_env_var`. For hosted or authenticated governance, disable the
bundled entry and add a separate authenticated server under the exact bare
alias:

```toml
# ~/.codex/config.toml
[plugins."unitares-governance@unitares-governance".mcp_servers.unitares-governance]
enabled = false
```

```bash
codex mcp add unitares-governance \
  --url "${UNITARES_SERVER_URL%/}/mcp/" \
  --bearer-token-env-var UNITARES_HTTP_API_TOKEN
```

For hosted governance, set the client variable to one token accepted by the
server's `UNITARES_MCP_BEARER_TOKENS` allowlist; do not expose the full
server-side allowlist to clients. Leaving the variable unset preserves the
unauthenticated local behavior for REST helpers and Claude; it is also the
expected state for Codex's bundled localhost transport.

## Upgrading from plugin 0.2.0

Claude Code caches installed plugins at `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`. A cache at version `0.2.0` predates the check-in trigger hooks shipped in `0.3.0`. To force a refresh:

```bash
rm -rf ~/.claude/plugins/cache/unitares-governance-plugin/unitares-governance/0.2.0/
```

The cache will repopulate on the next Claude Code launch. Verify the refresh landed by checking `hooks/` contains `post-stop` and `session-end` under the new version path.
