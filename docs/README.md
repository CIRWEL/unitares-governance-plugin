# Documentation

Reference and design material for the UNITARES governance plugin. For setup,
configuration, and the day-to-day workflow, start with the
[top-level README](../README.md); if you work from Codex or ChatGPT, start with
[CODEX_START.md](../CODEX_START.md).

## Map

| Surface | Where | Read it for |
|---|---|---|
| Project overview, install, config | [`README.md`](../README.md) | What this repo is, server bring-up, env vars, workflow |
| Codex/ChatGPT quickstart | [`CODEX_START.md`](../CODEX_START.md) | The preferred entry path — modes, recommended flow, continuity cache |
| Adapter reference | [`adapters.md`](./adapters.md) | Claude hooks, Codex/ChatGPT, and the local sidecar — full detail and endpoints |
| Check-in reference | [`check-ins.md`](./check-ins.md) | Claude check-in triggers, kill switch, diagnostic log, protective audit, upgrade steps |
| Contributing | [`CONTRIBUTING.md`](../CONTRIBUTING.md) | Branch/PR convention and review standard |
| Design notes | this directory | Why the plugin is shaped the way it is, and what it deliberately does not build |
| Skills | [`../skills/`](../skills/) | Agent-facing capability docs (governance fundamentals, lifecycle, dialectic, knowledge graph, Discord bridge) |
| Claude commands | [`../commands/`](../commands/) | Claude Code-only `/governance-start`, `/checkin`, `/diagnose`, `/dialectic` |

## Design notes

These are rationale documents. They explain a design decision — usually a
decision to borrow a narrow useful idea and decline the larger machine around
it — so the reasoning survives past the conversation that produced it.

- [**Orchestration is not governance**](./orchestration-vs-governance.md) —
  the two-layer model that frames the whole project. Orchestration routes work
  between agents; governance supervises a fleet whose topology is already in
  motion. Many "multi-agent" asks are governance-layer asks in disguise.
- [**The LLM wiki is a layer, not a competitor**](./llm-wiki-vs-kg.md) —
  how the UNITARES knowledge graph compares to Karpathy's LLM-wiki pattern,
  Graphiti, and GraphRAG. The KG dominates on the multi-agent, audited,
  governed problem; the one gap worth taking — compounding synthesis — belongs
  as a periodic lifecycle action, not a write-time hook.
- [**Do we need an ontology? No — we need a normalizer**](./ontology-need.md) —
  why the "ontology" question is mostly a tag-formatting problem, solved by a
  deterministic client-side normalizer plus a short curated synonym map on the
  server. Entity resolution, richer edge types, and an ontology agent stay
  documented and unbuilt.

A recurring theme runs through all three: this repo is the **Claude/Codex
client/plugin integration layer**, the server (`cirwel/unitares`) is the source
of truth, and host lifecycle bindings such as Hermes live in
`cirwel/unitares-host-adapter`. New design notes should keep that altitude —
argue a decision, mark what is deferred, and say which side of the
client/server/host-adapter line the work lands on.
