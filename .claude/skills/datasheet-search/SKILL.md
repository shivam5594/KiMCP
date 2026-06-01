---
name: datasheet-search
description: Locate and extract authoritative datasheet information for electronic components — where to look, how to identify the correct revision, how to extract pinouts, electrical specs, application circuits, and layout recommendations. Use when KiMCP needs to validate a part choice, generate a footprint, check a pinout, or ground a design decision in manufacturer data. Pairs with `errata-search` (post-publication issues), `vendor-search` (buying), `3d-models-and-footprints-search` (physical package).
---

# Datasheet Search

How to find, verify, and extract from component datasheets. Rule ids use prefix `DS-`.

## When to use this skill

- Validating pinout before footprint generation or pin-assignment.
- Sizing decoupling, pull-ups, reference dividers.
- Pulling absolute-maximum and recommended operating conditions for derating.
- Grounding a `suggest_*` output in manufacturer guidance.
- Cross-checking second-source parts.
- Layout guidance for switch-mode supplies, ADCs, RF, high-speed interfaces.

## Source priority

Always prefer primary over secondary.

- **DS-001 Primary (manufacturer's site).** `<manufacturer>.com/products/<part>/datasheet.pdf` or a documented equivalent. Always check revision date.
- **DS-002 Manufacturer mirrors on distributor sites (DigiKey, Mouser, Newark).** Generally current but may lag; cross-check revision if critical.
- **DS-003 Octopart / FindChips aggregators.** Useful for "what's the current datasheet URL", never as the source.
- **DS-004 LCSC / JLCPCB part page.** Often links the correct datasheet; sometimes hosts an outdated version — verify revision.
- **DS-005 Legacy `datasheetarchive.com`, `alldatasheet.com`, `datasheet4u.com`.** Last resort only; ad-heavy, unvetted, sometimes stale. Never rely on these for errata.
- **DS-006 Obsolete / discontinued parts: Wayback Machine** snapshots of the manufacturer's page.
- **DS-007 Application notes (AN-xxx), layout guides, evaluation-board user guides** from the same manufacturer are equally authoritative and often more specific than the datasheet for layout.

## Identifying the right document

- **DS-010 Match the exact MPN, including suffixes.** `STM32F446RET6` is different from `STM32F446RCT6`. Suffix letters encode temperature grade, package, reel/tray.
- **DS-011 Revision date visible and recorded.** A suggestion that cites a datasheet must cite the revision (e.g., "Rev. 9, Jun-2024").
- **DS-012 Prefer the consolidated datasheet.** For families, manufacturers sometimes split into "product brief" vs "full datasheet". Use full datasheet for design.
- **DS-013 "Preliminary" datasheets are unstable.** Flag downstream decisions as "preliminary" too.
- **DS-014 Separate application notes from the datasheet** — AN content can supersede what's in the datasheet for application-specific layout.

## What to extract

Minimum for every part landed in the design:

- **DS-020 Absolute maximum ratings.** Voltage, current, temperature, ESD.
- **DS-021 Recommended operating conditions.** Vcc range, Iq, I/O ranges.
- **DS-022 Pinout and pin descriptions.** Verify against the schematic symbol.
- **DS-023 Package dimensions and lead tolerances.** For footprint generation, see `3d-models-and-footprints-search`.
- **DS-024 Decoupling recommendations.** Count, value, placement.
- **DS-025 Layout recommendations.** Ground plane, loop area, keep-outs, crystal placement, antenna trace.
- **DS-026 Thermal characteristics.** θJA, θJC, Tj max.
- **DS-027 Timing parameters** for synchronous interfaces.
- **DS-028 Functional block diagram.** Confirms which blocks exist on which pins.
- **DS-029 Application circuits.** Typical schematic for the role; verify values.

Per-role extras:
- **Power ICs**: feedback divider, compensation network, inductor selection, output cap ESR bounds.
- **ADC/DAC**: reference routing, analog supply decoupling, PCB recommendations, input filter.
- **MCU / SoC**: reset circuit, boot-mode pins, oscillator ranges (external vs internal), brown-out.
- **Wireless**: antenna matching network, RF layout, trace impedance, keep-out.

## Verification

- **DS-030 Cross-check datasheet MPN against BOM.** An "equivalent" or "second source" may have subtly different pinout or ratings.
- **DS-031 Cross-check datasheet revision against errata** (see `errata-search`). A rev without an errata document is not a guarantee of none — it is a data point.
- **DS-032 If two datasheets conflict**, prefer the newer revision. Record both and the resolution.
- **DS-033 Cache locally at `<project>/docs/datasheets/<mpn>_<rev>.pdf`** for reproducibility. The BOM references the cached revision.

## Extraction workflow

1. Resolve the exact MPN (suffix included).
2. Pull the manufacturer's page; capture revision date and URL.
3. Download and hash the PDF; store in `docs/datasheets/`.
4. Extract relevant sections (text, tables, images). Prefer the PDF's text layer over OCR.
5. Structure extracted data:

```
{
  mpn: str,
  manufacturer: str,
  datasheet_url: str,
  datasheet_rev: str,           # "Rev. 9, Jun-2024"
  datasheet_sha256: str,
  abs_max: { ... },
  recommended: { ... },
  pins: [ { number, name, type, function, power_domain } ],
  package: { name, dimensions_mm, lead_count, pitch_mm, source: "datasheet|calculated" },
  thermal: { theta_ja, theta_jc, tj_max_c },
  decoupling_recommendation: { per_pin: [...], bulk: [...] },
  layout_recommendation_pdf_page_refs: [ int ],
  timing: { ... }              # optional
}
```

6. Return the structured object as part of the MCP tool result; attach the canonical source URL + rev in every suggestion derived from it.

## Special patterns

- **DS-040 Die-revision-specific datasheets.** Some parts (older TI, Intel) publish per-die-rev datasheets. The part's top marking determines the die rev; cross-check.
- **DS-041 Family-wide parameters vs part-specific.** A family datasheet may give a range; the specific part's datasheet gives exact. Prefer specific.
- **DS-042 Regional differences.** Automotive-grade (e.g., -Q100) datasheets differ from commercial. Don't mix.
- **DS-043 Lifecycle status in the datasheet header** is a hint — cross-check with `vendor-search:check_lifecycle`.
- **DS-044 Obsolete parts:** capture the last-known-good datasheet and the obsolescence notice together.

## Application-note hierarchy

App-notes, user guides, and reference designs often carry more weight than the datasheet for layout, configuration, and system-integration decisions. Treat them as first-class sources.

- **DS-050 Document types ranked** (highest-authority first for their domain):
  - **Datasheet**: absolute specs, ratings, pinout, package. Last word on electrical limits.
  - **Errata / silicon advisory**: overrides datasheet where silicon differs from spec (see `errata-search`).
  - **Reference manual / user guide**: for MCUs/SoCs, this is the detailed functional doc; pinout / peripherals / registers.
  - **Application note (AN-xxx / TN-xxx)**: topic-specific best practice from the vendor — layout, EMC, specific peripheral use.
  - **Evaluation-board user guide / schematic**: a vendor-produced reference layout. Treat the schematic as the highest-fidelity "how to use".
  - **Selection guide / product brief**: marketing-adjacent; use only for picking among parts, never for design.
  - **Design-in kit / cookbook**: vendor's curated app-notes + eval schematics; common on power ICs (TI, ADI).
- **DS-051 App-note supersedes datasheet on topic overlap.** A datasheet may show a simplified decoupling scheme; the app-note for the same part often shows the production-grade scheme. Use app-note.
- **DS-052 Evaluation-board schematic as ground truth** for layout questions. The vendor shipped this board and measured it; follow it unless you have a documented reason to deviate.
- **DS-053 Cross-link app-note to datasheet revision** — an AN published against datasheet Rev 5 may not fully apply to Rev 9. Check the AN header.
- **DS-054 Selection guide inconsistency.** Selection guides often list numbers inconsistent with individual datasheets. Always cross-check.
- **DS-055 Vendor white papers and training decks** useful but not normative; cite as context, not authority.
- **DS-056 Layout file downloads** (Altium, PADS, OrCAD) from the vendor — read-only reference even if the tool isn't installed; convert via SDF / ODB++ if needed.

## Datasheet-revision diff workflow

- **DS-060 Cache every datasheet revision the project has used.** `<project>/docs/datasheets/<mpn>/<rev>.pdf` with sha256 alongside.
- **DS-061 Diff on revision bump.** When a new revision drops, structural diff between cached old rev and new.
  - Text-layer diff (most PDFs) highlights numerical changes in tables and paragraphs.
  - Page-image diff for figure/schematic changes.
- **DS-062 Classify diff outcomes:**
  - *Editorial*: typo fixes, cosmetic. No action.
  - *Clarifying*: same spec, clearer wording. Record.
  - *Specification change*: numbers moved. Audit the design against new numbers.
  - *Silicon-rev-linked change*: new part stepping — triggers `errata-search:ER-023` refresh.
- **DS-063 Revision history table required.** Datasheet ToC → "Revision History" — every professional datasheet has one. If missing, treat datasheet as lower-trust.
- **DS-064 Rev bump triggers a design-review prompt** on any affected part; unresolved diffs block manufacturing-handoff in the KiMCP workflow.
- **DS-065 Legacy revisions preserved forever.** Even if the vendor hides old revisions, the project needs the spec under which it was designed.
- **DS-066 Tooling**: `diff-pdf`, `pdftotext` + `git diff --word-diff`, or commercial (DraftCompare, Adobe Acrobat Compare). KiMCP can wrap whichever is available.

## AEC-Q, medical, aerospace document types

- **DS-070 AEC-Q series (Automotive Electronics Council).** Qualification standards, not datasheets — AEC-Q100 (ICs), AEC-Q101 (discretes), AEC-Q200 (passives). A part's AEC-Q qualification report is a separate document, requested from the vendor's automotive portal.
- **DS-071 AEC-Q grade in the MPN suffix** — usually a `Q` or `-Q1`. Quote that exact grade, not the commercial equivalent.
- **DS-072 PPAP (Production Part Approval Process)** deliverables for automotive: control plan, FMEA, process capability, gauge R&R, IMDS — vendor provides, project stores.
- **DS-073 IMDS (International Material Data System)** for automotive material compliance. Per-part declaration maintained by vendor; link into BOM.
- **DS-074 Medical (IEC 60601)** requires device-level safety & risk file. Components typically don't have "medical datasheets" but may have biocompatibility declarations (ISO 10993) for contact materials.
- **DS-075 Aerospace (DO-254 for hardware, DO-160 environmental)** needs component-level documentation: radiation tolerance (TID/SEE), lot qualification, traceability. Mil-spec and space-grade parts come with SCDs (Source Control Drawings).
- **DS-076 Military (MIL-PRF / MIL-STD / MIL-DTL)** specs often replace vendor datasheets entirely — the spec is the datasheet. Qualified Parts List (QPL) defines acceptable sources.
- **DS-077 Space-grade parts** ship with a much richer package: TID testing report, SEE report, lot qualification, screened traveler. All archived per the project's configuration-management plan.
- **DS-078 Compliance-linked datasheet fields.**
  - `aec_q_grade`: 0 / 1 / 2 / 3 / null
  - `medical_biocompatibility`: bool
  - `aero_radiation_tolerant`: bool
  - `mil_spec`: MIL-PRF-xxxxx or null
  - `ppap_available`: bool
  - `imds_id`: str or null
- **DS-079 Store qualification reports alongside datasheets** in the project's datasheet cache. Report revisions tracked independently from datasheet revisions.
- **DS-080 Vendor may charge or gate access** to automotive / medical / aerospace documents. Record the NDA + access terms in the project.

## How the engine uses this skill

- `find_datasheet(mpn)` — returns structured object above.
- `extract_datasheet_facts(mpn, section)` — returns a named subset (e.g., `"thermal"`, `"layout"`).
- `find_application_notes(mpn, topic)` — returns ranked list of app-notes by relevance.
- `diff_datasheet_revisions(mpn, old_rev, new_rev)` — structural diff with classification.
- `find_qualification_docs(mpn, standard)` — locates AEC-Q, biocompatibility, DO-254, mil-spec, etc.
- Suggestions that cite the datasheet include `references: ["datasheet:<mpn>:<rev>:<page>"]` so the user can jump to exact pages.
- When a tool's output depends on a datasheet (e.g., `create_footprint` referencing package dimensions), the datasheet revision is recorded in the tool's `meta` so the output is reproducible.
