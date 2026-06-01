---
name: 3d-models-and-footprints-search
description: Find, validate, and integrate PCB footprints and 3D models for components — authoritative sources (KiCAD, vendor, Ultra Librarian, SnapEDA, manufacturer STEP), IPC-7351 validation, footprint-vs-datasheet consistency check, 3D-model origin/orientation/scale correctness, KiCAD library integration. Use when creating or selecting a footprint/3D model, when a BOM item has no library asset, or when verifying a library asset against the datasheet. Pairs with `datasheet-search` and `kicad-best-practices`.
---

# 3D Models & Footprints Search

Find, validate, and integrate footprints and 3D models for PCB components. Rule ids use prefix `LIB-`.

## When to use

- New MPN with no existing footprint in the project library.
- Verifying a library footprint matches the datasheet before committing a layout.
- Assigning 3D models for mechanical fit check.
- Migrating assets from external tools (Altium, Eagle, SnapEDA) into KiCAD.

## Sources (ranked)

- **LIB-001 Official KiCAD libraries.** First choice — curated, IPC-7351 compliant, well-tested. Covers most generic passives, connectors, and popular ICs.
- **LIB-002 Manufacturer CAD files.** Many manufacturers publish CAD assets (symbols, footprints, STEPs) on the product page. TI, ADI, Microchip, NXP, STMicro, Renesas, Infineon, ON Semi. Usually in Altium/Eagle/PADS/OrCAD and STEP — needs conversion.
- **LIB-003 Ultra Librarian.** Manufacturer-authorized central repository. KiCAD export available. Quality generally high but occasionally footprint details need verification.
- **LIB-004 SnapEDA.** Large community library; KiCAD export. Mixed quality — validate against the datasheet before committing to production.
- **LIB-005 Digi-Key / Mouser CAD asset links.** Often redirect to Ultra Librarian or SnapEDA.
- **LIB-006 Vendor-specific project libraries.**
  - Espressif maintains KiCAD libraries for ESP chips.
  - Raspberry Pi maintains KiCAD libs for RP2040.
  - Nordic has partial KiCAD assets.
- **LIB-007 Community KiCAD libraries.** `kicad-footprints`, `kicad-symbols`, `kicad-3d-models` on GitHub + various personal/vendor-specific repos. Verify each asset; community quality varies.
- **LIB-008 Build from scratch** using IPC-7351 generator or manual dimensioning from the datasheet. Always an option; sometimes faster than fixing a bad import.
- **LIB-009 GrabCAD / general CAD sites** for 3D models when no electrical asset ships with the part. Use only if the mechanical model matches the MPN's exact package.

## Footprint validation (LIB-010 series)

- **LIB-010 IPC-7351 dimensional compliance.** Pad dimensions derived from datasheet lead size + clearance, per IPC-7351 Nominal (N), Least (L), or Most (M) per use case.
- **LIB-011 Pitch match.** Imported footprint's pin-to-pin pitch matches the datasheet to ± 0.01 mm.
- **LIB-012 Pin 1 identified** in silkscreen (dot or chamfer) *and* in copper (square pad or smaller mark). Fabrication layer outlines pin 1 as well.
- **LIB-013 Courtyard present and sized**: IPC-7351 N = pad boundary + 0.25 mm, L = + 0.1 mm, M = + 0.5 mm.
- **LIB-014 Silkscreen outside the courtyard**, aligned to body, not pads.
- **LIB-015 Fab layer outline matches component body** for assembly drawing clarity.
- **LIB-016 Thermal pad present** for QFN/PQFN/DFN packages; paste mask for thermal pad is window-paned (not a single opening) per IPC-7525 to avoid tombstoning.
- **LIB-017 Pad shapes**: rectangular with rounded corners preferred; oval for through-hole to discourage cracking; stencil-compatible.
- **LIB-018 Hole and pad sizes** for through-hole per IPC-7251: drill = lead diameter + 0.2 mm; pad = drill + 0.5 mm.
- **LIB-019 Polarized components' orientation marks** match datasheet's convention (positive terminal for electrolytics, cathode for diodes).

## 3D model validation (LIB-020 series)

- **LIB-020 Origin coincides with the footprint origin.** When inserted into the footprint in KiCAD, the model sits on the correct pad cluster without offset.
- **LIB-021 Z=0 at the board surface.** A model whose baseline is below Z=0 intersects the board; above — floats.
- **LIB-022 Orientation matches footprint's pin-1 convention.** Rotate in KiCAD to align, once, in the footprint editor — not per-instance on the PCB.
- **LIB-023 Scale correct.** STEP usually exports in mm, but some sources export in inch; verify bounding box against datasheet package dimensions.
- **LIB-024 Package variant correct.** A "TQFP-32" model is not interchangeable with "TQFP-48"; verify lead count.
- **LIB-025 Model detail level appropriate.** Enough detail for mechanical clearance check; overly detailed models slow the 3D viewer unnecessarily.
- **LIB-026 File format.** STEP (`.step` or `.stp`) for KiCAD's native 3D system. VRML (`.wrl`) acceptable but lacks mechanical-cad fidelity; useful for 3D renders only.
- **LIB-027 Material / color hints** aesthetic, not critical. Keep simple materials for render speed.

## Cross-consistency

- **LIB-030 Footprint vs 3D model**: bounding box of the 3D must cover the footprint body outline (fab layer) and stay inside the courtyard.
- **LIB-031 Footprint vs datasheet**: overlay the library pad pattern on the datasheet's recommended land pattern — all lands match.
- **LIB-032 Symbol vs footprint pin count and pin names**: pin 1 on the symbol matches pin 1 on the footprint (KiCAD matches by pin number — cross-check name, too).
- **LIB-033 Symbol vs datasheet pin assignments**: manually verify the first time a symbol is adopted from a third-party library; errors are common.

## KiCAD integration

- **LIB-040 Place 3D model files** under `<project>/lib/3d/` or a top-level shared `3d/`. Reference via `${KIPRJMOD}/lib/3d/...` or `${KICAD_3DMODEL_DIR}/...`.
- **LIB-041 Never use absolute paths** in `.kicad_mod` `(model ...)` — breaks portability.
- **LIB-042 Place footprint files** under `<project>/lib/<project>.pretty/<name>.kicad_mod`. One footprint per file is the KiCAD convention.
- **LIB-043 Place symbol additions** into `<project>/lib/<project>.kicad_sym`. One library file per project is acceptable; split by domain if grows large.
- **LIB-044 `fp-lib-table` and `sym-lib-table` entries** point to the project library via `${KIPRJMOD}`.
- **LIB-045 Never overwrite the official KiCAD libraries in place**; always copy into the project library first (see `kicad-best-practices:KICAD-002`).

## Workflow — "new part, no assets"

1. `find_footprint(mpn)`: search official → manufacturer → Ultra Librarian → SnapEDA → community → "build".
2. `find_3d_model(mpn)`: same order.
3. If found: `validate_footprint_vs_datasheet(mpn)` runs LIB-010..LIB-019 with datasheet facts from `datasheet-search`.
4. If validated: copy into project library, rename per project conventions.
5. If not validated: fix the footprint (document the changes) or build from scratch.
6. Attach 3D model; run LIB-020..LIB-027 checks in KiCAD's 3D viewer (or via IPC API render).
7. Final: cross-consistency checks LIB-030..LIB-033.
8. Commit to project library with a note in `lib/README.md` citing source + validation date.

## Workflow — "verify existing part"

1. Load the symbol, footprint, and 3D model from the current library.
2. Pull `datasheet-search:find_datasheet(mpn)` facts.
3. Run the entire validation suite; produce a report.
4. If any rule fails, stop and fix before layout depends on this part.

## Data structures

```
FootprintValidation {
  mpn: str,
  footprint_source: "kicad-official" | "manufacturer" | "ultra-librarian" | "snapeda" | "community" | "custom",
  footprint_source_url: str,
  datasheet_rev: str,
  ipc7351_variant: "N" | "L" | "M",
  checks: [
    { rule_id: "LIB-010", passed: bool, detail: str }
    ...
  ]
}

ThreeDModelValidation {
  mpn: str,
  model_source: ...,
  model_source_url: str,
  format: "step" | "wrl" | "iges",
  bbox_mm: { x, y, z },
  datasheet_bbox_mm: { x, y, z },
  checks: [ ... ]
}
```

## Mechanical keep-outs from 3D

Derive keep-out geometry from the 3D model for downstream layout rules. These complement courtyard (2D DFA) with true-3D exclusion for components below and above the board. Pairs with `mechanical-integration:MECH-060..MECH-065`.

- **LIB-050 Body-shadow keep-out.** Project the 3D component body onto the board; no other components may occupy the footprint area on the same side above a threshold height.
- **LIB-051 Height-zone keep-out per region.** A region under the enclosure lid has a max height; components taller than the zone violate. Derived from 3D height field.
- **LIB-052 Bottom-side keep-outs from standoffs and bosses.** Standoffs project from the enclosure into board space on the non-component side — components in the standoff's shadow are forbidden.
- **LIB-053 Moving-part clearance zones** around buttons, switches, levers, hinges — swept volume drives the keep-out; not a static cuboid.
- **LIB-054 Thermal keep-out.** Hot components push an exclusion radius (depending on their thermal field) on nearby temperature-sensitive parts. Derived from BoM dissipation + spacing rules.
- **LIB-055 Connector mating keep-out.** Connector body + cable exit cone + mating-cycle insertion path all keep-out zones; mating connector / cable may not collide during insertion, seating, and unmating.
- **LIB-056 Fastener and tool keep-out.** Mounting screws need driver clearance; washer diameter + drive-bit swept zone on assembly side.
- **LIB-057 Keep-out exported to KiCAD layers** — `User.1`, `User.2`, `Eco1.User`, `Eco2.User` conventionally. Named for the source (e.g., `keepout_enclosure_lid`, `keepout_button_swept`).
- **LIB-058 Keep-out serialization** in a project-level file (`docs/keepouts.yaml`) with reason, source, height, shape; `.kicad_dru` rules generated from it.
- **LIB-059 3D clearance check tool** — runs the assembly STEP (PCB + components + enclosure) for intersections. Fails with highlighted parts if any 3D body overlaps a declared keep-out.

## Connector mating

Connectors are a frequent source of design errors — wrong mate, wrong orientation, wrong height. The footprint + 3D must encode enough to verify.

- **LIB-060 Mating connector identified** for every PCB-side connector. Store both MPNs; validation checks mating compatibility at library-load time.
- **LIB-061 Mating direction in 3D model.** A right-angle vs vertical connector determines cable exit direction. The 3D must reflect the chosen orientation.
- **LIB-062 Polarization features modeled.** Key slots, guide pins, latch mechanisms — needed for collision-check of mating sequence.
- **LIB-063 Connector stack height** under mated state, not unmated. Some connectors shrink / grow on mating; datasheet gives both numbers.
- **LIB-064 Cable side of the connector** modeled if relevant — an overmold or strain-relief boot has its own 3D footprint that may collide with neighbors.
- **LIB-065 Board-to-board mating.** Stackable connectors (Samtec, Molex PanelMate) — PCB-to-PCB clearance stacks from the connector pair height. Both boards' 3D models must reflect it.
- **LIB-066 FFC / FPC actuator swept volume.** Flipped actuators swing open — collision-check the fully-open position vs neighbors.
- **LIB-067 Retention mechanisms.** Latches, screws, bayonet; 3D includes the operated state envelope for retention check.
- **LIB-068 Multiple instances of the same connector family** (e.g., several USB-A side-by-side) — pitch + body clearance per datasheet. Sometimes smaller than one would assume; check.
- **LIB-069 Cable exit routing** traced in the 3D assembly — minimum bend radius observed (see `mechanical-integration:MECH-041`).
- **LIB-070 Connector 3D validation rules:**
  ```
  - mated_height ≤ max_stack_budget
  - mating_cone clear of obstructions within X mm
  - pin1_orientation matches footprint
  - actuator_swept volume clear
  - cable_exit direction matches enclosure opening
  ```

## Enclosure fit

Coupling the board to a mechanical enclosure requires fit checks at library-load and layout-finalize. Pairs with `mechanical-integration:MECH-010..MECH-015`.

- **LIB-080 Enclosure STEP imported as reference** into the project; stored under `<project>/mechanical/enclosure.step`. Origin aligned to board coordinate system (agreed with mechanical CAD; see `mechanical-integration:MECH-071`).
- **LIB-081 Board outline from mechanical, not hand-drawn.** DXF or STEP outline imported to `Edge.Cuts` (see `mechanical-integration:MECH-001`).
- **LIB-082 Mounting-hole positions fixed by enclosure.** Position tolerance ±0.1 mm typical; tighter for CNC metal enclosures.
- **LIB-083 Tallest-per-region map generated** from BoM height fields + footprint placements; compared to enclosure-lid clearance.
- **LIB-084 Connector / button / LED positions locked to enclosure openings** with ±0.25 mm typical. Verified via 3D overlay.
- **LIB-085 Standoff clearance zones** documented at library level — the footprint of a standoff's insertion axis rules out components in its shadow on both sides.
- **LIB-086 Acoustic clear path** for speakers / microphones — no component / trace routed through the acoustic channel between board and enclosure opening.
- **LIB-087 Antenna keep-out coordinated with RF** — some antenna clearance is mechanical (plastic-only above antenna) — see `rf-design:RF-030..RF-034`.
- **LIB-088 Enclosure-revision impact tracked.** When mechanical CAD ships a revision, re-run fit checks and log the delta (see `mechanical-integration:MECH-073`).
- **LIB-089 Fit report artifacts**: overlay STEP, per-component clearance distances, max-compression on any deformable gasket / thermal pad, worst-case tolerance stack-up. Stored under `<project>/mechanical/fit_rev_X.md` + STEP.
- **LIB-090 Fit check blocking on manufacturing-handoff.** Any LIB-050..LIB-088 violation marks the handoff workflow as requiring resolution or a documented waiver.

## How the engine uses this skill

- `find_footprint(mpn)` / `find_3d_model(mpn)` — returns candidate assets ranked by source priority + validation pre-pass.
- `validate_footprint_vs_datasheet(footprint_path, mpn)` — full validation suite.
- `install_asset(source, project_library)` — copy asset into the project library, rename, update tables.
- `compute_keepouts_from_3d(component)` — derives LIB-050..LIB-058 from the component 3D and exports keep-out zones.
- `check_mating(connector_mpn, mating_mpn)` — validates LIB-060..LIB-070.
- `check_enclosure_fit(project, enclosure_step)` — runs LIB-080..LIB-090 overlay check.
- Suggestions produced during `check_dfm` or `validate_design` cite LIB-xxx rules when a library asset is the root cause of a board-level issue (e.g., courtyard missing → courtyard-overlap DRC).
