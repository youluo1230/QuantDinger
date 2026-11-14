# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

This repo is **single-context**: one `GLOSSARY.md` and one `docs/adr/` at the repo root. There is no `GLOSSARY-MAP.md` and no `src/<context>/docs/adr/` tree — the backend lives under `backend_api_python/`, not `src/`.

## Before exploring, read these

- **`GLOSSARY.md`** at the repo root.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in.

Neither exists yet. If they don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

When they do appear, place them as:

```
/
├── GLOSSARY.md
└── docs/adr/
    ├── 0001-....md
    └── 0002-....md
```

## Existing domain material

`AGENTS.md` and `docs/architecture/` already carry the load-bearing vocabulary: process ownership, PostgreSQL as the durable source of truth, Kafka event transport, the `jobs` Redis durability role, tenant isolation, idempotency, leases and fencing, the `/api/agent/v1` Agent Gateway boundary, and live-trading authorization. The architecture documents under `docs/architecture/` (notably `ARCHITECTURE.md`, `MODULE_BOUNDARIES.md`, `CONCURRENCY_MODEL.md`, `PROCESS_ROLES_AND_TASKS.md`, and the versioned `V5_*`/`V6_*` upgrade plans) record decisions that predate any ADR directory.

Read them as the current source of truth for how the system is built, and prefer their terms over inventing synonyms. Two consequences for skills:

- A decision recorded in `docs/architecture/` is a decision, even though it is not an ADR. Contradicting it deserves the same explicit call-out an ADR conflict gets.
- When a decision genuinely gets resolved in conversation, an ADR under `docs/adr/` is the right home for it; don't rewrite the architecture guides to absorb it.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `GLOSSARY.md`, or as used in `docs/architecture/` where the glossary is still silent. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_

`AGENTS.md` lists the invariants that must not be silently changed — process ownership, the PostgreSQL/Kafka/Redis split, tenant isolation, idempotency, leases, fencing and audit behavior, the Agent Gateway contract, and live-trading safeguards. Treat a violation of any of those as a conflict to surface, not a detail to swallow. When a change touches `/api/agent/v1`, `docs/agent/agent-openapi.json` and its alignment tests are part of the contract, not documentation chores.
