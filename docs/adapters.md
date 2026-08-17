# Adapter Notes

How each client connects to a running UNITARES governance server. Adapters are a
convenience layer — the server is the source of truth and the client stays thin.
For env-var configuration see the [Configuration section of the README](../README.md#configuration).

## Boundary with `unitares-host-adapter`

This repo owns Claude/Codex plugin packaging, hook scripts, shared guidance, and
the generic identity sidecar. The Hermes-native lifecycle binding does **not**
live here; it lives in [`unitares-host-adapter`](https://github.com/CIRWEL/unitares-host-adapter)
as `unitares_host_adapter.bindings.hermes` and is loaded by a thin Hermes user
plugin at `~/.hermes/plugins/unitares`.

A direct Hermes MCP server entry is a third surface: it exposes callable
UNITARES tools, but it does not add lazy onboarding or automatic turn check-ins
unless the Hermes plugin/host-adapter layer is also enabled.

## Host Hook Contract

Hook intent is shared; wire payloads and execution policy are not. Every
host-dependent hook command must receive its host explicitly. Hook code must
normalize that host's payload before invoking shared lease, cache, or
governance behavior. It must never infer the host from whichever fields happen
to be present.

| Concern | Claude Code | Codex |
| --- | --- | --- |
| Manifest | `hooks/claude-hooks.json` | `hooks/codex-hooks.json` |
| Session-end matcher | Claude wildcard | Codex reason `other` |
| Edit matcher | `Edit\|Write\|MultiEdit` | canonical `^apply_patch$` |
| Edit input | scalar `tool_input.file_path` (or legacy `path`) | patch envelope in `tool_input.command` |
| Edit identity | required `tool_use_id`; uncorrelatable legacy events do not lease | required `tool_use_id`; subagents can share the parent `session_id` |
| Governance rewrite | known Claude/legacy aliases; `updatedInput` without a permission decision | canonical `unitares-governance` alias only; rewrite plus required allow only for anchored lifecycle, check-in, and read-only diagnostics |
| Configuration precedence | explicit environment, then `defaults.env` fallback | explicit environment, then `defaults.env` fallback |
| Post-edit policy | synchronous lease release alongside asynchronous local milestone plus optional watcher/auto-checkin | synchronous and bounded; lease cleanup plus local milestone only |
| Long-turn liveness | Stop and edit-threshold lifecycle signals | every completed PostToolUse receipt updates a local, identity-free slot ledger |
| Identity response | asynchronous; commits only against the exact PreToolUse cache-generation snapshot | synchronous; uses the same tool-scoped generation guard |
| Workspace briefing | dirty sibling worktrees, opt-out available | skipped to preserve the synchronous SessionStart budget |
| Session end | sub-second best-effort lease cleanup; Stop owns governance delivery | bounded lease cleanup only; Stop owns governance delivery |
| Stop response | `last_assistant_message`; tool count is unavailable | `last_assistant_message`; tool count is unavailable |
| Stop network budget | asynchronous; 10s onboard and 20s check-in defaults | synchronous; onboard capped at 5s and check-in capped at 15s inside the 30s hook |
| Lazy identity defaults | `claude-<workspace>`, model type `claude-code` | `codex-<workspace>`, model type `codex` |

`scripts/edit_hook_event.py` owns edit normalization. A Codex patch may name
multiple add/update/delete/move paths; all are leased as one batch, while the
milestone counter advances once for the tool invocation. Lease state and
holder identity are scoped by `tool_use_id`, because concurrent tools can share
a host session id. Missing-ID edit events fail open by default (or block when
leases are configured as required) and never release session-wide lease state.
`scripts/stop_hook_event.py` owns Stop summary
normalization and never fabricates a tool count for either current host
payload. Legacy Claude `final_text`/`tool_calls` input remains accepted for
older installations.

Claude may run network-bearing post hooks asynchronously. Codex does not
currently support async command handlers, so synchronous edit hooks have a
six-second manifest timeout and stricter internal deadlines. Turn-level Codex
governance remains the Stop hook's responsibility, not PostToolUse's. The broad
Codex PostToolUse activity hook is a deliberately narrower sensor: it performs
only a bounded local ledger update and never writes to an agent trajectory or
to the identity-free dark-session floor.

That provenance boundary is ontological, not just operational. An explicit
`sync_state` is agent-authored proprioceptive state. A Stop summary is a
hook-derived `substrate_interpretation` associated with the bound process. The
local activity ledger says only that the host delivered a completed
PostToolUse event for the slot. It contains no UUID, client session binding,
tool content, EISV, verdict, intent, or semantic progress claim. Tool-free
sampling and a single long-running tool call remain invisible until another
host event or Stop. An App Server/SDK event observer plus a dedicated
host-observation sink is the better integration when the orchestrator owns
Codex directly and needs complete central lifecycle telemetry.

Identity tools (`onboard`, `start_session`, `identity`, and `bind_session`) are
paired across PreToolUse and PostToolUse by `tool_use_id`. PreToolUse reserves
the next workspace generation plus a persistent HOME-level authority sequence;
PostToolUse consumes that one-shot token and commits only if no later identity
invocation, cache write, or clear superseded it while the call was in flight.
Missing IDs or snapshots fail closed for cache mutation. The authority record
at `~/.unitares-cache-authority/session-<slot>.json` also marks the optional
HOME mirror invalid before mutation, so a failed mirror write or clear cannot
revive stale identity through PWD fallback.

Claude's `PermissionDenied` event covers auto-mode classifier denials, not
manual permission-dialog denials, deny-rule matches, or a PreToolUse denial.
Failed edit execution is released by `PostToolUseFailure`; a synchronous
`PostToolBatch` cleanup covers leases left by the other completed-batch denial
paths, but releases only the exact `tool_use_id` values present in that batch.
SessionEnd and the lease TTL remain the interruption backstops. Stop is only a
turn boundary and never releases session-wide lease state because a sibling or
background edit may still be running under the same `session_id`.

Current Codex invokes PostToolUse only after a successful `apply_patch`, so a
failed or denied patch is not counted as an edit. Successful patches release
through a dedicated synchronous handler that is independent of the local
milestone pipeline. Codex exposes no matching failure hook today: a failed
patch's pre-acquired lease is released at SessionEnd or by the default
30-second TTL, and an immediate in-turn retry can therefore briefly encounter
its own still-active lease. Session-wide Stop cleanup remains intentionally
unsafe because concurrent tools and subagents can share the same `session_id`.
Denied or failed Codex check-ins are reclaimed by exact session slot at Stop;
an age-based snapshot prune covers crashes without deleting another live
workspace session's revision marker.

Claude plugin-provided SessionEnd hooks share a 1.5-second host budget; a
plugin manifest timeout does not enlarge it. SessionEnd therefore performs
only a bounded cleanup attempt. Abrupt shutdown may leave a lease until its
short TTL expires, while the preceding Stop event remains the final governance
delivery path.

Lifecycle hooks may release resources owned by their exact host event, but they
must not run `git stash`, `git reset`, `git checkout`, or `git clean`. A host
session can share a worktree with subagents and other clients, so SessionEnd is
not evidence that all dirty Git state belongs to the exiting process.

Milestone updates use a bounded cross-process lock. Manual check-ins snapshot
the milestone revision in PreToolUse under `tool_use_id`; PostToolUse resets
only that revision. An edit that lands while the governance call is in flight
therefore remains pending for the next check-in instead of being erased.
Slot-scoped session-cache merges likewise hold a stable sidecar lock across
their complete read/validate/write transaction, so concurrent identity and
check-in hooks cannot overwrite one another's fields.
Claude edit-threshold check-ins also acquire one expiring workspace delivery
claim before HTTP, preventing concurrent asynchronous hooks from submitting
the same threshold crossing twice without holding the milestone lock on I/O.
Operator environment values, including `0` and `off` kill switches, always
take precedence over values sourced from `config/defaults.env`. The deliberate
exception is `UNITARES_FILE_LEASES_REQUIRED`: when truthy, its fail-closed
policy takes precedence over `UNITARES_FILE_LEASES_ENABLED=0`, so contradictory
configuration enables lease enforcement rather than silently bypassing it.

## Claude

The current Claude adapter includes session-start, pre-edit, post-edit, and session-end hooks. Those hooks should be treated as an adapter convenience, not the canonical governance policy. In particular, frequent file writes should not automatically be interpreted as meaningful governance events.

The pre-edit hook acquires a BEAM file lease before Edit/Write/MultiEdit. Missing lease-plane configuration fails open by default, while real `held_by_other` contention blocks the edit with a visible explanation. Successful and failed edit events release their tool-scoped lease; completed-batch, Stop, SessionEnd, and TTL cleanup cover denials or interruptions that do not expose a matching post-tool event.

Workspace watcher fan-out is disabled by default. Enabling it requires both
`UNITARES_WATCHER_ENABLED=1` and an explicitly configured executable
`UNITARES_WATCHER_HOOK`; the plugin never auto-runs a repository-local watcher.

The `session-start` hook remains read-only: it tells the agent to call `start_session(force_new=true)` before substantive work. If the agent has not onboarded by the end of the turn, `post-stop` uses `scripts/onboard_helper.py` to lazily mint a fresh, slot-scoped identity and then emits the normal `turn_stop` summary under that identity. Set `UNITARES_AUTO_ONBOARD=off` or legacy `UNITARES_DISABLE_AUTO_ONBOARD=1` to fall back to identity-free floor observations for un-onboarded sessions.

For the full Claude check-in trigger contract, see [check-ins.md](./check-ins.md).

## Codex

Codex and ChatGPT support should stay minimal and explicit:

- package shared skills through `.codex-plugin/plugin.json`
- document manual command flows for agents that can use them
- treat `.unitares/session-<slot>.json` as the neutral local continuity cache; flat `.unitares/session.json` is legacy/read-only
- use `scripts/session_cache.py` as the shared cache helper across adapters
- acquire per-file leases for the canonical `apply_patch` payload without treating an edit as a governance check-in
- keep synchronous PostToolUse work local and bounded; use Stop for the turn-level substrate check-in

On Windows, command hooks require Git Bash and Python 3.12+ exposed inside Git
Bash as `python3`. Required lease mode emits a deny envelope when Bash itself is
missing instead of silently continuing the edit. On every host, a missing
Python interpreter or lease helper exits with the PreToolUse blocking code when
required mode is enabled.

If you want adapter-like onboarding/check-in behavior from Codex, run the sidecar
(below). The full Codex/ChatGPT quickstart lives in [CODEX_START.md](../CODEX_START.md).

## Sidecar

For clients without lifecycle hooks, run the local identity sidecar and send
governance REST tool calls through it:

```bash
python3 scripts/identity_sidecar.py \
  --server-url http://localhost:8767 \
  --workspace "$PWD" \
  --slot codex-local \
  --port 8768
```

Phase 1 is a dependency-free sidecar, not a full streamable-MCP/SSE
implementation. It wraps REST `/v1/tools/call` and minimal JSON-RPC MCP
`/mcp/` requests, lazily onboards a slot when needed, injects
`client_session_id` into attribution-relevant governance calls, forces
`force_new=true` for bare `onboard` / `start_session`, stamps the slot cache
after check-ins, and exposes `GET /audit`. Useful endpoints:

- `GET http://127.0.0.1:8768/client-config?slot=codex-local` for a generated MCP/client snippet
- `POST http://127.0.0.1:8768/v1/tools/call` with `{"name": "...", "arguments": {...}}`
- `POST http://127.0.0.1:8768/mcp/` for JSON-RPC MCP requests; `tools/call` is intercepted and other JSON requests pass through
- `POST http://127.0.0.1:8768/turn/checkin` with `response_text`, `complexity`, and an optional `confidence`. Omit `confidence` unless the caller is stating an agent-authored belief: the server mints a tactical prediction from any value supplied and scores it into the calibration curve, so a placeholder becomes a forecast nobody made.
- `POST http://127.0.0.1:8768/turn/stop` for an end-of-turn check-in
- `GET http://127.0.0.1:8768/audit?log_tail=200` for bounded local cache/log contract findings

Use `X-UNITARES-Slot` or top-level `{"slot": "..."}` when one sidecar serves
multiple clients. Without an explicit slot, the sidecar uses a workspace-derived
default slot.

### Plain CLI wrapper (`u-run`)

Use `scripts/u-run` when a runtime exposes neither lifecycle hooks nor an HTTP
client integration point:

```bash
scripts/u-run --class goose -- goose run --recipe review.yaml
```

The first `--` separates wrapper options from the child command. `u-run` then:

1. reuses a healthy sidecar for the same workspace and upstream server, or
   starts one and owns its cleanup;
2. creates one unique slot (or uses an explicit `--slot`) and calls
   `/session/start` once, which writes `.unitares/session-<slot>.json`;
3. exports `UNITARES_SERVER_URL`, `UNITARES_SIDECAR_URL`,
   `UNITARES_SIDECAR_SLOT`, and `UNITARES_MODEL_TYPE` to the child;
4. forwards `SIGINT`, `SIGTERM`, and `SIGHUP` to the child process group and
   preserves the child's shell exit status; and
5. submits one `/turn/stop` check-in from observed wall time and exit status,
   always labeled `epistemic_class="substrate_interpretation"` and with no
   confidence field.

The exit complexity proxy is intentionally small and inspectable: `0.30` base,
up to `0.35` over the first hour of observed runtime, plus `0.20` for a nonzero
exit, capped at the same `0.85` ceiling used by hook-derived summaries. It is a
substrate interpretation of process shape, not a claim about task success or
the agent's internal state.

Class selection is deliberately explicit. Passing `--class X` uses exactly
`X`; omitting it uses the neutral literal `u-run`. The wrapper never guesses a
calibration class from the command name, process name, or working directory.
Configuration defaults to `UNITARES_SERVER_URL` and `UNITARES_SIDECAR_URL`, or
to `http://localhost:8767` and `http://127.0.0.1:8768` respectively.

### Repo Boundary

The sidecar belongs in this repo while it remains a generic client facade. It is
integration code: local cache management, lifecycle convenience, redaction, and
REST/MCP proxying. The UNITARES server repo remains the source of truth for
identity semantics, governance policy, storage, and tool schemas.

Do not create a separate sidecar repo just to support local models. Local and
non-frontier model runners should route through the sidecar when they cannot
load a richer plugin, because the sidecar keeps process identity out of prompt
context and normalizes the model-visible response. A separate repo becomes
worth considering only if the sidecar grows independent packaging, nontrivial
runtime dependencies, or host-specific lifecycle bindings comparable to
`unitares-host-adapter`.

Raw direct MCP/REST remains available for advanced clients and operator
debugging. If a client uses raw direct calls in normal operation, its runner must
own the same responsibilities the sidecar owns here: private session retention,
identity injection on write calls, no proof material in prompts/logs, and
check-ins after meaningful work.

For clients that accept a URL MCP server, point them at
`http://127.0.0.1:8768/mcp/` only when they use JSON request/response MCP. The
generated `GET /client-config` response includes the URL, `X-UNITARES-Slot`
header, and a minimal `mcpServers` snippet. If a client requires streamable
HTTP/SSE semantics, use the upstream governance MCP endpoint until the sidecar
grows that transport path.
