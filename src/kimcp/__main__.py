"""Entry point for the `kimcp` console script."""

from __future__ import annotations

import argparse
import asyncio

from kimcp import __version__
from kimcp.config import load_config
from kimcp.logging_config import configure_logging
from kimcp.server import Server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kimcp", description="KiCAD MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="Transport mode. HTTP+SSE arrives in a later milestone.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["trace", "debug", "info", "warn", "error"],
        help="Override observability.log_level from config.",
    )
    parser.add_argument(
        "--no-entry-points",
        action="store_true",
        help="Skip entry-point tool discovery (useful in tests).",
    )
    parser.add_argument("--version", action="version", version=f"kimcp {__version__}")

    args = parser.parse_args(argv)

    # Load config first so logging honours observability.log_level /
    # log_path / log_format. --log-level beats config when supplied.
    config = load_config()
    configure_logging(config.observability, override_level=args.log_level)

    server = Server(config=config)
    if not args.no_entry_points:
        server.discover_tools()

    async def _run() -> None:
        await server.probe_backends()
        await server.run_stdio()

    if args.transport == "stdio":
        try:
            asyncio.run(_run())
        except KeyboardInterrupt:
            return 130
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
