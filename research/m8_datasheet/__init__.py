"""M8 datasheet fetcher + cache + rev-diff research prototype.

Nothing in this package is wired into the main KiMCP server yet. See README.md
for the design and integration plan.
"""

from .cache import DatasheetCache
from .fetcher import (
    AdapterError,
    AdapterSkip,
    DatasheetFetcher,
    ResolvedPdf,
    SourceAdapter,
)
from .models import (
    CacheEntry,
    Datasheet,
    DatasheetConfig,
    DatasheetDiff,
    DatasheetRevision,
    DiffClass,
    ExtractionConfidence,
    FetchAttempt,
    FetchRequest,
    FetchResult,
    FindingSeverity,
    LifecycleHint,
    ParameterChange,
    ParameterRow,
    RevisionProvenance,
    SectionChange,
    SourceKind,
)
from .revdiff import diff_revisions

__all__ = [
    "AdapterError",
    "AdapterSkip",
    "CacheEntry",
    "Datasheet",
    "DatasheetCache",
    "DatasheetConfig",
    "DatasheetDiff",
    "DatasheetFetcher",
    "DatasheetRevision",
    "DiffClass",
    "ExtractionConfidence",
    "FetchAttempt",
    "FetchRequest",
    "FetchResult",
    "FindingSeverity",
    "LifecycleHint",
    "ParameterChange",
    "ParameterRow",
    "ResolvedPdf",
    "RevisionProvenance",
    "SectionChange",
    "SourceAdapter",
    "SourceKind",
    "diff_revisions",
]
