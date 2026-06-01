# Backend Dispatcher

Minimum supported KiCAD: **9.0.0** (per ADR-0014). IPC is the **primary** mutation backend; SWIG is a shrinking residual. Older KiCAD versions are out of scope for the core; community back-ports live behind a compatibility shim, not in the default dispatcher.

## The four backends

### 1. IPC API (KiCAD ≥ 9) — primary mutation backend
- **Protobuf-over-nng** client (nanomsg-next-gen Req/Rep via `pynng`) talking to a running KiCAD instance over a local socket (Unix domain socket on POSIX, file-backed named-pipe socket on Windows). See ADR-0015 for why this is not gRPC.
- Wire format: `ApiRequest { header, message: google.protobuf.Any }` → `ApiResponse { header, status, message: Any }`. Token-based session after the first round-trip.
- Two-way sync — edits show up live in the user's open window.
- **Best for**: interactive edits, cross-probing, anything a user is watching — *and* per ADR-0014, the default for any mutating op when a KiCAD process is reachable.
- **Caveats**: requires a running KiCAD process with the API server enabled (Preferences > Plugins); proto contract coupled to KiCAD version (we pin via `kicad-python` in the `[ipc]` extra — see ADR-0015); per-call latency; residual feature gaps filled by SWIG (see §4) — gaps expected to shrink each KiCAD release.

### 2. `kicad-cli`
- Ships with KiCAD. Subcommands: `sch export`, `pcb export`, `pcb drc`, `sch erc`, `pcb render`, `version`, etc.
- Headless, process-per-invocation.
- **Best for**: batch exports, CI, DRC/ERC reports, manufacturing outputs, anything "give me a file".
- **Caveats**: no partial edits; one file out per call; startup cost on each invocation.

### 3. S-expression parser (ours)
- Reads and writes `.kicad_sch`, `.kicad_pcb`, `.kicad_sym`, `.kicad_mod`, `.kicad_pro`, `.kicad_dru`, `.kicad_wks` as structured data.
- Pure Python by default. Hot paths optionally in Rust via PyO3 (justify with profile data — see ADR-0002).
- **Best for**: bulk edits (rename-all, net-class reassign), fast reads, headless refactors, round-tripping small files.
- **Caveats**: must track KiCAD file-format versions; GUI is not updated during edit (user must reload if KiCAD is open on that file).

### 4. SWIG `pcbnew` — residual gap-filler
- Legacy Python bindings embedded in pcbnew.
- In-process with pcbnew.
- **Best for**: the shrinking set of features IPC API hasn't filled in yet (certain zone operations, some footprint-editor ops). Per ADR-0014, SWIG is never preferred when IPC can do the job.
- **Caveats**: deprecated direction; avoid when alternatives exist; unstable across KiCAD versions. Each KiCAD release should shrink this backend's footprint; schedule a dispatcher-matrix review per release to move ops off SWIG as IPC fills gaps.

## Selection matrix (defaults)

Selection order per operation (first available wins). Tools may override. Per ADR-0014, the defaults below are **IPC-first for interactive mutation**, **CLI for canonical exports**, **S-expr for reads and headless/CI writes**, **SWIG only when nothing else works**.

| Operation | Preferred | Fallback(s) | Notes |
|---|---|---|---|
| Read schematic / PCB structure | S-expr parser | IPC | Speed wins; no GUI sync needed |
| Interactive component move | IPC | — | Must be live-visible |
| Bulk rename net / net-class reassign (**headless / CI**) | S-expr parser (with snapshot) | IPC | Speed + atomicity; CI path |
| Bulk rename net / net-class reassign (**GUI open**) | IPC | S-expr parser + reload-prompt | ADR-0014: keep the user's editor in sync |
| Run DRC | kicad-cli | IPC | Headless report |
| Run ERC | kicad-cli | IPC | Headless report |
| Export Gerber / drill / position | kicad-cli | IPC | Canonical outputs |
| Export 3D (STEP / VRML) | kicad-cli | IPC | Canonical outputs |
| Render PDF / SVG | kicad-cli | IPC | — |
| Add component to board | IPC | SWIG (until IPC covers case) | IPC primary per ADR-0014 |
| Edit zone boundary | IPC | SWIG (until IPC covers case) | IPC primary per ADR-0014 |
| Footprint-editor ops (pad geom, etc.) | IPC | SWIG (until IPC covers case) | IPC primary per ADR-0014 |
| Create project from template | file ops + templates | IPC | Pure file operation |
| Library management — interactive | IPC | S-expr parser | Keeps KiCAD's library-in-editor coherent |
| Library management — bulk / CI | S-expr parser | IPC | Headless-write carve-out |
| Simulation | ngspice subprocess | — | Separate tool chain |
| Autorouting | `freerouting` subprocess (via DSN/SES) | — | External tool |
| Annotation | IPC | S-expr parser | Users often have editor open |
| Apply design rules (`.kicad_dru`) | S-expr parser | IPC | File-level config |

Live-GUI-visible operations default to IPC; when IPC is unavailable, the tool either:

1. refuses with a clear error, or
2. proceeds headlessly and returns a `live_sync=false` warning in the result.

The choice is per-tool and declared in its metadata. Per ADR-0014, IPC is reachable whenever a KiCAD 9+ process exposes the API socket; the dispatcher probes this once per session and again on explicit reconnect.

## Dispatcher contract

Each tool declares in its metadata:

```
required_backends: set[Backend]       # backends that CAN service this op
preferred: list[Backend]              # ordered preference
live_gui_visible: bool                # does this need to show in the GUI?
mutates: bool                         # does this write persistent state?
destructive: bool                     # irreversible without a snapshot?
```

Dispatcher logic:

1. Probe available backends (IPC reachable? CLI present? SWIG importable?). Cache per-session.
2. Walk `preferred`; pick first available.
3. If `live_gui_visible` and IPC unavailable:
   - If `mutates` and a non-IPC backend can do it → proceed, emit `live_sync=false` warning.
   - Else → return error telling the user how to start KiCAD / enable the API.
4. If `destructive`, ensure snapshot is taken before delegating (see `safety.md`).

## Per-KiCAD-version backend matrix

Maintain a matrix of `(KiCAD version, backend, supported operations)` in `tests/backend_support_matrix.md` in the impl repo. Integration tests run across matrix entries for supported platforms. Per ADR-0014, the default CI matrix covers **KiCAD 9.x and 10.x** only; community-contributed back-port entries may add rows behind a compat-shim flag but are not part of the default support commitment.

## Adding a new backend

1. New ADR in `DECISIONS.md` justifying the addition (why existing four don't cover it).
2. Implement the backend adapter with a uniform interface.
3. Extend selection matrix above.
4. Add integration tests.
5. Update every affected tool's `required_backends` / `preferred`.

## Removing a backend

Only via a superseding ADR. Minimum deprecation window: two KiCAD release cycles, with visible warnings in tool outputs during the window.
