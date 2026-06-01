---
name: errata-search
description: Locate and interpret component errata — post-publication manufacturer notices documenting known issues, silicon bugs, limitations, and workarounds. Use when validating a part choice, diagnosing odd behavior, or ensuring a design accounts for known silicon issues. Pairs with `datasheet-search` (initial spec), `vendor-search` (lifecycle / PCN tracking).
---

# Errata Search

Find, parse, and apply errata to a design. Rule ids use prefix `ER-`.

## When to use

- Before finalizing a part choice for a new design.
- When a reference design from the manufacturer deviates oddly from the datasheet — often explained in errata.
- When debugging a board that works "mostly" — marginal behavior is a classic errata footprint.
- Periodically during a product's lifecycle — manufacturers publish new errata after release.

## Sources

- **ER-001 Manufacturer errata document** — usually a separate PDF titled `<part>-errata.pdf` or `<part>-erratum.pdf` or `Product Advisory`. Examples:
  - STMicroelectronics: *Errata sheet* (`ES0xxx`).
  - NXP: *Errata* appended to datasheet or separate `Eratum`.
  - Microchip: `AN-xxx` errata notes.
  - TI: *Advisory* or *Silicon Errata* in their product folders.
  - Espressif: `Errata sheets` per chip rev (`ESP32_ECO...`).
  - Renesas / Dialog: *PCNs* + errata sheets.
- **ER-002 Product Change Notifications (PCN)** — not strictly errata but often contain limitations discovered between lots. Distributed via distributor portals and manufacturer subscription feeds.
- **ER-003 Manufacturer forum / knowledge base.** Vendor answers on their own forum (ST Community, NXP Community, Microchip Forum, Arm Connect, Espressif forum) sometimes document issues faster than formal errata.
- **ER-004 Errata compilation sites.** Community wikis and forums (e.g., EEVBlog threads). Treat as hints pointing toward a primary document; never cite as primary.
- **ER-005 Git issue trackers** for firmware that wraps hardware (vendor SDKs, Linux drivers) frequently document silicon bugs in workarounds. Line comments citing "SiLabs errata ID X" are high-signal.
- **ER-006 Reference-design release notes.** When a vendor ships a reference design with a FIXME, there's usually a silicon reason.

## Identifying applicable errata

- **ER-010 Errata apply to silicon revisions, not MPNs alone.** The top marking / date code reveals the silicon rev. Older units may have issues that newer stepping fixed.
- **ER-011 Die rev vs ordering code.** Sometimes two different ordering codes share a die; an errata list applicable to die X applies to all orderings built on it.
- **ER-012 Errata date must predate your stock.** An errata published *after* your manufacturing run may or may not apply; check the "first silicon rev affected" field.
- **ER-013 "Fixed in rev X" is a commitment**, but also a signal that older stock shipping into supply chains will still have the issue.
- **ER-014 Errata with workarounds classified as hard or soft.**
  - Hard: silicon-level; requires hardware workaround (pullup, series R, extra trace).
  - Soft: firmware workaround; note for software team.

## What to extract per erratum

```
{
  erratum_id: str,
  document_source: url,
  document_rev: str,
  silicon_revisions_affected: [ str ],
  fixed_in_revision: str | null,
  category: "functional" | "electrical" | "thermal" | "timing" | "security" | "other",
  description: str,
  conditions: str,                 # when does it manifest?
  impact: str,                     # what fails?
  workaround: {
    type: "hardware" | "firmware" | "combination" | "none",
    hardware_change: [ ... ],      # e.g., "pull-up on EN pin"
    firmware_change: [ ... ],      # e.g., "disable ADC calibration"
    notes: str
  },
  severity_recommended: "info" | "warn" | "error"
}
```

## Workflow

1. Given an MPN, locate the manufacturer's errata document (`find_errata(mpn)`).
2. Identify silicon rev(s) relevant to the project — either from top-marking photos, supplier date codes, or by asking the user.
3. Filter applicable errata.
4. For each, extract the structured object above.
5. Attach workarounds as hardware-change suggestions in the design-review output (`suggest_decoupling` / `add_component` / `edit_component` hints), with `rule_id: "ER-..."`.
6. Firmware-only workarounds surface as notes in the design-review output, tagged for software handoff — the MCP does not patch firmware.

## Integration with design review

- **ER-020 Design-review prompt fetches errata for every IC.** Board-level suggestions are produced for any hardware-workaround errata.
- **ER-021 Errata cached with the project.** `<project>/docs/errata/<mpn>_<rev>/<erratum_id>.md` with frontmatter of the structured object above. Cached content includes the resolution plan (applied / not-applicable-why / software-handoff).
- **ER-022 Unresolved errata surfaced in BOM validation.** BOM export warns if any listed MPN has a hardware-workaround erratum without a resolution note.
- **ER-023 Revision tracking.** When an errata document revision bumps, a design-review run surfaces the diff. The project's errata cache records the cached revision + the live revision and the delta.

## Anti-patterns

- **ER-030 Do not assume "no errata document = no issues".** For consumer parts, especially from smaller vendors, errata may not be published at all.
- **ER-031 Community-reported issues are data, not truth.** Track them separately; chase the manufacturer for confirmation before making a change.
- **ER-032 Don't silently apply a workaround.** Record it in `docs/errata/` with the citation so future designers know why the extra resistor exists.

## Silicon stepping from top-marking

- **ER-040 Top-marking is the authoritative silicon-rev identifier** for a specific physical part in hand. Ordering code alone doesn't reveal stepping.
- **ER-041 Top-marking layout varies by vendor.** Typical lines: manufacturer logo, part number, date code (YYWW), lot code, country, silicon rev / die revision letter. Some vendors encode the rev in the last letter of line 2 or in a separate glyph.
- **ER-042 Vendor decoder tables** usually in a separate app-note or on the product page — e.g., STM32's "How to identify the silicon revision", ESP32's "Chip revision identification", NXP's "Product identification system".
- **ER-043 Photograph every reel / tube / tray received** for a production run; archive with the lot for future errata-applicability checks.
- **ER-044 In-circuit rev read** where the silicon exposes it — `DBGMCU_IDCODE` on STM32, `EFUSE_RD_MAC_SYS_0` on ESP32, `DEVID` on many MCUs. Firmware logs the rev at boot; record in the test-data log.
- **ER-045 Date-code to stepping correlation** sometimes tracked by vendor: "Parts with date code > WW35/2024 are silicon rev Y". Useful but not a substitute for direct ID.
- **ER-046 Rev-lock manufacturing stock** — order all parts for a production run from a single lot / stepping when errata-sensitivity high; document the rev in the manufacturing-readiness review.
- **ER-047 Top-mark-recognition tooling.** OCR on production-line cameras captures rev automatically; KiMCP can accept a `lot_rev` field per BOM line. Without automation, bag-and-label at incoming inspection.
- **ER-048 Counterfeit detection.** Top markings are also the first thing counterfeiters fake — cross-check with X-ray die images and / or decapsulation on flagged lots (see `vendor-search:VND-005`).

## PCN (Product Change Notification) workflow

PCNs are distinct from errata: they announce intentional vendor changes to a part (die shrink, package change, fab move, material change). Tracked separately because the workflow is different.

- **ER-050 PCN is a forward-looking announcement**, not a backward-looking bug report. Vendor commits to a change on a date; action window usually 90-180 days.
- **ER-051 Subscribe to PCNs** via Digi-Key MyLists, Arrow ChangeNotification, Z2Data, or direct vendor portals (TI MyTI, ST myST, Microchip PCN portal, Nexperia, etc.). Every critical MPN on the BOM enrolled.
- **ER-052 PCN categories:**
  - *Die change* — silicon revision bump; re-qualify.
  - *Package change* — outline, lead material, finish; footprint & solderability check.
  - *Wafer fab change* — parametric drift possible; qualify.
  - *Assembly site change* — QA policy changes; usually no functional impact but audit.
  - *Marking change* — update top-mark decoder.
  - *Datasheet change* — triggers `DS-060` diff workflow.
  - *Label / packaging change* — logistics only.
  - *Discontinuance* — bridges to `vendor-search:VND-030..VND-033`.
- **ER-053 Impact assessment per PCN**: does it affect form, fit, or function? If yes, requalification planned within the response window; if no, signed acknowledgment and archive.
- **ER-054 Last-time-buy triggered** on discontinuance PCNs. Compute stock requirement through EOL; coordinate with `vendor-search:VND-032`.
- **ER-055 PCN trail on critical designs** is a compliance deliverable in some industries (automotive IATF 16949, medical ISO 13485).
- **ER-056 PCN cache.** `<project>/docs/pcns/<mpn>/<pcn_id>.md` with frontmatter: pcn_id, vendor, category, announcement_date, effective_date, cutoff_date, impact_assessment, action_taken.
- **ER-057 Differences from errata workflow:** PCNs are scheduled; errata are discovered. PCN response window is firm; errata workaround is optional if issue doesn't apply to design.

## Field-escape tracking

A field escape is an issue found after units shipped to customers — post-production, post-errata. Distinct process from pre-release errata.

- **ER-060 Source of field escapes:**
  - Returned units with failures (RMA).
  - Customer-reported anomalies.
  - Warranty claims patterns.
  - Service / repair logs trending up.
  - Connected-device telemetry (firmware crash counts, sensor drift, radio failure rate).
- **ER-061 Record per field escape:** serial number(s), manufacture lot, failure symptom, environment, firmware version, reproducibility, root cause when known.
- **ER-062 Root-cause categories:**
  - Component (errata, batch variance, aging / wearout, counterfeit).
  - Assembly (solder joint, cold-solder, mis-orientation).
  - Design (marginal timing, missed corner, EMC).
  - Firmware (regression, edge case).
  - Environmental (beyond spec).
  - User error / abuse.
- **ER-063 Connect field escape back to BOM & rev.** A pattern tied to a specific lot → new PCN or errata-applicability review. A pattern tied to a rev → design change for next rev.
- **ER-064 Containment action** — stop-ship, rework, return-and-repair, firmware patch, user advisory. Scale matches risk: safety issue → immediate containment; cosmetic issue → next-rev fix.
- **ER-065 8D or equivalent** problem-solving report for escapes of meaningful scale. The MCP can structure an 8D template referencing the failure dataset.
- **ER-066 Feedback loop into design.** Field escape resolution updates `docs/lessons_learned.md`; rule additions considered for this skill on the next review cycle.
- **ER-067 Regulatory reporting** for safety-relevant escapes: FDA MDR (medical), NHTSA (automotive), CPSC (consumer) — determine notification obligations early.
- **ER-068 Communication to customers** via field notice, service bulletin, or mass-email per affected population. Record deployed notice in the field-escape case.
- **ER-069 Statistical analysis on telemetry** — Weibull fit on failure rate by age / cycle / lot isolates wear-out from random failure; drives warranty reserve and next-gen design targets.

## How the engine uses this skill

- `find_errata(mpn, silicon_rev=None)` — returns list of applicable erratum objects.
- `decode_top_marking(mpn, marking_lines)` — returns silicon-rev + date code + country + lot.
- `fetch_pcns(mpn, since=None)` — returns active and historical PCNs from subscribed sources.
- `check_errata_resolved(project)` — walks BOM, checks the project's errata cache, returns unresolved items.
- `check_pcn_queue(project)` — returns open PCNs awaiting assessment.
- `suggest_erratum_workaround(erratum_id)` — returns structured hardware-change suggestion (component additions / edits) or firmware-handoff note.
- `log_field_escape(project, report)` — adds a field-escape case; triggers design-review refresh if root cause in BOM.
- Suggestions produced during design-review cite erratum ids; outputs are appended to the run's report.
