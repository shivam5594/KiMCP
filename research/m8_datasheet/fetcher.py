"""Source-ranked async datasheet fetcher.

High-level flow:

    FetchRequest
        -> DatasheetCache.lookup(mpn)   # short-circuit if not force_refresh
        -> SourceAdapter.resolve(mpn)   # walk source_order; first PDF wins
        -> content validation (PDF magic, size bound, content-type)
        -> revision inference
        -> DatasheetCache.store(entry, bytes)
        -> FetchResult

Each `SourceAdapter` is responsible for its own host-specific URL pattern and
credential handling. Adapters are composable: adding a new manufacturer or a
new distributor is implementing one class and registering it on the fetcher.

Rate limiting is global per host: a simple token bucket keyed on the request's
hostname, so parallel calls to different manufacturers don't starve each other.

No business logic about *which* revision is "best" lives here — the fetcher
returns whatever the highest-ranked adapter produces, tags it, stores it. The
caller (or the revdiff module) decides what to do with it.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import urlparse

import httpx

from .cache import DatasheetCache, sha256_bytes, slugify
from .models import (
    CacheEntry,
    DatasheetConfig,
    DatasheetRevision,
    ExtractionConfidence,
    FetchAttempt,
    FetchRequest,
    FetchResult,
    LifecycleHint,
    RevisionProvenance,
    SourceKind,
)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class _HostRateLimiter:
    """Simple per-host token bucket. Async-safe.

    Tokens refill continuously at `rps`. A request that would over-spend the
    bucket sleeps until enough have accumulated. This is gentler on vendors
    than hard throttling.
    """

    def __init__(self, rps: float) -> None:
        self._rps = rps
        self._last: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, host: str) -> None:
        async with self._lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            min_gap = 1.0 / self._rps
            wait = (last + min_gap) - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._last[host] = now


# ---------------------------------------------------------------------------
# Adapter interface
# ---------------------------------------------------------------------------


@dataclass
class ResolvedPdf:
    """What an adapter returns on success."""

    raw_bytes: bytes
    source_url: str
    http_status: int
    revision_hint: str | None = None
    revision_date_hint: datetime | None = None
    lifecycle_hint: LifecycleHint = LifecycleHint.UNKNOWN
    archival: bool = False
    extra_metadata: dict[str, object] = field(default_factory=dict)


class SourceAdapter(ABC):
    """Base class for all datasheet source adapters.

    Lifecycle:
        - Adapters are instantiated once per `DatasheetFetcher`.
        - `resolve(req)` returns a ResolvedPdf or raises `AdapterSkip` /
          `AdapterError`. Skips (missing keys, request doesn't apply) are
          silent in the sense that they don't log as errors, but they are
          recorded in the `FetchAttempt` chain.
    """

    kind: SourceKind = SourceKind.MANUFACTURER   # overridden by subclasses
    name: str = "SourceAdapter"

    def __init__(self, config: DatasheetConfig, client: httpx.AsyncClient, limiter: _HostRateLimiter) -> None:
        self._config = config
        self._client = client
        self._limiter = limiter

    @abstractmethod
    async def resolve(self, req: FetchRequest) -> ResolvedPdf: ...

    # Helpers available to subclasses --------------------------------------

    async def _get(self, url: str) -> httpx.Response:
        host = urlparse(url).hostname or ""
        await self._limiter.acquire(host)
        return await self._client.get(url, follow_redirects=True)

    def _resolve_secret(self, indirection: str | None) -> str | None:
        """Resolve `env:VAR` / `keychain:svc/acct` / `file:path` indirections.

        Matches the configuration.md secrets policy. Missing secrets return
        None (adapter should treat that as `AdapterSkip`).
        """
        if not indirection:
            return None
        if indirection.startswith("env:"):
            return os.environ.get(indirection[4:])
        if indirection.startswith("file:"):
            p = indirection[5:]
            try:
                st = os.stat(p)
            except OSError:
                return None
            # Match configuration.md's 0600 requirement.
            if st.st_mode & 0o077:
                return None
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except OSError:
                return None
        if indirection.startswith("keychain:"):
            # Keychain lookup is platform-specific; defer to a later milestone.
            return None
        # Literal secret in config is not allowed per configuration.md; ignore.
        return None


class AdapterSkip(Exception):
    """Adapter cannot service this request (missing credentials, disabled, etc.)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AdapterError(Exception):
    """Adapter tried and failed (network error, non-pdf content, etc.)."""

    def __init__(self, reason: str, *, http_status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Concrete adapters (stubs; intentionally minimal)
# ---------------------------------------------------------------------------


class ManufacturerAdapter(SourceAdapter):
    """DS-001 primary source.

    Uses `config.manufacturer_url_templates` — a dict of
    manufacturer_slug -> URL template with `{mpn}` / `{mpn_lower}` / `{mpn_upper}`
    placeholders. No hardcoded mappings (ADR-0009).
    """

    kind = SourceKind.MANUFACTURER
    name = "ManufacturerAdapter"

    async def resolve(self, req: FetchRequest) -> ResolvedPdf:
        if not req.manufacturer:
            raise AdapterSkip("no manufacturer hint on the request")
        template = self._config.manufacturer_url_templates.get(slugify(req.manufacturer))
        if not template:
            raise AdapterSkip(f"no URL template for manufacturer '{req.manufacturer}'")
        url = (
            template
            .replace("{mpn}", req.mpn)
            .replace("{mpn_lower}", req.mpn.lower())
            .replace("{mpn_upper}", req.mpn.upper())
        )
        resp = await self._get(url)
        _raise_for_non_pdf(resp, url)
        return ResolvedPdf(
            raw_bytes=resp.content,
            source_url=str(resp.url),
            http_status=resp.status_code,
        )


class DigiKeyAdapter(SourceAdapter):
    """DS-002. Resolves a product page, scrapes the first datasheet link."""

    kind = SourceKind.DIGIKEY
    name = "DigiKeyAdapter"

    async def resolve(self, req: FetchRequest) -> ResolvedPdf:
        client_id = self._resolve_secret(self._config.digikey_client_id)
        client_secret = self._resolve_secret(self._config.digikey_client_secret)
        if not (client_id and client_secret):
            raise AdapterSkip("digikey credentials not configured")
        # Intentionally abstract — the OAuth dance + product search endpoints
        # are an implementation detail for M8. Real code plugs in the v4 API
        # via a small client. The test harness replaces this method.
        raise AdapterSkip("digikey adapter is a stub in the M8 proposal")


class MouserAdapter(SourceAdapter):
    kind = SourceKind.MOUSER
    name = "MouserAdapter"

    async def resolve(self, req: FetchRequest) -> ResolvedPdf:
        api_key = self._resolve_secret(self._config.mouser_api_key)
        if not api_key:
            raise AdapterSkip("mouser api key not configured")
        raise AdapterSkip("mouser adapter is a stub in the M8 proposal")


class OctopartAdapter(SourceAdapter):
    """DS-003. URL resolver only — we do not accept Octopart-hosted bytes."""

    kind = SourceKind.OCTOPART
    name = "OctopartAdapter"

    async def resolve(self, req: FetchRequest) -> ResolvedPdf:
        api_key = self._resolve_secret(self._config.octopart_api_key)
        if not api_key:
            raise AdapterSkip("octopart api key not configured")
        raise AdapterSkip("octopart adapter is a stub in the M8 proposal")


class LCSCAdapter(SourceAdapter):
    kind = SourceKind.LCSC
    name = "LCSCAdapter"

    async def resolve(self, req: FetchRequest) -> ResolvedPdf:
        raise AdapterSkip("lcsc adapter is a stub in the M8 proposal")


class WaybackAdapter(SourceAdapter):
    """DS-006. Triggered only when config.allow_archive is True."""

    kind = SourceKind.WAYBACK
    name = "WaybackAdapter"

    async def resolve(self, req: FetchRequest) -> ResolvedPdf:
        if not self._config.allow_archive:
            raise AdapterSkip("archive access disabled")
        raise AdapterSkip("wayback adapter is a stub in the M8 proposal")


class CommunityAdapter(SourceAdapter):
    """DS-005. OFF by default, opt-in via FetchRequest."""

    kind = SourceKind.COMMUNITY
    name = "CommunityAdapter"

    async def resolve(self, req: FetchRequest) -> ResolvedPdf:
        if not (self._config.allow_community_mirrors and req.allow_community_mirrors):
            raise AdapterSkip("community mirrors disabled")
        raise AdapterSkip("community adapter is a stub in the M8 proposal")


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


_PDF_MAGIC = b"%PDF-"


def _raise_for_non_pdf(resp: httpx.Response, url: str) -> None:
    if resp.status_code != 200:
        raise AdapterError(f"HTTP {resp.status_code}", http_status=resp.status_code)
    ctype = resp.headers.get("content-type", "").lower()
    if "pdf" not in ctype and not resp.content.startswith(_PDF_MAGIC):
        raise AdapterError(f"not a PDF ({ctype!r}) at {url}")


class DatasheetFetcher:
    """Source-ranked fetcher with cache integration.

    Construct once per process; it owns an `httpx.AsyncClient` and a rate
    limiter. Call `fetch(req)` as many times as you like concurrently.

    Adapter order follows `config.source_order`. Unknown `SourceKind` values
    are skipped silently (tolerant for forward compat).
    """

    def __init__(
        self,
        config: DatasheetConfig,
        cache: DatasheetCache,
        *,
        client: httpx.AsyncClient | None = None,
        extra_adapters: Sequence[SourceAdapter] | None = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.request_timeout_s),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            headers={"User-Agent": "KiMCP/datasheet-fetcher (+https://kimcp.dev)"},
            http2=True,
        )
        self._owns_client = client is None
        self._limiter = _HostRateLimiter(rps=config.rate_limit_per_host_rps)

        # Build default adapter registry; extras appended at the end.
        self._adapters: dict[SourceKind, SourceAdapter] = {
            SourceKind.MANUFACTURER: ManufacturerAdapter(config, self._client, self._limiter),
            SourceKind.DIGIKEY: DigiKeyAdapter(config, self._client, self._limiter),
            SourceKind.MOUSER: MouserAdapter(config, self._client, self._limiter),
            SourceKind.OCTOPART: OctopartAdapter(config, self._client, self._limiter),
            SourceKind.LCSC: LCSCAdapter(config, self._client, self._limiter),
            SourceKind.WAYBACK: WaybackAdapter(config, self._client, self._limiter),
            SourceKind.COMMUNITY: CommunityAdapter(config, self._client, self._limiter),
        }
        if extra_adapters:
            for a in extra_adapters:
                self._adapters[a.kind] = a

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch(self, req: FetchRequest) -> FetchResult:
        attempts: list[FetchAttempt] = []
        warnings: list[str] = []

        # Cache short-circuit ------------------------------------------------
        if not req.force_refresh:
            hit = self._cache.lookup_best(req.mpn, manufacturer=req.manufacturer)
            if hit is not None and (req.prefer_rev is None or hit.cache_entry.revision == req.prefer_rev):
                attempts.append(
                    FetchAttempt(
                        source_kind=SourceKind.CACHE,
                        adapter_name="DatasheetCache",
                        url_tried=None,
                        outcome="hit",
                        detail=f"rev={hit.cache_entry.revision}",
                    )
                )
                return FetchResult(
                    request=req,
                    revision=hit,
                    attempts=attempts,
                    from_cache=True,
                    warnings=warnings,
                )

        # Network attempts --------------------------------------------------
        for kind in self._config.source_order:
            adapter = self._adapters.get(kind)
            if adapter is None:
                attempts.append(
                    FetchAttempt(
                        source_kind=kind,
                        adapter_name="<missing>",
                        outcome="skipped",
                        detail="no adapter registered for source kind",
                    )
                )
                continue
            started = time.monotonic()
            try:
                resolved = await adapter.resolve(req)
            except AdapterSkip as e:
                attempts.append(
                    FetchAttempt(
                        source_kind=kind,
                        adapter_name=adapter.name,
                        outcome="skipped",
                        detail=e.reason,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                )
                continue
            except AdapterError as e:
                attempts.append(
                    FetchAttempt(
                        source_kind=kind,
                        adapter_name=adapter.name,
                        outcome="invalid_pdf" if "not a PDF" in e.reason else "error",
                        detail=e.reason,
                        http_status=e.http_status,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                )
                continue
            except httpx.HTTPError as e:
                attempts.append(
                    FetchAttempt(
                        source_kind=kind,
                        adapter_name=adapter.name,
                        outcome="error",
                        detail=f"{type(e).__name__}: {e}",
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                )
                continue

            # Size cap enforcement (protects cache from runaway downloads).
            if len(resolved.raw_bytes) > self._config.max_pdf_mb * 1024 * 1024:
                attempts.append(
                    FetchAttempt(
                        source_kind=kind,
                        adapter_name=adapter.name,
                        url_tried=resolved.source_url,
                        outcome="error",
                        detail=f"pdf exceeds max_pdf_mb={self._config.max_pdf_mb}",
                        fetched_bytes=len(resolved.raw_bytes),
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                )
                continue

            attempts.append(
                FetchAttempt(
                    source_kind=kind,
                    adapter_name=adapter.name,
                    url_tried=resolved.source_url,
                    http_status=resolved.http_status,
                    outcome="hit",
                    detail=None,
                    fetched_bytes=len(resolved.raw_bytes),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )

            revision, rev_prov, rev_date = _infer_revision(
                raw_bytes=resolved.raw_bytes,
                url=resolved.source_url,
                hint=resolved.revision_hint,
                hint_date=resolved.revision_date_hint,
            )

            entry = CacheEntry(
                mpn=req.mpn,
                manufacturer=req.manufacturer or "Unknown",
                revision=revision,
                revision_provenance=rev_prov,
                revision_date=rev_date,
                lifecycle_hint=resolved.lifecycle_hint,
                sha256=sha256_bytes(resolved.raw_bytes),
                size_bytes=len(resolved.raw_bytes),
                page_count=None,     # filled in after cache.store by revdiff's page counter
                source_kind=kind,
                source_url=resolved.source_url,
                resolver_chain=list(attempts),
                archival=resolved.archival,
                relative_pdf_path="",      # populated by cache.store
                metadata=dict(resolved.extra_metadata),
            )
            rev = await self._cache.store(entry, resolved.raw_bytes)

            if rev.cache_entry.revision_provenance == RevisionProvenance.INFERRED:
                warnings.append(
                    "Revision label was inferred — no Revision History table, PDF metadata, or filename hint was found."
                )

            return FetchResult(
                request=req,
                revision=rev,
                attempts=attempts,
                from_cache=False,
                warnings=warnings,
            )

        # All adapters failed / skipped.
        return FetchResult(
            request=req,
            revision=None,
            attempts=attempts,
            from_cache=False,
            warnings=warnings + ["no source yielded a valid PDF"],
        )


# ---------------------------------------------------------------------------
# Revision inference (module-level so it's trivial to unit-test)
# ---------------------------------------------------------------------------


_REV_HISTORY_RE = re.compile(r"rev(?:ision)?(?:\s+history)?", re.IGNORECASE)
_REV_FILENAME_RE = re.compile(r"rev(?:ision)?[_\-\s]*([a-z0-9.\-]+)", re.IGNORECASE)
_REV_LINE_RE = re.compile(r"rev(?:ision)?\.?\s*([a-z0-9.\-]+)", re.IGNORECASE)


def _infer_revision(
    *,
    raw_bytes: bytes,
    url: str | None,
    hint: str | None,
    hint_date: datetime | None,
) -> tuple[str, RevisionProvenance, datetime | None]:
    """Best-effort revision inference. Ordered per README.

    We deliberately keep this non-PDF-library-dependent: the revdiff module
    already uses pdfplumber for the richer parse. Here we need a quick answer
    for the cache key.
    """
    if hint:
        return hint, RevisionProvenance.REV_HISTORY_TABLE, hint_date

    # Peek into PDF bytes for metadata-ish strings. A full xref parse is
    # out of scope; the Revision History table check lives in revdiff where
    # pdfplumber is loaded.
    head = raw_bytes[:65536]
    if m := _REV_LINE_RE.search(head.decode("latin-1", errors="ignore")):
        return m.group(1), RevisionProvenance.PDF_METADATA, hint_date

    if url:
        if m := _REV_FILENAME_RE.search(url):
            return m.group(1), RevisionProvenance.FILENAME, hint_date

    # Fallback.
    digest = sha256_bytes(raw_bytes)[:8]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"unknown-{stamp}-{digest}", RevisionProvenance.INFERRED, None
