---
name: battery-and-low-power
description: Battery-powered and low-power design — chemistry selection, charger topology, fuel gauging, protection (OVP/OCP/OTP/reverse), pack design, sleep-mode architecture, quiescent-current budgeting, low-power MCU patterns, wake sources, leakage accounting, runtime estimation, and energy-harvesting integration. Use when any portion of the design is battery-powered, targets low standby current, or requires runtime estimation; or when KiMCP's domain-knowledge engine runs battery/low-power checks. Pairs with `power-integrity` (PDN), `high-voltage-design` (pack HV > 60 V DC), `compliance-and-emc-testing` (UN 38.3, battery-specific standards).
---

# Battery & Low-Power Design

Rules and checks for battery-powered and low-power systems. Rule ids use prefix `BAT-`.

## Scope

- Chemistry selection (LiPo, Li-ion, LiFePO4, NiMH, alkaline primary, coin cells)
- Charger topology and IC selection
- Fuel gauging and state-of-charge estimation
- Protection (over/under-voltage, over-current, over-temperature, reverse)
- Pack design (single-cell to multi-cell series/parallel)
- Sleep modes and power-mode architecture
- Quiescent current (Iq) budgeting
- Low-power MCU and peripheral patterns
- Wake sources and wake-from-sleep logic
- Leakage accounting (pull-ups, level shifters, parasitic paths)
- Runtime estimation
- Energy harvesting (solar, piezo, thermoelectric)

Paired:
- PDN rules → `power-integrity` (PI-)
- HV packs > 60 V DC → `high-voltage-design`
- Transport and safety certs (UN 38.3, IEC 62133) → `compliance-and-emc-testing` touches; full coverage out of scope here

## Chemistry selection

- **BAT-001 LiPo (Li-polymer)**: flat/flex packaging, 3.7 V nominal, 4.2 V full, 3.0 V empty. Best energy density per volume for custom shapes.
- **BAT-002 Cylindrical Li-ion (18650, 21700)**: robust, cheap, high energy density. Standard form factors; easy to source second-hand cells (caution: counterfeits).
- **BAT-003 LiFePO4 (LFP)**: 3.2 V nominal, longer cycle life (2000+), safer thermally. Lower energy density (~130 Wh/kg vs ~250 Wh/kg for LiPo). Preferred for long-life and safety-critical.
- **BAT-004 NiMH**: 1.2 V/cell, safe but lower density. Reasonable for slow-charge, long-standby designs.
- **BAT-005 Alkaline primary**: 1.5 V/cell, shelf life 10 years, no charging — devices with multi-year standby requirements.
- **BAT-006 Coin cells (CR2032, CR1225, …)**: low current, 3 V nominal, unsuitable for any load > ~5 mA pulsed or ~100 µA continuous. Use for RTC backup, tiny wearables, beacons.
- **BAT-007 Cell voltage and discharge curve** sourced from manufacturer datasheet — do not extrapolate generic curves. Internal resistance increases at low temperature and with aging.
- **BAT-008 Operating temperature window**: LiPo -20 to 60 °C discharge, 0 to 45 °C charge. LiFePO4 wider on discharge side; charging still limited. Design circuit to refuse charging below 0 °C (CAN damage cell).

## Charger topology

- **BAT-010 Linear charger** (e.g., TP4056, MCP73831): cheap, simple, fine for < 1 A charge and < 1 W dissipation. Heats up; derate or heat-sink.
- **BAT-011 Switching charger** (e.g., BQ25895, BQ2417x, LTC4155): efficient for > 1 A charge and USB-PD inputs.
- **BAT-012 CC-CV algorithm respected.** Constant-current to cell's max charge voltage, then constant-voltage taper to 0.05 C termination.
- **BAT-013 Charge termination at C/10 or C/20 per chemistry datasheet.** Premature termination undercharges; late termination ages cell faster.
- **BAT-014 NTC thermistor on cell** for charger IC's thermal-protection input. Without it, the IC assumes room temperature.
- **BAT-015 Input OVP and reverse-polarity protection** on the DC input to the charger. USB inputs are usually safe; barrel inputs are not.
- **BAT-016 Path-select logic** for devices that operate while charging: ideal diode + charge IC's OTG / power-path feature to prevent the system from back-feeding the charger.
- **BAT-017 Inrush / soft-start on switched chargers** to keep within upstream USB limits (100 mA / 500 mA / 1.5 A / 3 A depending on class).

## Protection

- **BAT-020 Dedicated protection IC** (DW01, BQ2940x, S-8261) with external MOSFET pair on every Li-chemistry cell. Required for safety cert + warranty.
- **BAT-021 OVP threshold per chemistry**: 4.25 V for standard LiPo; 3.65 V for LFP.
- **BAT-022 UVP threshold**: 2.75-3.0 V for Li-chemistry (avoids copper dissolution on over-discharge).
- **BAT-023 OCP threshold**: per cell's safe discharge rating; commonly 3-10 A for small LiPo.
- **BAT-024 OTP**: NTC thermistor, shut off above 60 °C.
- **BAT-025 Short-circuit protection** via the protection IC's fast threshold or a series fuse.
- **BAT-026 Reverse-polarity protection** on external connectors that could be miswired (barrel jacks, screw terminals).
- **BAT-027 Pack-level protection beyond cell-level** in multi-cell packs: cell-balancing IC monitors per-cell voltages and balances during charging.

## Multi-cell pack design

- **BAT-030 Balance-charge required for multi-cell Li**: BMS measures per-cell voltages, dissipative or active balancing.
- **BAT-031 Cell matching at assembly**: pack cells of similar capacity and internal resistance — imbalanced packs age prematurely.
- **BAT-032 Cell fuses** between cells for thermal-runaway containment on larger packs.
- **BAT-033 Pre-charge resistor** for capacitive loads on high-voltage packs.
- **BAT-034 Service disconnect / HV interlock** for packs > 60 V DC (see `high-voltage-design:HV-055`).
- **BAT-035 Sense-line routing**: each cell voltage sensed separately with low-current lines; locate fuse at the cell end of the sense wire.

## Fuel gauging

- **BAT-040 Coulomb-counting IC** (MAX1704x, LTC2941, BQ27xx) for accurate state-of-charge (SoC) with long life.
- **BAT-041 Voltage-only SoC** works poorly for flat discharge curves (LiFePO4 especially); use coulomb counting for those.
- **BAT-042 Battery model calibrated** — each chemistry / manufacturer / capacity has its own discharge curve. Use manufacturer's provided battery profile or measure one.
- **BAT-043 Temperature compensation** — internal resistance and capacity both temperature-dependent.
- **BAT-044 Impedance tracking** (TI's Impedance Track, ADI's ModelGauge) gives best accuracy at cost of more compute / IC complexity.
- **BAT-045 Runtime estimate surfaces to user** with confidence bounds, not false precision.

## Sleep modes & Iq budgeting

- **BAT-050 Define power modes explicitly**: Active / Idle / Sleep / Deep-sleep / Ship mode. Document what is on, off, retained in each.
- **BAT-051 Iq budget spreadsheet** per mode. Sum every active LDO, MCU, sensor, pull-up, leakage path. Reconcile against measured Iq on prototype.
- **BAT-052 Target standby current based on runtime spec**: for 1-year standby on a 2000 mAh pack, average current ≤ 228 µA. Break down by mode occupancy: 99% sleep at 1 µA + 1% active at 22.5 mA = 230 µA.
- **BAT-053 Ship-mode / shipping mode** disconnects battery entirely — required for long shelf-life products with Li-chemistry.
- **BAT-054 Wake-on-event architecture**: deep sleep by default; wake on user button, RTC alarm, accelerometer interrupt, radio packet. Avoid polling.
- **BAT-055 Wake sources prioritized**; unwanted wake events can dominate average current.
- **BAT-056 Peripheral power-gate**: sensors, flash, radios on power-switch MOSFET or LDO-with-enable; not always-on.
- **BAT-057 Voltage regulator selection** for low Iq LDOs — Iq at no load matters more than full-load efficiency. TLV702 (~25 µA), LP5907 (~12 µA), MIC5219 (~100 µA) — pick the right one.
- **BAT-058 Buck converters for efficiency at higher loads**; switch to LDO at low loads for simplicity and low Iq. Some parts auto-switch (Pulse-Skipping Modulation).

## Low-power MCU patterns

- **BAT-060 MCU sleep current characterized** — datasheet numbers are typical-case. Measure on your PCB at target temperature.
- **BAT-061 RAM retention vs full reset** — trade-off: retention uses more sleep current but saves wake-up time and software state.
- **BAT-062 Peripheral clock gating** — disable peripherals you're not using; some peripherals draw disproportionate sleep current.
- **BAT-063 GPIO pull-resistors chosen to avoid leakage** — unused GPIOs as input with weak pull or output driven to Vcc / GND (depending on what's connected).
- **BAT-064 Level shifter direction-hold current** — bi-directional auto-direction shifters leak; prefer directional translators for always-on lines.
- **BAT-065 Flash / external memory sleep modes entered** — external QSPI flash can pull mA in idle unless placed in deep power-down.
- **BAT-066 Bootloader and firmware designed for wake speed** if wake-from-sleep latency matters.

## Leakage accounting

- **BAT-070 Pull-up and pull-down resistors contribute.** 10 kΩ pull-up on 3.3 V = 330 µA when driven low. Size pulls for the signal's slowest edge requirement, not habitually 10 k.
- **BAT-071 Voltage-divider sense lines** continuous leaks. Use enable-gated dividers for always-on rails.
- **BAT-072 LED indicators** draw 1-20 mA continuous. On-demand LEDs only, not "power ON" indicators in low-power designs.
- **BAT-073 Switching regulator quiescent** often higher than LDO. Many modern bucks have ultra-low-Iq modes (< 10 µA).
- **BAT-074 Cap leakage** small but non-zero; tantalum and electrolytic higher than ceramic. Matters on µA-class Iq budgets.
- **BAT-075 I2C / SPI pull-ups** if always-on, even in sleep. Gate pull-ups with sleep control or use I2C peripherals that release the bus.
- **BAT-076 RTC crystal oscillator current** (sub-µA typical) — select 32.768 kHz crystal with low-current oscillator driver on MCU.

## Runtime estimation

- **BAT-080 Capacity × average current × duty cycle** gives first-order runtime.
- **BAT-081 Derate for temperature, aging, BOL vs EOL** — specify runtime at EOL (end-of-life) capacity, typically 80% of BOL (beginning-of-life) after 300-500 cycles.
- **BAT-082 Worst-case-operating-profile** runtime = realistic lower bound; best-case runtime is marketing, not spec.
- **BAT-083 Validate with measurement.** Runtime estimation validated on representative hardware over at least one full discharge cycle.

## Energy harvesting

- **BAT-090 Harvester selection**: solar (indoor vs outdoor), thermoelectric, piezo, RF.
- **BAT-091 Harvesting IC with MPPT** for efficient conversion (TI BQ25504/5, ADP5090).
- **BAT-092 Storage element**: supercap (high cycle, low energy) or Li-chemistry buffer (higher energy, charge cycle limit).
- **BAT-093 Cold-start behavior** — some harvesting ICs need a minimum voltage to start; provide initial kick (button, separate primary).
- **BAT-094 Duty-cycling under harvest** — total energy budget = harvest rate × availability. Radio transmissions and sensor reads duty-cycled to match.

## Safety & transport

- **BAT-100 UN 38.3 required for shipping** lithium-chemistry batteries internationally.
- **BAT-101 IEC 62133** for secondary cells / packs safety.
- **BAT-102 UL 1642 / UL 2054** cell / pack US safety.
- **BAT-103 Transport markings** on packaging for Li-battery products (IATA DGR).
- **BAT-104 Consumer-accessible battery compartments** per IEC 62368 / applicable product standard — keyed or tooled access if high-energy.

## How the engine uses this skill

- `calculate_runtime(project, profile)` sums per-mode currents × duty cycle, derates for temperature/aging, returns runtime estimate with confidence bounds.
- `check_charger_compliance(charger_ic, cell_chem)` validates OVP/UVP/OCP/OTP thresholds against cell datasheet.
- `suggest_sleep_architecture(peripherals, wake_sources)` proposes mode structure and identifies peripherals that need power-gating.
- `audit_iq_budget(project)` enumerates always-on components with their Iq and flags sources > configurable threshold.
- Suggestions cite `rule_id: "BAT-xxx"` plus cell / IC datasheet references.
