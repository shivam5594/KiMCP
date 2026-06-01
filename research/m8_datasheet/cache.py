"""On-disk cache for fetched datasheets.

Layout (from README.md):

    <cache_root>/
      datasheets/
        <mfr_slug>/
          <mpn_slug>/
            <rev_slug>/
              <sha256>.pdf
              <sha256>.json
            index.json
        _global_index.sqlite        (optional, self-healing)

Design invariants:
- sha256 is the content-address leaf. Two fetches of the same rev that differ
  in a single byte still produce two cache entries (DS-060, DS-065).
- Sidecar JSON is authoritative. SQLite is an optimization; we rebuild it
  from sidecars if it goes missing.
- All writes are atomic (`os.replace`) so crashes don't leave partial files.
- No project-specific behaviour. `cache_root` always comes from config. When
  `DatasheetConfig.project_mirror` is set, writes are *mirrored* to that
  directory as a convenience — the global cache remains canonical (DS-033).

This module does not care *how* a PDF was obtained — the fetcher builds a
`CacheEntry` and hands it here. That separation keeps the fetch policy (rate
limiting, adapter order, user-agent) out of the cache's hair.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import aiofiles

from .models import (
    CacheEntry,
    Datasheet,
    DatasheetConfig,
    DatasheetRevision,
    ExtractionConfidence,
    Sha256,
    SourceKind,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(s: str) -> str:
    """ASCII-safe lowercase slug for on-disk directory names.

    We keep this permissive: collapse any non-alphanumeric to dashes, trim.
    The real MPN / manufacturer / revision strings live in the sidecar JSON so
    this is purely a filename-safety concern.
    """
    out = _SLUG_RE.sub("-", s.lower()).strip("-")
    return out or "unknown"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DatasheetCache:
    """On-disk cache. Thread/async safe via a per-instance asyncio.Lock on writes.

    Usage pattern:

        cache = DatasheetCache(config)
        await cache.init()
        if (existing := cache.lookup_best(mpn)):
            use(existing)
        else:
            # fetcher runs, produces (entry, raw_bytes)
            rev = await cache.store(entry, raw_bytes)
    """

    def __init__(self, config: DatasheetConfig) -> None:
        self._config = config
        self._root = (config.cache_root / "datasheets").resolve()
        self._index_path = (config.cache_root / "datasheets" / "_global_index.sqlite").resolve()
        self._write_lock = asyncio.Lock()
        self._db: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Ensure directories + SQLite index exist. Safe to call multiple times."""
        self._root.mkdir(parents=True, exist_ok=True)
        if self._config.project_mirror:
            self._config.project_mirror.mkdir(parents=True, exist_ok=True)
        self._open_index()
        # Light self-heal: if the index has no rows but the filesystem does,
        # rebuild. This is cheap at startup and saves us from stale indexes
        # after manual file operations.
        if self._index_looks_empty():
            await self.rebuild_index()

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def store(self, entry: CacheEntry, raw_bytes: bytes) -> DatasheetRevision:
        """Write the PDF + sidecar and return the DatasheetRevision record.

        The caller is responsible for populating `entry.sha256`, `entry.size_bytes`,
        and `entry.relative_pdf_path`. `store` will overwrite those if they are
        inconsistent with `raw_bytes` (defense in depth against a buggy fetcher).
        """
        computed = sha256_bytes(raw_bytes)
        if entry.sha256 != computed:
            entry = entry.model_copy(update={"sha256": computed, "size_bytes": len(raw_bytes)})

        rel = self._relative_pdf_path(entry)
        entry = entry.model_copy(update={"relative_pdf_path": rel})
        abs_pdf = (self._root.parent / rel).resolve()
        abs_json = abs_pdf.with_suffix(".json")

        async with self._write_lock:
            abs_pdf.parent.mkdir(parents=True, exist_ok=True)
            await _atomic_write_bytes(abs_pdf, raw_bytes)
            await _atomic_write_text(abs_json, entry.model_dump_json(indent=2))
            self._upsert_index(entry)
            await self._write_per_mpn_manifest(entry)

            # Optional mirror into project docs/datasheets/ (DS-033).
            if self._config.project_mirror:
                mirror_rel = Path(entry.manufacturer, entry.mpn, entry.revision)
                mirror_dir = (self._config.project_mirror / mirror_rel).resolve()
                mirror_dir.mkdir(parents=True, exist_ok=True)
                await _atomic_write_bytes(mirror_dir / f"{entry.sha256}.pdf", raw_bytes)
                await _atomic_write_text(
                    mirror_dir / f"{entry.sha256}.json",
                    entry.model_dump_json(indent=2),
                )

        return DatasheetRevision(
            cache_entry=entry,
            absolute_pdf_path=abs_pdf,
            extraction_confidence=ExtractionConfidence.MEDIUM,
        )

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def lookup(
        self,
        mpn: str,
        *,
        revision: str | None = None,
        manufacturer: str | None = None,
    ) -> list[DatasheetRevision]:
        """Return all matching revisions, newest fetched first.

        `revision=None` matches any revision for the mpn. `manufacturer=None`
        matches any manufacturer (ambiguity is fine; callers decide).
        """
        self._open_index()
        assert self._db is not None
        params: list[object] = [mpn]
        clauses = ["mpn = ?"]
        if revision is not None:
            clauses.append("revision = ?")
            params.append(revision)
        if manufacturer is not None:
            clauses.append("manufacturer = ?")
            params.append(manufacturer)
        sql = f"""
            SELECT relative_pdf_path FROM entries
            WHERE {' AND '.join(clauses)}
            ORDER BY fetched_at DESC
        """
        rows = self._db.execute(sql, params).fetchall()
        out: list[DatasheetRevision] = []
        for (rel,) in rows:
            rev = self._load_revision_by_rel(rel)
            if rev is not None:
                out.append(rev)
        return out

    def lookup_best(self, mpn: str, manufacturer: str | None = None) -> DatasheetRevision | None:
        """Pick the 'best' cached revision for this MPN.

        'Best' = the Datasheet.current() rule in models.py: newest trusted
        revision, falling back to newest fetched.
        """
        revs = self.lookup(mpn, manufacturer=manufacturer)
        if not revs:
            return None
        ds = Datasheet(
            mpn=mpn,
            manufacturer=revs[0].cache_entry.manufacturer,
            revisions=revs,
        )
        return ds.current()

    def as_datasheet(self, mpn: str, manufacturer: str | None = None) -> Datasheet | None:
        revs = self.lookup(mpn, manufacturer=manufacturer)
        if not revs:
            return None
        return Datasheet(mpn=mpn, manufacturer=revs[0].cache_entry.manufacturer, revisions=revs)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def rebuild_index(self) -> int:
        """Walk every sidecar JSON and repopulate the SQLite index.

        Returns count of entries. Called automatically on `init()` when the
        index looks empty but the filesystem has content.
        """
        self._open_index()
        assert self._db is not None
        with self._db:
            self._db.execute("DELETE FROM entries")
        n = 0
        async with self._write_lock:
            for json_path in self._root.rglob("*.json"):
                if json_path.name == "index.json":
                    continue
                try:
                    async with aiofiles.open(json_path, "r", encoding="utf-8") as f:
                        entry = CacheEntry.model_validate_json(await f.read())
                except Exception:
                    # Corrupt sidecar — skip. A later 'cache doctor' task can
                    # surface these to the user.
                    continue
                self._upsert_index(entry)
                n += 1
        return n

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _relative_pdf_path(self, entry: CacheEntry) -> str:
        parts = (
            slugify(entry.manufacturer),
            slugify(entry.mpn),
            slugify(entry.revision),
            f"{entry.sha256}.pdf",
        )
        return "/".join(parts)

    def _load_revision_by_rel(self, rel: str) -> DatasheetRevision | None:
        abs_pdf = (self._root.parent / rel).resolve()
        abs_json = abs_pdf.with_suffix(".json")
        if not abs_json.is_file() or not abs_pdf.is_file():
            return None
        try:
            entry = CacheEntry.model_validate_json(abs_json.read_text(encoding="utf-8"))
        except Exception:
            return None
        return DatasheetRevision(
            cache_entry=entry,
            absolute_pdf_path=abs_pdf,
            extraction_confidence=ExtractionConfidence.MEDIUM,
        )

    def _open_index(self) -> None:
        if self._db is not None:
            return
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._index_path, isolation_level=None)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                sha256 TEXT PRIMARY KEY,
                mpn TEXT NOT NULL,
                manufacturer TEXT NOT NULL,
                revision TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                relative_pdf_path TEXT NOT NULL
            )
            """
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS entries_mpn ON entries(mpn)")
        self._db.execute("CREATE INDEX IF NOT EXISTS entries_mfr ON entries(manufacturer)")

    def _upsert_index(self, entry: CacheEntry) -> None:
        self._open_index()
        assert self._db is not None
        self._db.execute(
            """
            INSERT INTO entries (sha256, mpn, manufacturer, revision, source_kind, fetched_at, relative_pdf_path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                mpn = excluded.mpn,
                manufacturer = excluded.manufacturer,
                revision = excluded.revision,
                source_kind = excluded.source_kind,
                fetched_at = excluded.fetched_at,
                relative_pdf_path = excluded.relative_pdf_path
            """,
            (
                entry.sha256,
                entry.mpn,
                entry.manufacturer,
                entry.revision,
                entry.source_kind.value,
                entry.fetched_at.astimezone(timezone.utc).isoformat(),
                entry.relative_pdf_path,
            ),
        )

    def _index_looks_empty(self) -> bool:
        self._open_index()
        assert self._db is not None
        (count,) = self._db.execute("SELECT COUNT(*) FROM entries").fetchone()
        if count > 0:
            return False
        # Cheap filesystem existence check — look for any sidecar JSON under the root.
        for _ in self._root.rglob("*.json"):
            return True
        return False

    async def _write_per_mpn_manifest(self, entry: CacheEntry) -> None:
        """Write `index.json` in the MPN's directory summarizing its revisions."""
        mpn_dir = (
            self._root
            / slugify(entry.manufacturer)
            / slugify(entry.mpn)
        )
        manifest_path = mpn_dir / "index.json"
        revs: dict[str, list[str]] = {}
        for sidecar in mpn_dir.rglob("*.json"):
            if sidecar.name == "index.json":
                continue
            try:
                ce = CacheEntry.model_validate_json(sidecar.read_text(encoding="utf-8"))
            except Exception:
                continue
            revs.setdefault(ce.revision, []).append(ce.sha256)
        payload = {
            "schema_version": 1,
            "mpn": entry.mpn,
            "manufacturer": entry.manufacturer,
            "revisions": revs,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await _atomic_write_text(manifest_path, json.dumps(payload, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


async def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    async with aiofiles.open(tmp, "wb") as f:
        await f.write(data)
    tmp.replace(path)


async def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
        await f.write(text)
    tmp.replace(path)
