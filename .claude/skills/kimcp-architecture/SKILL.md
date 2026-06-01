---
name: kimcp-architecture
description: Architectural blueprint for KiMCP — a from-scratch MCP server exposing every KiCAD feature. Use when designing, extending, reviewing, or debugging the MCP server internals: transports, backend dispatcher, tool registry, schemas, resources, performance, and safety. Pairs with DECISIONS.md (ADR log) which records the reason behind every non-obvious choice. Consult this skill before touching server internals or adding tools.
---

# KiMCP Architecture

Purely architectural. No roadmap, milestones, or phasing. No code. When a decision is made here, record the *why* in `DECISIONS.md` as a new ADR entry. Never silently change architecture — supersede an ADR instead.

## Non-goals

- Implementation timeline or MVP/v1/v2 phasing.
- Code samples.
- Domain knowledge (signal-integrity, DFM, etc.) — those live in sibling skills and are invoked by the domain-knowledge engine at runtime.
- Referencing or borrowing from existing open-source KiCAD MCPs — explicit constraint (see ADR-0001).

## Layered model

```
 ┌─────────────────────────────────────────────────────────┐
 │ Transport (STDIO / HTTP+SSE, JSON-RPC 2.0)              │
 ├─────────────────────────────────────────────────────────┤
 │ Tool registry (plugin discovery, schema, versioning)    │
 ├─────────────────────────────────────────────────────────┤
 │ Session / context (per-conn state, prefs, active proj)  │
 ├─────────────────────────────────────────────────────────┤
 │ Domain-knowledge engine (validators, suggestions, why)  │
 ├─────────────────────────────────────────────────────────┤
 │ Backend dispatcher (IPC / CLI / S-expr / SWIG)          │
 ├─────────────────────────────────────────────────────────┤
 │ KiCAD backends                                          │
 └─────────────────────────────────────────────────────────┘
       ▲                         ▲
       │                         │
   Resources                  Prompts
 (cached, file-watched)   (canned workflows)
```

See topic files for details:

- `DECISIONS.md` — ADR log. **Append-only.**
- `transport.md` — transport layer.
- `backends.md` — backend dispatcher logic and selection rules.
- `tool-catalog.md` — complete KiCAD-feature → tool(s) mapping, naming conventions.
- `schemas.md` — schema design, validation, versioning, deprecation.
- `performance.md` — performance strategies and measurement.
- `safety.md` — safety model.
- `resources-and-prompts.md` — MCP resources and prompt library.

## The four backends (one-liner each — details in backends.md)

1. **IPC API (KiCAD ≥9)** — live GUI session, two-way sync. Preferred for interactive edits.
2. **`kicad-cli`** — headless batch (DRC, ERC, exports). Preferred for CI and manufacturing outputs.
3. **S-expression parser (ours)** — fastest path for reads and headless bulk edits.
4. **SWIG `pcbnew`** — legacy Python bindings, used only when other three don't cover a feature.

Every tool declares which backends can service it; the dispatcher picks one per call based on availability, intent, and the `live_gui_visible` / `mutates` flags.

## Language and dependencies

- **Python 3.11+** primary (matches KiCAD's native scripting surface).
- **Rust via PyO3** only for profiled hot paths (large `.kicad_pcb` parsing, Gerber streaming). Do not add Rust upfront; justify with measurement.
- **Pydantic v2** as the schema source of truth. JSON-Schema is *generated* from Pydantic for the MCP surface.
- **`pynng` + `protobuf`** for the IPC API — protobuf-over-nng Req/Rep, not gRPC (see ADR-0015). Protos sourced from `kicad-python` via the `[ipc]` optional extra.
- **subprocess + asyncio** for `kicad-cli`.
- **ngspice** for simulation (subprocess).
- No framework lock-in for MCP protocol — hand-rolled JSON-RPC handler on top of a tiny transport abstraction. See ADR-0012 before changing this.

## Genericity rules (hard constraints)

1. No hardcoded paths, library names, part numbers, reference-designator conventions.
2. Defaults match KiCAD's official library defaults, and only those.
3. Cross-platform (macOS, Linux, Windows) — each backend must work on all three or be marked platform-gated.
4. Tools register via entry points, not static imports. External packages can contribute tools.
5. Every user-facing string is localizable (even if only English ships).
6. No project-specific assumptions in the core. If a user has conventions (e.g., "our boards always have four layers"), they configure the MCP; they don't patch it.

## Domain-knowledge engine

First-class layer. Not a middleware afterthought.

- Invokes sibling skills (`signal-integrity`, `power-integrity`, `dfm`, `datasheet-search`, `errata-search`, `vendor-search`, `3d-models-and-footprints-search`, `electrical-cad-best-practices`, `kicad-best-practices`).
- Runs pre-save validators on mutating tools.
- Attaches a `suggestions` array to tool results when applicable (each suggestion has `rule`, `severity`, `why`, `fix_hint`).
- Supports a user-adjustable `strictness` (off / hints / enforce).
- Explanations are *always* sourced — every suggestion cites the rule it came from so users can audit and override.

## Safety model (summary — details in safety.md)

- Read / write separation at the tool level.
- `dry_run=true` supported on every mutating tool.
- Snapshot-before-write: git commit if the project is a repo, else a timestamped directory copy.
- Confirmation required (prompt-level) for: file deletion, library overwrite, schematic structural deletes (sheet delete, net delete), mass-rename across ≥N objects (configurable threshold).
- Undo log for IPC-mediated edits when the backend supports it.

## Testing strategy (high level — implementation details in impl repo)

- Coverage matrix: every KiCAD menu item, shortcut, and `kicad-cli` subcommand maps to a tool or a deliberate exclusion with reason.
- Golden-file tests for the S-expression parser (read → write → byte-diff against fixture).
- Integration tests run against real KiCAD installations on each supported platform and KiCAD version.
- Property-based tests for rule engines in the domain-knowledge layer.
- No test of "delivered behavior" may mock all four backends — at least one integration path is real.

## When to update this skill

- Architectural change → new ADR in `DECISIONS.md`; update the affected topic file; update SKILL.md only if the layered model itself changed.
- New tool category → update `tool-catalog.md`.
- New backend → update `backends.md` + ADR.
- New domain skill added → update the domain-knowledge engine section in this file.
