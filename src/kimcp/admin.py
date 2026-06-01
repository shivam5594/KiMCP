"""Admin CLI — `kimcp-cli`.

Offline utilities: validate the merged config, list discovered tools, etc.
No transport, no KiCAD interaction.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Any

from kimcp import __version__
from kimcp.config import load_config
from kimcp.tools.registry import ToolRegistry

# -- helpers ---------------------------------------------------------------

# Category order and labels for the table view. Tool name prefixes are
# matched top-down; the first hit wins.
_CATEGORIES: list[tuple[str, str]] = [
    ("sch_", "Schematic"),
    ("pcb_", "PCB"),
    ("lib_", "Library"),
]
_DEFAULT_CATEGORY = "Diagnostics"


def _categorize(name: str) -> str:
    for prefix, label in _CATEGORIES:
        if name.startswith(prefix):
            return label
    return _DEFAULT_CATEGORY


def _short_desc(description: str, max_len: int) -> str:
    """Return the first sentence, truncated to *max_len* characters."""
    # Take up to the first period that is followed by a space or is at end.
    for i, ch in enumerate(description):
        if ch == "." and (i + 1 == len(description) or description[i + 1] == " "):
            description = description[: i + 1]
            break
    if len(description) <= max_len:
        return description
    return description[: max_len - 1] + "…"


def _classification_badge(tool: Any) -> str:
    """Single-char classification badge for the table view."""
    cls = getattr(tool, "classification", None)
    if cls is None:
        return " "
    return {
        "read": "R",
        "mutate": "M",
        "destructive": "D",
        "external": "E",
    }.get(str(cls), " ")


# -- commands --------------------------------------------------------------


def _cmd_config_show(args: argparse.Namespace) -> int:
    cfg = load_config()
    json.dump(cfg.model_dump(mode="json"), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_config_validate(args: argparse.Namespace) -> int:
    load_config()  # raises on invalid
    print("config OK", file=sys.stderr)
    return 0


def _cmd_tools_list(args: argparse.Namespace) -> int:
    reg = ToolRegistry()
    reg.load_entry_points()
    tools = reg.all_tools()
    if not tools:
        print("no tools discovered", file=sys.stderr)
        return 0

    fmt = getattr(args, "format", "table")
    sorted_tools = sorted(tools, key=lambda t: t.name)

    if fmt == "raw":
        for t in sorted_tools:
            print(f"{t.name}\t{t.version}\t{t.description}")
        return 0

    if fmt == "json":
        payload = [
            {
                "name": t.name,
                "version": t.version,
                "classification": str(getattr(t, "classification", "")),
                "description": t.description,
            }
            for t in sorted_tools
        ]
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    # ---- table (default) ----
    _print_table(sorted_tools)
    return 0


def _print_table(tools: list[Any]) -> None:
    """Pretty-print tools grouped by category with aligned columns."""
    term_width = shutil.get_terminal_size((100, 24)).columns

    # Group by category, preserving sort order within each group.
    groups: dict[str, list[Any]] = {}
    for t in tools:
        cat = _categorize(t.name)
        groups.setdefault(cat, []).append(t)

    # Column widths.
    name_w = max(len(t.name) for t in tools)
    # badge(1) + space(1) + name + gap(2) + ver + gap(2) + desc
    ver_w = max(len(t.version) for t in tools)
    fixed_w = 1 + 1 + name_w + 2 + ver_w + 3  # badge, space, name, gaps
    desc_w = max(term_width - fixed_w, 20)

    # Use color when stdout is a terminal and NO_COLOR is not set.
    use_color = sys.stdout.isatty() and "NO_COLOR" not in os.environ

    def _dim(text: str) -> str:
        return f"\033[2m{text}\033[0m" if use_color else text

    def _bold(text: str) -> str:
        return f"\033[1m{text}\033[0m" if use_color else text

    def _cyan(text: str) -> str:
        return f"\033[36m{text}\033[0m" if use_color else text

    def _badge_color(badge: str) -> str:
        if not use_color:
            return badge
        colors = {"R": "\033[32m", "M": "\033[33m", "D": "\033[31m", "E": "\033[35m"}
        code = colors.get(badge, "")
        return f"{code}{badge}\033[0m" if code else badge

    # Category display order: follow _CATEGORIES order, then _DEFAULT.
    cat_order = [label for _, label in _CATEGORIES] + [_DEFAULT_CATEGORY]
    ordered_cats = [c for c in cat_order if c in groups]

    total = len(tools)
    header = f"KiMCP Tools ({total} discovered)"
    print()
    print(_bold(header))
    print(_dim("─" * min(len(header) + 4, term_width)))
    print()
    print(
        _dim(f"  {'TOOL':<{name_w}}  {'VER':<{ver_w}}  {'DESCRIPTION'}")
    )
    print(_dim(f"  {'─' * name_w}  {'─' * ver_w}  {'─' * desc_w}"))

    for cat in ordered_cats:
        cat_tools = groups[cat]
        print()
        print(f"  {_cyan(_bold(cat))} {_dim(f'({len(cat_tools)})')}")
        print()
        for t in cat_tools:
            badge = _badge_color(_classification_badge(t))
            desc = _short_desc(t.description, desc_w)
            print(
                f"{badge} {t.name:<{name_w}}  {_dim(t.version):<{ver_w + (7 if use_color else 0)}}  {desc}"
            )

    print()
    print(
        _dim(f"  R=read  M=mutate  D=destructive  E=external")
    )
    print()


# -- parser ----------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kimcp-cli", description="KiMCP admin utilities")
    parser.add_argument("--version", action="version", version=f"kimcp-cli {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    p_cfg = sub.add_parser("config", help="Configuration utilities")
    cfg_sub = p_cfg.add_subparsers(dest="config_cmd", required=True)
    p_show = cfg_sub.add_parser("show", help="Print the merged (effective) config as JSON")
    p_show.set_defaults(func=_cmd_config_show)
    p_val = cfg_sub.add_parser("validate", help="Validate the current config")
    p_val.set_defaults(func=_cmd_config_validate)

    p_tools = sub.add_parser("tools", help="Tool utilities")
    tools_sub = p_tools.add_subparsers(dest="tools_cmd", required=True)
    p_list = tools_sub.add_parser("list", help="List discovered tools")
    p_list.add_argument(
        "--format",
        choices=["table", "raw", "json"],
        default="table",
        help="Output format (default: table). 'raw' gives tab-separated output for scripts.",
    )
    p_list.set_defaults(func=_cmd_tools_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
