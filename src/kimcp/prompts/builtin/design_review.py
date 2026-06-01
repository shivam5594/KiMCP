"""``design-review`` — canned workflow for ERC + DRC sweep.

One of the seven prompts listed in ``resources-and-prompts.md``. This
one covers the "quickly find the obvious problems" loop: run ERC on
the schematic, run DRC on the PCB, surface both finding lists with a
request for triage.

Scope for the first ship: text-only expansion. The prompt message
names the exact tools the LLM should call and in what order, but the
actual invocations happen client-side (under user consent), not here.
This matches MCP's "prompt expands to messages; LLM drives tools"
division of labor — see ``prompts/base.py`` for the rationale.

Why these particular tools:

* ``sch_erc`` — covers the "wires that don't connect" and "unused
  inputs" class of schematic bugs that bite every project eventually.
* ``pcb_drc`` — covers clearance / annular ring / unconnected-net
  violations. The PCB analog of ERC.

Deliberately NOT pulling in cross-referenced domain-knowledge checks
(Thread C territory) — this prompt only orchestrates what the server
can already do today. A follow-up ``design-review-deep`` can layer
the domain checks on once Thread C ships.
"""

from __future__ import annotations

from typing import Any

from kimcp.prompts.base import Prompt, PromptArgument


class DesignReviewPrompt(Prompt):
    """Chain ERC + DRC, request a structured triage summary."""

    name = "design-review"
    description = (
        "Run ERC on the schematic and DRC on the PCB, then summarize findings "
        "grouped by severity with fix hints."
    )
    arguments = (
        PromptArgument(
            name="sch_path",
            description=(
                "Path to the .kicad_sch file to review. Relative paths resolve "
                "against the project root."
            ),
            required=True,
        ),
        PromptArgument(
            name="pcb_path",
            description=(
                "Path to the .kicad_pcb file to review. Relative paths resolve "
                "against the project root."
            ),
            required=True,
        ),
    )

    def render(self, arguments: dict[str, str]) -> list[dict[str, Any]]:
        sch_path = arguments["sch_path"]
        pcb_path = arguments["pcb_path"]
        text = _TEMPLATE.format(sch_path=sch_path, pcb_path=pcb_path)
        return [
            {
                "role": "user",
                "content": {"type": "text", "text": text},
            }
        ]


# Template kept module-level (not inlined) so a test can assert the
# expansion shape without recomputing the f-string.
_TEMPLATE = """\
Please run a design review on this KiCAD project and produce a structured triage.

Step 1: Run the `sch_erc` tool on `{sch_path}`. Collect the findings.

Step 2: Run the `pcb_drc` tool on `{pcb_path}`. Collect the findings.

Step 3: Summarize the combined findings grouped by severity (error / warning / \
info). For each group:

  - Count of findings.
  - Top 3 representative examples with: rule id, affected reference(s) if \
available, short description, and a one-line fix hint.

Step 4: Call out any findings that appear in both reports (e.g. an unconnected \
net flagged by both ERC and DRC) — those usually indicate a root cause worth \
fixing once instead of twice.

If either tool reports `status != "ok"`, stop and surface the error — don't \
pretend to review a file the server couldn't read.
"""


__all__ = ["DesignReviewPrompt"]
