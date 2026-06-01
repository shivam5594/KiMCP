"""``circuit-proposal`` — present a design for user approval before editing.

This prompt implements the "design preview gate" workflow: when the user
asks for a circuit (e.g. "create a 32V-to-12V DC-DC converter"), the LLM
should first present a structured proposal and wait for explicit approval
before invoking any schematic mutation tools.

The gate exists because hardware engineers need to review:
  - Topology choice (buck, boost, LDO, etc.)
  - Component selection (specific part numbers, values)
  - Pin assignments and connectivity
  - Design trade-offs and alternatives considered

Without this gate, the AI jumps straight into editing .kicad_sch files,
producing a design the user never agreed to. That's the wrong workflow
for hardware — mistakes in schematics propagate to PCB, fabrication, and
assembly. Catching a wrong topology choice after 40 sch_add_* calls
wastes everyone's time.

The prompt expands to messages that tell the LLM to:
  1. Present the design as a structured proposal (topology, BOM, netlist)
  2. Explicitly ask for user approval or feedback
  3. Only proceed to schematic editing after approval

This prompt is automatically suggested when the LLM is about to create
or significantly modify a circuit. It does NOT block individual tweaks
(moving a wire, adding a junction) — the gate is for design-level
decisions, not per-element edits.
"""

from __future__ import annotations

from typing import Any

from kimcp.prompts.base import Prompt, PromptArgument


class CircuitProposalPrompt(Prompt):
    """Present a circuit design for approval before schematic editing."""

    name = "circuit-proposal"
    description = (
        "Present a structured circuit design proposal and get user approval "
        "before making any schematic changes."
    )
    arguments = (
        PromptArgument(
            name="requirement",
            description=(
                "What the user asked for — e.g. '32V to 12V DC-DC buck converter, "
                "3A output, high efficiency'. Include voltage, current, topology "
                "preferences, and any constraints mentioned."
            ),
            required=True,
        ),
        PromptArgument(
            name="sch_path",
            description=(
                "Path to the .kicad_sch file that will be edited. Relative paths "
                "resolve against the project root."
            ),
            required=False,
        ),
    )

    def render(self, arguments: dict[str, str]) -> list[dict[str, Any]]:
        requirement = arguments["requirement"]
        sch_path = arguments.get("sch_path", "(not specified yet)")
        text = _TEMPLATE.format(requirement=requirement, sch_path=sch_path)
        return [
            {
                "role": "user",
                "content": {"type": "text", "text": text},
            }
        ]


_TEMPLATE = """\
The user wants to create or modify a circuit. Before touching any schematic \
files, present a structured design proposal and wait for explicit approval.

Requirement: {requirement}
Target schematic: {sch_path}

## Instructions

**STOP. Do NOT call any sch_add_*, sch_compose, or sch_embed_lib_symbol tools yet.**

Instead, present the design proposal in this exact format:

### 1. Topology

State the chosen circuit topology (e.g. synchronous buck, async buck, boost, \
LDO, flyback) and why it fits the requirements. If multiple topologies are \
viable, briefly list alternatives with trade-offs.

### 2. Key Component Selection

Present a table:

| Ref | Part Number | Value | Package | Description | Why This Part |
|-----|-------------|-------|---------|-------------|---------------|

Include: main IC, inductors, capacitors (input/output), resistors (feedback \
divider, current sense), diodes, MOSFETs if external, and protection components. \
Use real, orderable part numbers — not generic placeholders.

### 2b. Library Availability

For each component, indicate where the KiCAD symbol comes from:

| Part | KiCAD Built-in? | Available Online? | Needs Custom Creation? |
|------|-----------------|-------------------|----------------------|

- **Built-in**: available in KiCAD's standard libraries (Device, Connector, etc.)
- **Online**: downloadable .kicad_sym from SnapEDA, Ultra Librarian, or manufacturer
- **Custom**: no existing symbol found — must create with lib_add_symbol

Ask the user: "Shall I search online for the unavailable symbols? Downloaded \
symbols will be saved to your local library for future projects."

### 3. Design Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| Input voltage range | | |
| Output voltage | | |
| Output current (max) | | |
| Switching frequency | | |
| Estimated efficiency | | |
| Ripple (output) | | |
| Thermal considerations | | |

### 4. Schematic Connectivity

Show the net connections as a readable list:

```
U1 pin VIN  ←── C_IN+ ←── VIN_32V
U1 pin SW   ──→ L1 ──→ VOUT_12V
U1 pin FB   ←── R2/R1 divider from VOUT
U1 pin GND  ──→ GND plane
...
```

### 5. Design Notes

Any critical layout considerations, thermal notes, or gotchas the user \
should know about before approving.

---

## CRITICAL: Schematic Readability Rules

When you implement the approved design, you MUST follow these rules:

### Wire-first connectivity (NO label spam)

**Connect nearby pins with WIRES (sch_add_wire), NOT labels (sch_add_label).**

Labels are net-name references, not a general connection mechanism. A human \
reads a schematic by following wires — labels break visual flow and make the \
schematic unreadable when overused.

**Legitimate label uses (single-digit count per sheet):**
- Cross-sheet connectivity (global/hierarchical labels)
- Distinguishing ground domains (AGND, DGND, PGND)
- Naming electrically important signals (CLK, SDA, MOSI, nRESET)
- Long-distance same-sheet nets where wire routing would be visually noisy

**Power symbols (GND, +12V, +32V) via sch_add_power are fine** — they are \
the standard KiCAD convention for marking power rails. But every component's \
pin-to-pin connection on the same sheet MUST be a wire, not a label.

**Bad example (label spam):**
```
U1.VIN ← label "+32V"    C1.pin1 ← label "+32V"    (NO! Use a wire)
U1.GND ← label "GND"     C1.pin2 ← label "GND"     (NO! GND power symbol is OK, but wire between nearby pins)
```

**Good example (wire-first):**
```
U1.VIN ──wire──→ C1.pin1   (direct wire connection)
U1.GND ──wire──→ C1.pin2 ──wire──→ GND_power_symbol   (wire to shared GND)
```

### Layout spacing
- Place components on 2.54mm (100-mil) grid
- Minimum ~5mm between symbol bounding boxes
- Route wires as orthogonal H/V segments
- Add junctions (sch_add_junction) at every T-connection

---

**After presenting the proposal, ask:**

> Does this design look good? I can proceed to create the schematic, or \
> adjust the topology / component selection / values based on your feedback.

**Wait for the user's explicit approval before calling any mutation tools.**

If the user provides feedback, revise the proposal and present it again. \
Only after receiving approval (e.g. "looks good", "go ahead", "approved", \
"proceed") should you begin placing symbols, wires, and labels.
"""


__all__ = ["CircuitProposalPrompt"]
