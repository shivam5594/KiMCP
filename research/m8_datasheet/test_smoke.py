"""End-to-end smoke test for the M8 datasheet subsystem.

What this exercises:
    - DatasheetCache.init() creates the on-disk layout.
    - DatasheetFetcher.fetch() walks adapters, rate limits, stores to cache.
    - Second fetch is a cache hit (from_cache=True).
    - diff_revisions() produces a DatasheetDiff with realistic findings
      between two handcrafted PDF fixtures that share an MPN but differ in
      an electrical-characteristic value.

Fixture strategy:
    This test does NOT check a vendor PDF into the repo. Instead, it
    generates two minimal PDFs at runtime that contain the shape of a real
    datasheet (Revision History header, one Electrical Characteristics
    table with Min/Typ/Max/Unit columns). We prefer `reportlab` if it's
    available; otherwise we fall back to a raw PDF writer that emits a
    minimal but valid PDF byte stream. Either way, no network.

Network:
    Adapters are replaced with a controlled in-memory adapter via the
    `extra_adapters` parameter of `DatasheetFetcher`, so the test is
    airtight — nothing leaves the process.

Run:
    pytest -xvs research/m8_datasheet/test_smoke.py
"""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from research.m8_datasheet import (
    DatasheetCache,
    DatasheetConfig,
    DatasheetFetcher,
    FetchRequest,
    ResolvedPdf,
    SourceAdapter,
    SourceKind,
    diff_revisions,
)
from research.m8_datasheet.fetcher import AdapterSkip


# ---------------------------------------------------------------------------
# Fixture PDF generation
# ---------------------------------------------------------------------------


def _make_pdf_reportlab(
    *,
    rev_label: str,
    vdd_max: str,
    page_count: int,
) -> bytes:
    """Build a simple datasheet-shaped PDF via reportlab."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    # Page 1: Title + Revision History
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, h - 72, "STM32F103C8T6 Datasheet")
    c.setFont("Helvetica", 12)
    c.drawString(72, h - 100, f"Revision {rev_label}")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, h - 140, "1 Revision History")
    c.setFont("Helvetica", 10)
    c.drawString(72, h - 160, f"Rev. {rev_label}   Jun-2024   Updated electrical characteristics.")
    c.showPage()

    # Page 2: Electrical Characteristics table
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, h - 72, "2 Electrical Characteristics")
    c.setFont("Helvetica", 10)
    y = h - 110
    c.drawString(72, y, "Parameter      Symbol   Min    Typ    Max    Unit")
    c.drawString(72, y - 20, f"Supply voltage Vdd      2.0    3.3    {vdd_max}    V")
    c.drawString(72, y - 40, "IO voltage     Vio      1.8    3.3    3.6    V")
    c.drawString(72, y - 60, "Operating temperature Ta  -40    25    85    C")
    c.showPage()

    for i in range(page_count - 2):
        c.setFont("Helvetica-Bold", 14)
        c.drawString(72, h - 72, f"{3 + i} Appendix Section {i}")
        c.setFont("Helvetica", 10)
        c.drawString(72, h - 100, "Filler.")
        c.showPage()

    c.save()
    return buf.getvalue()


def _make_pdf_minimal(*, text: str) -> bytes:
    """Fallback minimal PDF generator. Produces a valid one-page PDF.

    Good enough for the cache store/lookup path; pdfplumber-heavy tests skip
    if reportlab isn't installed (see the test body).
    """
    # A hand-rolled minimal PDF. Won't have parsable tables, but is a valid PDF.
    body = text.replace("\n", " ")
    content = (
        f"BT /F1 12 Tf 72 770 Td ({body[:200]}) Tj ET"
    ).encode("latin-1", errors="ignore")
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        b"4 0 obj << /Length " + str(len(content)).encode() + b" >> stream\n"
        + content
        + b"\nendstream endobj\n"
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"xref\n0 6\n0000000000 65535 f \n"
        b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"
    )
    return pdf


def _try_make_pdf(*, rev_label: str, vdd_max: str, page_count: int = 4) -> tuple[bytes, bool]:
    """Return (pdf_bytes, is_structured). is_structured=True when reportlab worked."""
    try:
        return _make_pdf_reportlab(rev_label=rev_label, vdd_max=vdd_max, page_count=page_count), True
    except Exception:
        return _make_pdf_minimal(text=f"STM32F103C8T6 Rev {rev_label} Vdd max {vdd_max}"), False


# ---------------------------------------------------------------------------
# Controlled adapter
# ---------------------------------------------------------------------------


class FixturePdfAdapter(SourceAdapter):
    """A SourceAdapter that returns pre-baked PDF bytes.

    Replaces every network-backed adapter for the smoke test. We register it
    under the MANUFACTURER kind so it fires first in the default source_order.
    """

    kind = SourceKind.MANUFACTURER
    name = "FixturePdfAdapter"

    def __init__(self, payloads_by_rev: dict[str, bytes]) -> None:
        # Don't call super().__init__ — we don't need httpx / limiter.
        self._payloads = payloads_by_rev
        self._current: str | None = None

    def set_current(self, rev_label: str) -> None:
        self._current = rev_label

    async def resolve(self, req):
        if self._current is None or self._current not in self._payloads:
            raise AdapterSkip("no fixture loaded")
        payload = self._payloads[self._current]
        return ResolvedPdf(
            raw_bytes=payload,
            source_url=f"https://fixture.local/{req.mpn}/{self._current}.pdf",
            http_status=200,
            revision_hint=self._current,
            revision_date_hint=datetime(2024, 6, 1, tzinfo=timezone.utc),
        )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smoke_fetch_cache_diff(tmp_path: Path) -> None:
    rev5_bytes, structured_5 = _try_make_pdf(rev_label="5", vdd_max="3.6", page_count=4)
    rev8_bytes, structured_8 = _try_make_pdf(rev_label="8", vdd_max="3.7", page_count=5)

    config = DatasheetConfig(
        cache_root=tmp_path / "cache",
        rate_limit_per_host_rps=1000.0,    # effectively off for the test
    )
    cache = DatasheetCache(config)
    await cache.init()

    adapter = FixturePdfAdapter({"5": rev5_bytes, "8": rev8_bytes})

    fetcher = DatasheetFetcher(
        config=config,
        cache=cache,
        client=httpx.AsyncClient(),       # never used by FixturePdfAdapter
        extra_adapters=[adapter],
    )
    try:
        # --- Fetch rev 5 -------------------------------------------------
        adapter.set_current("5")
        req = FetchRequest(mpn="STM32F103C8T6", manufacturer="STMicroelectronics")
        res5 = await fetcher.fetch(req)
        assert res5.ok, res5.warnings
        assert res5.from_cache is False
        assert res5.revision is not None
        assert res5.revision.cache_entry.revision == "5"
        assert res5.revision.absolute_pdf_path.is_file()
        sidecar = res5.revision.absolute_pdf_path.with_suffix(".json")
        assert sidecar.is_file()

        # Second fetch = cache hit.
        res5_again = await fetcher.fetch(req)
        assert res5_again.ok
        assert res5_again.from_cache is True

        # --- Fetch rev 8 -------------------------------------------------
        adapter.set_current("8")
        res8 = await fetcher.fetch(FetchRequest(
            mpn="STM32F103C8T6",
            manufacturer="STMicroelectronics",
            force_refresh=True,             # don't hit the cached rev 5
        ))
        assert res8.ok, res8.warnings
        assert res8.from_cache is False
        assert res8.revision.cache_entry.revision == "8"
        assert res8.revision.cache_entry.sha256 != res5.revision.cache_entry.sha256

        # --- Two revisions both listed in the cache --------------------
        all_revs = cache.lookup("STM32F103C8T6")
        assert {r.cache_entry.revision for r in all_revs} == {"5", "8"}

        # --- Diff --------------------------------------------------------
        diff = diff_revisions(res5.revision, res8.revision, config)
        assert diff.mpn == "STM32F103C8T6"
        assert diff.old_revision == "5"
        assert diff.new_revision == "8"

        if structured_5 and structured_8:
            # reportlab-generated PDFs carry parsable tables + headings.
            assert diff.page_count_delta == 1
            # One appendix section was added between rev 5 (4 pages) and rev 8 (5 pages).
            added = [s for s in diff.section_changes if s.change_kind == "added"]
            assert added, "expected at least one added section"
            # Vdd max went from 3.6 -> 3.7; that should surface as a parameter change.
            vdd_changes = [
                pc for pc in diff.parameter_changes
                if "supply" in (pc.parameter_name or "").lower() or (pc.symbol or "").lower() == "vdd"
            ]
            assert vdd_changes, (
                f"expected Vdd parameter change, got: "
                f"{[pc.parameter_name for pc in diff.parameter_changes]}"
            )
        else:
            # Fallback PDFs can't be table-parsed; ensure the diff at least
            # returns cleanly with correct metadata.
            assert diff.old_sha256 == res5.revision.cache_entry.sha256
            assert diff.new_sha256 == res8.revision.cache_entry.sha256

        # DS-061 / DS-063 cited on any non-empty diff.
        assert "DS-061" in diff.rule_citations
        assert "DS-063" in diff.rule_citations
    finally:
        await fetcher.aclose()
        cache.close()


def test_smoke_sync_runner() -> None:
    """Allow running the smoke test without pytest-asyncio configured.

    `pytest -xvs test_smoke.py::test_smoke_sync_runner` works even if the
    event-loop marker isn't wired up in the host project.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        asyncio.run(test_smoke_fetch_cache_diff(Path(d)))   # type: ignore[arg-type]


if __name__ == "__main__":
    test_smoke_sync_runner()
    print("smoke test passed")
