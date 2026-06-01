"""Built-in prompts registered by ``Server`` on boot.

Concrete prompt classes live in sibling modules. Keeping the import
list here (instead of auto-discovering with ``pkgutil``) means every
builtin is visible at a glance and no new prompt gets silently wired
by dropping a file in the directory.
"""

from __future__ import annotations

from kimcp.prompts.builtin.circuit_proposal import CircuitProposalPrompt
from kimcp.prompts.builtin.design_review import DesignReviewPrompt
from kimcp.prompts.builtin.manufacturing_handoff import ManufacturingHandoffPrompt

__all__ = ["CircuitProposalPrompt", "DesignReviewPrompt", "ManufacturingHandoffPrompt"]
