"""Server wiring — config + registry + dispatcher + handler + transport.

M0 exposes a subset of the MCP method set sufficient for a client to
initialize, list tools, and call tools. Resources, prompts, and cancellation
arrive in later milestones.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from kimcp import __version__
from kimcp._types import Backend, ToolClass
from kimcp.backends.base import BackendAdapter
from kimcp.backends.cli import CliBackend
from kimcp.backends.dispatcher import BackendAvailability, Dispatcher
from kimcp.backends.ipc import IpcBackend
from kimcp.backends.sexpr import SexprBackend
from kimcp.backends.swig import SwigBackend
from kimcp.config import Config, load_config
from kimcp.errors import INVALID_PARAMS, METHOD_NOT_FOUND, VALIDATION_ERROR, RpcError
from kimcp.prompts import PromptRegistry
from kimcp.prompts.builtin import DesignReviewPrompt, ManufacturingHandoffPrompt
from kimcp.resources import ResourceProvider
from kimcp.rpc import JsonRpcHandler, dispatch_loop
from kimcp.safety import SnapshotPolicy
from kimcp.safety.audit import record as audit_record
from kimcp.sexpr.cache import DEFAULT_MAX_BYTES, ParseCache
from kimcp.sexpr.watcher import CacheInvalidator
from kimcp.tools.base import Tool
from kimcp.tools.builtin.config_show import ConfigShowTool
from kimcp.tools.builtin.ipc_get_version import IpcGetVersionTool
from kimcp.tools.builtin.kicad_ipc_status import KiCadIpcStatusTool
from kimcp.tools.builtin.kicad_version import KiCadVersionTool
from kimcp.tools.builtin.pcb_drc import PcbDrcTool
from kimcp.tools.builtin.pcb_export_drill import PcbExportDrillTool
from kimcp.tools.builtin.pcb_export_gerbers import PcbExportGerbersTool
from kimcp.tools.builtin.pcb_export_pdf import PcbExportPdfTool
from kimcp.tools.builtin.pcb_export_pos import PcbExportPosTool
from kimcp.tools.builtin.pcb_export_step import PcbExportStepTool
from kimcp.tools.builtin.sch_add_junction import SchAddJunctionTool
from kimcp.tools.builtin.sch_add_label import SchAddLabelTool
from kimcp.tools.builtin.sch_add_no_connect import SchAddNoConnectTool
from kimcp.tools.builtin.sch_add_power import SchAddPowerTool
from kimcp.tools.builtin.sch_add_sheet import SchAddSheetTool
from kimcp.tools.builtin.sch_add_symbol import SchAddSymbolTool
from kimcp.tools.builtin.sch_compose import SchComposeTool
from kimcp.tools.builtin.sch_add_wire import SchAddWireTool
from kimcp.tools.builtin.sch_delete import SchDeleteTool
from kimcp.tools.builtin.sch_embed_lib_symbol import SchEmbedLibSymbolTool
from kimcp.tools.builtin.sch_erc import SchErcTool
from kimcp.tools.builtin.sch_export_bom import SchExportBomTool
from kimcp.tools.builtin.sch_export_netlist import SchExportNetlistTool
from kimcp.tools.builtin.sch_export_pdf import SchExportPdfTool
from kimcp.tools.builtin.sch_set_title_block import SchSetTitleBlockTool
from kimcp.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


# MCP spec version we target. Bumped in lockstep with protocol upgrades.
MCP_PROTOCOL_VERSION = "2025-06-18"


class Server:
    def __init__(
        self,
        *,
        config: Config | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.config = config or load_config()
        # ``project_root`` roots the resources layer (M13). Default to cwd
        # to match ``load_config``'s project-local discovery rule — passing
        # an explicit path is primarily for tests and for future admin
        # entry points that want to open a project outside cwd.
        self.project_root: Path = (project_root or Path.cwd()).resolve()

        self.registry = ToolRegistry()
        self.availability = BackendAvailability()
        self.dispatcher = Dispatcher(self.availability)

        self._cli_backend = CliBackend(
            configured_path=self.config.kicad.cli_exe,
            min_version=self.config.kicad.min_version,
        )
        self._ipc_backend = IpcBackend(
            configured_path=self.config.kicad.ipc_socket,
        )
        # Sexpr cache + optional file watcher — watcher built only when
        # ``performance.file_watch=true`` (the default). When off, the cache
        # still works — it just relies on ``get()``'s stat-based
        # invalidation rather than eager eviction (see
        # ``sexpr/watcher.py`` for the trade-off).
        #
        # Cap is ``performance.cache_max_mb`` * 1 MiB; bypass capping when
        # the knob is explicitly set to 0 by treating it as "tiny cache",
        # not "unbounded" — prevents a typo from letting the cache eat all
        # memory.
        cache_max_bytes = max(0, self.config.performance.cache_max_mb) * 1024 * 1024
        if cache_max_bytes == 0:
            cache_max_bytes = DEFAULT_MAX_BYTES
        self._parse_cache = ParseCache(max_bytes=cache_max_bytes)
        # Snapshot cadence governor (Perf P1b). Owned by the server so
        # the counter survives across tool calls in one session and
        # resets on restart. Each mutating tool that exposes
        # set_snapshot_policy gets this instance.
        self._snapshot_policy = SnapshotPolicy(
            every_n_calls=self.config.safety.snapshot_every_n_calls,
        )
        self._cache_invalidator: CacheInvalidator | None = None
        if self.config.performance.file_watch:
            self._cache_invalidator = CacheInvalidator(self._parse_cache)
            # Schedule the project root so any KiCAD file under it gets
            # eager-eviction on edit. ``schedule`` is tolerant of missing
            # dirs — synthetic project_roots in tests won't crash boot.
            self._cache_invalidator.schedule(self.project_root)
        self._sexpr_backend = SexprBackend(
            cache=self._parse_cache, watcher=self._cache_invalidator
        )
        self._backends: dict[str, BackendAdapter] = {
            "ipc": self._ipc_backend,
            "cli": self._cli_backend,
            "sexpr": self._sexpr_backend,
            "swig": SwigBackend(),
        }

        self.resources = ResourceProvider(self.project_root)

        # Prompts registry — populated with the builtin canned-workflow
        # prompts at construction time. No entry-point discovery yet;
        # third-party prompts will load through the same mechanism as
        # tools when we see a concrete third party.
        self.prompts = PromptRegistry()
        self.prompts.register(DesignReviewPrompt())
        self.prompts.register(ManufacturingHandoffPrompt())

        self.handler = JsonRpcHandler()
        self._install_builtin_methods()

    # ------------------------------------------------------------------
    # Tool discovery / registration
    # ------------------------------------------------------------------

    def register_tool(self, tool: Tool[Any, Any]) -> None:
        self.registry.register(tool)
        self._inject_dependencies(tool)

    def discover_tools(self) -> int:
        added = self.registry.load_entry_points()
        for t in self.registry.all_tools():
            self._inject_dependencies(t)
        log.info("discovered %d tool(s) via entry points", added)
        return added

    def _inject_dependencies(self, tool: Tool[Any, Any]) -> None:
        """Wire live server-owned singletons into tools that opted in via setters.

        Tools that need access to shared state (config, CLI backend, etc.) ask
        for it through a `set_*` method. The server checks each known
        dependency and injects when the tool exposes the corresponding setter.
        This keeps the Tool base class narrow while still supporting
        dependency injection without globals.
        """
        if isinstance(tool, ConfigShowTool):
            tool.set_config(self.config)
        if isinstance(tool, KiCadVersionTool):
            tool.set_cli_backend(self._cli_backend)
        if isinstance(tool, KiCadIpcStatusTool):
            tool.set_ipc_backend(self._ipc_backend)
        if isinstance(tool, IpcGetVersionTool):
            tool.set_ipc_backend(self._ipc_backend)
        if isinstance(tool, PcbDrcTool):
            tool.set_cli_backend(self._cli_backend)
        if isinstance(tool, PcbExportGerbersTool):
            tool.set_cli_backend(self._cli_backend)
        if isinstance(tool, PcbExportDrillTool):
            tool.set_cli_backend(self._cli_backend)
        if isinstance(tool, PcbExportStepTool):
            tool.set_cli_backend(self._cli_backend)
        if isinstance(tool, PcbExportPosTool):
            tool.set_cli_backend(self._cli_backend)
        if isinstance(tool, PcbExportPdfTool):
            tool.set_cli_backend(self._cli_backend)
        if isinstance(tool, SchErcTool):
            tool.set_cli_backend(self._cli_backend)
        if isinstance(tool, SchExportNetlistTool):
            tool.set_cli_backend(self._cli_backend)
        if isinstance(tool, SchExportBomTool):
            tool.set_cli_backend(self._cli_backend)
        if isinstance(tool, SchExportPdfTool):
            tool.set_cli_backend(self._cli_backend)
        if isinstance(tool, SchSetTitleBlockTool):
            tool.set_config(self.config)
        if isinstance(tool, SchAddSymbolTool):
            tool.set_config(self.config)
        if isinstance(tool, SchAddWireTool):
            tool.set_config(self.config)
        if isinstance(tool, SchAddJunctionTool):
            tool.set_config(self.config)
        if isinstance(tool, SchAddLabelTool):
            tool.set_config(self.config)
        if isinstance(tool, SchAddPowerTool):
            tool.set_config(self.config)
        if isinstance(tool, SchAddSheetTool):
            tool.set_config(self.config)
        if isinstance(tool, SchEmbedLibSymbolTool):
            tool.set_config(self.config)
        if isinstance(tool, SchAddNoConnectTool):
            tool.set_config(self.config)
        if isinstance(tool, SchDeleteTool):
            tool.set_config(self.config)
        if isinstance(tool, SchComposeTool):
            tool.set_config(self.config)

        # ParseCache injection — any schematic-mutating or schematic-reading
        # tool that opts in via ``set_parse_cache`` gets the server-owned
        # cache. Using hasattr keeps the list tools (sch_list_*) and any
        # future tool registered through entry points without forcing them
        # into the explicit isinstance chain above. Per Perf P0a: every
        # call routed through the cache saves ~96 ms / 100 KB on a hit.
        if hasattr(tool, "set_parse_cache"):
            tool.set_parse_cache(self._parse_cache)

        # Snapshot-cadence injection (Perf P1b). Same hasattr-based
        # discovery pattern. Mutating tools that opted in by exposing
        # set_snapshot_policy get the server-owned policy and respect
        # ``safety.snapshot_every_n_calls``.
        if hasattr(tool, "set_snapshot_policy"):
            tool.set_snapshot_policy(self._snapshot_policy)

    # ------------------------------------------------------------------
    # Audit (M32)
    # ------------------------------------------------------------------

    def _should_audit(self, tool: Tool[Any, Any]) -> bool:
        """Return True when this call should leave an audit-log entry.

        The rules follow `safety.md`:

        * ``audit_enabled=False`` — log nothing at all.
        * MUTATE / DESTRUCTIVE / EXTERNAL — always logged when enabled.
          These are the classes that change state on disk, invoke a
          subprocess with side-effects, or hit a paid external API, so
          "who did what, when" is load-bearing for incident response.
        * READ — only logged when ``audit_read_tools=True``. READ calls
          are high-volume (every ``list_symbols`` would write a line)
          and low-risk, so opting them in is an explicit deployment
          choice for compliance-sensitive hosts.
        """
        safety = self.config.safety
        if not safety.audit_enabled:
            return False
        if tool.classification == ToolClass.READ:
            return safety.audit_read_tools
        return True

    # ------------------------------------------------------------------
    # Backend probing
    # ------------------------------------------------------------------

    async def probe_backends(self) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for name, backend in self._backends.items():
            try:
                ok = await backend.probe()
            except Exception:
                log.exception("probe failed for backend %s", name)
                ok = False
            self.availability.mark(backend.kind, ok)
            results[name] = ok
        # File-watcher starts after probes — a failing probe doesn't imply
        # we shouldn't watch, but we want the observer thread born into a
        # process that's about to live, not one mid-crash. ``start`` is
        # idempotent, so re-probes (HTTP transport, reconnect) stay cheap.
        if self._cache_invalidator is not None:
            self._cache_invalidator.start()
        return results

    async def aclose(self) -> None:
        """Release per-session backend resources. Idempotent.

        ``IpcBackend`` holds a long-lived ``pynng.Req0`` socket opened on
        the first ``call()``; leaving it dangling at shutdown leaks an fd
        and a thread in pynng's internal pool. ``SexprBackend`` optionally
        owns a watchdog observer thread (``performance.file_watch=true``)
        that needs joining so we don't leak a daemon thread across
        long-running hosts (e.g. HTTP transport) or across pytest's
        in-process restarts.

        Fanning out here keeps knowledge of "which backends own what"
        local to the server — transports call a single method and don't
        need to know which subset of backends have cleanup work.
        """
        await self._ipc_backend.aclose()
        await self._sexpr_backend.aclose()

    # ------------------------------------------------------------------
    # JSON-RPC method handlers (MCP-flavored)
    # ------------------------------------------------------------------

    def _install_builtin_methods(self) -> None:
        self.handler.register("initialize", self._handle_initialize)
        self.handler.register("initialized", self._handle_initialized)  # notification
        self.handler.register("tools/list", self._handle_tools_list)
        self.handler.register("tools/call", self._handle_tools_call)
        self.handler.register("resources/list", self._handle_resources_list)
        self.handler.register("resources/read", self._handle_resources_read)
        self.handler.register("prompts/list", self._handle_prompts_list)
        self.handler.register("prompts/get", self._handle_prompts_get)
        self.handler.register("shutdown", self._handle_shutdown)

    async def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverInfo": {"name": "kimcp", "version": __version__},
            "capabilities": {
                "tools": {"listChanged": False},
                # Resources (M13): read-only KiCAD file discovery under
                # ``project_root``. No listChanged / subscribe yet — those
                # depend on wiring the sexpr cache's file watcher into a
                # notification emitter.
                "resources": {"listChanged": False, "subscribe": False},
                # Prompts: canned-workflow templates per
                # ``resources-and-prompts.md``. ``listChanged=False``
                # because the registry is populated once at construction
                # and never mutated at runtime; revisit if we grow a
                # per-session prompt-reload path.
                "prompts": {"listChanged": False},
            },
        }

    async def _handle_initialized(self, params: dict[str, Any]) -> None:
        # Notification — no response body.
        log.debug("client sent 'initialized'")
        return None

    async def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": self.registry.mcp_tool_list()}

    async def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise RpcError(INVALID_PARAMS, "'name' is required and must be a string")

        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "'arguments' must be an object")

        tool = self.registry.get(name)
        if tool is None:
            raise RpcError(METHOD_NOT_FOUND, f"unknown tool: {name}")

        try:
            input_obj = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise RpcError(
                VALIDATION_ERROR,
                "input validation failed",
                {"errors": exc.errors(include_url=False)},
            ) from exc

        # Backend selection. Tools with an empty `preferred_backends` tuple
        # (e.g., ping, version, config_show, and the backend self-reporter
        # diagnostics) are backend-agnostic and skip the dispatcher entirely —
        # `meta.backend_used` stays None for them, which is the honest answer.
        # For tools that *use* a backend to service the call, `dispatcher.pick`
        # returns the winner or raises `RpcError(BACKEND_UNAVAILABLE)`; the
        # latter propagates as a JSON-RPC error without further translation.
        #
        # `required_backends or None` collapses the default `frozenset()` (the
        # base-class declaration meaning "no required filter") to `None`, so
        # the dispatcher's required-set filter doesn't reject every backend.
        chosen: Backend | None = None
        if tool.preferred_backends:
            chosen = self.dispatcher.pick(
                preferred=tool.preferred_backends,
                required=tool.required_backends or None,
            )
            log.info("tools/call: dispatching %s via %s backend", name, chosen.value)

        start = time.monotonic()
        output = await tool.run(input_obj)
        duration_ms = int((time.monotonic() - start) * 1000)
        # Fill in timing on the envelope even if the tool didn't touch it.
        output.meta.duration_ms = duration_ms
        if chosen is not None:
            # Annotate only when the dispatcher ran — backend-agnostic tools
            # keep `backend_used = None`, preserving the envelope's "no backend
            # serviced this call" contract.
            output.meta.backend_used = chosen

        # Audit log (M32). Every MUTATE / DESTRUCTIVE / EXTERNAL call gets a
        # line in ``<project>/.kimcp/audit.log`` by default. READ calls are
        # audited only when explicitly opted in (``safety.audit_read_tools``)
        # because they're high-volume and low-risk. Audit failures MUST NOT
        # break the tool call — a full disk or a permission error shouldn't
        # hide the legitimate result the user already computed.
        if self._should_audit(tool):
            try:
                audit_record(
                    self.project_root,
                    tool=tool.name,
                    input_summary=_summarize_input(arguments),
                    snapshot_ref=output.meta.snapshot_ref,
                )
            except OSError:
                log.exception("audit log write failed for tool=%s", tool.name)

        # MCP `tools/call` result shape (spec 2025-06-18):
        #   { content: [...], structuredContent?: object, isError?: bool }
        # Returning the raw envelope at the top level (what we did in M0)
        # works for our own e2e tests but renders as "no output" in real
        # MCP clients (Claude Code / Claude Desktop), which look for the
        # `content` array. We surface the same envelope twice: as a JSON
        # text block in `content` (so any client renders something useful)
        # and as `structuredContent` (so spec-2025-06-18 clients can read
        # the typed payload directly without re-parsing). `isError` stays
        # False — domain-level failures are encoded in the envelope's
        # `status` field; only RpcError raises become JSON-RPC errors.
        dumped: dict[str, Any] = output.model_dump(mode="json")
        return {
            "content": [{"type": "text", "text": json.dumps(dumped, indent=2)}],
            "structuredContent": dumped,
            "isError": False,
        }

    async def _handle_resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        # Pagination is advertised off — ``cursor`` in params is simply
        # ignored. Kept in the signature because MCP clients may still
        # send it per the spec's optional cursor contract.
        return {"resources": self.resources.list_resources()}

    async def _handle_resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise RpcError(INVALID_PARAMS, "'uri' is required and must be a string")
        return {"contents": self.resources.read(uri)}

    async def _handle_prompts_list(self, params: dict[str, Any]) -> dict[str, Any]:
        # Pagination advertised off (same rationale as resources/list);
        # a ``cursor`` param would be accepted and ignored. The spec
        # allows clients to send it regardless of capability.
        return {"prompts": self.prompts.mcp_prompt_list()}

    async def _handle_prompts_get(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise RpcError(INVALID_PARAMS, "'name' is required and must be a string")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "'arguments' must be an object")
        return self.prompts.render(name, arguments)

    async def _handle_shutdown(self, params: dict[str, Any]) -> None:
        return None

    # ------------------------------------------------------------------
    # Transports
    # ------------------------------------------------------------------

    async def run_stdio(self) -> None:
        # Lazy import to keep `kimcp` importable without stdio side-effects.
        from kimcp.transport.stdio import StdioTransport

        # Populate backend availability once at startup. Without this, any
        # tool with a non-empty `preferred_backends` would raise
        # BACKEND_UNAVAILABLE on every call — the dispatcher reads from
        # `self.availability`, which starts empty. Probing once at boot
        # matches how clients expect a local MCP server to behave; callers
        # that need to re-probe mid-session can trigger it via a future
        # diagnostic tool.
        await self.probe_backends()

        transport = await StdioTransport.create()
        try:
            await dispatch_loop(transport, self.handler)
        finally:
            # Nested finally: aclose MUST run even if transport.close
            # raises, and transport.close MUST run even if aclose raises.
            # Either failure is unusual enough that we'd rather finish
            # the other half of shutdown than short-circuit it.
            try:
                await self.aclose()
            finally:
                await transport.close()


# --------------------------------------------------------------------------
# Audit input summarizer (M32)
# --------------------------------------------------------------------------

# Strings longer than this are truncated in the audit summary. Raw inputs can
# hit a few MB on tools like ``pcb_drc`` (file paths are fine; opaque payloads
# are not), and the audit log is meant to be grep-able, not a data dump.
_AUDIT_STR_LIMIT = 160


def _summarize_input(arguments: dict[str, Any]) -> dict[str, Any]:
    """Shape a compact, JSON-safe summary of a tool's raw arguments.

    The audit log is for forensics, not replay. We keep the top-level key
    structure (so you can answer "was ``dry_run`` true?") but avoid dumping
    raw multi-kilobyte payloads. The rules:

    * ``None``, ``bool``, ``int``, ``float`` — passed through.
    * ``str`` — truncated to ``_AUDIT_STR_LIMIT`` chars with an ellipsis.
    * ``list`` — replaced with ``{"_type": "list", "len": N}``.
    * ``dict`` — recursed one level; nested values go through the same
      summarizer so a nested string also gets truncated.
    * ``bytes`` — replaced with ``{"_type": "bytes", "len": N}``.
    * anything else — ``repr()``-style marker with type name.

    The output is always ``json.dumps``-safe so the audit writer never
    fails on a weird pydantic model or Path object leaking through.
    """
    return {k: _summarize_value(v) for k, v in arguments.items()}


def _summarize_value(v: Any) -> Any:
    if v is None or isinstance(v, bool | int | float):
        return v
    if isinstance(v, str):
        if len(v) <= _AUDIT_STR_LIMIT:
            return v
        return v[: _AUDIT_STR_LIMIT - 3] + "..."
    if isinstance(v, list):
        return {"_type": "list", "len": len(v)}
    if isinstance(v, dict):
        return {k: _summarize_value(inner) for k, inner in v.items()}
    if isinstance(v, bytes):
        return {"_type": "bytes", "len": len(v)}
    # Fall-through for unexpected types (Path, Pydantic models that sneak
    # through pre-validation, etc.). Keep it string-safe so the JSON
    # writer downstream never raises.
    return {"_type": type(v).__name__, "repr": repr(v)[:_AUDIT_STR_LIMIT]}


__all__ = ["MCP_PROTOCOL_VERSION", "Server"]
