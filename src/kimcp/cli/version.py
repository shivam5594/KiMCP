"""KiCAD version parsing + comparison.

`kicad-cli version` output varies slightly across packages:

    Application: kicad-cli
    Version: 9.0.1+9.0.0-0-10.fc40, release build

    Libraries:
        wxWidgets 3.2.5
        ...

or (nightly / self-built):

    Application: kicad-cli
    Version: 10.0.0-rc1-1234-gabcdef01, development build

or the bare form:

    9.0.1

Parser strategy: pick the first chunk on a `Version:` line (if present)
or the whole trimmed input; extract the first `N.N.N` semver-like
sequence. Everything after that is the `extra` tag preserved on the
parsed object for diagnostics.

Only major / minor / patch participate in comparison — suffix / build
metadata never affects ordering. That's correct for our min-version
gate (we care whether IPC is available on the platform, which is a
major-version concern).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class KiCadVersion:
    """Major/minor/patch triple with the original string kept for logs.

    We deliberately declare `order=False` (the default) and implement the
    comparison dunders below so that `raw` (the original string with
    suffix / build metadata) does *not* affect ordering. That's correct
    for the min-version gate — suffix differences never mean "lower".
    """

    major: int
    minor: int
    patch: int
    raw: str = ""

    def __post_init__(self) -> None:
        # Validate non-negative components. Negative values wouldn't come
        # from parse_cli_version but could sneak in via direct constructor calls.
        if self.major < 0 or self.minor < 0 or self.patch < 0:
            raise ValueError(f"version components must be non-negative: {self}")

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"KiCadVersion({self.major}.{self.minor}.{self.patch}, raw={self.raw!r})"

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    # -- ordering: compare only numeric triple ---------------------------

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, KiCadVersion):
            return NotImplemented
        return self.as_tuple() < other.as_tuple()

    def __le__(self, other: object) -> bool:
        if not isinstance(other, KiCadVersion):
            return NotImplemented
        return self.as_tuple() <= other.as_tuple()

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, KiCadVersion):
            return NotImplemented
        return self.as_tuple() > other.as_tuple()

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, KiCadVersion):
            return NotImplemented
        return self.as_tuple() >= other.as_tuple()

    def __eq__(self, other: object) -> bool:
        # Deliberately strict: `KiCadVersion(9,0,0) == (9,0,0)` is False.
        # Returning NotImplemented for non-KiCadVersion types means Python
        # falls back to the other operand's __eq__, which for a plain tuple
        # also returns NotImplemented and the comparison ends up False. That
        # prevents accidental tuple-compare bugs from silently succeeding.
        if not isinstance(other, KiCadVersion):
            return NotImplemented
        return self.as_tuple() == other.as_tuple()

    def __hash__(self) -> int:
        return hash(self.as_tuple())

    # -- parsing ---------------------------------------------------------

    @classmethod
    def parse(cls, s: str) -> KiCadVersion | None:
        """Best-effort parse. Returns None if no semver-like triple is found."""
        m = _SEMVER_RE.search(s)
        if not m:
            return None
        return cls(
            major=int(m.group(1)),
            minor=int(m.group(2)),
            patch=int(m.group(3)),
            raw=s.strip(),
        )


def parse_cli_version(stdout: str) -> KiCadVersion | None:
    """Extract a KiCadVersion from `kicad-cli version` output.

    Prefers the `Version:` line when present (that's what recent KiCAD
    releases emit); falls back to searching the whole output otherwise.
    """
    if not stdout:
        return None

    # Prefer a 'Version:' line — most specific match.
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("version:"):
            after = stripped.split(":", 1)[1].strip()
            parsed = KiCadVersion.parse(after)
            if parsed is not None:
                return parsed

    # Fall back to whole-output search for the first semver triple.
    return KiCadVersion.parse(stdout)


__all__ = ["KiCadVersion", "parse_cli_version"]
