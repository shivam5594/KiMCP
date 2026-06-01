---
name: electrical-cad-best-practices
description: General electrical CAD design best practices — schematic principles, PCB layout principles, thermal management, EMI/EMC, mixed-signal design, component selection, DFA/DFT, and documentation. Use when reviewing or authoring schematics/boards, when the domain-knowledge engine runs, or when suggesting improvements beyond the literal user ask. Paired sibling skills cover specialized areas — signal-integrity, power-integrity, dfm — and are referenced here rather than duplicated.
---

# Electrical CAD Best Practices (general)

Cross-cutting design practices that are not covered by the specialized sibling skills (`signal-integrity`, `power-integrity`, `dfm`). The MCP's domain-knowledge engine pulls rules from here alongside the specialized skills, citing the rule id in every suggestion.

Rule ids in this skill use the prefix `CAD-`.

## Scope

In scope:
- Schematic design principles
- PCB layout principles (general — SI/PI/DFM in sibling skills)
- EMI/EMC awareness
- Thermal management
- Mixed-signal design
- Component selection (the engineering side — sourcing lives in `vendor-search`)
- Design for Assembly / Test (DFA / DFT)
- Documentation standards

Out of scope (see sibling skills):
- Signal integrity → `signal-integrity`
- Power integrity / PDN / decoupling → `power-integrity`
- Design for manufacturing trace/space/etc. → `dfm`
- Finding datasheets → `datasheet-search`
- Finding errata → `errata-search`
- Vendor pricing / lifecycle → `vendor-search`
- Footprints / 3D models → `3d-models-and-footprints-search`

## Schematic principles

- **CAD-001 Hierarchy matches functional blocks.** One sheet per functional block (power, clocks, MCU, connectors, analog front-end). Keep each sheet readable at page scale without zooming.
- **CAD-002 Signal flow left-to-right, power top-to-bottom.** Consistent reading direction makes reviews faster.
- **CAD-003 No-connect is explicit.** Every unused pin marked with a `NC` symbol. Never leave a pin floating visually.
- **CAD-004 Power symbols, not wires, for rails.** Use `+3V3`, `GND`, `VCC` power symbols; do not route power as ordinary wires across sheets.
- **CAD-005 Net names are meaningful.** `DDR_CK_P` not `NET_127`. Default unnamed nets are allowed only for trivial passives.
- **CAD-006 Pull-ups / pull-downs shown clearly.** Resistor value and rail both visible at the pin.
- **CAD-007 Decoupling adjacent to its IC in the schematic.** Even if PCB placement is what matters, schematic-side adjacency aids review.
- **CAD-008 Ref-des prefixes follow IEEE 315.** R / C / L / U / Q / D / J / SW / Y / F / TP / FB. Project can override; if so, record it in project config.
- **CAD-009 Annotate every component.** No `R?` or `U?` in a design handed off. Re-annotate after structural changes.
- **CAD-010 Every IC has a visible power/ground on the schematic** (even if via hidden pins), with bypass caps shown at the IC.
- **CAD-011 ERC clean before PCB work.** Unresolved ERC = design incomplete. Suppress a warning only with a written rationale.
- **CAD-012 Design intent documented in graphic text**, near the block it concerns — not in an external document only.
- **CAD-013 Protection at every external interface**: ESD diodes at connectors, TVS on power-in, series Rs on signals that leave the board.
- **CAD-014 Reset and boot-strap networks shown with timing notes** (RC time constants) if any IC requires them.

## PCB layout principles (general)

- **CAD-101 Place before route.** Finalize critical placement (MCU, memory, connectors, power) before any routing.
- **CAD-102 Net-classes defined before routing.** Widths, clearances, differential pair rules set up first (see `signal-integrity`, `power-integrity`, `dfm`).
- **CAD-103 Stackup locked before routing.** Dielectric thickness drives impedance (see `signal-integrity`).
- **CAD-104 Courtyards non-overlapping.** DFA rule; see `dfm` for specific clearances.
- **CAD-105 Connector locations fixed by mechanical first.** Pick connectors → their footprints → board outline → then internal components.
- **CAD-106 Components oriented consistently.** Polarized parts (electrolytics, diodes, ICs) aligned to ease assembly QA.
- **CAD-107 Silkscreen aids assembly, not clutter.** Reference designators near their components; readable orientation; polarity marks on all polarized parts; keepout from pads and vias.
- **CAD-108 Test points on every critical net** (power rails, clocks, resets, communication buses). See DFT section.
- **CAD-109 Fiducials for PnP.** Two or three global fiducials at known positions; local fiducials on fine-pitch ICs.
- **CAD-110 Mounting holes plated or not per mechanical spec.** Always clearly keep-out zones around mounting hardware.
- **CAD-111 Keep-outs respected near crystals, antennas, high-voltage.**

## EMI / EMC

- **CAD-201 Ground plane continuous under high-speed signals.** Avoid splits unless there's a specific reason (and then a bridging cap or stitched return path).
- **CAD-202 Minimize loop area** for any current that is switched — especially power-switching loops, clock returns, and antenna-like traces.
- **CAD-203 Isolate noisy from quiet.** Switch-mode power, clocks, digital logic on one side; analog, RF, sensitive measurement on the other. Bridge by a single, shielded, filtered connection.
- **CAD-204 Crystal loop kept tight and shielded.** Guard trace or guard pour around the crystal; local ground under it.
- **CAD-205 Shield connectors that enter the outside world.** Metal shell grounded; common-mode choke on twisted pair; TVS at the connector pins.
- **CAD-206 Pi / T / LC filters on power entering sensitive blocks.** Ferrite beads only with a known impedance curve — not as a generic "filter".
- **CAD-207 Chassis ground and signal ground tied with care.** One point tie, or AC tie via capacitor.
- **CAD-208 Cable shields bonded 360°.** Pigtail bonds defeat the purpose above low kHz.
- **CAD-209 Clocks and buses inside the board stack** (inner layers) where possible, with ground reference layers above and below.
- **CAD-210 Obey spacing to board edge** for high-speed signals and high-voltage (IPC-2221).

## Thermal management

- **CAD-301 Calculate junction temperature** for any part dissipating > 250 mW; add copper / vias / heatsinks as needed to stay ≤ 80% of rated Tj at max ambient.
- **CAD-302 Thermal relief on pads connected to large copper.** Hand-solder: larger relief; reflow: smaller relief or solid.
- **CAD-303 Thermal vias under hot devices.** Typical 0.3 mm drilled, 0.6 mm pad, on a 1 mm grid. Plated-through; paste mask opening avoids tombstoning.
- **CAD-304 Copper pour for heat spreading on inner layers** where top/bottom space is constrained.
- **CAD-305 Component spacing for airflow** when passive cooling; document the airflow direction assumption.
- **CAD-306 Derate.** Capacitors → 50% voltage in continuous service, less for ceramics due to DC bias. Resistors → 60% power. ICs → 80% junction temperature. MOSFETs → 60% RDS(on) self-heat budget.
- **CAD-307 Hot components away from electrolytics and temperature-sensitive analog.**

## Mixed-signal design

- **CAD-401 One ground plane by default.** Split-ground is a specialist choice made by someone who can justify it and route returns carefully. Single solid plane is the safe default for most designs.
- **CAD-402 Analog and digital placed in separate regions** on the same plane. Sensitive analog away from switch-mode supplies and clock routing.
- **CAD-403 ADC/DAC placement straddles the analog/digital boundary.** Follow the datasheet layout recommendation verbatim.
- **CAD-404 Reference voltage dedicated net, Kelvin-connected** where accuracy matters. Reference decoupling next to the IC pin.
- **CAD-405 Clocks never cross the analog region** unless shielded and ground-stitched.
- **CAD-406 Separate supplies for analog and digital rails** (even if derived from the same source) with filtered bead + local decoupling.

## Component selection (engineering side)

- **CAD-501 Lifecycle status checked.** Active parts only for new designs; NRND and "Last Time Buy" flagged. Use `vendor-search` skill to verify.
- **CAD-502 Second source exists.** One-source parts are accepted only with a documented rationale.
- **CAD-503 Derating in component choice itself.** Choose a part whose ratings are comfortable at worst-case operating conditions, not marginal.
- **CAD-504 Package picked for assembly capability.** BGA / 0201 require capable assembly — confirm the fab/assembly can do them.
- **CAD-505 Tolerance analysis on precision networks.** RC time constants, dividers for references, current sense — check with worst-case tolerances over temperature.
- **CAD-506 Datasheet revision recorded** in the BOM / PLM. Errata checked (see `errata-search`).
- **CAD-507 Footprint verified against datasheet** before using a library footprint (see `3d-models-and-footprints-search`).
- **CAD-508 3D model present and accurate** for mechanical fit.
- **CAD-509 Polarity and first-pin direction verified** on every package, every revision.
- **CAD-510 Environmental rating meets product spec** (temperature, humidity, vibration, altitude).

## Design for Assembly (DFA)

- **CAD-601 Minimum component spacing honored.** 0.2 mm between 0402s, 0.4 mm between ICs with leads. Defer to assembly house spec when stricter.
- **CAD-602 Components same-side where possible.** Two-sided assembly costs more; reserve bottom for connectors / LEDs / thermal or for density-forced placement.
- **CAD-603 Orientation consistent across arrays** for tape-and-reel pickup.
- **CAD-604 Polarity marked in silkscreen AND copper.** Silkscreen can get obscured under paste.
- **CAD-605 Fiducials per assembly house spec.** Typical: 1 mm copper dot, 2 mm silk keep-out, plated.
- **CAD-606 Hand-solder candidates flagged.** Through-hole connectors and any part that cannot reflow.
- **CAD-607 No tombstoning risk.** Balanced copper on both pads of 0402 / 0201; or avoid 0201 unless assembly is qualified.
- **CAD-608 Paste mask tuned per-pad for fine pitch.** 80-90% of copper for QFNs; window-panes under thermal pads.
- **CAD-609 Panelization considered.** V-score, mouse-bites, or route; breakaway tabs away from connectors.

## Design for Test (DFT)

- **CAD-701 Test points on all power rails** with meaningful names (`TP_3V3`, not `TP1`).
- **CAD-702 Test points on reset, clock-out, critical signals.**
- **CAD-703 Test points accessible from one side** where possible; if bed-of-nails, one-side preferred.
- **CAD-704 Programming/debug header** (JTAG / SWD) present on every board with a programmable part.
- **CAD-705 Boundary scan compliance** for parts that support it; JTAG chain order documented.
- **CAD-706 LED indicators** for key power rails and heartbeats.
- **CAD-707 Bring-up mode** (e.g., boot from safe source) considered in schematic.
- **CAD-708 In-circuit-test pads or dedicated TP per net of interest.** 1 mm round, open-solder-mask.
- **CAD-709 Document test plan alongside schematic.** Which TPs for which tests.

## Documentation standards

- **CAD-801 Schematic readable as a PDF.** Every page has title block, revision, designer, sheet number, date.
- **CAD-802 Notes on schematic explain non-obvious choices.** Crystal load cap values, pull strengths, unusual divider ratios.
- **CAD-803 BOM complete.** MPN, manufacturer, description, reference, quantity, package, placement side, alternates.
- **CAD-804 Assembly drawing exported** with reference designators visible, top and bottom.
- **CAD-805 Fabrication drawing exported** with stackup, board dimensions, hole chart, notes.
- **CAD-806 README at project root** with toolchain (KiCAD version), dependencies (libraries), fab/assembly profile, revision history.
- **CAD-807 Revision history in title block**, not only in git. Version visible on the physical PCB.

## ESD / I/O protection (depth)

ESD and surge protection on ports that leave the board. Generalizes `CAD-013` and `CAD-205`; immunity-test levels in `compliance-and-emc-testing:EMC-013..EMC-017`.

- **CAD-220 Classify every port.** Internal-only, board-to-board (short cable), user-touchable, cable-to-outside-world. Protection level scales with exposure.
- **CAD-221 TVS (diode) for fast events.** Clamping voltage below the IC's abs-max at the protection target current. Bi-directional for AC / differential signals, uni-directional for DC rails.
- **CAD-222 TVS placed at the connector**, return tied directly to chassis (or PE) with the shortest possible path. Any routing length between connector and TVS is an antenna and an inductor that spoils the clamp.
- **CAD-223 Series resistor / ferrite between TVS and IC.** 10-100 Ω limits peak current into the IC's on-die diode during the residual pulse; ferrite chosen for its impedance at the surge frequency (MHz-class).
- **CAD-224 Capacitance budget on data lines.** TVS adds pF — too much slows edges. USB 2.0 ≤ 3 pF; USB 3 ≤ 0.5 pF; HDMI ≤ 1 pF; 100BASE-TX ≤ 5 pF. Use RF-class low-C TVS for > 1 Gbps.
- **CAD-225 Surge (IEC 61000-4-5) differs from ESD (61000-4-2)** — much more energy, slower rise. Hybrid protection: gas-discharge-tube or MOV primary + TVS secondary, separated by a series inductor or PTC.
- **CAD-226 Reverse polarity on DC power-in.** Series Schottky (drop + efficiency hit) or P-MOSFET ideal-diode (cheaper drop, more parts) or a fuse + TVS clamp (sacrificial).
- **CAD-227 Overvoltage on DC power-in.** TVS for transients; crowbar SCR + fuse for sustained OV; monitor-IC-triggered disconnect for graceful shutdown.
- **CAD-228 Overcurrent.** Fast fuse for hard faults; PTC (resettable) for user-facing ports. Current rating at operating + in-rush; trip time matched to downstream sensitivity.
- **CAD-229 EFT (burst) protection** = common-mode choke + Y-caps-to-chassis + TVS. The CMC blocks the burst, caps shunt to chassis, TVS catches the residual.
- **CAD-230 Chassis path for ESD must exist before the first sensitive net.** If there is no chassis on the board, ESD current returns via signal ground and couples into the whole PDN. Add a chassis copper area at every connector.
- **CAD-231 GDT (gas discharge tube) for high-energy events** on mains or telecom ports. Leakage-current-sensitive designs accept GDT + post-filter; RF paths cannot tolerate GDT parasitics.
- **CAD-232 Test-what-you-ship.** ESD gun pre-compliance on prototype before chamber time; fix by layout, not by adding parts (see `compliance-and-emc-testing:EMC-044`).

## Power budget & energy estimation

Spreadsheet-level energy accounting that feeds thermal, PDN sizing, and battery runtime. Pairs with `battery-and-low-power:BAT-051` for sleep-mode breakdowns.

- **CAD-320 Per-rail current matrix** columns: rail → consumers → Iq / I_active / I_burst / duty. Rows: one per IC + passives worth mentioning.
- **CAD-321 Worst-case, typical, and minimum columns** — datasheets give typ; design for max; report typ.
- **CAD-322 Temperature derating in the budget.** Semiconductor Iq rises with temperature; LDO dropout rises with current. Evaluate at worst-case junction temperature, not bench.
- **CAD-323 Efficiency-aware budgeting.** Input current on a buck = output power / (Vin × efficiency); quote efficiency at the expected load, not only peak.
- **CAD-324 Losses as line items.** Body-diode drops, RDS(on), inductor DCR, cap ESR on ripple-current paths. Small per-item, big in aggregate.
- **CAD-325 Peak vs average distinction.** Battery runtime = average; fuse rating = peak-plus-margin; PDN decap = transient.
- **CAD-326 Duty cycle per subsystem** (radio on 1% of time, sensor read 10%). Not all subsystems are on simultaneously — document which combinations are realizable.
- **CAD-327 Energy per operation**, for duty-cycled systems: `E_op = Σ (P_mode × t_mode)` per event.
- **CAD-328 Reconcile with measurement.** Budgeted vs measured current on prototype; document the delta and its source (datasheet typ vs sample, ambient, firmware state).
- **CAD-329 Budget updated on BOM churn.** A drop-in alternate with 2× Iq silently wrecks runtime; treat vendor change as an IQ re-audit trigger.

## Reliability & FIT rates

Component-level reliability budgeting. Pairs with vendor-side lifecycle (`vendor-search:VND-010`) and transport/handling (see `high-voltage-design:HV-080`).

- **CAD-520 System FIT = Σ component FIT.** FIT = failures per 10⁹ device-hours. Rough starting point: generic resistors 1, ceramic caps 1-3, electrolytics 50-500, semiconductors 30-100, connectors 10-50, relays 100+.
- **CAD-521 MTBF = 10⁹ / FIT_system** — expressed in hours. A 1000 FIT system = 10⁶ h MTBF ≈ 114 years at device level; per-unit reliability is not per-population.
- **CAD-522 Accelerate at temperature.** Arrhenius: ×2 failure rate per 10°C rise (approximate). A part at 85°C fails ~4× faster than the same part at 65°C.
- **CAD-523 Derate to improve FIT.** Caps at 50% voltage, resistors at 60% power, ICs at 80% Tj. Derating is a reliability investment, not decoration.
- **CAD-524 Electrolytics age fastest.** Lifetime halves every 10°C above rated. Specify 105°C low-ESR where service temperature is high; avoid 85°C standard on anything near a warm IC or PSU.
- **CAD-525 Mechanical fatigue modes:** solder-joint cracking from thermal cycling (especially BGA, large passives), connector mating cycles, flex-circuit bend cycles. Expected cycles part of the spec.
- **CAD-526 Single-point failures flagged.** A 1-of-N redundancy scheme is useful only if the shared element (connector, fuse, LDO) is not itself the dominant FIT.
- **CAD-527 Accelerated-life test plan** for products with > 5-year service life — HALT (highly accelerated life test), HAST (humidity/thermal), thermal cycling. Results feed FIT adjustments.
- **CAD-528 Known failure modes** tracked per chemistry / package: tantalum caps short (fire risk; polymer preferred on sensitive rails), MLCCs crack (thermal shock, flex stress), solder cracks under BGAs, electrolytics dry out, connectors fret-corrode.
- **CAD-529 MIL-HDBK-217 / Telcordia SR-332 / IEC 62380** are the standards for quantitative reliability prediction; pick one per project and document.

## Environmental sealing & IP rating

Enclosure-level sealing and PCB-level robustness against environmental stress. Pairs with `mechanical-integration` for enclosure detail.

- **CAD-1000 IP rating chosen per use case.** IP54 indoor, IP65 splash-resistant, IP67 submersible (1 m 30 min), IP68 deeper submersible, IP69K high-pressure washdown. See IEC 60529.
- **CAD-1001 Conformal coating for humidity / salt-spray / pollution-degree improvement.** Types: AR (acrylic, rework-friendly), UR (urethane, chemical resistant), SR (silicone, high-temp), ER (epoxy, hardest). Coat after cleaning; mask connectors / test points / adjustable components.
- **CAD-1002 Potting / encapsulation** for full sealing of sensitive sub-assemblies — trade off serviceability vs protection. Resin CTE must match or cope with PCB CTE to avoid cracking components.
- **CAD-1003 Gaskets at enclosure seams.** Design compression 15-30% of gasket thickness; groove dimensions per gasket vendor spec.
- **CAD-1004 Breather / vent membrane** (Gore-Tex or equivalent) for sealed enclosures with internal pressure changes (altitude, temp cycling). Prevents gasket blow-out; IP rating maintained.
- **CAD-1005 Connector IP rating matches enclosure.** An IP68 box with an IP20 connector is IP20. Use sealed connectors, grommets, or cable glands.
- **CAD-1006 Salt-spray / corrosion considerations.** ENIG or hard-gold finish over HASL for marine / outdoor; silver on copper tarnishes; aluminium / steel fasteners in contact with copper corrode galvanically.
- **CAD-1007 UV exposure** degrades plastics, silkscreens, and some conformal coats. Outdoor-rated materials; silkscreen text rated for service life.
- **CAD-1008 Operating temperature range declared** in specs; component selection, electrolytic / battery sealing, lubricant viscosity, adhesive service temp all tie to this.
- **CAD-1009 Condensation protection.** Rapid temperature drops cause water ingress via breathing; conformal coat + internal desiccant pack + regular power-on drying cycles.
- **CAD-1010 Altitude derating.** Air is thinner at altitude → clearance is derated. IEC 60664 provides altitude correction factors; creep / clearance doubled above ~2000 m (see `high-voltage-design:HV-005`).

## How the engine uses this skill

When a mutating tool runs, the domain-knowledge engine walks the rules above, filtering by applicability (e.g., `CAD-301` only fires when a component has dissipation > 250 mW). Each hit becomes a `Suggestion` with:

- `rule_id`: `CAD-301`
- `skill`: `electrical-cad-best-practices`
- `severity`: `hint` / `warn` / `error` (per rule; configurable)
- `message`: short human-readable
- `why`: the reasoning stored with the rule
- `fix_hint`: concrete next action
- `references`: IPC / IEEE standards, app notes, or sibling-skill rule ids

Rules reference sibling-skill rules where applicable, so a single violation may cite multiple origins (e.g., a crystal-loop issue cites `CAD-204` and `signal-integrity:SI-030`).
