---
name: vendor-search
description: Find and compare parts across distributors and manufacturers — availability, pricing, lifecycle status, MOQ, lead time, RoHS/REACH status, packaging (tape/reel vs tube vs tray), alternates, and cross-references. Use when pricing a BOM, checking stock, finding alternates for obsolete parts, validating lifecycle, or sourcing a part with tight constraints. Pairs with `datasheet-search` (spec), `errata-search` (silicon issues), `3d-models-and-footprints-search` (library assets).
---

# Vendor Search

Find, compare, and qualify component sources. Rule ids use prefix `VND-`.

## When to use

- Pricing a BOM and computing per-unit cost / NRE.
- Checking whether a part is available in required quantity with acceptable lead time.
- Finding drop-in alternates for out-of-stock or obsolete parts.
- Qualifying a second source for single-sourced critical parts.
- Lifecycle audit of a long-running design.
- Choosing between tape-and-reel, tube, and tray packaging per assembly-house preference.

## Source priority

- **VND-001 Primary distributors (authorized).** Digi-Key, Mouser, Arrow, Newark/Farnell, Avnet, Future Electronics. Authorized means: direct relationship with manufacturer, genuine stock, real datasheet links, honest lifecycle info.
- **VND-002 LCSC / JLCPCB.** Authorized in China; heavy overlap with JLC assembly; LCSC PN maps to JLC stock for assembly.
- **VND-003 Manufacturer direct.** Some manufacturers sell from their own store (TI store, Microchip Direct). Best for samples / small qty; lead time can be longer.
- **VND-004 Aggregators.** Octopart, FindChips, Z2Data, Netcomponents — *search* authorized distributors + brokers. Use for price comparison; always route orders through authorized sources.
- **VND-005 Brokers.** Last resort, counterfeit risk is real. Require X-ray, decapsulation, or AS6171-compliant inspection for anything critical.
- **VND-006 Second-hand markets (AliExpress, Taobao, eBay).** Never for production; limited use for repair-parts-of-last-resort after provenance check.

## Checks per part

- **VND-010 Lifecycle status.** Active / NRND / EOL / LTB / Obsolete. Prefer Active. NRND + adequate last-time-buy stock is acceptable for a short-life product.
- **VND-011 Availability.** Stock on hand, on order, lead time at the authorized distributor of choice.
- **VND-012 MOQ and price break.** Some parts are 1+ priced, some jump at 10 / 100 / 1 000 / reel.
- **VND-013 Package options.** Reel / tube / tray / cut-tape. Assembly houses charge extra for cut-tape and for new reel setups; reel quantity minimums may dominate cost.
- **VND-014 Country of origin.** For US / EU customs and ITAR-sensitive designs.
- **VND-015 RoHS / REACH / lead-free status.** Must match the product's compliance target.
- **VND-016 Temperature grade.** Commercial / Industrial / Automotive (-Q100) / Military. Match to operating environment.
- **VND-017 Date code / year of manufacture.** Old stock of polymer caps, tantalums, and some silicon can have re-reliability concerns.
- **VND-018 Packaging for hand-solder vs reflow.** Tape-and-reel for SMT line; cut-tape / tubes acceptable for hand-built prototypes.

## Alternate selection

- **VND-020 Define "drop-in" precisely.** Same pinout? Same package? Same electrical spec across range? Same pattern of datasheet tolerance?
- **VND-021 Cross-reference tools.**
  - Manufacturer sites publish cross-reference tables for generics (e.g., TI ↔ ADI op-amps).
  - DigiKey/Mouser "Alternative Parts" — honest but shallow.
  - Octopart "Similar Parts" — noisy, manual vet required.
  - Z2Data, SiliconExpert — paid, deeper.
- **VND-022 Evaluate alternates against these axes.**
  1. Pinout (identical? pin-translatable? different?)
  2. Package (footprint compatible?)
  3. Absolute maximum ratings (broader than original? stricter?)
  4. Functional spec (gain, bandwidth, resolution, …)
  5. Timing / interface compatibility
  6. Datasheet revision of alternate (is it stable? errata?)
  7. Lifecycle status of alternate (better or worse?)
  8. Price / availability
- **VND-023 Rank alternates** in search results: exact drop-in first, footprint-compatible second, pin-translatable third, redesign-required last.
- **VND-024 Document alternate footprint in the symbol.** `Alternate_MPN_1`, `Alternate_MPN_2` fields on the symbol so the BOM always has second sources.

## Lifecycle monitoring

- **VND-030 Subscribe to manufacturer PCNs** for every critical part — automated via Digi-Key, Arrow, or Z2Data tools.
- **VND-031 Annual lifecycle audit** on any project older than six months. Active-part-today becomes NRND-next-year surprisingly often.
- **VND-032 LTB timing tracked.** Last Time Buy windows vary — some one month, some one year. Plan stock accordingly.
- **VND-033 Counterfeit risk rises as lifecycle declines.** Tighten source control on any NRND/EOL part; require AS6171 inspection for brokers.

## Data fields extracted per part

```
{
  mpn: str,
  manufacturer: str,
  lifecycle: "active" | "nrnd" | "eol" | "ltb" | "obsolete" | "unknown",
  lifecycle_as_of: ISO8601 date,
  distributors: [
    {
      name: "digikey" | "mouser" | "arrow" | "lcsc" | ...,
      distributor_pn: str,
      stock: int,
      price_breaks: [ { qty, unit_price, currency } ],
      moq: int,
      packaging: "reel" | "cut-tape" | "tube" | "tray",
      country_of_origin: str,
      lead_time_weeks: int | null,
      url: str
    }
  ],
  compliance: {
    rohs: bool,
    reach_svhc: bool,
    conflict_minerals: str,     # reporting status
    halogen_free: bool | null
  },
  temperature_grade: str,
  alternates: [
    {
      mpn: str,
      compatibility: "drop-in" | "footprint" | "pin-translatable" | "redesign",
      notes: str
    }
  ]
}
```

## Workflow

1. `find_vendor_data(mpn)` — aggregate from primary distributors (authorized first).
2. Normalize across distributors — price per qty curve, lead time, packaging.
3. Present comparison sorted by (lifecycle preference, price at project qty, lead time, stock available).
4. For alternates: invoke `compare_vendors` on each candidate, produce a ranked list.

## Integration with BOM

- **VND-040 BOM tool embeds a `sourcing` block** next to each part row: primary distributor pick, alternates, total-at-qty.
- **VND-041 "Fat BOM" export** includes vendor data snapshot; lives in `<project>/manufacturing/rev_X/bom_with_sourcing.csv`.
- **VND-042 Sourcing snapshot hashed** per revision; any change triggers a design-review alert.
- **VND-043 Assembly-house-ready BOM** (e.g., JLCPCB format with LCSC part column; PCBWay format) generated from the fat BOM.

## Special cases

- **VND-050 China-only parts.** Many capable ICs, MOSFETs, power parts are stocked only on LCSC. Authorize LCSC / JLCPCB as a primary for Asia-oriented runs; accept more friction for US/EU-sourced runs.
- **VND-051 Manufacturer-direct-only parts** (TI, Renesas, Intel older lines). Build lead time into schedule.
- **VND-052 Licenseable / export-controlled parts.** FPGAs, certain DSPs, RF PAs — check ECCN/HTS and end-user compliance before committing.
- **VND-053 Counterfeit-prone parts** (popular MOSFETs, memory, classic op-amps). Higher source-control standards; always authorized distributor.
- **VND-054 Long-lead-time parts** (MCUs in shortage years) — design in buffer stock or reserve via distributor holds.

## HS codes, tariffs, and export controls

Parts crossing borders carry a classification and a duty rate. Getting it wrong on a commercial invoice delays shipments and triggers compliance penalties.

- **VND-060 HS (Harmonized System) code per part.** 6-digit international baseline, extended nationally (HTS in the US, TARIC in the EU, 10-12 digits). Common IC codes: 8542.31 (processors), 8542.32 (memory), 8542.33 (amplifiers), 8542.39 (other ICs). Capacitors: 8532.2x. Passives: 8533.xx.
- **VND-061 HS code source priority**: manufacturer declaration > distributor product page > importer historical classification > binding-ruling service. Never guess.
- **VND-062 Tariff rate** is country-of-origin + country-of-import + HS combination. Section 301 (US-China) is a famous example; rates change politically — check current at ship time, not at design time.
- **VND-063 Country of origin** on distributor product pages is the legal CoO for tariff purposes — physical last-substantial-transformation. Not always where the fab is located.
- **VND-064 Free-trade agreements reduce tariffs** if origin qualifies (USMCA, EU-UK TCA, CPTPP, etc.). Required documentation: origin certificate, HS code, description.
- **VND-065 Export control (ECCN / CCL / EU dual-use list / Wassenaar)** distinct from tariffs. Parts flagged for EAR (US), ITAR (US defense), EU dual-use require export license for certain destinations.
- **VND-066 ECCN per part** usually 5A991 (generic) or 3A001.a.3 / .4 (encryption / high-performance processors / RF). Vendor declares; importer verifies.
- **VND-067 Sanctioned-party screening** against OFAC SDN, EU consolidated, UN sanctions — required by law in many jurisdictions; integrate into BOM build-release.
- **VND-068 Dual-use tech (FPGAs, DSPs, RF PAs, high-frequency ADC/DAC, crypto ICs)** often requires destination license for CN/RU/IR/KP even when commercially marketed.
- **VND-069 Importer of Record responsibility** — whoever imports holds the legal responsibility for correct classification. Turnkey EMS contracts should name the IoR explicitly.
- **VND-070 Extended BOM fields for trade:**
  ```
  hs_code: "8542.31.00"
  hs_code_source: "manufacturer-declared" | "distributor" | "historical" | "ruling"
  country_of_origin: "TW"
  eccn: "5A991"
  ftas_eligible: ["USMCA", "EU-UK-TCA"]
  license_required_for: [ "CN", "RU", "IR", "KP" ]
  ```
- **VND-071 Landed cost calculation**: unit price + freight + duty + broker fees + insurance. Often 5-20% adder on duty + freight; affects BOM cost meaningfully.

## Distributor API automation

Automating BOM pricing, lifecycle, and stock via distributor APIs.

- **VND-080 Digi-Key API v4** (OAuth 2.0 client credentials) — product-details, pricing, stock, alternates, categories. Rate-limited (usually 100 req/min free tier).
- **VND-081 Mouser API** — simple API-key auth; search, part-details, pricing. Limit ~1000 req/day free tier.
- **VND-082 Octopart / Nexar API** (GraphQL, paid tiers) — aggregates 300+ distributors; useful for finding-the-cheapest / cross-reference workflows. Pricey at volume.
- **VND-083 Arrow Electronics API, Avnet API, Farnell API** exist with individual auth. Use when those distributors dominate your mix.
- **VND-084 LCSC does not publish official API.** Community tools (EasyEDA scraper, `JLCPCB-Parts-Database`) exist; use respectfully, cache aggressively, fall back to web-scrape only on cache miss.
- **VND-085 Caching policy.** Distributor prices change daily, stock changes minute-by-minute. Cache pricing 24 h, stock 1 h, part-details 30 days. Cache keyed by (mpn, distributor, query_time).
- **VND-086 Rate-limit-aware clients.** Back off on 429; batch queries (Digi-Key supports up to 50 MPNs per request); parallelize across distributors.
- **VND-087 Credential storage** in OS keychain / env vars / secrets manager — never in git (see `kimcp-architecture:security.md`).
- **VND-088 BOM-level price refresh** scheduled hourly / daily by default; on-demand via tool call. Triggered automatically before manufacturing-handoff.
- **VND-089 API audit trail**: log every API call with timestamp, mpn, distributor, response summary. Helps reconcile pricing surprises and honor distributor T&C.
- **VND-090 Fallback chain**: primary authorized distributor → secondary → aggregator → manual. Each step logged; BOM notes primary-failed reason if fallback engaged.
- **VND-091 Data normalization**: currency conversion to project base currency; qty-break curves stored uniformly; lead-time fields harmonized despite distributor-specific wording.
- **VND-092 API T&C compliance** — Digi-Key API forbids resale of data, Mouser API forbids bulk mirroring. Respect; KiMCP's cache is private to the project.

## Consignment vs turnkey assembly

Two contracting models for assembly, each with sourcing implications.

- **VND-100 Turnkey (full-turnkey) assembly.** EMS procures all components, manages lot qualification, stores until build, assembles. Fewer touchpoints for the designer; EMS-controlled BOM.
- **VND-101 Consignment (kitted) assembly.** Designer procures all components, ships a kit to EMS, EMS assembles. Full BOM control; heavier logistics.
- **VND-102 Partial-turnkey / hybrid.** EMS procures commodity components (passives, generic ICs); designer consigns specialty / custom parts. Most common model for mid-volume.
- **VND-103 Turnkey upsides:** single point of accountability, EMS's purchasing leverage (often better prices), buffer-stock management, faster on reorders.
- **VND-104 Turnkey downsides:** EMS markups (typically 5-15% on component cost), approved-vendor-list restrictions (EMS may refuse rare parts), limited transparency on substitutions.
- **VND-105 Consignment upsides:** transparent BOM cost, designer picks exact MPN, can use direct vendor relationships / samples / distributor promo pricing.
- **VND-106 Consignment downsides:** kitting overhead, shipping / customs risk, MOQ waste for low-usage parts, lost-/damaged-in-transit handling.
- **VND-107 Substitution policy** in contract — explicit AVL per part, "equivalent only with designer approval", emergency substitution rules during line stops.
- **VND-108 Stocking agreements** for production runs — EMS holds N weeks of stock; designer reimburses on build draw. Common on turnkey, rare on consignment.
- **VND-109 IP & NDA considerations** — turnkey gives EMS access to the full BOM and design files; consignment can hold back. Relevant for products with proprietary components.
- **VND-110 Volume threshold for turnkey** — typically > 1000 units/quarter makes turnkey economically rational; below, consignment often cheaper because EMS markup dominates.
- **VND-111 Assembly house preference embedded in fab profile.** JLCPCB = turnkey-only for their in-house service; PCBCart / PCBA / MacroFab = either. Encode in project config.
- **VND-112 BOM format differs** — turnkey needs the EMS's template (JLCPCB CSV, PCBWay Excel, Macrofab JSON); consignment uses the designer's kit-BOM. `VND-043` handles both.

## How the engine uses this skill

- `compare_vendors(mpn)` — returns the per-part block above.
- `check_lifecycle(mpn)` — returns lifecycle status + source.
- `find_alternate(mpn, constraints)` — ranked alternates.
- `price_bom(project, qty)` — sums the fat BOM at project quantity across selected distributors.
- `classify_hs_code(mpn)` — fetches manufacturer HS declaration; falls back to distributor / historical.
- `compute_landed_cost(project, qty, destination)` — unit + freight + duty + broker fees.
- `screen_sanctions(project)` — runs end-user / end-destination checks.
- `refresh_distributor_data(project)` — hits APIs, respects caches and T&C.
- `export_assembly_bom(project, ems_template)` — emits EMS-format BOM for turnkey or kit-list for consignment.
- Suggestions produced by BOM/design-review cite `rule_id: "VND-xxx"` and the distributor source URL.
