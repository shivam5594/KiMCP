# M8: Datasheet Fetcher + Cache + Revision-Diff (Research / Proposal)

Status: **Proposal** — not wired into the main server. This package is a self-contained
prototype for the milestone M8 subsystem. It targets eventual drop-in under
`src/kimcp/services/datasheet/` once M0 scaffolding lands. Nothing here assumes a
specific import path.

## Goals

- Fetch manufacturer datasheets for an MPN, preferring primary sources, with
  graceful fallback to authorized mirrors.
- Cache every version ever observed by the project, keyed by `(mpn, rev)`, so old
  revisions are never lost even if the vendor hides them (DS-060, DS-065).
- Produce a structured, classifiable diff between two revisions suitable for
  design-review prompts and the domain-knowledge engine (DS-061, DS-062, DS-064).
- Expose everything through Pydantic v2 models so the eventual MCP surface
  generates clean JSON Schema (ADR-0006).

## Non-goals (for this milestone)

- Errata cross-referencing (handled by the `errata-search` skill's subsystem in a
  later milestone — DS-031 integration point documented below but not built).
- OCR of image-only datasheets. We assume text-layer PDFs; image-only PDFs are
  flagged and returned with `extraction_confidence="low"` and no parameter-table
  delta (see "Open questions").
- Qualification-document handling (AEC-Q / PPAP / IMDS / SCDs — DS-070..DS-080).
  The schema carries optional fields for these so the subsystem is forward-
  compatible, but the fetcher does not gate on them.
- Live MCP tool wiring. `find_datasheet` / `diff_datasheet_revisions` are outlined
  in "Integration points" but live in the main server, not here.

## ADR alignment

- **ADR-0006**: Pydantic v2 is the schema source of truth (`models.py`).
- **ADR-0009**: No hardcoded paths. Cache root is resolved from
  `DatasheetConfig.cache_root` (defaults to `platformdirs.user_cache_dir("kimcp")`
  with an env-var override). No MPN-specific logic, no project-convention assumptions.
- **ADR-0010**: On-disk cache is content-addressed by sha256; sidecar JSON carries
  `(path, mtime, sha256)` so it plugs into the MCP resource cache model.
- **ADR-0013**: Every `DatasheetDiff` finding carries a `rule_id` pointing at the
  `DS-0xx` rule that justifies the classification.

## Source priority (matches DS-001..DS-006)

Each source is implemented as a `SourceAdapter` subclass. The fetcher walks them
in config order (default below) and takes the first successful, content-validated
PDF. Adapters that require API keys degrade to "skip with reason" when keys are
absent — no hard failure (consistent with `external_apis` config indirection).

| Rank | Adapter           | Rule     | Notes                                             |
|------|-------------------|----------|---------------------------------------------------|
| 1    | `ManufacturerAdapter` | DS-001  | Uses manufacturer-specific URL templates from config; per-brand resolver plugins. |
| 2    | `DigiKeyAdapter`  | DS-002   | Product-page scrape → datasheet link. Requires API key; degrades if absent. |
| 3    | `MouserAdapter`   | DS-002   | Same pattern as DigiKey.                          |
| 4    | `OctopartAdapter` | DS-003   | Used to *find* the URL, not as the source itself — we resolve, then re-fetch directly. |
| 5    | `LCSCAdapter`     | DS-004   | Useful last-mile mirror; rev verified before accepting. |
| 6    | `WaybackAdapter`  | DS-006   | Only triggered when `allow_archive=True`; for obsolete parts. |
| —    | `CommunityAdapter` | DS-005 | **Disabled by default.** Must be opted in per call via `FetchRequest.allow_community_mirrors=True` and logged to audit trail. |

Adapters emit `FetchAttempt` records so the final `FetchResult` carries the full
provenance chain for audit.

## Cache layout

Content-addressed on disk, keyed on `(manufacturer, mpn, rev)` with a sha256 leaf
so the same-revision PDF replaced in place by the vendor still produces two
distinct cache entries:

```
<cache_root>/
  datasheets/
    <mfr_slug>/
      <mpn_slug>/
        <rev_slug>/
          <sha256>.pdf            # raw bytes as fetched
          <sha256>.json           # sidecar metadata (CacheEntry)
        index.json                # per-MPN manifest: list of revs + sha256 pointers
      index.json                  # per-manufacturer manifest (optional; written lazily)
    _global_index.sqlite          # optional: quick (mpn) → revs lookup; rebuildable from JSON
```

- `mfr_slug`, `mpn_slug`, `rev_slug` are `slugify()`'d to safe lowercase ASCII with
  dashes. The raw values live in the sidecar `CacheEntry.metadata`.
- All `.json` sidecars are `CacheEntry.model_dump_json(indent=2)` — stable key
  order, suitable for committing into `<project>/docs/datasheets/` (DS-033, DS-060)
  when the user wants project-local reproducibility.
- The SQLite index is an *optimization*. If it's missing or corrupt, it is
  rebuilt from the JSON sidecars (self-healing cache).

### Why `(mpn, rev)` not `(mpn, url)`?

Because different URLs may serve the same revision (mirror convergence), and the
same URL may serve different revisions over time (vendor replacing the file).
Revision is the semantic key; url is provenance metadata.

### Rev-detection fallback chain

Some datasheets don't expose a structured revision. We resolve in order:

1. **Revision History table parse.** DS-063 — every professional datasheet has
   one; we pdfplumber the first ~25 pages for a table whose header matches
   `r"rev(ision)?( history)?"` (case-insensitive) and take the top row's rev
   string + date.
2. **Document-properties metadata.** `PDF /CreationDate`, `/ModDate`, Keywords.
3. **Filename pattern.** `stm32f103c8t6-rev5.pdf`, `DS12345_Rev9.pdf`.
4. **Fallback.** `unknown-<yyyymmdd>-<sha256[:8]>` using fetch date. Tagged
   `rev_provenance="inferred"` so downstream consumers know to be cautious.

## Rev-diff algorithm (`revdiff.py`)

Input: two `DatasheetRevision` records (old, new) resolved through the cache.
Output: `DatasheetDiff` Pydantic model with findings classified per DS-062.

Pipeline:

1. **Page-count delta.** Simple integer delta. Large swings (>10% or >5 pages)
   emit a `warn` finding; small swings are `info`.
2. **Section heading changes.** Extract headings (pdfplumber per-page text →
   regex for numbered headings like `^\d+(\.\d+)*\s+[A-Z]`), normalize whitespace,
   compute an ordered-sequence diff. Renamed/added/removed section findings
   carry the page numbers for both revs.
3. **Parameter-table delta (the load-bearing piece).**
   - Locate electrical characteristic tables by scanning for heading matches
     against a config-driven list (defaults: "Electrical Characteristics",
     "Recommended Operating Conditions", "Absolute Maximum Ratings", "DC
     Characteristics", "AC Characteristics", "Thermal Characteristics").
     Config is `DatasheetConfig.parameter_table_headings` so no hardcoding.
   - `pdfplumber.extract_tables()` with a tolerant strategy.
   - Normalize each row to `(parameter_name, symbol, min, typ, max, unit, conditions)`.
   - Match rows between revs by `(parameter_name, symbol)` using fuzzy
     normalization (lowercase, strip punctuation, collapse whitespace).
   - Per-row classification:
     - *Editorial* — only whitespace / punctuation diff. (DS-062 editorial.)
     - *Clarifying* — text change but numeric min/typ/max unchanged.
     - *Specification change* — any numeric value moved. Flag with the delta.
     - *New* / *Removed* — unmatched rows.
   - Best-effort: when a table can't be parsed confidently (confidence heuristic
     described in the docstring), emit a `ParameterTableUnparsed` finding instead
     of silent gaps. The design prefers "I don't know" over "maybe nothing".
4. **Errata-relevant field changes.** Config list of field names that, when they
   move, trigger a `critical` finding (defaults: operating temperature range,
   absolute maximum ratings, I/O voltage ranges, ESD ratings, bandgap voltage
   tolerance). Emits a `errata_candidate=True` marker so a later milestone's
   `errata-search` integration can auto-open an errata check.
5. **Silicon-rev hint.** DS-062 silicon-rev-linked — we look for changes in a
   "Device Identification / Silicon Revision" table or strings like "Device ID
   = 0x...". If the value moved, finding is tagged `silicon_rev_linked=True`.
6. **Overall classification.** The worst-severity finding wins the diff's
   `overall_severity`. `error`/`critical` blocks manufacturing-handoff downstream
   (DS-064).

## Integration points (for later milestones, not built here)

- **MCP tools** (map to `tool-catalog.md` conventions):
  - `find_datasheet(mpn, manufacturer=None, prefer_rev=None)` → `FetchResult`.
  - `list_datasheet_revisions(mpn)` → list of `DatasheetRevision` from cache.
  - `diff_datasheet_revisions(mpn, old_rev, new_rev)` → `DatasheetDiff`.
- **MCP resources**:
  - `kimcp://datasheet/{mpn}` → latest cached revision metadata.
  - `kimcp://datasheet/{mpn}/{rev}` → full `DatasheetRevision`.
  - `kimcp://datasheet/{mpn}/{rev}/pdf` → raw bytes, `application/pdf`.
- **Domain-knowledge engine hook**:
  - `DatasheetDiff` findings map cleanly to `Suggestion` records — each finding
    already carries `rule_id`, `severity`, `message`, `why`, `references`.
  - A diff with `errata_candidate=True` rows triggers the future
    `errata-search` subsystem to re-query (DS-031, DS-062 silicon-rev-linked).
- **Safety model**:
  - Fetching is non-destructive. No snapshot needed.
  - Cache eviction (if ever added) is destructive — defer design until limits matter.
- **Config surface** (additions to `configuration.md`'s `external_apis` section):
  ```
  [datasheet]
  cache_root               = "auto"          # auto ⇒ platformdirs.user_cache_dir("kimcp")
  project_mirror           = "<project>/docs/datasheets/"   # optional secondary write target
  source_order             = ["manufacturer", "digikey", "mouser", "octopart", "lcsc"]
  allow_community_mirrors  = false
  allow_archive            = true
  rate_limit_per_host_rps  = 1.0
  request_timeout_s        = 30
  max_pdf_mb               = 32
  parameter_table_headings = ["Absolute Maximum Ratings", "Electrical Characteristics", ...]
  errata_relevant_fields   = ["operating temperature", "absolute maximum", ...]
  ```

## Dependencies to promote into `pyproject.toml` (M0 task, not ours)

- `httpx>=0.27` (async client, HTTP/2)
- `pydantic>=2.6`
- `pdfplumber>=0.11`
- `platformdirs>=4`
- `aiofiles>=23`

Soft / dev-only:
- `pytest`, `pytest-asyncio`, `respx` (httpx mocking).

## Testing strategy

- `test_smoke.py`: end-to-end flow against a recorded fixture
  (`fixtures/stm32f103c8t6_rev5.pdf`) with `httpx` fully mocked via `respx`.
  - Fetches "rev 5" and "rev 8" of `STM32F103C8T6` (both fixtures).
  - Asserts cache structure on disk, then runs `diff_revisions` and checks that
    at least one `ParameterChange` and one section-heading change are emitted.
- Unit tests (not included in this research drop; M0 lands the test infra):
  - Per-adapter URL-resolution against captured HTML fixtures.
  - Cache self-healing (delete SQLite index, cache still resolves).
  - Revision-inference fallback chain with PDFs missing each hint.

## Open questions / trade-offs

1. **Parameter-table extraction fragility.** pdfplumber is good, but
   manufacturer-specific table layouts (multi-row headers, merged cells,
   footnotes interleaved as rows) will miss. For M8 we accept best-effort with
   a visible "unparsed" finding. A later milestone can bring in Camelot or a
   trained layout model if we need better.
2. **Octopart API terms.** OctopartAdapter is structured to *resolve* a direct
   URL then fetch from that URL — consistent with DS-003 ("never as the source").
   If Octopart's ToS limits direct re-fetching, swap to their provided signed URL
   flow. Flagged in code.
3. **Archive provenance.** Wayback snapshots can serve datasheets the
   manufacturer has removed. We preserve them, but mark `CacheEntry.archival=True`
   and include the Wayback timestamp so reviewers know the source is archival.
4. **Manufacturer URL templates.** Instead of scraping every vendor site, we
   carry a config-driven map of known URL patterns (loaded from a separate TOML
   so adding a manufacturer doesn't require a code change). This keeps ADR-0009
   clean — the patterns are data, not code.
5. **Rate limiting.** Per-host token-bucket at `rate_limit_per_host_rps` (default
   1 rps). Per-tool calls batched on the same host share the bucket. This keeps
   us polite and avoids vendor-side block-listing during CI runs.
6. **Project-local cache mirror.** DS-033 wants `<project>/docs/datasheets/` for
   reproducibility. We optionally *write-through* to the project mirror when
   `project_mirror` is configured, but the canonical cache is always the global
   cache — the project mirror is a copy for git-tracking convenience.

## File inventory

| File         | Purpose                                                      |
|--------------|--------------------------------------------------------------|
| `README.md`  | This document.                                               |
| `models.py`  | Pydantic v2 schemas for everything crossing a boundary.      |
| `fetcher.py` | Async source-ranked fetch pipeline with per-adapter HTTP.    |
| `cache.py`   | On-disk cache with sidecar JSON + self-healing SQLite index. |
| `revdiff.py` | pdfplumber-based structural diff producing `DatasheetDiff`.  |
| `test_smoke.py` | End-to-end smoke test using a recorded PDF fixture.       |
| `fixtures/` | Placeholder for recorded PDFs (empty in this drop; see below). |

### Fixture note

We do **not** check a vendor PDF into the repo. `test_smoke.py` generates a
minimal fixture PDF at test time using `reportlab` if installed, or falls back
to a pre-baked byte-level stub that exercises the code paths without being a
real datasheet. Production test infrastructure (M0) should add a checked-in
fixture with clear licensing — see the test file's docstring for requirements.

## Deferred rules (with reason)

| Rule    | Deferred because |
|---------|------------------|
| DS-031  | errata cross-check — `errata-search` subsystem is a separate milestone. We emit `errata_candidate=True` markers so it can consume them later. |
| DS-040  | die-revision-per-datasheet handling needs a part-marking input channel that doesn't exist yet. Schema carries `die_rev: str | None` for when it does. |
| DS-050–DS-056 | Application-note hierarchy — out of scope for *datasheet* fetcher. A sibling `appnote` service will plug into the same cache pattern. |
| DS-070–DS-080 | Qualification docs (AEC-Q, PPAP, IMDS, DO-254, mil-spec). Schema reserves optional fields (`aec_q_grade`, `mil_spec`, etc.) so parts can be tagged when the vendor portal integration lands. |
| DS-033 (project mirror) | Built as an *optional* feature via `project_mirror` config; turned off by default so M0 can wire it after safety-model review. |
