---
name: high-voltage-design
description: High-voltage design rules — creepage and clearance per IEC 60664 and IEC 62368, isolation barriers, reinforced insulation, slots and cut-outs, pollution-degree and overvoltage-category selection, conformal coating, Y/X capacitor selection, optocoupler and digital-isolator usage, and PCB layout patterns for AC mains / battery packs / motor drives. Use when any portion of the design exceeds ~42 V AC or ~60 V DC, when there is any galvanic isolation requirement, or when KiMCP's domain-knowledge engine runs HV checks. Pairs with `dfm` (slot/drill capability), `compliance-and-emc-testing` (IEC 62368 evaluation), `electrical-cad-best-practices` (CAD-207 chassis ground).
---

# High-Voltage Design

Rules and checks for designs with mains, high-voltage DC, or isolation barriers. Rule ids use prefix `HV-`.

## Scope

- Creepage and clearance per pollution degree and overvoltage category
- Isolation barriers (functional, basic, supplementary, double, reinforced)
- Mains-connected design patterns
- Battery-pack and EV-range HV design
- Slots, cut-outs, and grooves for creepage
- Component selection (Y / X caps, opto / digital isolators, reinforced transformers)
- Safety-agency compliance (UL / CSA / TÜV / IEC)
- Conformal coating, potting, encapsulation
- HV routing, wide-spacing rules, and testing

Out of scope:
- Low-voltage practice → `electrical-cad-best-practices`
- EMC/EMI at HV → cross-references to `compliance-and-emc-testing` (EMC-) and this skill (HV-)
- RF at high voltage (e.g., CMUT driver) → specialist, out of scope here

## Definitions (brief — consult IEC 60664 for authoritative)

- **Creepage**: shortest distance along an insulating surface between two conductive parts at different potentials.
- **Clearance**: shortest distance through air between two conductive parts at different potentials.
- **Pollution degree (PD)**: 1 (clean/sealed), 2 (office), 3 (industrial), 4 (outdoor).
- **Overvoltage category (OVC)**: I (signal) to IV (mains supply).
- **Working voltage (V_rms or V_peak)**: operating voltage across the gap, not peak transient.
- **Insulation type**: functional, basic, supplementary, double (basic+supplementary), reinforced (single layer tested to double).

## Creepage & clearance (core rules)

- **HV-001 Pull creepage/clearance tables from IEC 60664-1** for the specific pollution degree and working voltage. Project fab profile or configuration declares PD and OVC; the engine picks the table row.
- **HV-002 Defaults for desktop mains (230 V) PD2 OVC II**: clearance ≥ 1.5 mm, creepage ≥ 3.0 mm (functional); reinforced doubles these. Always verify against current standard — safety standards update.
- **HV-003 Battery-pack HV (≤ 60 V DC)**: still apply creepage/clearance for SELV boundary (minimum 0.2 mm practical clearance; 0.5 mm creepage); above 60 V DC treat as hazardous.
- **HV-004 800 V EV rail PD2 OVC II reinforced**: clearance ≥ 5.5 mm, creepage ≥ 11.3 mm (approx — confirm current standard). Re-verify for your region + standard.
- **HV-005 Work voltage is peak, not RMS, for impulse clearance** — include transient category (OVC) when sizing.
- **HV-006 Allow margin.** Design for 10-20% more than the standard minimum; manufacturing variation eats margin.
- **HV-007 Slots/cut-outs to extend creepage** where spacing is insufficient — document each with width ≥ 1 mm (narrower slots don't increase creepage per IEC; a 1 mm slot is conservative).
- **HV-008 Conformal coating changes PD but is not a free pass.** IEC requires specific qualification; uncoated still the default assumption for agency tests.

## Isolation barriers

- **HV-010 Identify every barrier in the schematic.** Label each with "Functional / Basic / Supplementary / Double / Reinforced" + working voltage + standard it conforms to.
- **HV-011 Barrier crossings are known.** Only via approved components: optocouplers rated for Viotm, digital isolators (ADI iCoupler, TI ISOW, Silicon Labs Si86xx) with data-sheet reinforced ratings, transformers with UL/CSA/VDE approval.
- **HV-012 Creepage applies inside the component too.** A basic-isolation opto in an application that needs reinforced must be replaced or doubled.
- **HV-013 Y-capacitors across the barrier rated per their class.** Y1 / Y2 ratings tied to insulation class — do not substitute.
- **HV-014 Barrier routing**: no trace crosses the barrier footprint. Plane cuts are explicit and aligned. Silk labels "HAZARDOUS" / "ISOLATED" on each side.
- **HV-015 Solder-mask does NOT count as insulation** for barrier purposes. Do not rely on silk or mask for HV.
- **HV-016 Reinforced isolation = full double on PCB** — two independent insulations, each rated basic, with both tested.

## Component selection

- **HV-020 X caps (across line)**: Y-class rating or X-class AC-mains rated. Self-healing film preferred over MLCC at mains-peak.
- **HV-021 Y caps (line-to-ground)**: Y1 for reinforced, Y2 for basic. Safety agency certificate (UL, TÜV, VDE) visible on the part.
- **HV-022 Optocoupler with Viotm ≥ working voltage + safety margin**. Apparent creepage and clearance inside the part listed on datasheet.
- **HV-023 Digital isolator rated for the barrier class**. Many modern parts are "basic" only — reinforced isolators cost more.
- **HV-024 Transformers with multi-agency approval** (UL 1446 class / IEC 61558 / VDE) for regulated products.
- **HV-025 MOSFETs / IGBTs for HV switching**: avalanche-rated; derate Vds to 70-80% of rated with transient headroom; Rth_JC per thermal budget.
- **HV-026 Snubbers across HV switches**: RC or RCD clamp sized for the leakage-inductance energy.
- **HV-027 HV electrolytics rated for 85°C or 105°C** matching product service. AC-mains bulk typically 450 V DC across the bulk.
- **HV-028 Fuse / PTC for fault current**: inrush-rated; fuse-holder clearance ≥ safety standard; track/solder work thermally capable.

## Layout patterns

- **HV-040 Mains inlet region clearly demarcated**: protective earth, line, neutral distinct; track widths sized for fault current + short-duration fuse arc; fuses on line side of switch.
- **HV-041 Creepage-blocking slots** on the board between primary-side and secondary-side — ideally full cut-out (not slot), labeled on silk.
- **HV-042 No vias in the barrier zone.** Even a well-placed via reduces PCB thickness and the creepage around it.
- **HV-043 HV routing widths by current + ΔT**, not by IPC-2221 naive tables. Re-derive from IPC-2152 for inner layers.
- **HV-044 Thermal relief on HV pads only when current allows.** Often HV carries enough current that solid connections are required.
- **HV-045 Mounting holes on HV side grounded to chassis with explicit PE**; note rating on silk.
- **HV-046 Test points on HV surfaces are dangerous.** Avoid or use safety-pad covered with standoffs / labeled "HV PRESENT".
- **HV-047 Label the board.** Silkscreen HAZARD / VOLTAGE / PE / BARRIER markings.

## EV / battery-pack HV

- **HV-050 Pack voltages above 60 V DC treated as mains-equivalent** for PCB spacing purposes in many standards.
- **HV-051 Isolation monitoring** circuit required per FMVSS / ISO 6469 depending on region.
- **HV-052 Battery management IC's CSB/CSP sense lines routed carefully** — they can float to pack voltage through a bad connection.
- **HV-053 Pre-charge resistor sized** to limit inrush within HV contactor capability.
- **HV-054 Discharge path verified** — a pack that cannot be discharged safely is a service liability.
- **HV-055 HV interlock loop** signal's disconnection actually kills HV — do not stub it.

## Slots and mechanical

- **HV-060 Slot width ≥ 1 mm** to reliably count toward creepage.
- **HV-061 Slot shall be accessible for cleaning** if pollution degree matters.
- **HV-062 Routing around slots increases trace length** for low-voltage control; ensure return paths aren't compromised (`signal-integrity:SI-031`).
- **HV-063 Edge plating of isolated regions** for mounting in grounded enclosures — careful with creepage to chassis.

## Conformal coating / potting

- **HV-070 Coating improves pollution degree rating only if qualified.** Don't assume PD1 just because you coated.
- **HV-071 Potting (resin-fill) substantially raises creepage tolerance** — used in industrial supplies that live outdoors.
- **HV-072 Coating thickness + dielectric strength recorded.**
- **HV-073 Components rated for coating temperatures.** Solvents and cure temperatures affect some components.

## Testing & certification

- **HV-080 Hi-pot test (dielectric withstand)**: PCB and assembly tested at > 2 × working voltage per IEC; failures = rejection, not rework.
- **HV-081 Insulation resistance test**: > 100 MΩ at rated voltage across barriers.
- **HV-082 Partial-discharge test** on reinforced barriers above 250 V working.
- **HV-083 Touch-current test** per product safety standard.
- **HV-084 Standards**: IEC 62368-1 (audio/video/IT), IEC 61010 (lab/industrial instruments), IEC 60601 (medical), UL 60950 (legacy → 62368), UL 2580 (battery), ISO 6469 (EV).

## Safety-agency submission

- **HV-090 Bill-of-materials with safety components flagged** (Y/X caps, transformers, fuses, optocouplers): MPN, rating, agency certificate ID.
- **HV-091 Construction review**: creepage/clearance diagram, barrier definition, insulation class.
- **HV-092 Test report package**: hi-pot, leakage, temperature rise, abnormal-operation test, fault-insertion test.
- **HV-093 Agency label / marking requirements** met (CE, UKCA, UL, FCC, etc.).

## How the engine uses this skill

- `check_hv_spacing(board)` computes actual creepage/clearance on the board for nets declared as HV in net classes; flags violations by standard.
- `suggest_slot(board, between_nets)` proposes a slot/cut-out to extend creepage where spacing is tight.
- `validate_isolation_barrier(barrier_id)` confirms the barrier's components and layout meet the declared insulation class.
- `check_safety_bom(project)` verifies Y/X caps and safety-critical components carry valid agency ratings in their BOM fields.
- Suggestions cite `rule_id: "HV-xxx"` plus the relevant IEC/UL standard clause.
