---
name: compliance-and-emc-testing
description: Regulatory compliance and EMC testing strategy — FCC Part 15, CE (EU EMC, RED, LVD), UKCA, ISED, MIC, CCC, UL/CSA, IEC 61000 series, CISPR emissions/immunity limits, pre-compliance setups, ESD/EFT/surge/RF immunity testing, radiated/conducted emissions fixes, debug workflow, test-house preparation, and modular-approval strategy. Use when planning certification, running pre-compliance, troubleshooting compliance failures, or when KiMCP's domain-knowledge engine is evaluating a design against a target market. Pairs with `rf-design`, `high-voltage-design`, `electrical-cad-best-practices` (EMI/EMC design-time), `signal-integrity` (SSN).
---

# Compliance & EMC Testing

Rules for passing emissions, immunity, and safety tests — plus the debug loop when a design fails. Rule ids use prefix `EMC-`.

## Scope

- Regulatory regimes per target market
- Pre-compliance: in-house setups, measurements, diagnostics
- Formal compliance at an accredited lab
- Test categories: radiated emissions, conducted emissions, radiated immunity, conducted immunity, ESD, EFT/burst, surge, dips/interruptions, harmonics/flicker
- Debug workflow and common fix patterns
- Modular approval strategy (pre-certified modules)
- Documentation: reports, declaration of conformity, technical file

Paired:
- Design-time EMI/EMC practice → `electrical-cad-best-practices:CAD-201..CAD-210`
- RF module pre-cert → `rf-design:RF-075`
- HV / mains LVD → `high-voltage-design`
- SSN origins → `signal-integrity`

## Regulatory regimes (overview)

- **EMC-001 United States**: FCC Part 15 for unintentional radiators (digital devices), Part 15 C for intentional radiators (wireless). Subparts B (digital), C/E/F (wireless bands).
- **EMC-002 European Union**: CE marking via EMC Directive (2014/30/EU), Radio Equipment Directive (2014/53/EU), Low Voltage Directive (2014/35/EU), RoHS (2011/65/EU), REACH.
- **EMC-003 United Kingdom**: UKCA mirrors CE post-Brexit.
- **EMC-004 Canada**: ISED ICES-003 (digital) and RSS-series (radio).
- **EMC-005 Japan**: VCCI (voluntary, industry-accepted) for emissions; MIC certification for radio (Giteki mark).
- **EMC-006 China**: SRRC for radio, CCC for safety/EMC on regulated products.
- **EMC-007 Korea**: KCC.
- **EMC-008 Australia/NZ**: RCM via ACMA.
- Regional bodies update — verify current citation before submission.

## Test categories & limits

### Emissions

- **EMC-010 Conducted emissions (CISPR 32 / EN 55032 / FCC Part 15 B)**: 150 kHz–30 MHz on AC mains. Class B (residential) stricter than Class A (industrial).
- **EMC-011 Radiated emissions (CISPR 32)**: 30 MHz-6 GHz typical; higher for devices operating above 108 MHz. Class B limit 40 dBµV/m @ 30-230 MHz, 47 dBµV/m @ 230-1000 MHz (3 m distance, quasi-peak detector).
- **EMC-012 Harmonics (IEC 61000-3-2)** and **flicker (IEC 61000-3-3)**: for mains-powered equipment with current > 16 A.

### Immunity

- **EMC-013 ESD (IEC 61000-4-2)**: contact discharge ±4 kV (industrial), ±8 kV (commercial); air discharge up to ±15 kV. Class B criterion: no permanent damage, temporary degradation OK.
- **EMC-014 EFT/Burst (IEC 61000-4-4)**: ±2 kV on power ports, ±1 kV on signal ports.
- **EMC-015 Surge (IEC 61000-4-5)**: ±2 kV line-to-earth, ±1 kV line-to-line on AC mains.
- **EMC-016 Radiated immunity (IEC 61000-4-3)**: 3 V/m (light-industrial) or 10 V/m (industrial) from 80 MHz to 6 GHz.
- **EMC-017 Conducted immunity (IEC 61000-4-6)**: 3 V or 10 V rms on cable ports, 150 kHz-80 MHz.
- **EMC-018 Dips/interruptions (IEC 61000-4-11)**: power-line interruption tests; equipment must behave per product standard.

### Other

- **EMC-019 LVD / safety**: IEC 62368-1 (audio/video/IT), IEC 61010 (measurement), IEC 60601 (medical). Covered by `high-voltage-design` where relevant.

## Pre-compliance

- **EMC-030 Near-field probe set** (H-field loops + E-field sniffer) used early — locates on-PCB emitters before chamber time.
- **EMC-031 Spectrum analyzer or SDR** with ≥ 6 GHz range and quasi-peak detection option.
- **EMC-032 GTEM cell or open-area test site (OATS)** for repeatable radiated measurements at some scale factor relative to accredited sites.
- **EMC-033 LISN (line impedance stabilization network)** for conducted emissions — FCC 50-µH M1 or CISPR 50-µH/50-Ω.
- **EMC-034 Correlation to accredited lab ≠ identical numbers**; aim to see trends, margins, hotspots. Pre-compliance passes with ≥ 6 dB margin typically translate to lab pass.
- **EMC-035 ESD gun** (IEC 61000-4-2 class) for immunity pre-check; ±4 kV contact / ±8 kV air.
- **EMC-036 Schedule pre-compliance early.** The first failing pre-compliance is the most valuable — catches layout-driven issues before PCB re-spin is expensive.

## Debug workflow

- **EMC-040 Radiated emissions fail → frequency identified first.** Harmonic of a known clock? Switching frequency of SMPS? Data bus line rate?
- **EMC-041 Near-field probe hunts the source.** Probe hovered over suspected areas; peak measured against surrounding baseline.
- **EMC-042 Once source located, fix pattern**:
  - Clock: slower edges (source-terminated), shield layer, reduced clock amplitude, spread-spectrum.
  - SMPS: input/output filtering, layout loop area reduction, snubber, shield can, common-mode choke.
  - Data bus: impedance control, termination review, common-mode choke on cable.
- **EMC-043 Conducted emissions fail → AC mains filter review.** X+Y caps, CM choke, differential-mode choke, current rating of ferrite.
- **EMC-044 ESD fail → injection point analyzed.** TVS placement must be upstream of any sensitive component relative to the ESD entry; PCB routing to TVS matters (short, direct).
- **EMC-045 Radiated immunity fail → cable harness resonances.** Ferrite beads on cables, shield bonding, common-mode chokes.
- **EMC-046 Each fix verified before next**; don't throw all the parts at the board at once — you lose signal on what actually mattered.
- **EMC-047 Fix priority**: layout (cheapest re-spin), filters (mid), shielding (expensive), mechanical (most expensive).

## Design-level fix patterns

Shortcut mapping from symptom → layer to inspect. Each cross-references a design-time rule where applicable.

- Digital harmonics dominate radiated spectrum: clock layout, edge rates, return paths (`signal-integrity:SI-030..SI-036`, `electrical-cad-best-practices:CAD-209`).
- SMPS switching frequency and harmonics dominate: input/output filtering, switch loop, shield (`power-integrity:PI-061..PI-066`).
- Broadband noise: ground plane splits? return-path crossings? dangling connectors? (`signal-integrity:SI-031`).
- ESD fails at data connector: TVS sizing, placement, ground to chassis (`electrical-cad-best-practices:CAD-205`).
- RF PA self-emissions: match network, low-pass, shielding (`rf-design:RF-020`, `RF-050`).

## Certification paths

- **EMC-060 DIY compliance with accredited lab reporting**: cheapest for tech companies; requires competent in-house pre-compliance.
- **EMC-061 Turn-key via compliance consultant**: higher cost; right for first-time designs or regulated-industry products.
- **EMC-062 Modular approval**: use pre-certified radio modules (Wi-Fi / BLE / cellular modules with FCC/IC/CE/IMI IDs). Host system still needs EMC testing, not emissions in the module's band.
- **EMC-063 Medical / industrial specific standards** (IEC 60601, IEC 60945 marine) layered on top of general EMC — additional tests.
- **EMC-064 Automotive**: ISO 7637, ISO 11452, CISPR 25 — separate from consumer regulations.

## Test-house preparation

- **EMC-070 Test plan written and reviewed.** Modes exercised per product: max data rate, all peripherals active, worst-case cable config.
- **EMC-071 Test fixture provided.** Batteries fully charged or simulators; cables documented; representative plastic/metal enclosure.
- **EMC-072 EUT documentation**: block diagram, operating modes, cable lengths, installation instructions. Test lab needs it ahead of time.
- **EMC-073 Multiple EUT units shipped.** Board-level failures sometimes unit-specific; two or three units insures schedule.
- **EMC-074 Photos of EUT from all angles** with test setup — part of the technical file.

## Documentation & declarations

- **EMC-080 Declaration of Conformity (DoC)**: product name, manufacturer, standards referenced, responsible party, signature. Retained 10 years per EU directives.
- **EMC-081 Technical file**: schematics, BOM, layout, user manual, test reports, risk assessment, DoC. Retained per directive.
- **EMC-082 User manual EMC statements**: "Caution: changes or modifications not expressly approved by [manufacturer] could void the user's authority to operate the equipment" and similar per region.
- **EMC-083 Labeling**: FCC ID, CE mark, UKCA mark, warnings — per product standard.

## Modular approval pitfalls

- **EMC-090 Pre-certified module ≠ free pass at product level.** Host-level emissions testing still required for the total product.
- **EMC-091 Module antenna swap or attenuator change voids modular approval** in most jurisdictions.
- **EMC-092 Transmitter power at the antenna terminal is what's certified.** Host-level PCB traces contributing to emissions don't fall under modular.
- **EMC-093 Permitted antenna list per module** is part of the approval — use antennas on the list or re-certify.

## How the engine uses this skill

- `estimate_emissions(project)` uses known clock frequencies + switching regulators + bus rates to predict which spectrum ranges risk EMC-010/EMC-011 failure.
- `check_immunity_protection(connector)` walks ESD/EFT/surge rules against connector footprints, flags missing TVS / CM chokes / ferrite beads.
- `suggest_emc_fixes(failure_frequency, measurements)` returns ranked fix candidates with cross-references to design-time rules.
- `build_technical_file(project)` assembles a DoC-ready directory from project artifacts and BOM.
- Suggestions cite `rule_id: "EMC-xxx"` plus the regulatory-standard clause.
