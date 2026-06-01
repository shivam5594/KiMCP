"""``manufacturing-handoff`` — DRC-clean → gerbers + drill + pos + BOM + 3D.

The second canned workflow. Where ``design-review`` is "find problems",
this is "everything the fab needs, in one motion". The skill doc
(``resources-and-prompts.md``) lists it as the manufacturing-facing
prompt; we implement the subset that maps to tools already shipped:

* ``pcb_drc`` — gate. A non-clean DRC stops the handoff.
* ``pcb_export_gerbers`` — copper + mask + silk + edge cuts.
* ``pcb_export_drill`` — Excellon drill file.
* ``pcb_export_pos`` — pick-and-place CSV for assembly.
* ``sch_export_bom`` — bill of materials.
* ``pcb_export_step`` — 3D STEP for mechanical integration.

Deliberately NOT in this ship:

* **Zip packaging.** We don't have a ``pcb_package_fab`` tool yet;
  the prompt tells the LLM to leave each artifact where the export
  tool put it and report paths, rather than inventing a zipping step
  the server can't perform.
* **Fab profile attach / stackup PDF.** The skill doc calls these
  out; both need tools that don't exist yet. The prompt mentions the
  ``fab_profile`` argument so when those tools land the template
  extends naturally instead of requiring a breaking rename.

The ``output_dir`` argument is optional and collapses to each tool's
default on absence, which is deliberately *not* a single common dir
— each tool has its own default (``./out/gerbers``, ``./out/bom.csv``,
etc.) and the prompt reflects that to avoid pretending at a
coordinated output layout that the server doesn't actually enforce.
"""

from __future__ import annotations

from typing import Any

from kimcp.prompts.base import Prompt, PromptArgument


class ManufacturingHandoffPrompt(Prompt):
    """DRC gate then export every artifact the fab needs."""

    name = "manufacturing-handoff"
    description = (
        "Gate on clean DRC, then export gerbers, drill, pick-and-place, BOM, "
        "and STEP for a fab handoff package."
    )
    arguments = (
        PromptArgument(
            name="pcb_path",
            description="Path to the .kicad_pcb file to hand off.",
            required=True,
        ),
        PromptArgument(
            name="sch_path",
            description="Path to the .kicad_sch file for the BOM export.",
            required=True,
        ),
        PromptArgument(
            name="fab_profile",
            description=(
                "Optional fab-capability profile name. Used to contextualize "
                "DRC expectations in the summary (e.g. 'JLCPCB-2L-HDI'). No "
                "server-side tool honours this yet — purely descriptive "
                "context for the LLM's summary."
            ),
            required=False,
        ),
    )

    def render(self, arguments: dict[str, str]) -> list[dict[str, Any]]:
        pcb_path = arguments["pcb_path"]
        sch_path = arguments["sch_path"]
        fab_profile = arguments.get("fab_profile")
        profile_line = (
            f"Target fab profile: `{fab_profile}`."
            if fab_profile
            else "No fab profile supplied — use generic 2-layer defaults for DRC triage."
        )
        text = _TEMPLATE.format(
            pcb_path=pcb_path, sch_path=sch_path, profile_line=profile_line
        )
        return [
            {
                "role": "user",
                "content": {"type": "text", "text": text},
            }
        ]


_TEMPLATE = """\
Please build a manufacturing handoff package for this KiCAD project.

{profile_line}

Step 1: Run `pcb_drc` on `{pcb_path}`. If any errors are reported, STOP. \
Summarize the errors and ask the user whether to proceed anyway or fix first. \
Do not move to export if DRC reports errors without explicit confirmation.

Step 2 (assuming DRC clean or user confirmed): Call each of the following \
tools in order. Each tool writes to its own default output location unless \
you supply `output_dir`.

  - `pcb_export_gerbers` on `{pcb_path}` — copper + mask + silk + edge cuts.
  - `pcb_export_drill` on `{pcb_path}` — Excellon drill file(s).
  - `pcb_export_pos` on `{pcb_path}` — pick-and-place CSV for assembly.
  - `sch_export_bom` on `{sch_path}` — bill of materials.
  - `pcb_export_step` on `{pcb_path}` — 3D STEP for mechanical integration.

Step 3: Summarize the handoff package. For each tool, report:

  - `status` field from the tool response (ok / dry_run / error).
  - Output path(s) where applicable.
  - Any warnings surfaced in the tool's `note` field.

Step 4: Call out any tool that failed or was skipped. Missing artifacts are \
the single most common cause of fab rejection; surface them explicitly rather \
than burying them in a long success list.
"""


__all__ = ["ManufacturingHandoffPrompt"]
