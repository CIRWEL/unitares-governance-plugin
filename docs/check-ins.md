# Check-In and Liveness Triggers

The adapters emit canonical `process_agent_update` calls at semantic trigger points.
`session-start` is deliberately read-only: it checks server reachability,
fetches the governance fundamentals excerpt, and prompts the agent to call
`start_session(force_new=true)` / `onboard(force_new=true)` itself only when no
identity is cached for this fresh process. If the agent does not do that before
the turn ends, `post-stop` lazily onboards a slot-scoped identity before
emitting `turn_stop`; if that fails, it records an identity-free floor
observation instead. Once a process is bound, later turns continue via
`client_session_id` and `sync_state()` rather than fresh onboarding.

| Trigger | Hook script | Frequency | `metadata.event` |
|---|---|---|---|
| Claude turn ends | `post-stop` | per Claude turn | `turn_stop` |
| Codex turn ends | `post-stop` | per Codex turn | `turn_stop` |
| Edit threshold crossed | `post-edit` | every N edits or T seconds | `auto_edit` |

Codex has three distinct signal classes. They should not be counted as if they
were the same measurement:

| Signal | Producer | Storage path | What it measures |
|---|---|---|---|
| Explicit `sync_state` | agent | identity-bound check-in (`agent_report` default) | agent-authored proprioceptive state |
| `turn_stop` | Stop hook | identity-bound check-in (`substrate_interpretation`) | host interpretation of the completed turn |
| PostToolUse activity | PostToolUse hook | local `~/.unitares/codex-activity/` ledger | completed-tool events received for one Codex slot |

The local activity observer is on by default. It does not synthesize a
check-in, EISV vector, intent, or progress, and it does not write to
`/v1/substrate/observe`: that endpoint is the coverage-gap floor for sessions
that never onboarded. Disable local recording with
`UNITARES_CODEX_LIVENESS=off` or tune its bounded lock wait with
`UNITARES_CODEX_ACTIVITY_LOCK_TIMEOUT_S`.

This hook path is a diagnostic bridge for the packaged Codex client. If a
custom orchestrator owns Codex App Server or the Codex SDK, consume its native
turn/item event stream and attribute those events as host observations. A
dedicated host-observation sink is required before that telemetry can be
centralized without contaminating agent trajectories or dark-session counts.

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
check-in and identity-free substrate observation. Disable a single trigger by
removing its entry from `hooks/hooks.json` or `hooks/codex-hooks.json`.

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
