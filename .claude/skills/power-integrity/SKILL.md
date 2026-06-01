---
name: power-integrity
description: Power integrity rules — decoupling (per-pin caps), bulk capacitance, PDN target impedance, power planes, power sequencing, LDO vs SMPS selection. Use when designing, routing, or reviewing a power distribution network, or when KiMCP's domain-knowledge engine runs PI checks. Pairs with `signal-integrity` (return paths) and `electrical-cad-best-practices` (thermal, mixed-signal).
---

# Power Integrity

Rules and checks for power distribution network (PDN) design. Rule ids use prefix `PI-`.

## Scope

- Decoupling strategy (per-pin, per-bank, per-voltage-rail)
- Bulk capacitance sizing
- PDN target impedance
- Power planes and routing
- Power sequencing
- LDO vs SMPS selection
- Power entry protection

Paired skills:
- Return paths, reference-plane integrity → `signal-integrity` (SI-030..SI-036)
- Thermal, derating → `electrical-cad-best-practices` (CAD-301..CAD-307)
- Solder-mask and via rules for power copper → `dfm`

## Decoupling

### Count & values

- **PI-001 One ceramic decoupling capacitor per power pin of every IC.** Exception: a single smaller IC with two power pins on the same rail may share one 0.1 µF if and only if the two pins are < 5 mm apart in the footprint.
- **PI-002 Value rule of thumb by frequency content.**
  - > 100 MHz content: 0.1 µF 0402 (MLCC, X7R or better) per power pin
  - 10-100 MHz: 1 µF 0402 / 0603 per bank
  - 1-10 MHz: 10 µF 0603 / 0805 per rail
  - < 1 MHz: bulk (see PI-020)
- **PI-003 Multiple values in parallel ≠ automatically better.** Anti-resonance between mismatched caps creates impedance peaks. Prefer 2-3 strategically picked values or a single larger-volume cap over a scattershot of 10 values.
- **PI-004 Use the same dielectric family where possible** (all X7R, or all C0G) to avoid unexpected aggregate behavior.
- **PI-005 DC bias derating on ceramics is real.** A 10 µF 0402 0603 6.3 V at 3.3 V may deliver < 5 µF effective. Size 2-3× the nameplate; specify rated voltage ≥ 2× operating voltage for MLCCs on power rails.

### Placement

- **PI-006 Decap adjacent to its power pin**, on the same side as the IC, with the shortest possible loop to the return plane.
- **PI-007 Cap pad → IC pin → plane loop area minimized.** A decap 10 mm away with a long plane return is worse than a slightly-wrong-value cap with a 1 mm loop.
- **PI-008 Via-in-pad for BGA power fields** when necessary to shorten the loop; pay DFM cost (see `dfm`).
- **PI-009 Orientation for smallest loop.** For a cap between power and ground, put GND pad nearest to the IC's ground via.
- **PI-010 Cap between power pin and return plane** — not across a plane split.

## Bulk capacitance

- **PI-020 One bulk capacitor per rail** sized to supply transient load until the upstream supply can respond.
- **PI-021 Sizing target: ΔV_max transient across the rail stays within spec** during the worst-case load step. Classic first-order: `C_bulk ≥ ΔI × t_response / ΔV_max`.
- **PI-022 Tantalum or polymer for > 22 µF** ratings; electrolytic only below ~65 °C service temperature unless derated heavily.
- **PI-023 Low-ESR bulk at the input of a SMPS** tuned to the switching frequency per datasheet.
- **PI-024 Bulk at the output of the SMPS** sized to meet downstream decap's needs plus transient margin.
- **PI-025 Bulk near power entry** for ESD/inrush — separate from downstream rail bulks.

## PDN target impedance

- **PI-030 Compute target impedance: `Z_target = ΔV_max / ΔI_max`.** E.g., 3.3 V ± 3% with 1 A step → 99 mΩ.
- **PI-031 Target holds across the bandwidth of the load.** For modern digital, DC to ~1 GHz.
- **PI-032 Stack of caps + plane pair delivers `Z_target` at increasing frequencies.**
  - DC-kHz: bulk (polymer/electrolytic)
  - kHz-MHz: multilayer bulk (10-100 µF MLCC)
  - MHz-100 MHz: mid-value MLCC
  - 100 MHz-GHz: close-proximity small MLCC + plane pair capacitance
- **PI-033 Plane pair capacitance matters > 100 MHz.** Tight dielectric between power and ground plane (≤ 0.1 mm) provides embedded capacitance unattainable with lumped caps.
- **PI-034 Simulate or model PDN impedance** for rails supplying multi-watt digital or sensitive analog. Free tools: ADS open-source toolbox, PI-specific helpers. At minimum: hand-calculate dominant frequencies and compare to targets.

## Power planes and routing

- **PI-040 Prefer solid power plane to routed power traces** for rails > 500 mA or > 50 MHz content.
- **PI-041 Multi-rail board uses plane partitions carefully.** Partitioned power plane is fine; partitioned *ground* plane is hazardous (see `signal-integrity:SI-031`).
- **PI-042 Power plane width at trace-routed rails ≥ 2× the current's required trace width.** Use IPC-2152 or equivalent to pick trace width for current.
- **PI-043 Plane necking forbidden** at via arrays. Open up the anti-pads or add a local plane cut-around.
- **PI-044 Vias connecting power to decap pads: at least one via per pad** on high-current rails.
- **PI-045 Ferrite beads only where the DC current and self-resonant frequency are verified.** A bead that passes clock harmonics defeats its purpose.

## Power sequencing

- **PI-050 Sequence documented** for every multi-rail design. Which rail first? Which must be high before another rises?
- **PI-051 Power-good signals used**, not open-loop timing assumptions.
- **PI-052 Reset released after all rails are valid** — not before.
- **PI-053 Reverse sequencing on shutdown** unless the datasheet says otherwise.
- **PI-054 Inrush-limited if cold-start current exceeds upstream limits** — NTC, soft-start, or discrete inrush limiter.
- **PI-055 Latch-up risk avoided**: I/O pins not driven before core/IO power rails are within spec.

## LDO vs SMPS selection

- **PI-060 LDO for low dropout, low noise, < 500 mW dissipated** in steady state. Ideal for analog rails, post-regulation of a switcher.
- **PI-061 SMPS for efficiency**, especially at > 500 mW dissipation. Mandatory for battery-powered designs with tight energy budgets.
- **PI-062 LDO after SMPS for low-noise analog rails** — SMPS upstream provides efficient bulk conversion; LDO cleans the residual ripple.
- **PI-063 SMPS layout per datasheet, verbatim.** SMPS performance depends on switch loop area, inductor placement, feedback routing — deviate at your own risk.
- **PI-064 Feedback resistor divider Kelvin-sensed** at the point of load for tight regulation.
- **PI-065 SMPS switch node is an aggressor.** Keep it away from analog, sensitive digital, antennas.
- **PI-066 SMPS inductor area enclosed** by GND pour and vias — contains magnetic field.

## Power entry

- **PI-070 Protection at the connector**: reverse polarity, overvoltage (TVS), overcurrent (fuse / PTC), ESD.
- **PI-071 Common-mode choke** on DC power from a long cable.
- **PI-072 Pi filter (CLC) or T filter** at power entry for EMI-sensitive downstream circuits.
- **PI-073 Hot-plug inrush managed.** Soft-start circuit or inrush limiter on rails that ramp significant bulk cap.

## Per-IC patterns

- **PI-080 FPGA / high-pin-count digital**: decoupling scheme from the vendor's PDN tool, not ad-hoc.
- **PI-081 Op-amp**: 0.1 µF per rail, at the pin. Sensitive inputs may want 10 µF + 0.1 µF for LF rejection.
- **PI-082 ADC/DAC**: analog and digital supplies independently decoupled, often with a ferrite bead between if both are derived from one rail. Reference pin decoupling per datasheet — treat reference pins as first-class power.
- **PI-083 Crystal oscillator ICs**: decoupling at the pin, plus a pull-up on enable and a ground guard around the crystal.
- **PI-084 RF PAs**: bulk + bypass + RF-choke on the supply pin; ferrite beads with self-resonant frequency above the operating band.

## Load-step / di/dt analysis

- **PI-090 Characterize the load-step event.** ΔI from min to max, slew rate dI/dt, repetition rate. MCUs waking from deep sleep: hundreds of mA in < 1 µs; FPGAs: amperes per ns in reconfiguration.
- **PI-091 Transient voltage excursion = L_loop × dI/dt** at high frequency, `ΔV = Δ I × Z_pdn(f)` at the excited frequency. Shrinking loop inductance is first-order.
- **PI-092 Response-time hierarchy** under a load step:
  - 0-100 ns: plane capacitance + closest MLCC
  - 100 ns-10 µs: local decoupling stack
  - 10 µs-100 µs: bulk caps
  - > 100 µs: regulator control loop
  Each layer must hold Z_target until the next takes over.
- **PI-093 Regulator bandwidth sets the slowest layer.** A regulator with 10 kHz crossover cannot react to a 1 µs event — upstream responsibility stops at ~100 µs.
- **PI-094 Anti-resonance between decap layers** (e.g., MLCC ESL vs bulk capacitance) produces impedance peaks; re-examine cap selection if peaks land within the load spectrum.
- **PI-095 Layout-induced inductance** between decap and load dominates at GHz-class events. Use via-in-pad, thick plane, proximity.
- **PI-096 Measurement technique.** VNA shunt-through on the live board or power-probe + scope under a programmable load; `signal-integrity:SI-096` for PSRR-sensitive paths.
- **PI-097 Simulator-driven sizing**. See `simulation-workflow:SIM-050..SIM-055` for PDN impedance simulation and transient response.

## Regulation accuracy (line / load / temperature)

- **PI-100 Spec the full error budget.** Output voltage accuracy = initial set-point tolerance + line regulation + load regulation + temperature drift + transient deviation. Compare Σ to downstream IC's supply tolerance.
- **PI-101 Initial set-point tolerance** from reference accuracy + divider tolerance + op-amp offset. 1% Vref + 1% resistor pair + 0.1% op-amp ≈ ±2% total on a standard LDO.
- **PI-102 Line regulation** (ΔVout per ΔVin) per datasheet, usually at DC — plus PSRR curve vs frequency for AC line noise.
- **PI-103 Load regulation** (ΔVout per ΔIout) typically 0.01-0.1% for good LDOs, larger for cheap switchers.
- **PI-104 Temperature drift** on feedback divider resistors (matched pairs help) and on the internal reference.
- **PI-105 Kelvin sense at the point of load** cancels IR drop in the supply trace. Route sense lines separately, as a differential pair to the sense pins, not the pour.
- **PI-106 Remote-sense cable length** limits by op-amp stability; add compensation network if the sense-cable phase margin thin.
- **PI-107 Margin test.** Run Vin min / Vin max / Tmin / Tmax combinations; the output must stay in spec at all corners.
- **PI-108 Digital POL regulators with PMBus** enable readback + trim; use for high-accuracy rails where budget is < 1%.
- **PI-109 Reference buffer on ADC / DAC supplies.** The regulator alone is insufficient — add reference-grade LDO (e.g., LT3042) or a dedicated voltage reference at the load.

## Low-power sleep modes

- **PI-120 Rails held up in sleep must stay in regulation at Iq-only load.** Some regulators lose accuracy or stop switching below a min load; pick parts with good light-load behavior (PFM / PSM / pulse-skip).
- **PI-121 Power-gating MOSFET on deep-sleep-only rails.** See `battery-and-low-power:BAT-056`.
- **PI-122 Wake-latency from sleep** includes regulator startup time. If waking means bringing up a rail, startup current inrushes the upstream again.
- **PI-123 Supervisor ICs in sleep** — match their Iq to the target (sub-µA class) and choose active-low / active-high polarity to avoid leakage paths.
- **PI-124 Leakage paths at sleep boundaries** — level shifters, pull-ups on isolated rails, ESD diodes reverse-biased. Audit each net that crosses a gated rail for microamp-class leakage (see `battery-and-low-power:BAT-070..BAT-076`).
- **PI-125 Retention supplies** for SRAM / register state are separate from main digital rail; typically 0.9-1.1 V; very low Iq budget; often from a tiny dedicated LDO or a supercap.
- **PI-126 Real-time clock rail** independent supply from coin cell / supercap; isolation from main rail via Schottky or ideal-diode.
- **PI-127 POR / BOR behavior at sleep entry and exit** — ensure no glitch wakes the MCU prematurely or misses a wake signal.

## Cold-start and inrush

- **PI-130 Inrush current = C_bulk × dV/dt** during power-up. A 1000 µF bulk cap on 12 V reaching steady state in 10 ms draws ~1.2 A peak average — instantaneous peak much higher.
- **PI-131 Upstream current limit.** USB source limits (100 mA / 500 mA / 1.5 A / 3 A), PoE class budgets, wall-wart ratings. Exceeding trips the source.
- **PI-132 NTC thermistor in series** cheap inrush limiter; cold resistance drops to near-zero after warm-up. Not suitable for repeated on/off cycles — NTC must cool.
- **PI-133 Active soft-start** — MOSFET with gate controlled via RC; hot-swap controller IC (LTC4215, TPS2420) for robust designs.
- **PI-134 Pre-charge resistor + bypass contactor** on high-voltage systems (EV packs, see `high-voltage-design:HV-053`).
- **PI-135 Cold-start below rated voltage.** Some regulators need > 1 V headroom to start; below that they hiccup. Specify startup Vin, not operating Vin.
- **PI-136 Low-temperature startup** — electrolytic ESR rises 10× at -40°C; inrush filter tuned at 25°C may fail cold. Test boundary conditions.
- **PI-137 Latch-up at cold-start.** Out-of-order rail bring-up driving I/O while core is still off; classic failure. PI-052..PI-055 enforced.
- **PI-138 Battery cold-start.** Internal resistance rises below 0°C for Li-chem; brown-out below the UVP threshold; see `battery-and-low-power:BAT-007..BAT-008`.
- **PI-139 Brown-out behavior documented** — what happens if Vin sags mid-operation? Regulator drops out? MCU resets? System crashes cleanly? Designed and tested behavior.

## How the engine uses these rules

- `suggest_decoupling` walks PI-001..PI-010 against the schematic + placement, proposing missing caps and flagging misplaced ones, with concrete `add_component` + `move_component` fix hints.
- `suggest_bulk_capacitance` runs PI-020..PI-025 against rail topology.
- `check_pdn_impedance` (if simulation available) runs PI-030..PI-034.
- `check_power_sequencing` inspects the schematic for PI-050..PI-055.
- `check_load_step_response` runs PI-090..PI-097 against load-profile metadata.
- `audit_regulation_accuracy` sums PI-100..PI-109 error terms and compares to downstream tolerance.
- `check_inrush_budget` walks PI-130..PI-139 against upstream source limits.
- Rules cross-reference `signal-integrity:SI-031..SI-036` for return-path implications and `electrical-cad-best-practices:CAD-401..CAD-406` for mixed-signal placement.
