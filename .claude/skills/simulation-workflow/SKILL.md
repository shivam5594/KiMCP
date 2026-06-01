---
name: simulation-workflow
description: End-to-end simulation workflow — ngspice (DC/AC/transient/noise/distortion) integration, IBIS / IBIS-AMI for signal integrity, S-parameter (Touchstone) handling, PDN impedance simulation, thermal sim integration, model sourcing and validation, simulator configuration, result interpretation, and KiMCP exposure. Use when a design decision requires simulation, when a user asks for DC/AC/SI/PDN/thermal analysis, or when the MCP needs to ground a quantitative claim. Pairs with `signal-integrity`, `power-integrity`, `rf-design`, `electrical-cad-best-practices`.
---

# Simulation Workflow

Rules and patterns for running simulations from KiMCP. Rule ids use prefix `SIM-`.

## Scope

- ngspice integration (DC / AC / transient / noise / distortion / sensitivity / TF)
- IBIS and IBIS-AMI for SI on digital interfaces
- Touchstone (.s1p..s4p) handling for RF / SI
- PDN-impedance simulation (plane-pair, lumped, full 3D when justified)
- Thermal simulation integration (third-party tools, invoked via adapters)
- Model sourcing, validation, and licensing
- Simulator configuration (solver options, numerical stability)
- Result interpretation and uncertainty
- How MCP tools expose simulation

Paired:
- SI rules → `signal-integrity:SI-080..SI-082`
- PDN impedance targets → `power-integrity:PI-030..PI-034`
- RF EM-field sim → `rf-design:RF-...` (EM sim handled here when invoked)

## ngspice integration

- **SIM-001 ngspice as subprocess**, not linked — isolates crashes, simplifies upgrades.
- **SIM-002 Netlist generated from schematic** via `export_netlist` (format `Spice`). `.subckt` blocks for hierarchical sheets.
- **SIM-003 Model files attached to symbols** via the simulation-model field. Missing models replaced with *ideal* equivalents silently is forbidden — the engine raises a warning.
- **SIM-004 Simulation profile captured** in the project: `.control` block or an explicit profile TOML in `<project>/sim/<profile>.toml`.
- **SIM-005 Temperature sweeps** for anything expected to see range. Default profile includes `.temp 0` + `.temp 25` + `.temp 85`.
- **SIM-006 Monte Carlo or corner analysis** for precision analog — tolerance/variance pulled from component datasheet fields.
- **SIM-007 Initial conditions declared** for convergence — `.IC` statements on nodes that wouldn't converge from zero state (oscillators, regenerative circuits).

## Analysis types

- **SIM-010 DC operating point**: `.op` — baseline sanity check on every analog circuit.
- **SIM-011 DC sweep**: `.dc` — V/I vs parameter. Good for transfer curves, load lines.
- **SIM-012 AC small-signal**: `.ac` — gain, phase, filter response, PDN impedance. Small-signal only; no large-signal effects.
- **SIM-013 Transient**: `.tran` — time-domain, large-signal. Slowest; convergence-sensitive.
- **SIM-014 Noise**: `.noise` — input-referred noise from a source, integrated over bandwidth.
- **SIM-015 Distortion**: `.disto` — THD / IMD; small-signal only.
- **SIM-016 Sensitivity**: `.sens` — derivative of output vs component values; excellent for tolerance analysis.
- **SIM-017 Transfer function**: `.tf` — small-signal DC gain, Zin, Zout.

## Solver options & numerical stability

- **SIM-020 Solver defaults** usually fine for discrete analog; tighten for switching regulators and RF.
- **SIM-021 `reltol`, `abstol`, `vntol`, `chgtol`** — tighten only when diagnosed; slows simulation without necessarily improving accuracy.
- **SIM-022 `method = gear` or `method = trap`** — trap (trapezoidal) default; gear (BDF) for stiff systems (switching regulators).
- **SIM-023 `.options rshunt = 1e12`** — insert tiny shunt to help convergence on floating nodes.
- **SIM-024 Max time step capped** to avoid aliasing fast events; commonly ≤ 1/20 of the fastest edge in the system.
- **SIM-025 Initial transient from zero state may fail** on regenerative systems — provide `.IC` or run a `.op` first and hand the state off.

## IBIS & IBIS-AMI

- **SIM-030 IBIS files from the IC vendor.** Confirm model version (usually v3.2 / v4.x / v5.x / v7.x). Older IBIS lacks jitter and equalization for modern SerDes.
- **SIM-031 IBIS-AMI for SerDes** — algorithmic model of Tx/Rx equalization. Required for PCIe / USB 3+ / MIPI-PHY SI work.
- **SIM-032 IBIS model check**: golden waveform / waveform-fitting, driver / receiver pull-up / pull-down curves consistent. Vendor-supplied validation reports.
- **SIM-033 Channel model** = Tx model + interconnect (S-parameters) + Rx model. Results = eye diagrams, BER.
- **SIM-034 Eye-diagram criteria**: eye height, eye width, timing jitter. Pass/fail per interface spec (covered under `signal-integrity:SI-081`).
- **SIM-035 Use a proper IBIS-AMI simulator** (Keysight ADS, Cadence Sigrity, HyperLynx, open-source alternatives). ngspice alone is insufficient for AMI.

## S-parameter (Touchstone) handling

- **SIM-040 .sNp files canonical.** `.s2p` (two-port), `.s4p` (four-port differential pair), `.s16p` (large connectors).
- **SIM-041 Port ordering convention**: either single-ended (P1, P2, P3, P4) or mixed-mode (DD11, DD12, CC11, ...). State convention in file header.
- **SIM-042 Reference impedance** normally 50 Ω — declared in Touchstone header (`# Hz S RI R 50`).
- **SIM-043 Interpolation across sweep**: use rational or passive-preserving interpolation when stitching between simulators.
- **SIM-044 Causality and passivity checks** — many tools silently accept non-passive files; run a check post-import.
- **SIM-045 PCB trace model from field solver** (Siemens HyperLynx / Keysight ADS / Cadence Sigrity) exported as Touchstone, then plugged into IBIS-AMI channel model.

## PDN impedance simulation

- **SIM-050 Lumped-element model of the PDN** (caps + plane-pair C + ESL) gives first-order impedance vs frequency. Free tools: KiCad built-in calculator, ADI's free PDN tool, TI WEBENCH, Murata SimSurfing.
- **SIM-051 Plane-pair capacitance** computed from area and stackup — dominant at high frequencies.
- **SIM-052 Cap ESL** from package and mounting geometry — shorter vias and pad-to-plane lower ESL.
- **SIM-053 Anti-resonance checks** — impedance peaks between cap values. Re-select values if peaks cross target.
- **SIM-054 3D PDN simulation** (Sigrity, ANSYS SIwave) for advanced designs — BGA packages, dense digital systems.
- **SIM-055 Measurement correlation** — VNA two-port shunt-through technique on bare board gives real PDN impedance; compare to simulation.

## Thermal simulation

- **SIM-060 Thermal adapters** to external tools: Simcenter Flotherm, ANSYS Icepak, open-source (openfoam, freefem). KiCAD 3D export (STEP) feeds these.
- **SIM-061 Component power dissipation per BOM** — MCU, MOSFET, LDO, etc. Source from datasheet typical conditions + duty cycle.
- **SIM-062 Boundary conditions**: ambient temperature, enclosure, airflow. Realistic, not best-case.
- **SIM-063 Validate with IR camera** on prototype when possible; sim is only as good as boundary conditions.

## EM-field simulation

- **SIM-070 2.5D solvers** (Sonnet, ADS Momentum, OpenEMS) for most PCB RF work.
- **SIM-071 3D full-wave solvers** (HFSS, CST) for antennas, connectors, complex shielding.
- **SIM-072 Mesh quality matters** — convergence study required before trusting a single simulation.
- **SIM-073 Material properties** (Dk, Df, conductivity) as a function of frequency — generic FR-4 defaults insufficient above 1 GHz.

## Model sourcing and validation

- **SIM-080 Vendor-supplied models preferred** over generic; manufacturers publish SPICE models and IBIS files for their parts.
- **SIM-081 Check model accuracy** — a BJT model might be accurate DC-wise but miss high-frequency behavior. Look at datasheet notes on model applicability.
- **SIM-082 Encrypted / proprietary models** acceptable but limit debugging. Document which model is in use.
- **SIM-083 Build missing models** from datasheet characterization when vendor-supplied is absent. Document derivation.
- **SIM-084 License compliance** — some models are free to use but not redistributable; track per-project licensing.

## Result interpretation

- **SIM-090 Every simulation result has an uncertainty**. State it.
- **SIM-091 Monte Carlo over parameter tolerances** gives spread; worst-case corners give bounds.
- **SIM-092 Compare sim to measurement** on prototype, document delta, adjust model or sim methodology.
- **SIM-093 Sim results never alone justify a design**; they are one input. Measured data on real hardware trumps simulation.
- **SIM-094 Simulation fails loudly, not silently** — convergence failures, non-physical results (negative resistance where there shouldn't be, etc.) must stop the run.

## KiMCP exposure

- `run_simulation(profile, analysis)` — wraps ngspice subprocess, returns raw output + structured parse of `.raw` file.
- `plot_simulation(analysis, signals)` — renders waveforms / Bode / eye as SVG/PNG.
- `check_si_eye(net, ibis_tx, ibis_rx, channel_sparams)` — integrates IBIS-AMI channel simulator; returns eye metrics.
- `check_pdn_impedance(rail, target_z)` — builds PDN lumped model from the schematic + stackup, computes Z(f), compares to target.
- `suggest_decap_values(rail, target_z)` — optimizes cap selection to meet target; references `power-integrity:PI-032..PI-034`.
- `export_touchstone(channel)` — runs field solver adapter (if configured) and emits `.sNp`.

## How the engine uses this skill

- `suggest_*` outputs sourced from simulation cite `rule_id: "SIM-xxx"` and attach result artifacts (raw files, plots) as resources.
- Sim failures surface warnings in tool outputs; persistent failures block the design-review prompt until addressed.
- Sim-driven claims in the MCP's suggestion body include simulator name, version, model ids, and a resource URI for the raw result.
