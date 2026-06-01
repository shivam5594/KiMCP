# Interactive Workflow

How a KiMCP session feels to drive: phase-gated, scope-aware, and visibly progressing. The model is "plan → execute in tight inner loop → confirm at gates" rather than "one giant call that runs everything." This document is the contract; ADR-0016/0017/0018 record the *why*.

## Motivation

The literal-execution model — fire every relevant skill on every tool call, walk the whole design end-to-end in one shot — is slow and opaque. Users want to **ideate freely**, **see motion**, and **defer commitment-phase work** (errata, vendor, DFM, compliance, simulation) until they are actually ready for it. They also work on **slices** of larger designs and want validation that is scoped, not project-wide.

This document defines the session model, the gate-based loop, the scope mechanism, and the engagement primitives that together make the experience interactive instead of monolithic.

## Core concepts

| Concept | Definition |
|---|---|
| **Session** | Persistent state per client: `phase`, `scope`, `plan`, `gate_history`, `parked`, `audit`. |
| **Phase** | Where the user is in the design lifecycle. Linear default, jumpable on explicit request. |
| **Scope** | The active slice of the design. Every tool call inherits it unless overridden per-call. |
| **Plan** | Ordered gate list, materialized as an MCP resource so it is always visible. |
| **Gate** | A confirmation checkpoint with a `confirmation_token` (per `safety.md`). Forward = pass gate; backward = replan. |
| **Activity stream** | Server-side counters and current-tool indicator surfaced via SSE between gates. |
| **Parked** | Work intentionally deferred from the current phase, surfaced at every gate so it is not lost. |

## Phases

```
ideation ─► validation ─► commitment ─► pcb ─► manufacturing
```

| Phase | User intent | Tools that fire by default | Skills loaded |
|---|---|---|---|
| **ideation** | Sketching topology, placing and wiring symbols | `sch_add_*`, `sch_delete`, `sch_list_*`, `lib_search_*` | `kicad-best-practices`, light `electrical-cad-best-practices`, `power-integrity` (placement rules only), `datasheet-search` (pinouts only) |
| **validation** | "Is this wired right?" | `sch_erc`, `sch_list_nets` | + `signal-integrity`, full `power-integrity` |
| **commitment** | "Lock the BOM" | `errata-search`, `vendor-search`, deep `datasheet-search` | + `errata-search`, `vendor-search` |
| **pcb** | Layout | `pcb_*` inspect, `sch_export_netlist`, `lib_attach_3d_model` | + `dfm`, `mechanical-integration`, layout-level `compliance-and-emc-testing`, conditionally `rf-design` / `high-voltage-design` |
| **manufacturing** | Fab outputs | `pcb_drc`, `pcb_drc_violations`, `pcb_export_*`, `sch_export_bom` | + full `dfm`, `compliance-and-emc-testing` |

Tools outside the current phase are not blocked — they are deferred-by-default. Calling one returns a hint:

```json
{
  "ok": false,
  "code": "PHASE_DEFERRED",
  "message": "errata-search is parked until 'commitment' phase",
  "current_phase": "ideation",
  "park_ref": "session/parked/errata-U3",
  "override": "pass force_phase=true to run anyway"
}
```

Explicit overrides are always allowed. The default protects the loop; it does not lock it.

## Scope model

Every tool that *reads*, *validates*, or *exports* accepts an optional `scope` argument. Default is the session's current scope. Mutating authoring tools (`sch_add_*`) inherit scope as a *placement hint* only (e.g. which sheet to add to).

| Form | Example | Resolves to |
|---|---|---|
| `project` | `scope: "project"` | Whole design |
| `sheet:<name>` | `scope: "sheet:power"` | One hierarchical sheet |
| `subtree:<root>` | `scope: "subtree:analog_front_end"` | A sheet and all its children |
| `section:<tag>` | `scope: "section:dcdc_3v3"` | User-tagged group of symbols across sheets |
| `refdes:<pattern>` | `scope: "refdes:U10-U19,L1-L5"` | Symbol slice by reference designator |
| `net:<pattern>` | `scope: "net:VDD_*,SW_*"` | Net-based slice |
| `area:<bbox>` | `scope: "area:pcb[10,10,40,40]"` | PCB region (mm) |

Sections are a first-class concept independent of sheets: a section can span sheets, and a sheet can contain multiple sections. Sections are stored in `<project>/.kimcp/sections.json` as `{tag: [refdes-or-uuid…]}`.

### Section auto-suggest

At intake, the server proposes a decomposition the user picks from. The proposal is heuristic (topology keywords in the intake brief, grouped by domain), not exhaustive — the user can edit it.

> "I see this project as 4 sections — which do you want to ideate first?
>  ① Power: 32 V → 3 V3, 32 V → 1 V3
>  ② MCU + clocks + reset
>  ③ Sensors + I/O
>  ④ Connectors + input protection"

## The interactive loop

```
┌──────────────────────────────────────────────────────────────┐
│ Phase: ideation     Scope: section:dcdc_3v3                  │
│ Plan ●────●────○────○────○────○   (gate 2 of 6)              │
├──────────────────────────────────────────────────────────────┤
│ Activity:                                                    │
│   ✓ U3 LM5145 placed                                         │
│   ✓ L1 4.7 µH placed                                         │
│   ⟳ wiring SW node…                                          │
├──────────────────────────────────────────────────────────────┤
│ Next gate: BOM commit for dcdc_3v3                           │
│   [ continue ideating ]  [ advance to validation ]           │
│ Parked: errata on LM5145, vendor stock on L1, DFM on U3      │
└──────────────────────────────────────────────────────────────┘
```

Every gate surface includes:

- **Done** — concrete counts, not percentages.
- **Decides** — what this gate locks in.
- **Unlocks** — what becomes active next.
- **Parked** — what stays deferred. (Reassures the user nothing is forgotten.)
- **Why this gate** — one line, sourced from the skill that owns the rule.

## Gate catalog (default workflow)

| # | Gate | Triggers when | Default action |
|---|---|---|---|
| **G0 Intake** | Goal, constraints, scope decomposition | Session start | Propose sections |
| **G1 Section pick** | Choose which section to ideate first | After G0 if >1 section | First section |
| **G2 Topology** | Architecture choices in scope | Per section, before parts | Server recommendation |
| **G3 Part shortlist** | 2–3 candidates with tradeoffs | Per section, before lib search | Top-ranked |
| **G4 Section ERC** | `sch_erc(scope=current)` results | User signals "done with section" | Show & fix |
| **G5 Section commit** | Errata + vendor on locked parts | After G4 passes | Show, advance |
| **G6 Next or move on** | Loop to G1 or advance phase | After G5 | Loop until all sections done |
| **G7 Project ERC** | `sch_erc(scope=project)` | Before PCB phase | Show & fix |
| **G8 PCB handoff** | Stackup, netclasses, outline | Entering PCB phase | Confirmed defaults |
| **G9 Manufacturing** | DFM + outputs | Entering manufacturing | Generate after confirm |

G1–G6 loop per section. Each section is a self-contained mini-project from the user's point of view.

## Suggestion severity, by phase

The domain-knowledge engine emits `severity` on every suggestion (`blocking | warning | advisory | info`). Sessions filter by phase:

| Phase | Floor severity | Effect |
|---|---|---|
| ideation | `blocking` | Only catches structural errors (reversed diode, missing GND, illegal pin tie). |
| validation | `warning` | Adds rule-of-thumb violations (decoupling count, return-path break). |
| commitment / pcb / manufacturing | `advisory` | Full suggestion stream. |
| any | `info` | Only on explicit `verbose=true`. |

This is the single biggest perceived-latency win: the engine still runs, but it does not dump 30 advisory suggestions on every wire add.

## Detour and override semantics

Users can step out of phase order without losing state.

| User says | Server does |
|---|---|
| "Run errata on U3 now" | Detour into commitment with `scope: refdes:U3`, return to caller phase. |
| "Simulate the 3 V3 loop" | Detour into simulation with `scope: section:dcdc_3v3`, return. |
| "Skip validation, go straight to PCB" | Warn (cite `ERC-*` rules), require explicit `confirmation_token`, advance. |
| "Show me what's parked" | Render `kimcp://session/parked`. |
| "Decompose this sheet" | Propose section tagging interactively. |
| "Resume" | Reload session from `<project>/.kimcp/session.json`; show current gate. |

Detours are first-class — they consume a token but do not reset gate progress.

## Replan

Discovery sometimes forces backward motion (e.g. the chosen controller has fatal errata, so topology must change). Replanning is explicit:

1. Server emits `replan_required` with reason and affected gates.
2. User confirms scope of rollback (which gates are reopened).
3. Affected gate history is marked `superseded`, not deleted (audit-preserving).
4. Plan resource updates; downstream gates re-queue.

## Engagement primitives

1. **Plan resource** `kimcp://session/plan` — markdown rendering of gate states. Always live; clients render it without polling.
2. **Activity counters** `kimcp://session/activity` — `{phase, scope, step, total, current_tool, last_tool_ms, elapsed}`. Server pushes via SSE.
3. **Parked items** `kimcp://session/parked` — list of deferred work with `{tool, scope, reason, eligible_in_phase}`.
4. **Severity-filtered suggestions** — see table above.
5. **One-line "why this gate?"** — gates cite the skill rule that motivates them.
6. **`kimcp resume`** — re-enter a session mid-flow without retyping context.

## Session persistence

Session state lives in `<project>/.kimcp/session.json`:

```json
{
  "session_id": "…",
  "phase": "ideation",
  "scope": "section:dcdc_3v3",
  "sections": { "dcdc_3v3": ["U3","L1","C10-C15","R20-R23"], "dcdc_1v3": [...] },
  "plan": [
    { "id": "G0", "status": "passed", "token": "…", "ts": "…" },
    { "id": "G1", "status": "passed", "token": "…", "ts": "…" },
    { "id": "G2", "status": "active" }
  ],
  "parked": [
    { "tool": "errata-search", "scope": "refdes:U3", "eligible_in_phase": "commitment" }
  ]
}
```

The file is rewritten atomically (temp + rename). Snapshots include it. `restore_snapshot` restores it too — including the gate cursor.

## Worked example — "32 V → 3 V3 (2 A) + 1 V3 (3 A) industrial"

```
G0 Intake  → propose sections [Power, Sequencing, Input protection]
G1 Section pick → Power
  ┌── loop: section = dcdc_3v3 ────────────────────────────────┐
  │ G2 Topology  → two singles (LM5145 × 2)                    │
  │ G3 Shortlist → LM5145 / TPS54360 / MP2316  → LM5145        │
  │ [ideate]      sch_add_* × ~25 with blocking-only severity  │
  │ G4 sch_erc(scope=section:dcdc_3v3) → 2 warnings, fix       │
  │ G5 errata+vendor on LM5145, L1, Cout      → all green      │
  │ G6 next section?     → yes, dcdc_1v3                       │
  └────────────────────────────────────────────────────────────┘
  ┌── loop: section = dcdc_1v3 ────────────────────────────────┐
  │ … same shape …                                             │
  └────────────────────────────────────────────────────────────┘
G6 next section? → no, advance
G7 sch_erc(scope=project) → clean
G8 PCB handoff: 4-layer, netclasses {PWR, SIG, SW_NODE}
G9 DFM + gerbers
```

Sections not currently active load no skills, draw no suggestions, fire no web fetches. The user spends most of the time in a fast, focused inner loop.

## Relationship to other architecture docs

- **`safety.md`** — gates use the existing `confirmation_token` mechanism; replans are first-class destructive operations and require tokens.
- **`schemas.md`** — every tool gains optional `scope: str` and optional `force_phase: bool`. Output envelope gains optional `meta.phase_deferred` and `meta.park_ref`.
- **`resources-and-prompts.md`** — three new resources: `kimcp://session/{plan,activity,parked}`. The default workflow is also exposed as a built-in prompt `interactive-design` for clients that prefer prompt-driven flow.
- **`backends.md`** — no change; phase / scope are session-layer concepts.
- **`performance.md`** — phase-gated skill loading and severity throttling are listed as the primary latency-reduction levers.
- **`observability.md`** — gate transitions and detours are logged as named events.

## Implementation notes

- No new MCP tools. The workflow layer is orchestration over existing tools.
- Phase / scope live in the session layer; tools remain pure with respect to them.
- Skills load on demand per phase, not all at session start.
- The default workflow is registered as a built-in prompt; external workflows can register via entry points (per `extensibility.md`).
- Severity is already in the suggestion schema (ADR-0013); only the filter is new.
