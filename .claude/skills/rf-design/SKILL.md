---
name: rf-design
description: RF-specific design rules — controlled-impedance RF transmission lines, matching networks, antennas, shielding, RF grounding, component selection for RF, layout separation, and receiver-sensitivity protection. Use when any signal above ~30 MHz is present as an RF signal (as opposed to a high-speed digital edge), when designing antennas or matching networks, when integrating wireless modules, or when KiMCP's domain-knowledge engine runs RF checks. Pairs with `signal-integrity` (high-speed digital), `compliance-and-emc-testing` (radiated emissions).
---

# RF Design

RF-specific practice. Distinct from high-speed digital in that RF signals are intentionally generated sinusoids (or modulated carriers) with narrow-band spectral constraints. Rule ids use prefix `RF-`.

## Scope

- RF transmission lines (microstrip, CPWG, stripline variants)
- Matching networks (discrete and distributed)
- Antennas — trace antennas, chip antennas, connector-fed
- Grounding for RF
- Shielding (cans, pours, stitching)
- Component selection for RF (caps, inductors, baluns, filters)
- Wireless-module integration (Wi-Fi, BLE, cellular, sub-GHz, GPS/GNSS)
- Keep-out zones and layout separation
- ESD / transient protection at RF ports

Paired skills:
- High-speed digital SI → `signal-integrity` (SI-001..SI-082)
- Radiated emissions testing → `compliance-and-emc-testing`
- EM-field sim → `simulation-workflow` (SIM-060 EM-sim workflow)

## When "RF" applies

- Narrow-band signal bands generated on purpose (ISM 433/868/915 MHz, BLE/Wi-Fi 2.4/5/6 GHz, cellular 700 MHz..6 GHz, GPS/GNSS 1.2/1.5 GHz, UWB 6-9 GHz).
- Or any signal where s-parameters, VSWR, and S11 are first-class concerns rather than rise-time and settling.
- If the design only carries high-speed digital (USB, PCIe, DDR), prefer `signal-integrity`; this skill's rules won't apply cleanly.

## Transmission lines

- **RF-001 Stackup locked before any RF trace.** Dielectric thickness and Er dominate line impedance.
- **RF-002 50 Ω single-ended default for RF** unless the interface dictates otherwise (e.g., certain video at 75 Ω, specific RF front-ends with non-50 Ω matching).
- **RF-003 Microstrip for top-layer antennas / matched traces**; stripline for routing between ground planes. CPWG preferred when ground plane is thin (< 0.25 mm) or impedance control is tight.
- **RF-004 CPWG ground-stitching vias on both sides of the trace, pitch ≤ λ/10** at the highest operating frequency. For 2.4 GHz (λ ≈ 125 mm in FR-4), pitch ≤ 12.5 mm. Closer for 5 GHz.
- **RF-005 Width tolerance tight.** Fab tolerance ±10% typical; request impedance-controlled service for VSWR-sensitive designs (see `signal-integrity:SI-005`).
- **RF-006 Do not route RF through high-density via fields.** Each via is an inductive discontinuity; impedance deviates.
- **RF-007 Bends ≥ 45° or curved**; no right angles. Chamfered 45° bends must be properly truncated per standard (not arbitrary).
- **RF-008 Avoid stubs on RF traces.** Unused branches act as filters.
- **RF-009 Keep RF traces short.** Loss scales with length × frequency; 1 dB/cm at 5 GHz is not uncommon on FR-4. Use low-loss laminate (Rogers, Megtron) above a project-specific threshold (often > 6 GHz).

## Matching networks

- **RF-020 Pi or T networks common for narrow-band matching.** Shunt-series-shunt (Pi) or series-shunt-series (T).
- **RF-021 Place matching components on a short, direct line between the source and the antenna/RF input.** Do not route around other components.
- **RF-022 0402 or 0201 packages for > 1 GHz** matching components; larger packages parasitics dominate.
- **RF-023 Leave space for three matching components minimum** even on designs where you expect none — tuning after VNA measurement almost always changes the BoM.
- **RF-024 DNP (do-not-populate) positions available for alternate topologies.** Labeled on the schematic. Assembly stuffs per a matching table.
- **RF-025 Use RF-grade MLCC** (Murata GJM / GRM, TDK C-series high-Q lines) for matching components, not generic X7R.
- **RF-026 Match against the antenna's measured impedance**, not the datasheet nominal. Antenna impedance shifts with PCB ground size, nearby metal, enclosure.
- **RF-027 Document the reference match** in `docs/rf_tuning.md`: component values, s-parameter fits, date, VNA + jig used.

## Antennas

- **RF-030 Follow the antenna-manufacturer layout guide verbatim** for chip antennas. Ground plane size, keep-out, feed-line impedance, and matching topology are specified there.
- **RF-031 Ground-plane clearance per antenna spec.** Typically 5-20 mm of copper-free zone under and around chip antennas; bigger for PCB trace antennas.
- **RF-032 Antenna near a board edge** for chip and trace antennas. Interior placement cuts radiation efficiency.
- **RF-033 No traces, vias, or components in the keep-out** — this includes inner-layer copper.
- **RF-034 Antenna feed line length and impedance set by datasheet.** If the layout forces a longer feed, add a matching component row.
- **RF-035 Diversity / MIMO antennas spaced ≥ λ/4** at the lowest operating frequency.
- **RF-036 Connector-fed antennas**: U.FL / IPEX MHF / SMA. U.FL rated for ~30 mating cycles — not for repeated service. IPEX-1 most common for production.
- **RF-037 GPS/GNSS antennas need a hemisphere view.** Place on the top, no metal shielding above. Active antennas need bias-T and a stable 3 V feed with low noise.
- **RF-038 Cellular antenna requires careful ground size**; small ground planes hurt sub-GHz bands (LTE B12/17/28). If target sub-GHz and ground plane < 80 mm, use a long monopole IFA with counter-poise.

## RF grounding

- **RF-040 Continuous ground plane under RF traces.** No splits, no cuts, no traces on the reference layer under RF.
- **RF-041 Via-stitch the RF ground generously.** Perimeter stitch around RF regions at pitch ≤ λ/10 (or λ/20 for high-frequency RF).
- **RF-042 Ground "moat" around RF module footprints** connects module ground pour to main ground via dense vias.
- **RF-043 Star grounding discouraged for RF** except for very specific reasons. Distributed ground with stitching is the norm.
- **RF-044 RF and digital ground NOT split.** A single solid ground plane with regional stitching and placement discipline beats split planes (see `electrical-cad-best-practices:CAD-401`).

## Shielding

- **RF-050 PCB-mount shield cans over sensitive RF blocks** (LNA, mixer, VCO, PA output) when the system is enclosure-level sensitive.
- **RF-051 Shield-can fence keepout** inside the can: footprint for fence soldered to ground; components set back ≥ 0.5 mm from fence inside.
- **RF-052 Hole / slot pattern in shield-can lid** for access / air flow sized ≤ λ/20 at highest frequency to avoid radiating.
- **RF-053 Pour-and-stitch "virtual shield"** on inner layer between RF and aggressor blocks when a shield-can isn't feasible.
- **RF-054 Board edge plating** (castellated or edge-plated) on RF modules blocks edge radiation; confirm fab supports it (see `dfm:DFM-032`).

## Component selection

- **RF-060 Inductors: wirewound high-Q for matching**; multilayer chip inductors below ~500 MHz acceptable. Q at operating frequency stated in datasheet — use it.
- **RF-061 Capacitors: C0G/NP0 for matching**, tight tolerance (≤ 2%). X7R ceramics shift too much with DC bias and temperature.
- **RF-062 Baluns selected for frequency range + impedance ratio** (e.g., 1:1 50 Ω → 50 Ω, 4:1 50 Ω → 12.5 Ω for certain transformer-coupled frontends).
- **RF-063 Filters (LPF/BPF) with stated insertion loss and return loss** across the band.
- **RF-064 SAW / ceramic filters for receiver pre-selection** when operating in noisy bands.
- **RF-065 PA / LNA selected for P1dB, IP3, NF** appropriate to budget analysis.
- **RF-066 Crystals / TCXOs for RF clocks** — phase noise matters; reference the IC's requirement (often degraded phase noise = degraded EVM).

## Wireless-module integration

- **RF-070 Reference design first.** Vendor-supplied schematic and layout followed literally for first revision; deviate only after measurement.
- **RF-071 Module keep-outs respected absolutely.** Keep-out under module = no traces, vias, or planes even on inner layers.
- **RF-072 Supply decoupling at module per datasheet.** Typically multiple values (see `power-integrity:PI-080..PI-084`).
- **RF-073 Module ground connected by dense vias.** Pad-every-other-via pattern or equivalent.
- **RF-074 RF output trace from module to antenna is impedance-controlled 50 Ω microstrip / CPWG** with matching component positions even if vendor says "no matching needed" — you may still need to tune on real PCBs.
- **RF-075 Certification pedigree.** Pre-certified modules save compliance time; confirm FCC/CE/ISED/MIC modular-approval IDs in BOM.
- **RF-076 Host-level layout may still fail compliance** if digital aggressors leak into RF — module pre-cert doesn't guarantee the whole board.

## Layout separation

- **RF-080 RF, digital, and power regions physically separated** on the PCB.
- **RF-081 Switchers, clocks, high-edge-rate digital routed away from RF region**; if unavoidable, on inner layers between ground planes with heavy stitching.
- **RF-082 Place sensitive RF (LNA input, antenna feed) as far from aggressors as the board allows**.
- **RF-083 Pattern-based netclass separation** encoded in `.kicad_dru` — high-aggressor nets blocked from RF keep-outs.

## ESD / transient at RF ports

- **RF-090 ESD at antenna ports with RF-rated TVS.** Generic TVS capacitance detunes the match. Use explicitly RF-specified devices (Nexperia PESD-X, Littelfuse SP3003 class).
- **RF-091 Low capacitance ESD parts** (sub-pF) for GHz antennas; a 1 pF part at 2.4 GHz reactance ≈ 66 Ω — already a mismatch.
- **RF-092 DC block caps at RF ports** when DC is present (bias-T) — sized large enough to not attenuate in-band, small enough to not load.

## Receiver-sensitivity protection

- **RF-100 Noise budget documented.** Thermal floor kTB + receiver NF + implementation loss + margin → required SNR at input.
- **RF-101 Board-level noise floor estimated**: SMPS emissions, clock harmonics, digital switching — keep below receiver floor by ≥ 10 dB.
- **RF-102 Harmonic-free-zone checks** — aggressor harmonics in receiver band escalated to compliance review.

## How the engine uses this skill

- `check_rf_layout(project)` walks applicable rules against RF net classes and module placements.
- `suggest_matching_components(net)` returns Pi/T component slot suggestions with values derived from the project's stackup and the antenna/RF-IC datasheet.
- `check_rf_keepout(footprint)` validates antenna keep-out on placement.
- Suggestions cite `rule_id: "RF-xxx"` and may cross-reference `signal-integrity` and `compliance-and-emc-testing` rules.
