---
name: dfm
description: Design for Manufacturing rules for PCBs — trace/space limits, annular rings, via aspect ratios, hole sizes, solder-mask and paste-mask rules, silkscreen rules, panelization, fab capability matching. Use when finalizing a layout, preparing manufacturing outputs, or when KiMCP's domain-knowledge engine runs DFM checks. Pairs with `electrical-cad-best-practices` (DFA/DFT at `CAD-601..CAD-709`) and `kicad-best-practices` (outputs at `KICAD-701..KICAD-709`).
---

# DFM — Design for Manufacturing

Rules to keep a PCB manufacturable. Rule ids use prefix `DFM-`. Every rule has a configurable numeric threshold driven by the project's *fab capability profile*. Defaults here are for a typical low-cost 2-4 layer fab (JLCPCB / PCBWay standard service).

Paired skills:
- Assembly / test practice → `electrical-cad-best-practices:CAD-601..CAD-709`
- Output formats → `kicad-best-practices:KICAD-701..KICAD-709`
- Trace widths for impedance → `signal-integrity`

## Fab capability profile

A configurable object (per-project, default loaded from a preset):

```
min_trace:                mm
min_space:                mm
min_via_drill:            mm
min_via_annular_ring:     mm
min_via_pad:              mm (derived from drill + 2×annular)
min_hole_to_hole_edge:    mm
min_hole_to_copper_edge:  mm
min_copper_to_edge:       mm
max_aspect_ratio:         drill depth / drill diameter (plating)
min_soldermask_dam:       mm
min_soldermask_opening:   mm (beyond pad)
min_silkscreen_line:      mm
min_silkscreen_text_h:    mm
min_silkscreen_to_pad:    mm
min_paste_expansion:      mm (signed, can be negative)
layer_count:              int
finished_copper_oz:       float
surface_finish:           HASL | LF-HASL | ENIG | OSP | ImAg | ImSn | HardGold
impedance_service:        bool
microvia_service:         bool
castellated_service:      bool
edge_plating_service:     bool
```

Presets ship for: `jlc_1-2_layer`, `jlc_4-6_layer`, `jlc_hdi`, `pcbway_standard`, `oshpark_4layer`, `pcbway_advanced`, `custom_high_speed`.

## Trace / space / copper

- **DFM-001 Minimum trace width ≥ `min_trace`.** Default: 0.127 mm (5 mil) for standard service; 0.10 mm (4 mil) advanced; 0.075 mm (3 mil) HDI.
- **DFM-002 Minimum clearance ≥ `min_space`.** Same default as DFM-001 as a rule of thumb; verify per fab.
- **DFM-003 Trace width for DC current** by IPC-2152: do not rely on IPC-2221 (outdated). Inner layers need wider traces than outer for the same current.
- **DFM-004 Voltage-based clearance (IPC-2221 Table 6-1) for high-voltage.** Increase spacing for > 48 V, especially across cutouts and slots.
- **DFM-005 Copper-to-board-edge ≥ `min_copper_to_edge`.** Default 0.2 mm for standard; 0.5 mm if routed edge.
- **DFM-006 No copper under rout line** — tool bit will chew copper.
- **DFM-007 Acid traps forbidden.** Angles < 90° between traces and pads create etch pockets. Use teardrops (KiCAD 8+).
- **DFM-008 Slivers forbidden.** Narrow copper features (width < 0.15 mm) float away in etch. Minimum feature width typically equals min trace × 1.5.

## Vias

- **DFM-010 Drill ≥ `min_via_drill`.** Default 0.3 mm standard, 0.25 mm advanced, 0.2 mm HDI.
- **DFM-011 Annular ring ≥ `min_via_annular_ring`.** Default 0.15 mm standard; 0.1 mm advanced; 0.075 mm HDI.
- **DFM-012 Via pad = drill + 2 × annular.** 0.3 + 2×0.15 = 0.6 mm typical.
- **DFM-013 Aspect ratio ≤ `max_aspect_ratio`.** Default 8:1 for plated through-hole standard; 10:1 advanced. HDI micro-vias 0.75:1.
- **DFM-014 Hole-to-hole spacing ≥ `min_hole_to_hole_edge`.** Default 0.25 mm edge-to-edge.
- **DFM-015 Hole-to-copper spacing ≥ `min_hole_to_copper_edge`.** Default 0.2 mm.
- **DFM-016 Tented or plugged via policy documented.** Default: tenting on top and bottom, no plugging. Tenting saves cost; plugging needed for via-in-pad BGAs.
- **DFM-017 Via-in-pad only with filled-and-capped service.** Without fill-and-cap, solder wicks into the via.
- **DFM-018 Microvia count kept within fab capability.** Every stacked microvia level increases cost sharply.

## Pads, footprints

- **DFM-020 Pad-to-pad clearance ≥ `min_space`** always — even within a footprint.
- **DFM-021 Pad shapes consistent with datasheet.** IPC-7351 Nominal ("N") for general use; "Least" (L) for density, "Most" (M) for hand-solder.
- **DFM-022 Paste-mask expansion per IPC-7351 or datasheet.** Typical: 0 to -0.05 mm for QFN thermal pads; split paste-mask for thermal pads to prevent solder float.
- **DFM-023 Solder mask opening > pad by `min_soldermask_opening`.** Default 0.05 mm per side; some fabs require 0.075 mm.
- **DFM-024 Solder-mask dam between pads ≥ `min_soldermask_dam`.** Default 0.1 mm; below that, fab merges the openings, solder bridges.
- **DFM-025 Thermal reliefs on pads attached to large copper** unless a pad is carrying significant current and needs the connection.
- **DFM-026 First-pin identifier on every polarized footprint** in silk and copper.
- **DFM-027 Courtyard ≥ 0.25 mm larger than component body** on every footprint (IPC-7351 N).
- **DFM-028 Fabrication layer outlines present** on every footprint for DFA and drawing export.

## Holes & slots

- **DFM-030 Mechanical hole ≥ 0.6 mm drill** unless fab explicitly supports smaller.
- **DFM-031 Slots / cutouts only if fab supports**; many "standard" services treat slots as extras.
- **DFM-032 Castellated edges via service-flagged.** Default: no castellations unless `castellated_service=true`.
- **DFM-033 NPTH vs PTH declared per hole.** Silent NPTH can become PTH (or vice versa) during fab import of wrong layer.
- **DFM-034 Plated slots priced as drilled holes**; count them separately in quotes.

## Solder mask / paste / silk

- **DFM-040 Solder mask over via (tented) if small via and low-density area**; open if pad.
- **DFM-041 Solder mask under BGA balls: clear.** Do not cover the pad with mask.
- **DFM-042 Paste mask opening < copper pad on fine-pitch** (80-90%) to avoid bridging.
- **DFM-043 Silkscreen over pads forbidden.** Fab will clip; clipped silk looks amateurish. KiCAD DRC has this rule — leave it enabled.
- **DFM-044 Silkscreen minimum line width ≥ `min_silkscreen_line`.** Default 0.15 mm.
- **DFM-045 Silkscreen text height ≥ `min_silkscreen_text_h`.** Default 0.8 mm; 1 mm preferred for legibility.
- **DFM-046 Silk-to-pad clearance ≥ `min_silkscreen_to_pad`.** Default 0.15 mm.

## Board outline & panelization

- **DFM-050 Board outline on `Edge.Cuts` only**, continuous (no gaps).
- **DFM-051 Rounded corners ≥ 1 mm** unless sharp corner is required — avoids chipping during depaneling.
- **DFM-052 Panel breakaway method declared.** Options: V-score, mouse-bite tabs, routed tabs. V-score is cheapest but requires straight panel edges.
- **DFM-053 Panel margin ≥ 5 mm** around each board for tooling / fiducials.
- **DFM-054 Fiducials on panel corners** for assembly, in addition to per-board fiducials.
- **DFM-055 Board size aligned to fab maximum panel.** Designs close to max panel often get surcharged or rejected.
- **DFM-056 Board thickness per fab standard.** Default 1.6 mm; high-speed / high layer count may need thicker.

## Surface finish & stackup

- **DFM-060 HASL unsuitable for fine pitch (< 0.5 mm)**; ENIG or OSP for fine pitch.
- **DFM-061 ENIG for BGAs and long-shelf-life storage**; costlier but flat.
- **DFM-062 Finished copper weight declared.** 1 oz typical; 2 oz for power; > 2 oz changes min trace/space.
- **DFM-063 Impedance-controlled stackup service used** for boards with controlled-Z nets. Plain stackup without impedance service = fab will not guarantee Z.

## High-speed & RF specific

- **DFM-070 Dielectric constant and loss tangent declared** per layer. Low-loss laminates (Isola, Rogers) for GHz signals.
- **DFM-071 Back-drilling service used** for stub-sensitive high-speed on thick boards.
- **DFM-072 Controlled-depth drilling** priced separately from through.

## Output-file DFM checks

- **DFM-080 Run DRC before every export**, with fab profile loaded as rule source.
- **DFM-081 Gerber review step** — render Gerbers in GerbView (or equivalent) and visually inspect. Missing copper, flipped layers, shifted origin caught here.
- **DFM-082 Drill-Gerber coincidence check** — drill holes fall exactly on pad centers.
- **DFM-083 Courtyard / assembly-layer included** in fab outputs only if fab accepts them (some reject unexpected layers).
- **DFM-084 BOM field validation**: part numbers exist in the ordering system, packages match footprints.

## HDI, microvias, stacked and staggered

HDI (High-Density Interconnect) uses microvias and finer features; each level dramatically increases fab cost and risk. Selected via `microvia_service=true` + stack type in the fab profile.

- **DFM-090 Microvia drill ≤ 0.15 mm, pad ≤ 0.30 mm typical.** Laser-drilled, plated, one or more levels.
- **DFM-091 Microvia aspect ratio ≤ 0.75:1** (IPC-2226 class III). Deeper holes cannot plate reliably.
- **DFM-092 Blind via spans from outer layer inward; buried via spans only inner layers.** Neither shows on the outer surface as a hole.
- **DFM-093 Stacked microvia stack count** driven by fab. Standard HDI: 1+N+1 (one microvia layer each side, N core). Advanced: 2+N+2 or any-layer HDI. Each level = cost step.
- **DFM-094 Staggered microvias** (shifted laterally between levels) are cheaper and more reliable than stacked but cost via real estate.
- **DFM-095 Via-in-pad on BGA fine-pitch** (< 0.5 mm pitch) almost always microvia + fill-and-cap. Non-HDI PTH via-in-pad requires conductive / non-conductive fill + cap + plate flat.
- **DFM-096 Capture pad / target pad sizes** set by fab — the microvia lands on a target pad and must register within tolerance.
- **DFM-097 Sequential lamination** for buried-via structures doubles (or more) fab cost vs standard lamination.
- **DFM-098 HDI design requires stackup-aware DRC rules.** `microvia_from_layer` and `microvia_to_layer` declared in `.kicad_dru` or equivalent.
- **DFM-099 Always-compare-to-non-HDI-alternative.** HDI is a big cost step; sometimes a larger board with standard tech is cheaper overall.

## Rigid-flex

Boards that combine rigid sections with flexible polyimide sections. `rigid_flex_service=true` in the fab profile enables.

- **DFM-100 Flex section stackup differs from rigid.** Typically 1-2 conductor layers on polyimide core; adhesive or adhesiveless construction.
- **DFM-101 Flex trace width and space** wider than rigid minimums for the same fab; 0.15 mm / 0.15 mm typical on flex service.
- **DFM-102 Flex bending radius ≥ 10× flex thickness** for static bend; ≥ 20× for dynamic bend (repeated flexing).
- **DFM-103 No vias in flex bend region.** Vias are rigid points; flexing fatigues via barrels. Keep vias in the rigid section or in a non-bending flex area.
- **DFM-104 Traces perpendicular to bend axis** to spread strain; curved traces preferred over right-angle transitions.
- **DFM-105 Copper on flex staggered between layers** (not stacked) to reduce I-beam stiffness across the bend.
- **DFM-106 Rigid-flex transition reinforcement** — coverlay extension into rigid section, anchor lines to prevent delamination under flex stress.
- **DFM-107 Stiffeners** bonded to the flex for component areas or connector support.
- **DFM-108 Coverlay vs solder mask on flex.** Coverlay (laminated polyimide film) for durability; flex solder-mask only for low-stress areas.
- **DFM-109 Panelization of rigid-flex** nontrivial — depaneling cuts must miss flex areas. V-score only in rigid regions; routing with tabs elsewhere.
- **DFM-110 Impedance on flex differs from rigid** — different Dk, thinner dielectric. Calculate per flex section's stackup separately.
- **DFM-111 IPC-2223 is the spec** for flex and rigid-flex design; IPC-6013 for qualification.

## Heavy copper (> 2 oz)

Boards with thick copper for high-current or thermal service. `finished_copper_oz > 2` activates heavy-copper rule shifts.

- **DFM-120 Minimum trace / space grow with copper weight.** 2 oz outer ≈ 0.2 mm / 0.2 mm typical; 3 oz ≈ 0.3 mm / 0.3 mm; 4 oz ≈ 0.4 mm / 0.4 mm. Fab-specific.
- **DFM-121 Etch undercut scales with copper thickness.** Trapezoidal cross-section pronounced at 4+ oz. Impedance calculations must account for it.
- **DFM-122 Annular ring larger** on heavy copper (0.2 mm+ typical) because drill-to-etch registration wider.
- **DFM-123 Solder-mask over thick copper challenging** — mask may not cover corners fully on very thick copper. Some fabs use mask-over-copper + planarization.
- **DFM-124 Mixed copper weights per layer** (e.g., 2 oz power layers + 1 oz signal layers) are common but cost extra; fab profile must support.
- **DFM-125 Thermal relief on heavy copper** less aggressive (often none) because current-carrying.
- **DFM-126 Plating bath differences** — thick outer copper plating requires longer plating cycles; check fab capability.
- **DFM-127 Hole wall copper min thickness** on heavy-copper boards typically 25 µm (1 mil) — same as standard; via pad stacks up outside.
- **DFM-128 IPC-2152 current capacity re-evaluated** — heavy copper carries much more current but creates its own heat; thermal simulation recommended above 30 A on a single trace.

## Impedance tolerance budget (fab side)

How close the fab can hold impedance; budget for SI rule SI-004 is split between design tolerance and fab tolerance.

- **DFM-130 Fab impedance tolerance ±10% standard**, ±7% premium service, ±5% advanced. Confirm with fab impedance service quote.
- **DFM-131 Dielectric thickness variance ±10%** (FR-4) typical; ±5% for tight-tolerance substrates. Dominant contributor to Z variance.
- **DFM-132 Dk variance on FR-4 is ±10%** across a panel at a single frequency, plus a 1-3% drift per GHz. Tight-tolerance laminates (Rogers 4003, Isola I-Tera) much tighter.
- **DFM-133 Copper thickness variance** — post-plating 10-20% on outer layers; ±10% on inner foil. Affects microstrip Z more than stripline.
- **DFM-134 Trace width etch tolerance ±0.025 mm** (1 mil) typical on 1 oz outer; wider on thicker copper. Fab compensates width for target Z.
- **DFM-135 Fab impedance coupon on panel** measured with TDR after lamination; coupon result certifies the panel. Cost ~$100-300 per stackup setup but necessary for controlled-Z.
- **DFM-136 Lot-to-lot variation exists.** Prototype-build impedance not guaranteed on production; re-verify at volume.
- **DFM-137 Stackup tolerance documented** in fab drawing — target Z, tolerance, measurement points, trace widths per layer.
- **DFM-138 Differential impedance tolerance wider than single-ended** because gap tolerance stacks on top of width tolerance.
- **DFM-139 Tighter tolerance costs.** Moving from ±10% to ±7% is ~10-20% board cost premium; ±5% is 30-50% premium. Budget accordingly.

## How the engine uses these rules

- `check_dfm` runs all applicable rules against the board state, rendering violations with exact coordinates and a fix hint per violation.
- `check_fab_compatibility` compares the board design to a configured fab profile and produces a go/no-go with a per-rule report.
- `check_hdi_stackup` validates the stackup matches the declared HDI class and fab capability.
- `check_rigid_flex` validates flex bend regions, vias, and transitions.
- `check_heavy_copper` validates spacing, annular rings, and mask rules per copper weight.
- `check_impedance_tolerance` compares fab's tolerance to SI-004 target and flags gaps.
- Severity is configurable per rule — defaults: hard capability violations = `error`, soft recommendations = `warn`.
- Every suggestion cites the rule id, the fab profile used, and the specific threshold that was violated.
