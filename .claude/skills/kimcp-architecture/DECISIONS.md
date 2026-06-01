# KiMCP Architecture Decisions Log

ADR-style log. **Append-only.** When revisiting a decision, add a new ADR that supersedes the old one — do not edit past entries. Each ADR captures the *why* so future agents can judge edge cases rather than blindly follow the rule.

## Template

```
## ADR-NNNN: <title>
Date: YYYY-MM-DD
Status: Proposed | Accepted | Superseded by ADR-NNNN
Context: what forced the decision (constraint, incident, user ask)
Decision: what we chose
Alternatives considered: what we rejected and why
Consequences: downstream effects — good and bad
Revisit trigger: signal that would cause us to reopen this
```

---

## ADR-0001: Build KiMCP from scratch, do not reference existing OSS KiCAD MCPs
Date: 2026-04-14
Status: Accepted
Context: Existing open-source KiCAD MCPs are limited in features, slow, do the bare minimum, and are tied to the author's project conventions (hardcoded paths, part numbers, reference-designator schemes).
Decision: Build KiMCP from first principles. Do not read, fork, or borrow from existing KiCAD MCP implementations.
Alternatives considered: Fork one (rejected — inherits the coupling problems we're trying to escape), thin wrapper around an existing MCP (rejected — same problems one layer deeper).
Consequences: Longer initial ramp. Full control over every architectural choice. No upstream to merge from.
Revisit trigger: if an OSS KiCAD MCP emerges that is already generic, fast, and feature-complete, reconsider whether to contribute there instead.

## ADR-0002: Python as primary implementation language
Date: 2026-04-14
Status: Accepted
Context: KiCAD's scripting surface — SWIG `pcbnew`, the IPC API client, action plugins, ngspice bindings — is Python-first. Anything else requires reimplementing large chunks of glue.
Decision: Python 3.11+. Rust is allowed only for profiled hot paths exposed via PyO3 (e.g., large-file S-expression parsing).
Alternatives considered: TypeScript/Node (rejected — worst KiCAD integration story), Rust-only (rejected — no native KiCAD ecosystem), Go (rejected — same), mixed primary-Rust/secondary-Python (rejected — swapped costs without swapped benefits).
Consequences: Single-language mostly. Rust becomes opt-in complexity justified by measurement.
Revisit trigger: if KiCAD's official scripting story moves away from Python (unlikely near-term).

## ADR-0003: Four-backend dispatcher strategy
Date: 2026-04-14
Status: Accepted
Context: No single KiCAD integration covers every feature with acceptable speed. IPC API is new and still filling out. `kicad-cli` is batch-only. SWIG is deprecated but has features IPC lacks. Direct S-expression parsing is fastest but can't update a running GUI.
Decision: Expose all four (IPC API, `kicad-cli`, S-expression parser, SWIG `pcbnew`). A dispatcher routes each operation to the best available backend per the selection matrix in `backends.md`.
Alternatives considered: IPC-only (rejected — feature gaps), parser-only (rejected — no live GUI, no simulation), CLI-only (rejected — no interactivity, no partial edits), any pair (rejected — still misses cases).
Consequences: More integration surface to test. Feature-complete on day one. Each tool must declare which backends it supports.
Revisit trigger: IPC API reaches 100% feature parity with SWIG and CLI — at that point SWIG can be deprecated and eventually removed.

## ADR-0004: Best-practices engine is a first-class layer
Date: 2026-04-14
Status: Accepted
Context: User explicitly wants the MCP to "deliver more than users expect" — to apply electrical-CAD and KiCAD best practices proactively, not merely execute the literal request.
Decision: Domain-knowledge engine is its own layer between session and dispatcher. It pulls from sibling skills (signal-integrity, power-integrity, dfm, etc.), runs pre-save validators, and returns `suggestions` alongside tool results with severity, rule citation, and `why`.
Alternatives considered: Bolt-on post-processor (rejected — weak integration, easy to bypass), per-tool ad-hoc checks (rejected — inconsistent coverage, duplicate logic).
Consequences: Tool outputs are richer than raw KiCAD state. Domain skills become load-bearing runtime assets, not just docs.
Revisit trigger: if suggestion quality degrades (too noisy / too quiet), revisit the strictness model rather than remove the layer.

## ADR-0005: Tools register as plugins via entry points
Date: 2026-04-14
Status: Accepted
Context: Users may want to disable tools, add private tools, or version tool sets independently of the core.
Decision: Tools register through Python entry points. Core discovers, validates schemas, and wires them into the registry at startup.
Alternatives considered: Static registry (rejected — brittle), dynamic file-scan (rejected — non-standard), decorator-based in-core (rejected — can't contribute out-of-tree).
Consequences: Slightly slower startup (entry-point discovery). Tool ecosystem can grow without core releases.
Revisit trigger: if entry-point startup cost becomes a user-visible problem.

## ADR-0006: Pydantic models are the schema source of truth
Date: 2026-04-14
Status: Accepted
Context: MCP requires JSON Schema in tool definitions. Python wants Pydantic for runtime validation. Two hand-written schemas would drift.
Decision: Pydantic v2 models are the source of truth. JSON Schema is generated from them for the MCP surface.
Alternatives considered: Hand-written JSON Schema (rejected — drift), marshmallow (rejected — weaker typing, less ergonomic), dataclasses + manual validators (rejected — reinvents Pydantic poorly).
Consequences: Single source of truth. Automatic validation. Clean error messages. Pydantic dependency baked in.
Revisit trigger: if Pydantic v3 has a breaking migration so large it invalidates the investment.

## ADR-0007: STDIO + HTTP+SSE transports; WebSocket deferred
Date: 2026-04-14
Status: Accepted
Context: Local CLI integration wants STDIO. Remote / long-lived sessions want HTTP+SSE. WebSocket provides bi-directional but adds little over SSE for our access pattern (server mostly responds).
Decision: Ship STDIO and HTTP+SSE. Defer WebSocket until a concrete need emerges.
Alternatives considered: STDIO-only (rejected — no remote), WebSocket now (rejected — unjustified complexity), HTTP polling (rejected — high overhead).
Consequences: Two transports to maintain. Shared handler core keeps divergence low.
Revisit trigger: a use case where the client must push events to the server asynchronously beyond JSON-RPC.

## ADR-0008: Safety defaults — dry-run always available, snapshot before write
Date: 2026-04-14
Status: Accepted
Context: PCB and schematic edits can be expensive to undo. Users want AI-assisted edits without losing work.
Decision: Every mutating tool supports `dry_run=true`. Before any destructive write (file delete, mass rename, library overwrite, schematic structural delete), the server snapshots: git commit if the project is a repo, else a timestamped copy directory.
Alternatives considered: No safeguards (rejected — too risky for the audience), always-manual-confirm (rejected — kills batch workflows), undo-log-only (rejected — insufficient across crashes).
Consequences: Small overhead on writes. Dramatically smaller blast radius on mistakes.
Revisit trigger: if snapshot storage becomes a user complaint; introduce rotation.

## ADR-0009: No hardcoded project conventions anywhere in the core
Date: 2026-04-14
Status: Accepted
Context: Existing MCPs fail the "generic" bar because they encoded their author's project (paths, part numbers, ref-des prefixes). User explicitly flagged this as a reason to start from scratch.
Decision: All paths, library names, reference-designator schemes, net-naming rules, stackup preferences, etc. are config-driven. Built-in defaults match KiCAD's official libraries and widely-adopted industry defaults — nothing else.
Alternatives considered: Opinionated defaults tuned for common cases (rejected — "common" is a trap; someone's project breaks).
Consequences: Larger config surface. Works for any KiCAD project out of the box.
Revisit trigger: if the config surface becomes unwieldy, consider profile presets (named bundles of settings) — never hardcoded defaults.

## ADR-0010: Resources are first-class, cached by (path, mtime, hash), file-watched
Date: 2026-04-14
Status: Accepted
Context: Tools routinely re-read the same schematic/PCB. Parsing large boards is expensive. MCP's resource model maps naturally to addressable project state.
Decision: Expose schematic, PCB, library, netlist, BOM, and DRC/ERC-report as MCP resources. Cache parsed representation keyed on (absolute path, mtime, sha256). Invalidate on file-change via `watchdog`.
Alternatives considered: Stateless (rejected — unacceptable latency), long-lived in-memory state without invalidation (rejected — staleness and leaks).
Consequences: Resource URIs surface consistently across tools. Predictable caching.
Revisit trigger: if hashing cost dominates on very large boards; consider mtime+size only with opt-in hashing.

## ADR-0011: No implementation roadmap in the architecture skill
Date: 2026-04-14
Status: Accepted
Context: User explicitly asked the architecture skill to be *purely architectural* — no phasing, no milestones, no MVP/v1/v2 split.
Decision: `kimcp-architecture` contains zero phasing information. Roadmap (if any) lives outside this skill, e.g., in the implementation repo's `ROADMAP.md`.
Alternatives considered: Include a phased roadmap (rejected — explicit user direction against this).
Consequences: Skill stays stable as phasing and priorities evolve. Architecture decisions do not get polluted by delivery calendars.
Revisit trigger: none — user direction.

## ADR-0012: Hand-rolled JSON-RPC over a tiny transport abstraction; no heavy MCP framework
Date: 2026-04-14
Status: Accepted
Context: MCP frameworks exist but bake in opinions about routing, session, and tool registration that conflict with the plugin-based, versioned-schema, domain-aware design here.
Decision: Implement the JSON-RPC layer ourselves over a 2-method transport interface (`read_message`, `write_message`). Transports (STDIO, HTTP+SSE) implement the interface.
Alternatives considered: Use an MCP server framework (rejected — imposes routing and session conventions that fight the dispatcher and domain-knowledge layer), use a general RPC framework (rejected — MCP has specific semantics).
Consequences: Slightly more code to own. Full control over the seam between MCP semantics and the dispatcher.
Revisit trigger: if the JSON-RPC layer balloons beyond a few hundred lines, reconsider adopting a library for the transport-to-JSON-RPC plumbing only.

## ADR-0013: Every suggestion from the domain-knowledge engine must cite its source rule
Date: 2026-04-14
Status: Accepted
Context: Users need to audit, override, and learn from suggestions. Unsourced advice is worse than no advice.
Decision: Suggestions have `{rule_id, skill, severity, message, why, fix_hint, references[]}`. `skill` points back to the sibling skill that originated the rule (e.g., `signal-integrity:SI-014`). `references[]` may point to datasheets, app notes, or standards (IPC-2221, etc.).
Alternatives considered: Plain-text suggestions (rejected — unauditable), source-only without severity (rejected — can't tune noise).
Consequences: Domain skills must define stable rule IDs. Schema slightly bigger.
Revisit trigger: none expected.

## ADR-0014: Target KiCAD 9+, IPC-first; older versions community-maintained
Date: 2026-04-14
Status: Accepted
Context: User's primary development machine runs KiCAD 10.0. KiCAD 9 (Feb 2025) introduced the gRPC IPC API, which dramatically simplifies mutations against a running editor. Supporting KiCAD 7 / 8 would triple the test matrix, force a full SWIG+S-expr mutation path in parallel with IPC, and slow iteration. Project is open-sourced (Apache-2.0) so older versions can be supported via community PRs without blocking initial release.
Decision: Core targets KiCAD 9+ with IPC as the primary mutation backend. The four-backend dispatcher from ADR-0003 remains, but the selection matrix is IPC-first: IPC preferred for all interactive + mutating ops; CLI for exports; S-expr for read and headless/CI writes; SWIG only fills residual IPC gaps and is expected to shrink each KiCAD release. Minimum supported: KiCAD 9.0.0.
Alternatives considered: Support KiCAD 7+8+9 (rejected — 3× test matrix, dual mutation paths, 7 is EOL), KiCAD 8+9 (rejected — pragmatic middle but still dual mutation path, delays release), KiCAD 10+ only (rejected — too aggressive, excludes the Feb-2025 KiCAD 9 user base).
Consequences: Single mutation backend (IPC) simplifies dispatcher, testing, and documentation. Calendar shortens ~2 weeks. Users on KiCAD ≤ 8 must upgrade or contribute back-port PRs. CI runs a single KiCAD-version matrix (9.x + 10.x).
Revisit trigger: KiCAD 11 ships — evaluate dropping KiCAD 9 support; OR community PRs land back-port support for 8 — evaluate accepting them into a compatibility layer.
Note 2026-04-15 (see ADR-0015): the phrase "gRPC IPC API" above is factually wrong. KiCAD 9's IPC is protobuf-over-nng (pynng Req/Rep), not gRPC. The direction of this ADR (KiCAD 9+, IPC-primary) stands; only the transport specifics were mis-described. ADR-0015 records the correction; do not edit this entry.

## ADR-0015: KiCAD IPC is protobuf-over-nng (pynng), not gRPC; proto sourcing via kicad-python
Date: 2026-04-15
Status: Accepted
Context: ADR-0014 and `backends.md` described the KiCAD 9+ IPC transport as "gRPC client talking to a running KiCAD instance over a local socket." The M5 survey of the official upstream bindings — `kicad-python` 0.6.0 on PyPI, MIT-licensed, released 2026-03-15 — revealed the transport is something else entirely:

- **Nanomsg-next-gen (nng)** over a Unix domain socket (POSIX) or a filesystem path interpreted as a named-pipe-backed socket (Windows), using the **Req/Rep** pattern via the `pynng` package. No gRPC service definitions, no HTTP/2, no channel abstraction.
- **Protobuf-encoded envelopes**: each request is an `ApiRequest { header { kicad_token, client_name }, message: google.protobuf.Any }`; each response is an `ApiResponse { header, status { status: ApiStatusCode, error_message }, message: google.protobuf.Any }`. The inner `Any` carries the per-call command / response type, packed via protobuf's `.Pack()`.
- **Token-based session binding**: the first response carries a `kicad_token` that the client must include in all subsequent requests for that session.
- Socket address is an `ipc://<path>` URI (nng scheme), with `KICAD_API_SOCKET` as the env-var override. Our `kimcp.ipc.socket._ENV_VAR` was `KICAD_API_SOCK` (typo); our platform-default paths were speculative rather than tracking upstream defaults.

Decision:

1. **Transport**: protobuf-over-nng via `pynng.Req0`. No gRPC. Amend `backends.md`, `SKILL.md`, `security.md`, `performance.md`, the README, the IPC backend docstrings, and the `[ipc]` optional-extra dependency list accordingly.

2. **Proto sourcing**: depend on `kicad-python` (MIT) as a runtime dependency in the `[ipc]` extra, and import **only `kipy.proto.*`** — the generated protobuf bindings for envelopes, types, and commands. We do **not** use `kipy.kicad.KiCad`, `kipy.board.Board`, `kipy.project.Project`, or any of their higher-level API wrappers — those impose an API shape that conflicts with our tool / dispatcher / safety / domain-knowledge stack. This gives us:
   - Zero proto-regeneration work on our side (kicad-python publishes pre-built bindings to PyPI).
   - Explicit version coupling: a `kicad-python` version pins a KiCAD version (0.6.0 targets 9.0.7). We bump the `[ipc]` extra when upstream ships new KiCAD support.
   - Clear separation between the **data contract** (theirs, vendored-via-pypi) and the **API surface** (ours).

3. **Version-skew handling**: kicad-python 0.6.0 was built against KiCAD 9.0.7 protos. Users on KiCAD 10.0 (current release as of this ADR) have wire compatibility for stable calls (Ping, GetVersion, GetOpenDocuments, board read ops) but cannot use 10-only additions until kicad-python 0.7 ships. The README documents a "supported KiCAD version matrix"; the `[ipc]` pin bumps each time upstream releases.

4. **Our IPC probe** (`asyncio.open_unix_connection`) remains valid for liveness checks: nng sits on AF_UNIX under the hood, so connection-accepted is sufficient evidence that KiCAD is listening. We do not need to speak nng framing for the probe — that's the real-call path (M5.3+), not the probe path.

Alternatives considered:

- **Vendor compiled protobuf .py files** from kicad-python into our tree. Rejected: machine-generated code is ugly under version control and offers no upside over a PyPI version pin; we'd re-vendor on every KiCAD version anyway.
- **Regenerate protos from KiCAD source ourselves** via `protoc`. Rejected: same maintenance burden as vendoring, plus we take on KiCAD's build-system complexity.
- **Depend on `kicad-python` as the full API surface** (`KiCad`, `Board`, …). Rejected: their abstractions are opinionated in ways that fight our tool model, safety model, and domain-knowledge layer. We'd spend more effort working around them than reimplementing the thin parts we actually need.
- **Stay on the "gRPC" naming despite being wrong**. Rejected: future contributors reading ADR-0014 and `backends.md` would install `grpcio`, get nothing working, and lose days. Accurate architecture documents pay for themselves the first time someone else touches them.

Consequences:

- `[ipc]` extra becomes `kicad-python >= 0.6.0, < 0.7`. That transitively pins `protobuf >= 5.29, < 6` and `pynng >= 0.9.0, < 0.10.0`.
- The `_ENV_VAR` constant in `kimcp.ipc.socket` becomes `KICAD_API_SOCKET` (bug fix, matches kipy / upstream).
- Platform-default socket paths align with kipy's defaults (`/tmp/kicad/api.sock` primary on POSIX, plus flatpak detection on Linux, plus `{tempdir}/kicad/api.sock` on Windows) while retaining our prior speculative paths as secondary fallbacks, so users with unusual installs don't regress.
- ADR-0014's broader direction (KiCAD 9+ only, IPC-first dispatcher) stands unchanged. Only the transport specifics are corrected.
- `[ipc]` pin bumps are now coupled to kicad-python's release cadence, which is itself coupled to KiCAD's. Accept as normal dependency-management work; add a release-hygiene note in the README.
- Test invariant "no `grpc` import at top level" becomes "no `pynng` / `kipy` import at top level" — same idea, correct dep name. Covered by the same test in `tests/unit/test_ipc_backend.py`.

Revisit trigger: KiCAD ships a gRPC gateway alongside the nng API (unlikely near-term); OR `kicad-python` is abandoned / materially diverges from KiCAD, at which point vendor-or-fork becomes necessary and a superseding ADR justifies the switch.

## ADR-0016: Session phase and scope are first-class state
Date: 2026-06-01
Status: Proposed
Context: The literal-execution model — load every skill, run every check, walk the whole design in one shot — produces a slow, opaque experience. Users want to ideate freely with minimal interruption, defer commitment-phase work (errata, vendor, DFM, compliance, simulation) until they actually need it, and work on slices of larger designs without project-wide validation noise. Per-tool ad-hoc flags would scatter this policy across the codebase; a session-layer model centralizes it.
Decision: A KiMCP session carries two pieces of first-class state — `phase` (one of `ideation | validation | commitment | pcb | manufacturing`) and `scope` (a typed slice expression: `project | sheet:<name> | subtree:<root> | section:<tag> | refdes:<pattern> | net:<pattern> | area:<bbox>`). Phase governs which tools fire by default, which skills load, and which suggestion severity floor applies. Scope is inherited by every read / validate / export call unless overridden per-call. Authoring tools (`sch_add_*`) treat scope as a placement hint. Sections are persisted in `<project>/.kimcp/sections.json` and the live session in `<project>/.kimcp/session.json`. Calling an out-of-phase tool returns a `PHASE_DEFERRED` hint with a `park_ref` and an `override` path (`force_phase=true`) — the default protects the loop, it does not lock it. See `workflow.md` for the full contract.
Alternatives considered: Phase as a per-tool argument (rejected — easy to forget, no global policy point, no way to enforce skill-loading discipline). Phase as a config setting only (rejected — needs to change mid-session as the user progresses, config is the wrong layer). Scope as a global filter applied at the dispatcher (rejected — collides with explicit per-call overrides and makes detours hard). No phase model, lean on skill-author discipline to throttle suggestions (rejected — proven brittle; severity floors are a structural fix).
Consequences: Session layer grows two persistent fields plus a sections file. Tools gain optional `scope: str` and `force_phase: bool` parameters. Skills are loaded on demand per phase rather than all-at-once, reducing context and triggering overhead. Out-of-phase calls produce a hint instead of either silently running or silently failing. Audit log gains phase / scope context on every entry. Restoring a snapshot restores the gate cursor too.
Revisit trigger: A workflow emerges that does not fit the linear phase model (e.g. simultaneous PCB and schematic editing as the primary mode) — at that point evaluate whether phase should become a multi-valued set rather than a single value.

## ADR-0017: Gate-based interactive workflow as the default driving model
Date: 2026-06-01
Status: Proposed
Context: A monolithic "do everything from prompt to gerbers" call produces minutes of opaque work the user cannot steer. Cheaper inner-loop iterations need visible motion and decision points so users can catch wrong direction early, inject preferences (vendor, package size, budget) without predicting them upfront, and feel collaborative control. The `confirmation_token` machinery from ADR-0008 / `safety.md` already exists; what's missing is a higher-level orchestration that uses tokens as the joints between work-phases rather than only on destructive ops.
Decision: KiMCP exposes a default interactive workflow as a built-in MCP prompt (`interactive-design`) with named gates G0–G9 (see `workflow.md`). Each gate is a confirmation checkpoint with `{id, question, options, default, why, decides, unlocks, parked}`, backed by a `confirmation_token`. Gates G1–G6 loop per section so multi-section designs feel like a sequence of focused mini-projects rather than one giant pass. Suggestion severity is filtered by phase (`ideation → blocking only`, `validation → +warning`, `commitment+ → full`). The plan, activity stream, and parked items are exposed as MCP resources (`kimcp://session/plan`, `…/activity`, `…/parked`) so clients show progress without polling. Detours ("simulate this now", "errata on U3 now") are first-class and return to the caller phase automatically. Replans are explicit, audit-preserving, and never delete past gate state.
Alternatives considered: Static linear scripts (rejected — brittle when discovery forces a topology change). Free-form "agent decides everything" (rejected — exactly the opaque, slow experience this ADR is designed to fix). One-shot gate at the end ("review everything before commit") (rejected — too late to course-correct, defeats early-catch). Per-tool prompts ("should I add C12?") (rejected — gate-on-actions instead of gate-on-decisions is the failure mode that makes users feel pestered).
Consequences: Default user experience becomes plan-then-execute with ~6–9 checkpoints rather than one opaque pass. Skill loading and web fetches are deferred until the gate that needs them, cutting perceived latency on the ideation loop. New surface area: built-in `interactive-design` prompt, three session resources, gate-token issuance and validation logic. Workflow steps are also discoverable by clients that prefer to step manually (the prompt is expandable into individual stages, per `resources-and-prompts.md`). Tests must cover gate state transitions, replan rollback, and detour return paths.
Revisit trigger: User feedback shows the default gate set is wrong (too coarse — users want sub-gates; or too fine — users skip half). Adjust the gate catalog in `workflow.md` before reaching for a structural change.

## ADR-0018: Optional `scope` parameter contract on all query, validate, and export tools
Date: 2026-06-01
Status: Proposed
Context: Multi-sheet schematics and complex PCBs make project-wide validation noisy and slow. Users want to ERC a single sheet, DRC one region, BOM one section, list nets within a hierarchical subtree. Adding tool-specific arguments for each kind of slice would fragment the surface; a single uniform `scope` contract is reusable across every tool that reads or validates state.
Decision: Every `read`, `mutate` (where meaningful), and `external` tool that operates on schematic or PCB state accepts an optional `scope: str` parameter. The grammar is fixed: `project | sheet:<name> | subtree:<root> | section:<tag> | refdes:<pattern> | net:<pattern> | area:<x,y,w,h>`. Default is the session's current scope. Authoring tools (`sch_add_*`, `lib_*` writes) accept it as a placement hint, not a filter. Validators (`sch_erc`, `pcb_drc`) restrict their analysis to the slice and clearly annotate scope-limited findings in their output. Exporters (`sch_export_bom`, `sch_export_netlist`, `pcb_export_gerbers`) produce partial outputs honoring the scope, with a `meta.scope_applied` field echoing what was honored. The scope grammar is parsed once in the session layer and passed downstream as a structured `ScopeSpec` object so backends do not each reparse strings.
Alternatives considered: Per-tool slice arguments (`sheet`, `section`, `refdes` as separate params) (rejected — combinatorial bloat, inconsistent naming, no shared resolver). Scope only at the session level, never per-call (rejected — common to need a one-shot detour like "ERC the whole project once" without leaving the section scope). XPath-style expressions (rejected — overpowered, hard to learn, hard to render in suggestions). Implicit scope from the last-touched sheet (rejected — magical, surprising, breaks reproducibility).
Consequences: Pydantic `ScopeSpec` model lives in `kimcp.session.scope`; all tool schemas reference it. Validators and exporters must implement the slice efficiently — typically by filtering symbol / net / footprint lists once at entry. Output envelope (`schemas.md`) gains `meta.scope_applied` and `meta.scope_excluded_count` so users see what was skipped. ERC / DRC reports are explicit about partial coverage to avoid false-confidence (a clean scoped ERC is *not* a clean project ERC). Tests must cover the seven scope forms across at least one read, one validate, and one export tool.
Revisit trigger: A scope form is requested often that does not fit the grammar (e.g. "all symbols with footprint matching X"). At that point, extend the grammar with a new form rather than carve out tool-specific arguments.
