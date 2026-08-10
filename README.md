# UNITARES Governance

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-plugin-d97757.svg)](https://docs.claude.com/en/docs/claude-code/plugins)
[![Codex Plugin](https://img.shields.io/badge/Codex-plugin-10a37f.svg)](./CODEX_START.md)
[![Version](https://img.shields.io/badge/version-0.4.12-blue.svg)](.claude-plugin/plugin.json)

Client/plugin integration layer for **UNITARES** — the runtime governance layer for heterogeneous AI-agent fleets. This repo provides Claude/Codex-facing skills, command guidance, hook scripts, and sidecar tooling for connecting coding agents to a running UNITARES governance server. The runtime itself lives in [`cirwel/unitares`](https://github.com/cirwel/unitares); Hermes-native lifecycle bindings live in [`cirwel/unitares-host-adapter`](https://github.com/cirwel/unitares-host-adapter).

## Install With Claude Code

This repository is a Claude Code plugin marketplace. Add the marketplace, then
install the plugin:

```text
/plugin marketplace add cirwel/unitares-governance-plugin
/plugin install unitares-governance@unitares-governance
```

The plugin ships an `.mcp.json` file that registers its governance MCP client.
No hand-edited `mcpServers` JSON is required. A UNITARES server must still be
reachable at `http://localhost:8767`, or at the base URL configured through
`UNITARES_SERVER_URL`; see [Prerequisites](#prerequisites). Codex and ChatGPT
users should start with [CODEX_START.md](./CODEX_START.md).

Current Claude Code hook payloads scope this plugin-bundled server as
`plugin_unitares-governance_unitares-governance`, producing tool names such as
`mcp__plugin_unitares-governance_unitares-governance__sync_state`. Codex uses
the bare `mcp__unitares-governance__sync_state` form instead.

Host contracts are tested with Claude Code 2.1.220 and Codex CLI 0.146.0;
these are the minimum tested versions. Add the Codex marketplace with
`codex plugin marketplace add cirwel/unitares-governance-plugin`, install the
plugin from `/plugins`, and start a new session. Review the bundled lifecycle
commands in `/hooks`; Codex does not run untrusted plugin hooks automatically.
The bundled Codex MCP transport is an unauthenticated localhost default. See
[Configuration](#configuration) before connecting Codex to a hosted or
authenticated server.

## Purpose

This repo is not the governance engine itself. It is the client and integration layer.

Use it to:

- onboard agents into UNITARES
- inspect governance state and operator diagnostics
- request dialectic review
- work with the knowledge graph
- adapt UNITARES workflows to Codex, ChatGPT, Claude, and other clients

## What Lives Elsewhere

- `unitares` contains the runtime, MCP server, storage, health checks, and governance logic
- `unitares-host-adapter` contains host lifecycle bindings, including the Hermes-native adapter used through a thin Hermes user plugin
- `unitares-governance-plugin` contains the Claude/Codex-facing plugin package, skills, command guidance, hook scripts, and sidecar tooling
- optional bridges like Discord can remain separate integrations

This repo should not duplicate server business logic or become the source of truth for thresholds that already live in the runtime.

## Current Surfaces in This Repo

- Codex/ChatGPT: plugin packaging, synchronous lifecycle hooks, shared skills, and explicit command guidance
- Claude: asynchronous-capable hooks, session helpers, command docs, and optional check-in conveniences
- Sidecar: local proxy/facade for clients without native lifecycle hooks, including local and non-frontier model runners

Hermes Agent is intentionally not listed here as the native path. For Hermes, use `unitares-host-adapter` and install a thin Hermes user plugin that imports `unitares_host_adapter.bindings.hermes`. Direct Hermes MCP config only exposes tools; it does not provide automatic lifecycle check-ins by itself.

The shared value in this repo is the workflow guidance and client integration surface, not a second copy of the governance model.

## Sidecar Placement

The sidecar lives in this repo at `scripts/identity_sidecar.py` because it is a
generic client facade over the UNITARES server. It does not own governance
policy, storage, EISV scoring, or identity semantics; those stay in
`cirwel/unitares`.

It does not need a separate repo while it remains a thin local bridge for
clients that cannot load lifecycle hooks. Split it out only if it becomes an
independently versioned host adapter with its own release cycle, packaging, or
runtime dependencies. For now, keeping it beside the Codex/Claude plugin docs
keeps the integration contract and conformance tests in one place.

For local models and smaller hosted models, prefer the sidecar over raw MCP
direct unless the runner already manages UNITARES lifecycle state. The model
should call task-level governance tools; the runner or sidecar should retain
and inject process identity outside the prompt.

For a plain CLI that has neither lifecycle hooks nor a sidecar client, use the
bundled process wrapper:

```bash
scripts/u-run --class goose -- goose run
```

`u-run` reuses a compatible local sidecar or starts one for the lifetime of the
child, starts one slot-scoped session, exports the sidecar/server context, and
forwards termination signals. It emits one exit check-in from observed wall
time and exit status as `substrate_interpretation`, without supplying agent
confidence. `--class` is never inferred; when omitted its neutral value is
`u-run`. See [the adapter guide](./docs/adapters.md#plain-cli-wrapper-u-run).

## Start Here

If you are using ChatGPT or Codex, start with [CODEX_START.md](./CODEX_START.md).

That path is now the preferred default. Claude hook automation remains supported, but it is no longer the canonical mental model for UNITARES usage.

## Documentation

| Document | What it covers |
|---|---|
| [CODEX_START.md](./CODEX_START.md) | Preferred entry path for Codex/ChatGPT — modes, recommended flow, continuity cache |
| [docs/](./docs/) | Documentation index and design-rationale notes (why the plugin is shaped this way) |
| [skills/](./skills/) | Agent-facing capability docs — governance fundamentals, lifecycle, dialectic, knowledge graph, Discord bridge |
| [commands/](./commands/) | Claude Code slash-command guidance — `/governance-start`, `/checkin`, `/diagnose`, `/dialectic` |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | Branch/PR convention and review standard |

## Core Workflow

The intended workflow is per **fresh process**, not per user message. Do not
call `start_session(force_new=true)` just because a new turn starts; that mints
another process identity. Once a process is bound, continue it with
`client_session_id`-backed `sync_state()` / `check_working_state()` calls.

1. `start_session(force_new=true)` once at process start, to mint a fresh process identity (`onboard(...)` is the canonical equivalent) — until you onboard, this process has no governance identity
2. if this fresh process is a real handoff from finished prior work, pass `parent_agent_id=<prior uuid>` and `spawn_reason="new_session"`
3. call `sync_state()` when there is meaningful agent state to report, usually at most once per assistant turn (`process_agent_update(...)` is the canonical equivalent) — automatic Stop interpretations and bootstrap rows remain separately labeled
4. call `check_working_state()` for read-only state
5. use `identity(agent_uuid=..., continuity_token=..., resume=true)` only for same-live-owner proof-owned rebinds
6. call `identity()` and `health_check()` when diagnosis is needed

`continuity_token` is not a normal check-in argument. Use the returned
`client_session_id` for ordinary same-process continuity; adapters should inject
it automatically when hooks are available.

On servers with the agent-experience envelope enabled, friendly alias responses
lift `next_action`, `state_summary`, `risk_summary`, `memory_suggestions`, and
`recovery_hint` when present while preserving the canonical payload under
`raw_governance`. Older compatibility surfaces may return the canonical payload
directly. Use `memory_suggestions` as retrieval cues, and prefer
`recovery_hint` before inventing a recovery path. If `low_confidence` or
`confidence_note` appears with memory suggestions, treat those suggestions as
exploratory leads until you open the details or re-run a better search.

The principle is simple: prefer regular behavioral baselines over raw activity noise. One real check-in per assistant turn is useful; per-tool or per-edit check-ins are usually not.

## Claude Commands

These custom slash commands are packaged for Claude Code. Codex uses the
skills and explicit MCP tool flow below; it does not install `commands/`.

| Command | Description |
|---------|-------------|
| `/governance-start` | Create or declare lineage for a Claude Code UNITARES session |
| `/checkin` | Manual turn-baseline check-in, plus meaningful milestones |
| `/diagnose` | Show current governance state plus identity/health diagnostics when needed |
| `/dialectic` | Request a dialectic review |

## Skills

| Skill | When to Use |
|-------|-------------|
| `unitares-governance:governance-fundamentals` | Understanding EISV, coherence, verdicts, and calibration |
| `unitares-governance:governance-lifecycle` | Onboarding, continuity, check-ins, and recovery |
| `unitares-governance:dialectic-reasoning` | Participating in dialectic sessions |
| `unitares-governance:knowledge-graph` | Searching and contributing to shared memory |
| `unitares-governance:discord-bridge` | Operating the Discord integration |

## Prerequisites

1. A running UNITARES governance server
2. Python 3.12+ available to hook shells as `python3`
3. The governance MCP endpoint reachable by the client

Windows clients additionally need Git Bash from Git for Windows. Marketplace
installation packages the hook sources but does not install the Python runtime.

This repo is a **client/plugin integration layer only** — it does not include the governance engine. You need a server running before any of these commands or skills do anything useful.

**Easiest server bring-up — Docker Compose:**

```bash
git clone https://github.com/cirwel/unitares.git
cd unitares
docker compose up
# server now at http://localhost:8767/mcp/
```

That single command brings up Postgres+AGE+pgvector, Redis, and the governance server. The Pi/Lumen embodiment side is optional — governance runs standalone. For bare-metal install (Homebrew Postgres, native AGE compile) see [unitares/README.md](https://github.com/cirwel/unitares#installation).

Once the server is up, **the Claude plugin registers its MCP client
automatically**. It ships an `.mcp.json` pointing at
`http://localhost:8767/mcp/`, so there is nothing to hand-edit. Installing the
plugin and starting Claude Code is enough.

For Claude Code on a different host or port, set `UNITARES_SERVER_URL` to the
**base** URL (no `/mcp/` suffix; the plugin appends it):

```bash
export UNITARES_SERVER_URL=https://gov.example.org   # plugin uses .../mcp/
export UNITARES_HTTP_API_TOKEN='replace-with-client-token'
```

Claude's bundled transport expands the token into its `Authorization` header.
Configure that bundled transport through these variables; do not rely on a
same-named manual Claude server overriding the plugin server.

The bundled Codex `.codex-mcp.json` is deliberately fixed to unauthenticated
`http://localhost:8767/mcp/` and does not declare a bearer-token environment
variable. For a hosted or authenticated Codex server, disable that bundled
transport and register a separate server under the exact
`unitares-governance` alias:

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

Codex hooks trust only the bare `unitares-governance` server alias. Claude's
plugin-scoped alias is host-specific and is not accepted at the Codex hook
boundary.

## Configuration

Environment variables. Explicit values take precedence over plugin defaults,
including `0` and `off` kill switches:

| Variable | Default | Description |
|----------|---------|-------------|
| `UNITARES_SERVER_URL` | `http://localhost:8767` | Governance server base URL |
| `UNITARES_HTTP_API_TOKEN` | unset | Client bearer token for governance REST calls, Claude's bundled MCP transport, and a separately registered authenticated Codex transport; hosted deployments must use one token accepted by the server |
| `UNITARES_AGENT_PREFIX` | host-specific | Prefix for generated client-side names (`claude` or `codex` unless overridden) |
| `UNITARES_AUTO_ONBOARD` | `on` | Let the host Stop hook create a slot-scoped identity before its first turn summary when needed |
| `UNITARES_FILE_LEASES_ENABLED` | `1` | Enable host edit leases (Claude Edit/Write/MultiEdit; Codex apply_patch) |
| `UNITARES_FILE_LEASES_REQUIRED` | `0` | Block edits when lease infrastructure is missing/unreachable; truthy values take precedence over `UNITARES_FILE_LEASES_ENABLED=0` |
| `UNITARES_FILE_LEASE_TTL_S` | `300` | Crash-recovery backstop for a lease not released by PostToolUse |
| `UNITARES_FILE_LEASE_BATCH_TIMEOUT_S` | `3.5` | Total bounded lease work per edit hook (clamped to four seconds) |
| `UNITARES_MILESTONE_LOCK_TIMEOUT_S` | `2.0` | Maximum wait for the cross-process milestone lock (clamped to four seconds) |
| `UNITARES_SESSION_CACHE_LOCK_TIMEOUT_S` | `2.0` | Maximum wait for a slot-scoped session-cache transaction (clamped to four seconds) |
| `UNITARES_AUTO_CHECKIN_CLAIM_TTL_S` | `30` | Crash-recovery expiry for Claude's single in-flight edit check-in claim (clamped to 30-120 seconds) |
| `UNITARES_WATCHER_ENABLED` | `0` | Opt in to the explicitly configured `UNITARES_WATCHER_HOOK`; workspace-local executables are never auto-run |
| `UNITARES_CODEX_LIVENESS` | `on` | Record local, slot-scoped completed-tool receipts; these are not check-ins or agent runtime |
| `UNITARES_CODEX_RUNTIME_OBSERVATIONS` | `on` | Emit bounded completed-tool rollups to the legacy runtime-named audit sink |
| `UNITARES_CODEX_HOST_HEARTBEATS` | `off` | Opt in to hook-parent PID heartbeats; a shared host PID never proves per-agent runtime |
| `UNITARES_CODEX_RUNTIME_IDLE_EXIT_S` | `3600` | Stop a detached slot worker after this many seconds without a completed-tool receipt |
| `LEASE_PLANE_BASE_URL` | `http://127.0.0.1:8788` | BEAM lease-plane HTTP base URL |
| `LEASE_PLANE_BEARER_TOKEN` | unset | Bearer used for lease-plane acquire, heartbeat, and release calls |

## Adapter Notes

Adapters are a convenience layer over the governance server, not the canonical
policy — the server stays the source of truth and the client stays thin.

- **Claude** — host-native lifecycle hooks, asynchronous edit check-ins, batch-completion cleanup, and BEAM file leases.
- **Codex/ChatGPT** — synchronous lifecycle hooks, multi-file apply_patch leases, Stop cleanup, local edit milestones, slot-scoped continuity cache, and separately labeled completed-tool audit evidence.
- **Sidecar** — a dependency-free local proxy/facade for clients without lifecycle hooks; recommended for local/non-frontier model runners that should not manage identity proof material in prompt context.
- **Hermes Agent** — native lifecycle binding lives in `unitares-host-adapter`; this repo is only relevant to Hermes if you deliberately route through the generic sidecar instead.

Full details, endpoints, and configuration are in [docs/adapters.md](./docs/adapters.md).
The Codex/ChatGPT quickstart is [CODEX_START.md](./CODEX_START.md).
Windows hook execution requires Git Bash and Python 3.12+ available as
`python3`; required lease mode fails closed when the wrapper cannot find Bash.

## Non-Goals

This repo should not:

- redefine the governance math
- duplicate server-side threshold logic
- auto-checkin every trivial file write by default
- override runtime verdicts locally

## Check-In Triggers

The Claude adapter emits canonical `process_agent_update` calls at `turn_stop`
and `auto_edit`. Codex Stop may emit one automatic `turn_stop`
`substrate_interpretation`; a manual `sync_state()` is the separately labeled
agent-authored check-in and is usually needed no more than once per turn.
Synthetic onboarding initialization and PostToolUse receipts are not real
check-ins. All adapter check-ins use `scripts/checkin.py`, which redacts
secrets, truncates, logs, and is fire-and-forget. A `UNITARES_CHECKINS=off` kill
switch suppresses them and the host-observation bridge.

For the trigger table, the diagnostic log format, the protective audit, the
known token-auth limitation, and plugin-cache upgrade steps, see
[docs/check-ins.md](./docs/check-ins.md).

## Development Workflow

Every change uses a fresh short-lived branch and pull request:

1. create a short-lived branch
2. keep the change focused
3. push the branch
4. run the pre-PR gate and open a draft PR
5. merge after review or self-review

Do not push directly to `master`, reuse a branch whose PR is merged or closed,
or append post-merge fixes to an old PR head. `scripts/dev/ship.sh` creates a
fresh branch from an up-to-date default branch, refuses delivered PR heads,
and opens a draft PR for staged, otherwise-clean changes.

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the repo convention.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
