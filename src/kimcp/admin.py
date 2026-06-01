"""Admin CLI — `kimcp-cli`.

Offline utilities: validate the merged config, list discovered tools, etc.
No transport, no KiCAD interaction.
"""

from __future__ import annotations

import argparse
import json
import sys

from kimcp import __version__
from kimcp.config import load_config
from kimcp.tools.registry import ToolRegistry


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
    for t in sorted(tools, key=lambda t: t.name):
        print(f"{t.name}\t{t.version}\t{t.description}")
    return 0


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
    p_list.set_defaults(func=_cmd_tools_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
