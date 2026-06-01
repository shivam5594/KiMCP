"""lib_register_library — register a library in a KiCAD lib-table (M46).

After creating a new ``.kicad_sym`` or ``.pretty/`` via M44/M45,
KiCAD can't find it until the library is registered in either the
global or project lib-table. This tool automates that step.

Two flavors of lib-table
------------------------

* **Symbol libraries** → ``sym-lib-table`` (top-head ``sym_lib_table``)
* **Footprint libraries** → ``fp-lib-table`` (top-head ``fp_lib_table``)

Both live at the project root for project-scoped registration, or in
``~/.config/kicad/<major>.<minor>/`` for global registration. The
file format is identical except for the top-head::

    (sym_lib_table
      (version 7)
      (lib (name "MyLib")
           (type "KiCad")
           (uri "${KIPRJMOD}/MyLib.kicad_sym")
           (options "")
           (descr "Custom symbols")))

Scope of the first ship
-----------------------

* **KiCad-type only.** ``type`` is always ``"KiCad"``. Legacy types
  (Eagle/Altium/IPC2581 importers) are niche and their URI schemes
  differ enough to warrant separate milestones.
* **Project-local URIs preferred.** When the library lives under the
  same project directory as the lib-table, we rewrite the URI to use
  ``${KIPRJMOD}/...`` — KiCAD's project-relative substitution token.
  When it's outside, we store the absolute path (user responsibility
  to keep the path stable).
* **File bootstrap.** If the target lib-table doesn't exist, we
  create it with the v7 format header. Matches what KiCAD does on
  first project save.

Conflict policy
---------------

If a library with the same nickname is already registered, we return
``nickname_exists`` unless ``overwrite=True`` is set. Overwrite
updates the existing row in place rather than duplicating the
nickname (which KiCAD would refuse to load at project open).

Status enum
-----------

* **ok**                   — entry added / updated.
* **dry_run**              — caller passed ``dry_run=True``.
* **invalid_input**        — missing/malformed nickname, missing library.
* **invalid_schema**       — existing lib-table's top-head doesn't match
                             ``table_kind``.
* **parse_failed**         — SEXPR parser rejected the existing file.
* **nickname_exists**      — a library with this nickname is registered
                             and ``overwrite`` is False.
* **write_failed**         — snapshot / save raised.

Backend: SEXPR, required.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from kimcp._types import Backend, ToolClass
from kimcp.config import Config
from kimcp.safety import SnapshotError, snapshot
from kimcp.schemas.envelope import ToolOutput
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.base import Tool
from kimcp.tools.builtin._sexpr_build import atom, slist

log = logging.getLogger(__name__)


# KiCAD's lib-table format version. Version 7 covers KiCAD 7/8/9/10 —
# the format hasn't churned since it landed. We stamp this on fresh
# bootstraps; existing files keep whatever version they have.
_LIB_TABLE_VERSION = 7

# Mapping of the two table kinds to their canonical top-head atoms
# and default URI suffix convention. Sym lib-tables reference
# ``.kicad_sym`` files; fp lib-tables reference ``.pretty/``
# directories.
_TABLE_KIND_TO_TOP_HEAD = {
    "symbol": "sym_lib_table",
    "footprint": "fp_lib_table",
}
_TABLE_KIND_TO_DEFAULT_FILENAME = {
    "symbol": "sym-lib-table",
    "footprint": "fp-lib-table",
}


# -- input / output --------------------------------------------------------


class LibRegisterLibraryInput(BaseModel):
    table_path: Path = Field(
        ...,
        description=(
            "Path to the lib-table file ('sym-lib-table' or "
            "'fp-lib-table'). Created if missing. For project-scoped "
            "registration, point at '<project_dir>/sym-lib-table' or "
            "'<project_dir>/fp-lib-table'."
        ),
    )
    table_kind: Literal["symbol", "footprint"] = Field(
        ...,
        description=(
            "Which lib-table we're editing. Must match the file's "
            "top-head ('sym_lib_table' vs 'fp_lib_table'). Mismatch "
            "returns invalid_schema rather than silently corrupting."
        ),
    )
    nickname: str = Field(
        ...,
        description=(
            "Library nickname — the prefix used in lib_ids throughout "
            "schematics and PCBs ('MyLib' → 'MyLib:MySymbol'). Must be "
            "unique within the table; non-empty; no colons."
        ),
    )
    library_path: Path = Field(
        ...,
        description=(
            "Path to the library. For symbol tables this is a "
            "'.kicad_sym' file; for footprint tables a '.pretty' "
            "directory. Rewritten to a '${KIPRJMOD}/...' URI when the "
            "path is under the project directory."
        ),
    )
    description: str = Field(
        default="",
        description="Human-readable description stored in the table row.",
    )
    options: str = Field(
        default="",
        description=(
            "KiCAD 'options' field on the table row. Almost always "
            "empty; used historically for legacy-format driver flags."
        ),
    )
    overwrite: bool = Field(
        default=False,
        description=(
            "If True and the nickname is already registered, update the "
            "existing row in place. Defaults to False so callers opt in "
            "explicitly (duplicate nicknames break KiCAD at load time)."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report the planned mutation without writing.",
    )

    @field_validator("nickname")
    @classmethod
    def _check_nickname(cls, v: str) -> str:
        if not v:
            raise ValueError("nickname must be non-empty")
        if ":" in v:
            raise ValueError(
                "nickname must not contain ':' — KiCAD uses that as the "
                "library/symbol separator in lib_ids"
            )
        return v


class LibRegisterLibraryOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "invalid_input",
        "invalid_schema",
        "parse_failed",
        "nickname_exists",
        "write_failed",
    ]
    table_path: str | None = Field(
        default=None, description="Resolved absolute path to the lib-table file."
    )
    nickname: str | None = Field(default=None)
    uri: str | None = Field(
        default=None,
        description=(
            "The URI as written to the row. Either '${KIPRJMOD}/...' "
            "(relative to the table's directory) or an absolute path."
        ),
    )
    created_table: bool = Field(
        default=False,
        description="True when the lib-table file was bootstrapped by this call.",
    )
    overwrote: bool = Field(
        default=False,
        description="True when an existing nickname was updated under overwrite=True.",
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class LibRegisterLibraryTool(
    Tool[LibRegisterLibraryInput, LibRegisterLibraryOutput]
):
    """Register a KiCAD library in a sym-lib-table / fp-lib-table."""

    name = "lib_register_library"
    version = "0.1.0"
    description = (
        "Register a KiCAD symbol or footprint library in the appropriate "
        "lib-table (sym-lib-table for .kicad_sym, fp-lib-table for "
        ".pretty). Bootstraps the lib-table file if missing. Rewrites "
        "project-local paths to '${KIPRJMOD}/...' URIs. Nickname "
        "conflicts return nickname_exists unless overwrite=True."
    )
    input_model = LibRegisterLibraryInput
    output_model = LibRegisterLibraryOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    def __init__(self, config: Config | None = None) -> None:
        self._config = config

    def set_config(self, config: Config) -> None:
        self._config = config

    async def run(
        self, input: LibRegisterLibraryInput
    ) -> LibRegisterLibraryOutput:
        # 1. Validate that library_path has a plausible shape for the
        # declared table_kind. Better to fail here than to register a
        # .pretty directory in sym-lib-table (KiCAD would load then
        # surface a confusing "no symbols found" at use time).
        lib_path = input.library_path.expanduser().resolve()
        if input.table_kind == "symbol":
            if lib_path.suffix.lower() != ".kicad_sym":
                return LibRegisterLibraryOutput(
                    status="invalid_input",
                    note=(
                        f"table_kind='symbol' expects a .kicad_sym path; "
                        f"got {lib_path.name}."
                    ),
                )
        else:  # footprint
            # .pretty must be a directory (or at least be named like one
            # when we're bootstrapping). Accept either an existing
            # directory or a planned path with the right suffix.
            if lib_path.suffix.lower() != ".pretty":
                return LibRegisterLibraryOutput(
                    status="invalid_input",
                    note=(
                        f"table_kind='footprint' expects a .pretty/ path; "
                        f"got {lib_path.name}."
                    ),
                )

        table_path = input.table_path.expanduser().resolve()
        expected_head = _TABLE_KIND_TO_TOP_HEAD[input.table_kind]

        # 2. Parse or bootstrap the lib-table.
        created_table = False
        doc: SexprDocument
        if table_path.exists():
            if not table_path.is_file():
                return LibRegisterLibraryOutput(
                    status="invalid_input",
                    table_path=str(table_path),
                    note=(
                        f"table_path exists but is not a regular file: "
                        f"{table_path}"
                    ),
                )
            try:
                doc = SexprDocument.from_path(table_path)
            except SexprParseError as exc:
                return LibRegisterLibraryOutput(
                    status="parse_failed",
                    table_path=str(table_path),
                    note=f"SEXPR parse failed: {exc}",
                )
            if doc.top_head != expected_head:
                return LibRegisterLibraryOutput(
                    status="invalid_schema",
                    table_path=str(table_path),
                    note=(
                        f"table_kind={input.table_kind!r} expects top-head "
                        f"{expected_head!r}; got "
                        f"{doc.top_head or '?'!r}. If you pointed at the "
                        "wrong file, swap to the matching sym-/fp- one."
                    ),
                )
        else:
            doc = _bootstrap_lib_table(table_path, expected_head)
            created_table = True

        # 3. URI derivation. Compute the path as KiCAD would store it —
        # relative to the table's directory via ${KIPRJMOD} when
        # possible, absolute otherwise. KIPRJMOD is the lib-table's
        # directory, not the project file's — but for project-scoped
        # tables (the common case) those coincide.
        uri = _compute_uri(table_dir=table_path.parent, library_path=lib_path)

        # 4. Conflict check on nickname.
        existing_idx = _find_lib_row_index(doc.root, input.nickname)
        if existing_idx is not None and not input.overwrite:
            return LibRegisterLibraryOutput(
                status="nickname_exists",
                table_path=str(table_path),
                nickname=input.nickname,
                note=(
                    f"nickname {input.nickname!r} is already registered "
                    f"in {table_path.name}. Pass overwrite=True to update "
                    "the existing row in place."
                ),
            )

        # 5. Dry-run short-circuit.
        if input.dry_run:
            action = "update" if existing_idx is not None else "append"
            hint = (
                f" Would also create lib-table at {table_path}."
                if created_table
                else ""
            )
            return LibRegisterLibraryOutput(
                status="dry_run",
                table_path=str(table_path),
                nickname=input.nickname,
                uri=uri,
                created_table=False,
                overwrote=False,
                note=(
                    f"dry_run=True; would {action} nickname "
                    f"{input.nickname!r} → {uri}.{hint}"
                ),
            )

        # 6. Build + insert the new row.
        new_row = _build_lib_row(
            nickname=input.nickname,
            uri=uri,
            description=input.description,
            options=input.options,
        )
        overwrote = False
        if existing_idx is not None:
            doc.root.replace(existing_idx, new_row)
            overwrote = True
        else:
            doc.root.append(new_row)

        # 7. Snapshot before filesystem write.
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode
        snapshot_ref: str | None = None
        try:
            snapshot_ref = snapshot(
                table_path.parent,
                mode=snapshot_mode,
                reason=(
                    f"lib_register_library:{table_path.name}:{input.nickname}"
                ),
            )
        except SnapshotError as exc:
            return LibRegisterLibraryOutput(
                status="write_failed",
                table_path=str(table_path),
                nickname=input.nickname,
                uri=uri,
                note=f"snapshot failed before write: {exc}.",
            )

        # 8. Save.
        try:
            table_path.parent.mkdir(parents=True, exist_ok=True)
            doc.save(table_path)
        except (OSError, RuntimeError) as exc:
            out_fail = LibRegisterLibraryOutput(
                status="write_failed",
                table_path=str(table_path),
                nickname=input.nickname,
                uri=uri,
                note=(
                    f"save failed after snapshot: {exc}. Restore from the "
                    "snapshot if needed."
                ),
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = LibRegisterLibraryOutput(
            status="ok",
            table_path=str(table_path),
            nickname=input.nickname,
            uri=uri,
            created_table=created_table,
            overwrote=overwrote,
        )
        out.meta.snapshot_ref = snapshot_ref
        return out


# -- helpers ---------------------------------------------------------------


def _compute_uri(*, table_dir: Path, library_path: Path) -> str:
    """Return the URI string KiCAD should store for this library row.

    When ``library_path`` is under ``table_dir`` (the common
    project-scoped case) we rewrite to ``${KIPRJMOD}/<relative>`` so
    the table remains portable across machines. Otherwise we store
    the absolute path — less portable but at least it'll resolve
    consistently on the authoring machine.

    Note: KiCAD resolves ``${KIPRJMOD}`` against the lib-table's own
    directory (same-dir semantics), not the ``.kicad_pro`` location.
    For a project-local sym-lib-table those coincide.
    """
    try:
        rel = library_path.relative_to(table_dir)
        # Forward slashes on every platform — KiCAD's own emission
        # uses them verbatim, and Windows KiCAD reads them fine.
        return f"${{KIPRJMOD}}/{rel.as_posix()}"
    except ValueError:
        return str(library_path)


def _bootstrap_lib_table(table_path: Path, expected_head: str) -> SexprDocument:
    """Synthesize a fresh lib-table in memory.

    Shape::

        (sym_lib_table
          (version 7))

    Opening this in KiCAD then saving (or adding a row via the GUI)
    round-trips without diff. The ``SexprDocument`` wrapper keeps us
    on the same write path as an existing file — save() validates the
    round-trip exactly like any other save.
    """
    body = f"({expected_head}\n\t(version {_LIB_TABLE_VERSION})\n)\n"
    return SexprDocument.from_bytes(table_path, body.encode("utf-8"))


def _find_lib_row_index(root: SList, nickname: str) -> int | None:
    """Return the index of the ``(lib ...)`` row with the given nickname."""
    for idx, child in enumerate(root.items):
        if not isinstance(child, SList) or child.head != "lib":
            continue
        name_node = child.find("name")
        if name_node is None or len(name_node.items) < 2:
            continue
        name_atom = name_node.items[1]
        if isinstance(name_atom, SAtom) and name_atom.text == nickname:
            return idx
    return None


def _build_lib_row(
    *,
    nickname: str,
    uri: str,
    description: str,
    options: str,
) -> SList:
    """``(lib (name "...") (type "KiCad") (uri "...") (options "...") (descr "..."))``.

    KiCAD writes the five-child form even when description + options
    are empty, so we do too — matches the GUI's emission so a
    subsequent "File → Save" from the library-table dialog is a no-op
    diff.
    """
    return slist(
        atom("lib"),
        slist(atom("name"), atom(nickname, quoted=True)),
        slist(atom("type"), atom("KiCad", quoted=True)),
        slist(atom("uri"), atom(uri, quoted=True)),
        slist(atom("options"), atom(options, quoted=True)),
        slist(atom("descr"), atom(description, quoted=True)),
    )


# Re-exported for unit tests that want to assert version-header shape
# without reaching into the module's private state.
def _get_table_version(doc: SexprDocument) -> int | None:
    node = doc.root.find("version")
    if node is None or len(node.items) < 2:
        return None
    v = node.items[1]
    if not isinstance(v, SAtom):
        return None
    try:
        return int(v.text)
    except ValueError:
        return None


__all__ = [
    "LibRegisterLibraryInput",
    "LibRegisterLibraryOutput",
    "LibRegisterLibraryTool",
]
