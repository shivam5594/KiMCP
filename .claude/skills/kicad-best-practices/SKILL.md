---
name: kicad-best-practices
description: KiCAD-specific workflow and project conventions — libraries (symbol/footprint/3D), project structure, version control, schematic and PCB conventions inside KiCAD, stackup and netclass setup, DRC/ERC workflow, manufacturing outputs, multi-board projects, and common KiCAD pitfalls. Use when using or operating on KiCAD projects, or when KiMCP is authoring/modifying KiCAD files. General electrical-CAD practice lives in `electrical-cad-best-practices` — do not duplicate.
---

# KiCAD Best Practices

KiCAD-specific workflow, file-format, and tool-usage practice. These are the rules the MCP applies when the act happens to be KiCAD-authored. General electrical-CAD principles (schematic style, DFA/DFT, etc.) live in `electrical-cad-best-practices` and specialized technical areas in other sibling skills.

Rule ids in this skill use the prefix `KICAD-`.

## Library management

- **KICAD-001 Official KiCAD libraries as the baseline.** `symbols/`, `footprints/`, `3dmodels/`. Register them once globally; do not re-register per project.
- **KICAD-002 Project-local library for modifications.** `<project>/lib/<project>.kicad_sym` and `<project>/lib/<project>.pretty/`. Never edit the official libraries in place.
- **KICAD-003 One library per vendor/domain at scale.** `power.kicad_sym`, `mcu.kicad_sym`, `connectors.kicad_sym` once the project-local library grows beyond ~50 symbols.
- **KICAD-004 Symbol + footprint + 3D coupled.** When adding a symbol, link a valid footprint; when creating a footprint, attach a 3D model (or explicitly mark "no 3D" in fabrication notes).
- **KICAD-005 Canonical symbol fields.** `Reference`, `Value`, `Footprint`, `Datasheet`, `Description`, `Manufacturer`, `MPN`, `LCSC` (if JLCPCB), `DigiKey`, `Mouser`. Missing `MPN` on a non-generic part is a warning.
- **KICAD-006 Do not rename official-library symbols in a project.** If modification is needed, copy into the project-local library and rename there.
- **KICAD-007 Footprint names match `lib:name` convention.** Fully qualified to avoid ambiguous resolves.
- **KICAD-008 Footprint Courtyard layer present and sized to datasheet.** No courtyard → DFA check fails.
- **KICAD-009 Silkscreen values hidden** on the PCB (Value field hidden by default); reference visible.
- **KICAD-010 Pad 1 marked in copper and silkscreen** on every footprint.
- **KICAD-011 3D model path is relative via `${KIPRJMOD}` or `${KICAD_3DMODEL_DIR}`.** Never absolute.

## Project structure

Recommended layout (not forced — `kimcp-architecture` ADR-0009 prohibits hardcoding):

```
<project>/
  <project>.kicad_pro
  <project>.kicad_sch
  <project>.kicad_pcb
  <project>.kicad_prl
  fp-lib-table
  sym-lib-table
  lib/
    <project>.kicad_sym
    <project>.pretty/
    3d/
  docs/
    README.md
    STACKUP.md
    BOM_NOTES.md
  manufacturing/
    rev_A/
      gerbers/
      drill/
      position/
      bom.csv
      assembly_top.pdf
      assembly_bot.pdf
      fab.pdf
      3d.step
  sim/
  .kimcp/          (snapshots, audit, cache — see safety.md)
  .gitignore
```

- **KICAD-101 Manufacturing outputs versioned by revision** (`rev_A/`, `rev_B/`), never overwritten.
- **KICAD-102 Docs folder has stackup, BOM notes, assembly notes.**
- **KICAD-103 README at project root.** KiCAD version, library dependencies, fab/assembly profile, build instructions.
- **KICAD-104 No absolute paths** in `fp-lib-table` / `sym-lib-table`. Use `${KIPRJMOD}`.

## Version control

- **KICAD-201 Commit `.kicad_sch`, `.kicad_pcb`, `.kicad_pro`, `.kicad_sym`, `.pretty/*.kicad_mod`, `*lib-table`.** All text.
- **KICAD-202 Ignore `.kicad_prl`** (personal layout).
- **KICAD-203 Ignore autosave / backup files.** `*.kicad_sch-bak`, `*.kicad_pcb-bak`, `*-backups/`, `~*`.
- **KICAD-204 Ignore fp-info-cache.**
- **KICAD-205 Tag releases.** Revision tags match the revision stamped in the title block and on the physical PCB.
- **KICAD-206 Atomic commits.** One logical change per commit (placement change, routing change, library update). Schematic + PCB sync should ideally be in one commit.
- **KICAD-207 Git LFS for exported binaries if kept in repo.** Gerbers, PDFs, STEPs. Prefer a separate artifacts repo or release attachments.
- **KICAD-208 Branch for experiments, merge after review.** DRC/ERC clean before merge to main.
- **KICAD-209 Snapshots from KiMCP stored outside git** (`.kimcp/snapshots/`) — `.gitignore` entry.

## Schematic conventions (KiCAD-specific)

- **KICAD-301 `Power Flag` on every power net for ERC.** Otherwise ERC reports false "no power source" warnings.
- **KICAD-302 Hierarchical sheets for blocks > ~40 components.** Name sheet files meaningfully: `power.kicad_sch`, `mcu.kicad_sym`.
- **KICAD-303 Re-annotate after structural change** (add/remove components, move between sheets).
- **KICAD-304 Back-annotation enabled** when PCB-side reordering is done.
- **KICAD-305 Net labels over bus-entries where practical.** Buses are convenient but labels are clearer in review.
- **KICAD-306 Use `Global Labels` for rails and signals shared across sheets**; `Hierarchical Labels` for sheet ports; `Local Labels` within a sheet.
- **KICAD-307 No orphan labels** (a label with no wire) — ERC catches these; do not suppress.
- **KICAD-308 Do not use graphic lines as wires.** ERC will not see them.
- **KICAD-309 Junctions explicit on 4-way intersections.** Default style is unambiguous.
- **KICAD-310 Use BOM fields consistently.** Project-level "BOM" export configured once; custom fields added via Edit Symbol Fields.
- **KICAD-311 Prefer wires over labels for same-sheet connection.** A label is a net-name *reference*, not a connection primitive. For two pins on the same sheet where a clean orthogonal route exists, use a wire — that is what a human follows when reading the schematic. Use labels only when the connection is (a) cross-sheet (global/hierarchical), (b) one of several distinct ground/power domains, (c) an electrically important named signal (CLK, MOSI, SDA, nRESET) where the name aids readability, or (d) a same-sheet net so long that a wire route would be visually noisy. KiMCP's `sch_add_label` emits a meta warning citing this rule when a same-net local label is placed near another within `safety.label_proximity_warn_mm` (default 25 mm) — that pattern should be a wire.
- **KICAD-312 Keep local-label count low per sheet.** A typical sheet should have a single-digit count of local labels. Tens of labels on one sheet is a smell — usually the wire tool was avoided. (Globals and hierarchicals can legitimately be more numerous since cross-sheet connectivity is their job.)
- **KICAD-313 Use bus wires for power nets, not individual symbols per pin.** One GND symbol with a horizontal ground bus at the bottom; one +5V / +3V3 symbol each with horizontal rail wires. Connect component pins to buses with vertical wires and junctions at T-connections. Individual power symbols at every pin create visual clutter.
- **KICAD-314 Never route +5V and +3V3 rails at the same Y coordinate.** Overlapping horizontal wires at the same Y short-circuit the nets. Use different Y levels or ensure non-overlapping X ranges.
- **KICAD-315 PWR_FLAG placement rules.** GND nets and regulator inputs driven only by passive components (connectors, diodes, fuses) need PWR_FLAG. Nets already driven by a `power_output` pin (e.g., AMS1117 VO) must NOT have PWR_FLAG — causes "two power outputs connected" ERC error. Check pin types in the lib_symbol: `power_output` drives the net; `output` (e.g., LM2596 OUT) does not satisfy ERC's power requirement and still needs PWR_FLAG. PWR_FLAG must be wired to the target net with a junction at the connection point.
- **KICAD-316 Use `_Small` symbol variants for kimcp placement.** Standard symbols (C_Polarized, LED, D_Schottky) have 3.81mm pin offsets that don't align with kimcp's 2.54mm grid snapping. Use `_Small` variants (C_Polarized_Small, LED_Small, D_Schottky_Small, D_TVS_Small, Fuse_Small) which have 2.54mm grid-aligned pins.
- **KICAD-317 Spread components generously on first placement; don't cram.** A4 sheets are 297×210mm — use the available space. Target ~15-25mm between functional blocks (input protection, regulator, LDO, indicators) and ~7-10mm between adjacent components within a block. Component references, values, and net labels overlap when symbols are too close (especially `_Small` variants where the body is tight against the pin). **Why:** rebuilding the layout later requires deleting and re-routing every wire/junction — far more work than placing generously from the start. **How to apply:** before placing the first component, sketch a block-level X-axis budget across the full sheet width. For a typical power supply chain (connector → fuse → diode → regulator → LDO → LEDs), allocate ~30-50mm per functional block, not ~15mm. Place the chain across the full usable width (~250mm on A4) rather than packing into the left third.
- **KICAD-318 Plan every coordinate on the schematic grid.** Symbol origins, wire endpoints, junction points, label anchors, no-connect markers, power ports, sheet-box corners — all should land on multiples of the active eeschema grid (100 mil / 2.54 mm by default; honoured project-wide unless `<project>.kicad_pro` overrides it). KiMCP's `sch_add_*` tools snap off-grid inputs to `safety.grid_snap_mm` and emit a meta.warnings entry naming the corrected values, but the agent should *plan* on-grid in the first place — that's what makes pin-to-wire junctions land cleanly, prevents `endpoint_off_grid` ERC warnings, and lets a human eyeball the layout. Pair with KICAD-316 (pick grid-aligned symbol variants) — both rules cover the same goal from different angles.

## Stackup, netclasses, design rules

- **KICAD-401 Stackup defined in `Board Setup → Physical Stackup` before routing.** Dielectric thickness, Er, copper weight.
- **KICAD-402 Netclasses created per signal group.** Typical starter set: `Default`, `Power`, `HS_Digital`, `Diff_100`, `Diff_90` (USB/HDMI subtypes as needed), `RF_50`.
- **KICAD-403 Assign nets to classes immediately after annotation.** Use `Board Setup → Net Classes → Pattern` rules where possible for auto-assignment.
- **KICAD-404 Custom rules file (`<project>.kicad_dru`)** for anything beyond per-netclass widths — e.g., keep-out near crystals, controlled skew.
- **KICAD-405 Fabrication profile drives minimum clearances/widths** (see `dfm` sibling skill). Do not loosen below fab capability.
- **KICAD-406 Differential pair rules set before routing** diff pairs. Width and gap from impedance calculator output.

## Routing

- **KICAD-501 Interactive router push-and-shove mode on** for anything hand-routed.
- **KICAD-502 Route critical signals first** — clocks, differential pairs, RF, reset.
- **KICAD-503 Length-tuning done after neighbors are stable.** Reroute tuning rarely fits when done last.
- **KICAD-504 Diff pairs routed as pairs, not as two nets.** Use `Route Differential Pair`.
- **KICAD-505 Do not cross plane splits.** If unavoidable, add stitching capacitor adjacent to crossing.
- **KICAD-506 Teardrops** (KiCAD 8+) on pads where trace width approaches pad width.
- **KICAD-507 Zones refilled before any DRC / export.** `B` refills; DRC on stale zones reports spurious clearances.
- **KICAD-508 Do not place traces in silkscreen-only layers** or board-edge zone.
- **KICAD-509 Via stitching near high-speed signals** for return-path continuity.

## DRC / ERC workflow

- **KICAD-601 ERC run and reviewed before first PCB layout.**
- **KICAD-602 DRC run and reviewed at every design milestone.** No exceptions for "small changes".
- **KICAD-603 DRC clean before manufacturing.** Any remaining violation requires written waiver in `docs/drc_waivers.md`.
- **KICAD-604 DRC includes solder-mask, silkscreen, courtyard, unconnected items, schematic parity.** Do not disable categories wholesale — disable specific rules with reasons.
- **KICAD-605 Refill zones before DRC.**
- **KICAD-606 Sync schematic to board** regularly; resolve differences, do not accumulate.
- **KICAD-607 "Update PCB from Schematic"** safer than "Update Schematic from PCB" for structure; back-annotation only for reference renumbering.

## Manufacturing outputs

- **KICAD-701 Gerber format: X2** (metadata-rich, preferred by modern fabs). Protel extensions when fab requests.
- **KICAD-702 Standard set**: `F.Cu`, `B.Cu`, inner copper layers, `F.Mask`, `B.Mask`, `F.Paste`, `B.Paste`, `F.Silkscreen`, `B.Silkscreen`, `Edge.Cuts`. Fab-specific drill & position files alongside.
- **KICAD-703 Drill file format: Excellon 2** with metadata. Separate PTH and NPTH.
- **KICAD-704 Position file**: CSV, mm, side per file for assembly houses that prefer that layout.
- **KICAD-705 Render fabrication drawing PDF** with stackup, notes, dimensions, hole chart.
- **KICAD-706 3D STEP exported** for mechanical check. Verify origin / flip before sending.
- **KICAD-707 Interactive BOM HTML generated** and shipped with the revision archive.
- **KICAD-708 Readme in manufacturing folder** with fab profile, panelization, special notes.
- **KICAD-709 Zip the manufacturing folder per revision**; store the hash in git.

## Multi-board projects

- **KICAD-801 One `.kicad_pro` per board.** KiCAD 8+ supports multi-board but one project per board remains the safest pattern.
- **KICAD-802 Shared libraries across boards** pulled from a common `lib/` at the parent directory.
- **KICAD-803 Connector pinouts defined in one place** (a shared schematic or doc) and referenced from each board.
- **KICAD-804 Inter-board nets documented.** Cable definitions in `docs/interconnect.md`.

## Pitfalls and gotchas (KiCAD-specific)

- **KICAD-901** Spice simulation requires model attachment on each relevant symbol — missing models fail silently as ideal parts.
- **KICAD-902** Net-name changes in PCB do not propagate to schematic without explicit back-annotation. Prefer schematic-as-source.
- **KICAD-903** `Edit Symbol Library Links` can relink a symbol across the whole schematic — powerful and dangerous. Snapshot first.
- **KICAD-904** Zone fills become stale after edits. Always `Edit → Fill all zones` before DRC and before export.
- **KICAD-905** Silkscreen over pads is silently clipped by default. Turn on "silkscreen over pads" DRC.
- **KICAD-906** Hidden pins on power nets need `Power Flag` for ERC.
- **KICAD-907** Imported Gerbers into GerbView do not round-trip to `.kicad_pcb`.
- **KICAD-908** 3D model alignment is per-footprint, not per-instance. Wrong rotation in library = wrong in every board.
- **KICAD-909** `kicad-cli` path differs per platform; never hardcode, probe at runtime.
- **KICAD-910** IPC API has version-specific endpoints; check server version before selecting API features.
- **KICAD-911** Library tables can diverge between global and project — `fp-lib-table` and `sym-lib-table`. Prefer project-level for portability.
- **KICAD-912** `annotate` uses current settings for start number; reset them for new projects or sheets are numbered oddly.
- **KICAD-913** kimcp-embedded symbols using `extends` fail pin resolution. When kimcp embeds a derived symbol (e.g., `LM2596S-5` extends `LM2596S-12`), the base gets library-prefixed (`Regulator_Switching:LM2596S-12`) but the `extends` reference stays unqualified (`LM2596S-12`), so KiCad cannot resolve pin positions. Fix: flatten the symbol — copy the base's sub-symbols (`_0_1`, `_1_1`) into the derived symbol, rename them to match the derived name, override properties (Value, Description, etc.), and remove the `extends` line.

## KiMCP-specific practices when touching KiCAD files

- **KICAD-A01 Before any write, snapshot** (see `kimcp-architecture:safety.md`).
- **KICAD-A02 Round-trip validation on S-expression writes.** Read back, byte-diff in structural form, fail loudly if unexpected differences.
- **KICAD-A03 Respect KiCAD version recorded in `.kicad_pro`.** Do not silently upgrade file-format version.
- **KICAD-A04 Do not modify library files while in use** by an open KiCAD process; prefer IPC write through the app or require the user to close the editor.
- **KICAD-A05 Preserve comments and formatting** where possible in S-expressions (round-trip identity for unaffected sections).
- **KICAD-A06 Always use `sch_add_symbol` for multi-pin components.** KiCad 8+ requires `(pin "N" (uuid "..."))` entries in every symbol instance for each pin. Only `sch_add_symbol` generates these. Without them, wires touching pin positions show as unconnected in ERC. If a symbol was placed without pin entries (e.g., by manual S-expression editing), delete it with `sch_delete` and re-place with `sch_add_symbol`.

## KiCAD plugin ecosystem

Known-good plugins for KiCAD 7 / 8 / 9. KiMCP treats these as optional adapters — present if installed, ignored otherwise.

- **KICAD-B01 InteractiveHtmlBom (iBOM)** by `openscopeproject`. Self-contained HTML BOM with hover-to-highlight on rendered board; standard deliverable for assembly. Install via PCM (Plugin and Content Manager) or git.
- **KICAD-B02 PcbDraw** by `yaqwsx`. Generates stylized top/bottom PCB renders (SVG/PNG) for documentation; component styles resolvable from MPN.
- **KICAD-B03 JLC-Tools / Fabrication Toolkit** by `bennymeg`. One-click JLCPCB-ready Gerber + drill + BOM + CPL export; handles rotation offsets that differ between KiCAD and JLC pick-and-place.
- **KICAD-B04 KiBot** by `INTI-CMNB`. CI-driven fabrication output pipeline; declarative YAML config; integrates Interactive HTML BOM, PcbDraw, DRC, ERC. Canonical for automated manufacturing-handoff.
- **KICAD-B05 KiCost** by `xesscorp`. BOM pricing across distributors; updates fat BOM from Digi-Key / Mouser / etc. Less maintained — evaluate before adopting.
- **KICAD-B06 KiKit** by `yaqwsx`. Panelization + fabrication scripting; replaces hand-panelization for arrays, V-score, mouse-bites.
- **KICAD-B07 Freerouting (CLI)** as autorouter; KiCAD 8+ integrates via DSN/SES round-trip. Not a substitute for hand routing on critical nets; useful for bulk nets.
- **KICAD-B08 Library loaders.** `kicad-lib-utils` for symbol/footprint diffing; `footprint-generator` by TerminalDiscipline for parametric IPC-7351 generation.
- **KICAD-B09 STEP exporter via `kicad-cli`** — no plugin needed; `kicad-cli pcb export step` is canonical.
- **KICAD-B10 Plugin trust model.** Plugins run Python in the KiCAD process — treat them like any third-party dependency (review, pin versions). KiMCP does not auto-install plugins.
- **KICAD-B11 Plugin / PCM version pinning** per project. Manufacturing-output reproducibility depends on plugin version; record in `docs/toolchain.md`.

## Custom DRC (.kicad_dru) syntax

KiCAD 7+ supports custom design rules as a rich DSL. Rules live in `<project>.kicad_dru` alongside the board file.

- **KICAD-C01 Syntax is S-expressions.** `(version 1)` header; each rule a `(rule "name" (condition "...") (constraint ...))` block.
- **KICAD-C02 Conditions select items.** Examples: `(condition "A.NetClass == 'Power'")`, `(condition "A.intersectsCourtyard('U5')")`, `(condition "A.NetName == '/USB_D_P'")`. Full expression DSL in KiCAD docs.
- **KICAD-C03 Constraints express the check.** `(constraint clearance (min 0.3mm))`, `(constraint track_width (min 0.2mm) (opt 0.25mm))`, `(constraint diff_pair_gap (min 0.15mm))`, `(constraint length (min 100mm) (max 150mm))`, `(constraint hole_to_hole (min 0.25mm))`.
- **KICAD-C04 Layer-scoped rules** via `(layer "F.Cu")` or `(layer outer)` / `(layer inner)`.
- **KICAD-C05 Named component selection.** `A.getField('MPN') == 'STM32F446RET6'` enables per-part rules.
- **KICAD-C06 Keep-out zones as rules**: `(condition "A.insideArea('crystal_keepout')") (constraint disallow track via pad)` enforces no copper inside a named zone.
- **KICAD-C07 High-voltage spacing** via rule, keyed on NetClass or NetName, enforcing creepage (see `high-voltage-design:HV-001..HV-007`).
- **KICAD-C08 Version the `.kicad_dru` file in git** — treat it as part of the board's DRC contract.
- **KICAD-C09 Document each rule's reason** in a comment above the rule block; otherwise DRC failures in a year have no provenance.
- **KICAD-C10 DRU rules are additive to built-in DRC** — they tighten or add constraints, they do not loosen built-ins. To loosen built-ins, adjust Board Setup defaults.
- **KICAD-C11 KiMCP generates DRU rules** from the fab profile + netclass + HV declarations (see `dfm:DFM-080`, `high-voltage-design:HV-001`). Generated rules carry a `; source: kimcp` comment.

## Simulation workflow (KiCAD-side specifics)

Workflow details particular to KiCAD's ngspice integration. Broad simulation patterns in `simulation-workflow`.

- **KICAD-D01 Simulation model on each symbol.** `Sim.Device`, `Sim.Type`, `Sim.Pins`, `Sim.Params`, `Sim.Library`, `Sim.Name` fields; set via Symbol Properties → Simulation Model.
- **KICAD-D02 ngspice embedded in eeschema.** Invoked from schematic editor → Inspect → Simulator. Runs `.cir` generated from the schematic.
- **KICAD-D03 Model files** in `<project>/sim/models/` or referenced by absolute env-var path. Track subcircuit `.lib` files via git (small text).
- **KICAD-D04 Power sources** via `Simulator_SPICE.kicad_sym` library or project-local symbols. Every analysis needs at least one excitation.
- **KICAD-D05 Analysis profile** saved in the schematic's simulation settings; reload consistently.
- **KICAD-D06 Probes on nets** — right-click net → Probe Signal — rather than node numbers; nets are stable under schematic edits.
- **KICAD-D07 SPICE model fields exported** as part of BOM to document which models each part uses.
- **KICAD-D08 KiCAD-ngspice limitations.** No mixed-signal (analog + digital) out of the box; no AMI; no transient-with-frequency-parameters. For beyond-ngspice use external tools (see `simulation-workflow:SIM-035`).
- **KICAD-D09 Preserve simulator settings across KiCAD upgrades.** Sim-settings schema occasionally changes between major versions; back them up before upgrading.
- **KICAD-D10 Regression run on each commit** via KiBot → ngspice; fail CI if key metrics drift out of bounds.

## Team / shared-library servers

Patterns for teams sharing symbols, footprints, 3D models, and design rules.

- **KICAD-E01 Git repository for shared libraries.** Submodule or subtree into each project's `<project>/shared-lib/`. Tagged releases consumed explicitly.
- **KICAD-E02 Monolithic library vs federated.** Small teams: one shared library repo with all symbols/footprints/3D. Large teams: per-domain repos (passives, connectors, MCUs, power) federated.
- **KICAD-E03 Review gate on library changes.** Pull-request on every symbol/footprint add; mandatory: datasheet link, IPC-7351 variant, source of truth, 3D model attached.
- **KICAD-E04 KiCad Plugin and Content Manager (PCM) repositories** for publishing internal plugins / libraries to team machines.
- **KICAD-E05 Global library tables not team-synchronized.** Per-project `fp-lib-table` and `sym-lib-table` point to shared-lib via `${KIPRJMOD}/shared-lib/...`.
- **KICAD-E06 Environment variables for shared paths** — `${COMPANY_LIB}` pointing at an NFS / Dropbox / S3-sync path; risky on multi-OS teams; prefer git submodule.
- **KICAD-E07 Footprint / symbol linting in CI** — hooks that run IPC-7351 checks, datasheet-link validation, courtyard presence on every PR.
- **KICAD-E08 Versioning of shared libraries.** Semver tags. Projects pin a specific tag; upgrades deliberate, not silent.
- **KICAD-E09 Library access control.** Read-only for engineers, write via PR. Admins merge; break-glass with approvals for urgent fixes.
- **KICAD-E10 Backup schedule** for the library repo; critical IP.
- **KICAD-E11 Private symbols for NDA parts** kept in a separate private repo with stricter access control.
- **KICAD-E12 Plugin-based library server.** InvenTree, Altium-365-style systems; integrate via custom plugin if team scale demands; KiMCP does not mandate.
