"""MCP ``resources/list`` + ``resources/read`` primitive (M13).

Exposes KiCAD artifacts (``.kicad_sch``, ``.kicad_pcb``, ``.kicad_pro``,
``.kicad_sym``, ``.kicad_mod``, ``.kicad_wks``, ``.kicad_dru``) inside the
server's project root as MCP resources — readable by any spec-compliant
client.

Why this exists
---------------

Prompt-driven schematic creation (the platform-first goal; see
``project_kimcp_milestones``) needs the LLM to *see* schematic state between
tool calls. The mutation tools (M12 and later) write; the LLM needs the
other half of the loop to read. Resources are MCP's standard primitive for
that — clients like Claude Desktop and Claude Code already know how to
surface them.

Scope of the first ship
-----------------------

Deliberately NOT in v1:

* **No pagination.** ``nextCursor`` is always absent. Projects contain
  single-digit numbers of KiCAD files — pagination would be dead weight.
* **No ``resources/subscribe`` / ``notifications/resources/list_changed``.**
  File-watching lives in the sexpr cache layer today; wiring a second
  watcher here would duplicate that. Revisit when a real client asks.
* **No parsed summaries.** Raw S-expression text. Parsing happens in tools;
  keeping the resource boundary dumb means the LLM sees what the files
  actually contain, not our interpretation of them.
* **No binary blob responses.** All KiCAD project files are text.

Security
--------

``ResourceProvider.read`` is the first boundary between the LLM-controlled
side of the protocol and the local filesystem. Path-traversal hygiene is
load-bearing. Every rejection (bad scheme, bad path, out-of-root, wrong
suffix, missing file, non-UTF-8) raises ``RpcError(INVALID_PARAMS)`` so the
JSON-RPC layer surfaces a clean error instead of silently returning empty
contents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from kimcp.errors import INVALID_PARAMS, RpcError

# Directory names pruned during list(). Everything else is walked.
#
# - ``.git`` / ``.venv`` / ``node_modules`` / ``__pycache__``: obvious.
# - ``.kimcp``: the server's own snapshot dir — exposing our own atomic-write
#   backups would let the LLM read stale copies as if they were live state.
# - ``fp-info-cache``: KiCAD's binary footprint index. Files inside have
#   ``.kicad_sym``-shaped names but aren't user content.
_EXCLUDE_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".kimcp",
        ".venv",
        "node_modules",
        "__pycache__",
        "fp-info-cache",
    }
)

# KiCAD file extensions we surface, mapped to a MIME type. ``application/x-*``
# is unofficial but consistent with how other EDA-adjacent tooling names
# these formats. We avoid ``text/plain`` so clients can route by type.
_MIME_BY_SUFFIX: dict[str, str] = {
    ".kicad_sch": "application/x-kicad-schematic",
    ".kicad_pcb": "application/x-kicad-pcb",
    ".kicad_pro": "application/x-kicad-project",
    ".kicad_sym": "application/x-kicad-symbol-library",
    ".kicad_mod": "application/x-kicad-footprint",
    ".kicad_wks": "application/x-kicad-worksheet",
    ".kicad_dru": "application/x-kicad-design-rules",
}

# Human-readable kind for the ``description`` field. Keeps descriptions
# stable without coupling them to the MIME string.
_KIND_BY_SUFFIX: dict[str, str] = {
    ".kicad_sch": "schematic",
    ".kicad_pcb": "pcb",
    ".kicad_pro": "project",
    ".kicad_sym": "symbol library",
    ".kicad_mod": "footprint",
    ".kicad_wks": "worksheet",
    ".kicad_dru": "design rules",
}


class ResourceProvider:
    """Discovers and reads KiCAD files under a single project root.

    One provider per server instance. The root is resolved to its canonical
    form at construction time so every containment check downstream compares
    symlink-free absolute paths on both sides.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root: Path = project_root.resolve()

    # ------------------------------------------------------------------
    # resources/list
    # ------------------------------------------------------------------

    def list_resources(self) -> list[dict[str, Any]]:
        """Return resource descriptors for every KiCAD file under the root.

        Sorted by POSIX-style relative path for reproducibility. Clients
        can rely on the ordering; tests pin it.

        Named ``list_resources`` rather than ``list`` so the builtin
        ``list[...]`` type shorthand stays usable in method annotations
        on this class (mypy resolves names in class scope first).
        """
        if not self.project_root.is_dir():
            return []

        found: list[tuple[str, Path]] = []
        self._walk(self.project_root, found)
        found.sort(key=lambda item: item[0])

        return [self._describe(abs_path) for _, abs_path in found]

    def _walk(self, directory: Path, out: list[tuple[str, Path]]) -> None:
        """Recursive walk that prunes excluded directories without listing them.

        ``Path.rglob`` can't skip a subtree — it would still enter
        ``.venv`` and ``node_modules`` before our filter ran. Walking by
        hand keeps the cost proportional to the KiCAD content, not the
        repo's language ecosystem junk.
        """
        try:
            entries = list(directory.iterdir())
        except OSError:
            return

        for entry in entries:
            # Skip symlinks wholesale: avoids both containment escapes
            # (a link pointing outside the project root) and infinite
            # loops (a link pointing at an ancestor).
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in _EXCLUDE_DIR_NAMES:
                    continue
                self._walk(entry, out)
                continue
            if not entry.is_file():
                continue
            if entry.suffix not in _MIME_BY_SUFFIX:
                continue
            try:
                rel = entry.relative_to(self.project_root).as_posix()
            except ValueError:
                # Defensive — iterdir can in theory emit a path that isn't
                # under the starting directory if a symlink sneaks past the
                # check above on a platform where ``is_symlink`` misfires.
                continue
            out.append((rel, entry))

    def _describe(self, abs_path: Path) -> dict[str, Any]:
        rel = abs_path.relative_to(self.project_root).as_posix()
        kind = _KIND_BY_SUFFIX[abs_path.suffix]
        descriptor: dict[str, Any] = {
            "uri": abs_path.as_uri(),
            "name": rel,
            "description": f"KiCAD {kind}: {rel}",
            "mimeType": _MIME_BY_SUFFIX[abs_path.suffix],
        }
        try:
            descriptor["size"] = abs_path.stat().st_size
        except OSError:
            # File vanished between iterdir and stat — skip the size
            # field rather than failing the whole listing.
            pass
        return descriptor

    # ------------------------------------------------------------------
    # resources/read
    # ------------------------------------------------------------------

    def read(self, uri: str) -> list[dict[str, Any]]:
        """Return the ``contents`` array for ``resources/read``.

        Every rejection path raises ``RpcError(INVALID_PARAMS)``. Returning
        empty contents on failure would look like "no data" to the client,
        which is meaningfully different from "you asked for something
        invalid" and leads to silent prompt failures.
        """
        abs_path = self._resolve_uri(uri)

        try:
            text = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise RpcError(
                INVALID_PARAMS,
                "resource is not valid UTF-8 text",
                {"uri": uri},
            ) from exc
        except OSError as exc:
            # Covers race-y disappearances after the existence check and
            # permission errors. ``exc.strerror`` is usually the friendlier
            # message; fall back to the exception repr if empty.
            raise RpcError(
                INVALID_PARAMS,
                f"failed to read resource: {exc.strerror or exc}",
                {"uri": uri},
            ) from exc

        return [
            {
                "uri": uri,
                "mimeType": _MIME_BY_SUFFIX[abs_path.suffix],
                "text": text,
            }
        ]

    # ------------------------------------------------------------------
    # internal — URI parsing + security
    # ------------------------------------------------------------------

    def _resolve_uri(self, uri: str) -> Path:
        parsed = urlparse(uri)

        if parsed.scheme != "file":
            raise RpcError(
                INVALID_PARAMS,
                f"unsupported URI scheme: {parsed.scheme or '(empty)'}",
                {"uri": uri, "expected_scheme": "file"},
            )
        # RFC 8089: ``file:`` authority must be empty or ``localhost``.
        # Anything else is either a remote file (out of scope) or a
        # malformed URI trying to look clever.
        if parsed.netloc not in ("", "localhost"):
            raise RpcError(
                INVALID_PARAMS,
                f"file:// URI must have empty or 'localhost' host, got {parsed.netloc!r}",
                {"uri": uri},
            )

        raw_path = unquote(parsed.path)
        if not raw_path:
            raise RpcError(
                INVALID_PARAMS,
                "file:// URI is missing a path",
                {"uri": uri},
            )

        candidate = Path(raw_path)
        if not candidate.is_absolute():
            raise RpcError(
                INVALID_PARAMS,
                "file:// URI must resolve to an absolute path",
                {"uri": uri},
            )

        # Resolve symlinks + ``..`` BEFORE the containment check. Using
        # ``strict=False`` means a missing file still normalizes cleanly,
        # so we can separate the "out of root" and "not found" errors
        # instead of collapsing them into one confusing message.
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise RpcError(
                INVALID_PARAMS,
                "resource URI is outside the project root",
                {"uri": uri, "project_root": str(self.project_root)},
            ) from exc

        if resolved.suffix not in _MIME_BY_SUFFIX:
            raise RpcError(
                INVALID_PARAMS,
                f"unsupported resource extension: {resolved.suffix or '(none)'}",
                {"uri": uri, "supported": sorted(_MIME_BY_SUFFIX)},
            )

        if not resolved.is_file():
            raise RpcError(
                INVALID_PARAMS,
                "resource not found",
                {"uri": uri},
            )
        return resolved


__all__ = ["ResourceProvider"]
