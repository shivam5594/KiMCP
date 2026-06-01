---
name: signal-integrity
description: Signal integrity rules and checks for high-speed and sensitive signals — controlled impedance, terminations, length matching, return paths, crosstalk, via transitions, high-speed routing. Use when routing or reviewing high-speed, differential, RF, or timing-critical signals, or when the KiMCP domain-knowledge engine runs SI checks. Pairs with `power-integrity`; many SI problems have PI origins (return path, decoupling).
---

# Signal Integrity

Rules and checks for signal integrity. Rule ids use prefix `SI-`. Cited by the KiMCP domain-knowledge engine with full reasoning.

## Scope

- Controlled impedance (single-ended and differential)
- Terminations (series, parallel, Thevenin, AC, diff)
- Length matching / skew
- Return paths and reference planes
- Crosstalk (same-layer and interlayer)
- Via transitions and stubs
- General high-speed routing

Paired skills:
- Decoupling and PDN → `power-integrity`
- Trace/space limits → `dfm`
- Crystal / clock placement practice → `electrical-cad-best-practices` (CAD-204, CAD-209)

## When SI matters

Rule of thumb: treat a trace as a transmission line when its electrical length exceeds ~1/6 of the signal's rise-time wavelength. For a 1 ns rise time with ~150 mm/ns velocity, this is ~25 mm. Anything clocked > ~10 MHz or with edges < 2 ns is in SI territory.

## Controlled impedance

- **SI-001 Define impedance targets per interface.** USB 2.0 ≈ 90 Ω diff; USB 3 / PCIe ≈ 85 Ω diff; HDMI ≈ 100 Ω diff; Ethernet ≈ 100 Ω diff; DDRx SE ≈ 40-50 Ω; RF ≈ 50 Ω SE. Confirm per PHY datasheet.
- **SI-002 Stackup drives impedance.** Dielectric thickness, Dk, copper thickness all matter. Lock stackup before setting trace widths.
- **SI-003 Use an impedance calculator sourced from the actual stackup** (KiCAD PCB Calculator, Saturn PCB, or fab-provided). Record results in `docs/STACKUP.md`.
- **SI-004 ±10% impedance tolerance default**, ±7% for DDR, ±5% for RF. Tighten only if a signal requires it.
- **SI-005 Verify with the fab.** Fabs adjust trace width per actual copper/dielectric after lamination — request impedance-controlled stackup service and accept their final widths.
- **SI-006 Coplanar waveguide (CPWG) for RF on thin stackups** — co-planar ground gives better impedance control than pure microstrip below ~0.25 mm.
- **SI-007 Inner-layer signals use stripline, top/bottom use microstrip.** Impedance math differs. Do not mix without recalculating.
- **SI-008 Diff pair width/gap matches diff impedance target, NOT single-ended.** A "50 Ω pair" is wrong — diff impedance ≠ 2× single-ended impedance (coupling factor matters).

## Terminations

- **SI-010 Rule-of-thumb thresholds.** Trace length > (rise_time × velocity) / 6 → terminate. For 1 ns edges on microstrip, > ~25 mm.
- **SI-011 Series termination (source).** Place at driver, 22-33 Ω typical, tuned to driver Zout + trace Z0. Preferred for point-to-point digital with one receiver.
- **SI-012 Parallel termination.** 50 Ω to ground for 50 Ω line; wastes DC power; used for bussed signals.
- **SI-013 Thevenin termination.** Two resistors to Vcc / GND; biases the line; common on multi-drop buses.
- **SI-014 AC termination.** RC to ground; DC-blocked parallel; saves DC power on signals with balanced duty.
- **SI-015 Differential termination.** 100 Ω (or 90 / 85 per standard) across the pair at the receiver end; often internal to the PHY — check datasheet before adding external.
- **SI-016 Do not terminate a line twice.** Internal PHY termination + external = over-damping; signals look slow and weak.
- **SI-017 Terminators placed at the point where reflection would occur.** Source terminator at driver, parallel at the far end, diff at the receiver.

## Length matching / skew

- **SI-020 Tolerance per interface.**
  - DDR3 byte-group DQ/DQS: ±25 mil
  - DDR4 byte-group: ±10 mil
  - USB 2.0 D+/D-: ±150 mil
  - USB 3 SS: ±5 mil
  - PCIe Gen2/3: ±5 mil
  - HDMI pair: ±5 mil; group: ±50 mil
  - Ethernet 1G pair: ±50 mil; group: ±1000 mil
  Defer to PHY datasheet.
- **SI-021 Delay (ps) is the real target; length (mil) is an approximation.** Stripline and microstrip have different velocity — matching by length across layers introduces skew.
- **SI-022 Intra-pair match first, inter-pair second.** Correct P/N within the pair before matching pairs to each other.
- **SI-023 Meander close to the source of skew.** If the pair has a via transition that adds 10 ps to P, compensate adjacent to the transition, not at the far end.
- **SI-024 Meander with bumps large enough to respect coupling.** 3× trace width spacing between bumps; shorter bumps on tighter pitch.
- **SI-025 Avoid snaking across split planes.** Matches length but ruins return path.

## Return path & reference plane

- **SI-030 Every signal has a return path.** The return current flows on the nearest adjacent plane under the trace — continuity is non-negotiable.
- **SI-031 Do not cross a plane split.** Return current detours, loop area explodes, EMI and crosstalk follow.
- **SI-032 If crossing is unavoidable: stitch the planes with a ≤ 0.1 µF capacitor within 5 mm of the crossing** on the same side as the signal, providing an AC return.
- **SI-033 Reference-layer change = via transition concern.** Signal moving from layer referring to GND to layer referring to VCC needs a stitching via/cap near the signal via.
- **SI-034 Guard ground vias around high-speed signal vias** (within 1 mm on each side) to carry the return across the layer change.
- **SI-035 Keep signal vias away from plane edges** (≥ 3× dielectric thickness) to avoid edge-radiation pickup.
- **SI-036 Solid plane beats split plane for mixed signal** in nearly every case — see `electrical-cad-best-practices:CAD-401`.

## Crosstalk

- **SI-040 3W rule for parallel runs.** Keep centerline spacing ≥ 3× trace width between non-related signals on the same layer.
- **SI-041 2H rule for parallel runs on adjacent layers.** Route orthogonal or keep parallel runs short.
- **SI-042 Aggressors identified.** Clocks, high-edge-rate digital, switch-mode power signals.
- **SI-043 Victims identified.** Slow ADC inputs, references, analog signals, reset lines.
- **SI-044 Guard traces / guard ground** with stitching vias between aggressor and victim when 3W is not feasible.
- **SI-045 Spacing as a function of length of parallel run.** 1 cm run → 3W is fine; 10 cm run → look at 5-7W.
- **SI-046 Shield sensitive analog traces** by surrounding with ground or routing on an inner layer between plane layers.

## Via transitions

- **SI-050 Minimize via count on high-speed signals.** Each via adds inductance, capacitance, and a stub.
- **SI-051 Via stub < 25% of rise-time wavelength.** For 4 GHz → stub < ~4 mm; back-drill on thick boards.
- **SI-052 Anti-pad sized to match impedance.** Too-small anti-pad = capacitive via = reflection. Fabs publish typical anti-pad recommendations.
- **SI-053 Pair-via layout: symmetrical.** Same orientation, same anti-pad, ground vias on both sides.
- **SI-054 Place ground stitching vias near signal vias** at a distance ≤ quarter-wavelength of the highest signal frequency.
- **SI-055 Blind/buried vias preferred over through for high-speed** on thick stackups — but cost implications; confirm with `dfm`.

## General high-speed routing

- **SI-060 Route high-speed in inner layers between ground planes** (stripline) when layer count allows. Better shielding, controllable impedance.
- **SI-061 Keep serpentine tuning > 3W spacing** to avoid self-coupling.
- **SI-062 Right-angle bends forbidden on high-speed.** Use 45° or curved. Right angles add reflections at high frequency.
- **SI-063 Reduce length at the receiver side** when possible — length differences hurt less at the source if the receiver has internal equalizer.
- **SI-064 Diff-pair skew balanced at the source first** and then total length matched to its neighbors.
- **SI-065 Keep crystal/clock traces short and guard-grounded**; see `electrical-cad-best-practices:CAD-204`.
- **SI-066 Reset line treated as near-high-speed** — glitches cause resets; use a filter RC at the receiver pin and place away from aggressors.
- **SI-067 JTAG / SWD acceptable slower; still route with ground reference** to avoid noise coupling into the debug chain.
- **SI-068 DDR fly-by topology obeys address/command/control bus rules** — single-ended 40/50 Ω, short stubs, ODT configured on the IC, length matched within ±25-50 mil depending on DDR generation.

## Simulation checkpoints

- **SI-080 Run IBIS simulation on critical nets** when signal integrity margins are unclear. KiCAD supports IBIS through ngspice/IBIS-AMI bridges or external tools.
- **SI-081 Eye diagram margin** should be ≥ 20% horizontal and ≥ 20% vertical at the receiver pin on serial links.
- **SI-082 TDR check** if fab service includes it, for impedance-controlled lines.

## Jitter budget

- **SI-090 Jitter decomposed into components.** Random (RJ, Gaussian), deterministic (DJ = DDJ + PJ + BUJ + ISI). Total jitter TJ(BER) = DJ_pp + N × RJ_rms where N depends on BER target (N ≈ 14.07 for 1e-12).
- **SI-091 TJ budget apportioned across link elements.** Transmitter + channel + receiver + reference clock. Each gets a slice; no component is allowed to eat the full budget.
- **SI-092 Reference-clock jitter into PLLs** multiplies at the output. PCIe Gen3 ref clock spec: 1 ps_rms (phase); bad ref clocks wreck a good channel.
- **SI-093 Inter-symbol interference (ISI) dominates at long channels.** Grows with loss and dispersion; managed by equalization (see pre-emphasis).
- **SI-094 Crosstalk-induced jitter** maps to SI-040..SI-046; budget a share for NEXT/FEXT from neighbors.
- **SI-095 Power-supply-induced jitter** on PLL / VCO rails → PI problem; see `power-integrity:PI-082`. PSRR of the driver / PLL IC at the noise frequency sets the tolerance.
- **SI-096 Jitter peaking from loop bandwidth mismatches** — CDR bandwidth vs upstream PLL bandwidth. Follow standard-specific jitter transfer masks.
- **SI-097 Measurement setup matters.** Scope jitter noise floor + trigger noise degrades measured jitter; reference a low-noise scope and a clean trigger source; separate TX and RX jitter in the analysis.

## Pre-emphasis, de-emphasis, equalization

- **SI-100 Purpose.** Compensate channel loss at high frequency so the receiver sees an open eye. Required for modern SerDes (PCIe Gen3+, USB 3.1+, 10G+ Ethernet, MIPI D-PHY / C-PHY at long reach).
- **SI-101 TX FFE (feed-forward equalizer)** — pre-weights current / next bit to overshoot transitions. Coefficients (C-1, C0, C+1) from standard spec or link training.
- **SI-102 De-emphasis = FFE with negative post-cursor** — attenuates steady-state so high-frequency edges stand out relative to DC content. Same family as FFE, different tap sign convention.
- **SI-103 RX CTLE (continuous-time linear equalizer)** — analog peaking filter at the receiver; gain vs frequency. Adapted via AGC or fixed per link profile.
- **SI-104 RX DFE (decision-feedback equalizer)** — digital equalization post-decision, cancels post-cursor ISI. Powerful but noise-sensitive; requires careful training.
- **SI-105 Link training** (PCIe Gen3+, USB3.1+, 100G-base-KR) negotiates EQ coefficients between TX and RX at startup. Channel must be within training range or link falls back / fails.
- **SI-106 Sim and measure with EQ enabled.** Unequalized eye tells you little about link margin; always present the post-EQ eye.
- **SI-107 Don't over-equalize.** Too much boost amplifies noise and crosstalk; link BER degrades.
- **SI-108 IBIS-AMI (see `simulation-workflow:SIM-031`)** is the canonical model for simulating TX/RX equalization algorithmically.

## Simultaneous-switching noise (SSN) / ground bounce

- **SI-110 SSN = L × dI/dt** on shared power/ground inductance. N bits switching together scale the noise linearly.
- **SI-111 Ground bounce** raises the local ground reference; logic-low output rises above receiver VIL threshold → false highs. Same mechanism on Vcc = Vcc droop → false lows.
- **SI-112 Reduce shared inductance.** More GND pins on the IC, shorter vias, plane-adjacent power pads, more decoupling (per PI-001..PI-010).
- **SI-113 Edge-rate management.** SSN ∝ dI/dt ∝ 1/t_rise. Slower edges reduce SSN linearly; use edge-control drivers where the interface allows.
- **SI-114 Spread switching in time.** Skew memory-bus write strobes slightly within datasheet timing to avoid all lines switching on the exact same edge.
- **SI-115 Balanced signaling** (LVDS, diff pair, SSTL with ODT) rejects SSN by design — prefer differential on any interface where you can choose.
- **SI-116 Quiet-bit allocation** on parallel buses for ground/sense lines interspersed between signal lines; reduces effective aggressor count per inch.
- **SI-117 Package inductance dominant on many ICs.** Noise cannot be fixed at the board level alone; pick an IC whose package (flip-chip, FCBGA) minimises bond-wire inductance on data I/O.

## Connectors and cables as SI elements

- **SI-120 Connector insertion loss is a channel element.** Datasheet S-parameters or vendor-provided Touchstone; stack with PCB traces and via transitions (see `simulation-workflow:SIM-040`).
- **SI-121 Connector mating variability** adds jitter and reflection. High-cycle connectors (SMA, RJ45) spec insertion loss over lifetime; cheap connectors may not.
- **SI-122 Cable impedance matches the interface.** 50 Ω coax, 100 Ω ethernet twisted-pair, 90 Ω USB 2.0 cable. Mismatched cable = reflection at both ends of the cable.
- **SI-123 Cable length budgeted with loss curve.** E.g., Cat5e UTP ~2 dB/100m at 100 MHz but ~20 dB at 1 GHz; USB 3.0 5 Gbps cable ~3 m limit.
- **SI-124 Shielding on cable.** Foil + braid for EMI / EMC; 360° bond at both ends (see `electrical-cad-best-practices:CAD-208`).
- **SI-125 Mixed-mode S-parameters for differential connectors** — SDD, SCC, SDC, SCD. Common-mode conversion (SDC/SCD) is the EMI risk.
- **SI-126 Connector footprint SI-aware.** Ground pins on inner positions of high-speed signal groups shorten return loops; orientation matters (see pinout in datasheet).
- **SI-127 Vias at the connector on same footprint stack.** Pad → via → inner plane → signal via — all on the shortest path; avoid routing across the connector body.
- **SI-128 Bulkhead / panel connectors** with a length of unavoidable cable on the enclosure side are on the budget — include them in channel sim.

## Backplane / multi-board topology

- **SI-130 Drop length is the stub length from the backplane main trace to the daughter card connector.** Short drops < quarter-wavelength of the highest frequency; longer → reflections.
- **SI-131 Daisy-chain vs T-topology vs star.** Daisy-chain (DDR fly-by) — controlled, predictable. T-junction — midpoint stub. Star — long stubs unless terminated at every leaf. Match topology to the standard.
- **SI-132 Backplane impedance continuity across connectors.** Connector Z0 matches board Z0; PCB stackup same on mother and daughter.
- **SI-133 Slot loading variable.** A slot with no card has a different impedance than with a card. Design for both populated and empty configurations if hot-plug matters.
- **SI-134 Termination location.** Source term at driver, parallel at far end. In a multi-drop bus, terminator at electrical end regardless of physical.
- **SI-135 Back-drilling stubs** on long backplane through-holes (see SI-051).
- **SI-136 AC coupling caps** on high-speed serial links between cards decouple common-mode and absorb DC offset between boards. 0.1 µF or per-standard (100 nF PCIe, 22 nF USB3).
- **SI-137 Differential skew across connector / cable** accumulates; tighter tolerance on backplane launch than on simple point-to-point.
- **SI-138 Hot-swap design**: pre-charge / staggered pin lengths (GND + VCC long, data short) so ground makes/breaks first; inrush controlled; signals driven into receivers that survive live insertion.

## How the engine uses these rules

- Each rule declares applicability: `net_matches`, `trace_length_min`, `edge_rate_max`, etc.
- When `check_signal_integrity` runs on a board, the engine walks nets × rules and emits suggestions.
- `suggest_return_path` runs SI-031 / SI-032 / SI-033 and produces concrete via/cap placement suggestions.
- `suggest_termination` runs SI-010 through SI-017 and returns a termination scheme per net with R values computed against the net class's Z0.
- `check_jitter_budget` aggregates SI-090..SI-097 against link-standard budgets, flags exceedances.
- `suggest_equalization` maps SI-100..SI-108 onto identified long/loss-heavy channels.
- `check_ssn` walks SI-110..SI-117 against parallel-bus nets with concurrent-switch counts derived from the schematic.
