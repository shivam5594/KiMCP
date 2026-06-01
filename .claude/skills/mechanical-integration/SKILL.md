---
name: mechanical-integration
description: Mechanical integration of the PCB — enclosure fit, connector mating and alignment, cable exit and strain relief, mounting and fasteners, PCB-to-panel interfaces, display / button / LED alignment to enclosure openings, 3D clearance checks, BOM-driven mechanical constraints, and KiCAD 3D export integration with mechanical CAD (STEP round-trip). Use when the PCB interfaces with an enclosure, a mating connector, a cable harness, a chassis, or any mechanical assembly; or when KiMCP's domain-knowledge engine runs mechanical-fit checks. Pairs with `3d-models-and-footprints-search` (models used for fit), `dfm` (board outline and slots), `electrical-cad-best-practices` (CAD-110 mounting, CAD-105 connector mechanical first).
---

# Mechanical Integration

Rules and checks for the PCB-to-mechanical interface. Rule ids use prefix `MECH-`.

## Scope

- Board outline, mounting, and fastener patterns
- Enclosure fit and tolerance stack-ups
- Connectors: mating, alignment, retention
- Cable exit, strain relief, bend-radius
- Panel-mount components (LEDs, buttons, displays, switches)
- Keep-out zones from mechanical features
- 3D model export and mechanical-CAD round-trip
- Drop / shock / vibration considerations at the layout level
- BoM-driven mechanical constraints (heights, weights, center of mass)
- Thermal-path mechanical interfaces (heatsinks, thermal pads)

Paired:
- Asset validation → `3d-models-and-footprints-search:LIB-020..LIB-027`, `LIB-030`
- Board outline DFM → `dfm:DFM-050..DFM-056`
- Design-time placement rules → `electrical-cad-best-practices:CAD-101..CAD-110`

## Board outline & mounting

- **MECH-001 Board outline set by mechanical CAD.** Import STEP/DXF into KiCAD `Edge.Cuts` rather than hand-drawing.
- **MECH-002 Mounting holes per enclosure spec.** Hole diameter, spacing, count taken from enclosure datasheet or mechanical drawing.
- **MECH-003 Mounting-hole tolerance bands.** Typical ±0.1 mm position tolerance; tighter for tight-fit enclosures (metal CNC parts).
- **MECH-004 Mounting-hole pad/keepout**: plated or non-plated per mechanical function (grounded to chassis → plated; isolated → non-plated). Copper keep-out around unplated holes to prevent shorts to fastener.
- **MECH-005 Fastener clearance** on both sides: washer diameter + drive clearance in 3D.
- **MECH-006 Standoffs and spacers** sized with clearance for all bottom-side components including their 3D height.
- **MECH-007 Mounting-hole pattern stress-calculated** for shock/vibration targets; add mounting points if board sag / resonance modes problematic.

## Enclosure fit & tolerance stack-ups

- **MECH-010 PCB fits with margin** inside the enclosure — typically 0.2-0.5 mm per side for injection-molded plastic, tighter for CNC metal.
- **MECH-011 Tolerance stack-up documented** — enclosure tolerance + PCB outline tolerance + component-height tolerance. Worst-case stack must not collide.
- **MECH-012 Tallest components identified.** Connector height, electrolytic heights, transformer heights — check all against enclosure lid clearance.
- **MECH-013 3D STEP assembly** in mechanical CAD includes PCB + all components + enclosure; no collisions at worst-case tolerance.
- **MECH-014 PCB locates via hard features** (mounting bosses, snap-fits, rails) — not just mounting screws.
- **MECH-015 Thermal expansion considered** for products with wide temperature ranges; plastic shrinks/expands more than FR-4.

## Connectors

- **MECH-020 Mating connector selected alongside board connector** — pair confirmed at the beginning of layout (see `electrical-cad-best-practices:CAD-105`).
- **MECH-021 Connector orientation matches cable exit direction.** Right-angle vs vertical vs through-hole choice tied to cable routing.
- **MECH-022 Connector body must clear enclosure opening with tolerance.** Typical 0.5 mm around; more for user-accessible.
- **MECH-023 Panel-mount vs PCB-mount connector distinction.** Panel-mount has an extra mechanical-support point on the enclosure — reduces PCB mounting stress.
- **MECH-024 Retention force (mating / un-mating)** within user ergonomics — neither popping off nor requiring pliers.
- **MECH-025 Retention mechanism (latch, screw, bayonet)** where user-accessible connectors are not to be unplugged accidentally.
- **MECH-026 Keyed connectors for polarity.** Unkeyed headers in user-accessible positions are a reliability liability.
- **MECH-027 USB / HDMI / RJ45 / D-sub mounting holes** reinforced to resist mating cycles. Through-hole footprints preferred over SMT for high-insertion connectors unless SMT variant is explicitly rated for the cycle count.
- **MECH-028 Connector location tolerance to panel** tight — often ±0.25 mm for visible connectors.
- **MECH-029 Connector height above board surface** matters for stacked boards, enclosure lids. 3D model validated per `3d-models-and-footprints-search:LIB-022`.

## Cable exit & strain relief

- **MECH-040 Strain relief designed** on every cable exit. Molded grommet, clamp, or strain-relief boot.
- **MECH-041 Minimum bend radius** respected per cable spec — typically 5× OD for flexible cables, 10× OD for rigid.
- **MECH-042 Cable-to-connector solder joints not load-bearing.** Crimp / IDC connectors for flex runs.
- **MECH-043 Cable ties / cable management features** (tie-down points, cable clips) planned, not improvised.
- **MECH-044 Shield termination** — 360° bonded to chassis or cable-shield via dedicated drain wire and ground ferrule. Pigtails are EMC hazards (see `electrical-cad-best-practices:CAD-208`).
- **MECH-045 Cable exit locations match target use** — coming out the back for permanent installations, sides for portable; avoid top exits that snag.

## Panel-mount components

- **MECH-050 LEDs, buttons, displays aligned to enclosure openings** with tolerance band. Hole-to-component center accuracy ±0.25 mm typical.
- **MECH-051 Light pipes or lenses selected for LEDs** that must show through thick enclosure walls.
- **MECH-052 Button actuation path** — plunger length, spring force, tactile feedback.
- **MECH-053 Rotary encoder / potentiometer knob clearance** on enclosure; shaft length matches knob + panel thickness.
- **MECH-054 Display bezel cutout** tolerance and viewing angle checked against display datasheet.
- **MECH-055 Touch-screen attachment method** (adhesive, snap, clamp) integrated into mechanical design; routing and FFC exit planned.
- **MECH-056 PCB-mount switches' actuator offset** from PCB surface to panel — verify stack-up.

## Keep-out from mechanical

- **MECH-060 Mechanical keepouts drawn on `User.1` / `User.2` / `Eco1.User`** KiCAD layers per convention. Edge.Cuts is the board outline only.
- **MECH-061 Standoff / boss keepouts** cover the boss diameter + fastener hardware + drive clearance.
- **MECH-062 Snap-fit keepouts** extend into the PCB region that plastic ribs occupy.
- **MECH-063 Battery compartment keepout** if the PCB shares space with batteries; include access for battery swap.
- **MECH-064 Speaker / microphone acoustic keep-out** — acoustic channels in the enclosure need clear paths; PCB traces and components can block.
- **MECH-065 Internal antenna keep-out coordinated with RF** — mechanical CAD aware of `rf-design:RF-030..RF-034` keepouts.

## 3D export & mechanical-CAD round-trip

- **MECH-070 STEP export after every significant layout change.** `kicad-cli pcb export step` with accurate origin.
- **MECH-071 Origin convention documented** — usually board center or a fiducial. Agreed between electrical and mechanical teams at project start.
- **MECH-072 Assembly mesh validated** in mechanical CAD — no intersections, correct component heights, correct connector orientation.
- **MECH-073 Mechanical revisions tracked** — when enclosure updates, PCB's mechanical constraints revisited.
- **MECH-074 Shared reference drawing** — dimensioned assembly drawing from mech CAD drives PCB outline; PCB drawing drives component placement.
- **MECH-075 Pick-and-place position file** cross-checked with 3D — occasional sign or rotation confusion.
- **MECH-076 VRML output for render-only mechanical reviews**; STEP for structural mech-CAD.

## Drop / shock / vibration

- **MECH-080 Large / heavy components near mounting holes** reduce leverage on lift during drop.
- **MECH-081 Tall / tower components staked** (epoxy, adhesive, bracket) in ruggedized designs.
- **MECH-082 Underfill for BGAs** in drop-expected products.
- **MECH-083 Connectors with mechanical reinforcement** — e.g., USB-C with metal-through-board latches.
- **MECH-084 Resonance modes analyzed** for applications with known vibration spectra (vehicle, industrial machinery) — add stiffening or mounting points.

## BoM-driven constraints

- **MECH-090 Height matrix per component** in the BoM fields: `Height_mm`. Tallest per region feeds the enclosure stack-up.
- **MECH-091 Weight and center-of-mass** relevant for portable / drop-sensitive products.
- **MECH-092 DFA orientation** — all polarized parts same direction (see `electrical-cad-best-practices:CAD-603`).
- **MECH-093 3D-model accuracy target** matches use — low-poly for enclosure fit is fine; high-poly for thermal sim.

## Thermal-path interfaces

- **MECH-100 Heatsink attachment method documented** (screws, clip, adhesive pad). Thermal pad specification (conductivity W/m·K, thickness mm) in mech drawings.
- **MECH-101 PCB cutouts or thermal vias aligned to heatsink pedestal** — misalignment kills thermal performance (see `electrical-cad-best-practices:CAD-303`).
- **MECH-102 Thermal-pad compressibility** accommodated by enclosure — over-compression damages components, under-compression leaves air gaps.
- **MECH-103 Gasket / thermal-pad rework plan** — these items are service parts in some products.

## How the engine uses this skill

- `check_mechanical_fit(project, enclosure_step)` imports the enclosure STEP and checks PCB + components for intersection / clearance at configured tolerance.
- `check_connector_alignment(connector, panel_opening)` verifies connector-to-opening alignment with tolerance band.
- `suggest_mounting_pattern(board_size, weight)` recommends mounting-hole count and positions for expected vibration class.
- `export_assembly_step(project)` returns a unified STEP suitable for mechanical-CAD review.
- Suggestions cite `rule_id: "MECH-xxx"` plus the specific mechanical-CAD or enclosure reference.
