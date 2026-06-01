"""Backend adapters + dispatcher (`.claude/skills/kimcp-architecture/backends.md`).

Per ADR-0003 + ADR-0014, four backends coexist: IPC (primary mutation path),
CLI (canonical exports), S-expression parser (reads + headless writes), SWIG
(residual gaps — shrinking each KiCAD release).
"""

from __future__ import annotations

from kimcp.backends.base import BackendAdapter
from kimcp.backends.cli import CliBackend
from kimcp.backends.dispatcher import BackendAvailability, Dispatcher
from kimcp.backends.ipc import IpcBackend
from kimcp.backends.sexpr import SexprBackend
from kimcp.backends.swig import SwigBackend

__all__ = [
    "BackendAdapter",
    "BackendAvailability",
    "CliBackend",
    "Dispatcher",
    "IpcBackend",
    "SexprBackend",
    "SwigBackend",
]
