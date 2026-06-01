"""Unit tests for ``kimcp.prompts`` — base + registry.

These pin the prompt-layer contract: registration, MCP surface shape,
argument validation, and the two builtin canned workflows. The server
wires ``prompts/list`` and ``prompts/get`` onto this registry; breaking
any of the guarantees here would surface as a wire-format regression.

Why unit-only (no e2e yet): the prompts layer is text-only — no
backends, no filesystem side-effects. The server-integration pass is
covered in ``test_server_handler.py`` where the RPC envelope shape is
the main thing to pin.
"""

from __future__ import annotations

from typing import Any

import pytest

from kimcp.errors import INVALID_PARAMS, METHOD_NOT_FOUND, RpcError
from kimcp.prompts import Prompt, PromptArgument, PromptRegistry
from kimcp.prompts.builtin import DesignReviewPrompt, ManufacturingHandoffPrompt


class _StubPrompt(Prompt):
    """Minimal prompt for testing the registry in isolation."""

    name = "stub"
    description = "a stub prompt"
    arguments = (
        PromptArgument(name="thing", description="the thing", required=True),
        PromptArgument(name="optional_thing", description="optional", required=False),
    )

    def render(self, arguments: dict[str, str]) -> list[dict[str, Any]]:
        thing = arguments["thing"]
        return [
            {
                "role": "user",
                "content": {"type": "text", "text": f"do the {thing}"},
            }
        ]


# -- PromptArgument.to_mcp() wire shape -----------------------------------


def test_prompt_argument_to_mcp_omits_empty_description() -> None:
    """When description is empty, the MCP entry must omit the key.

    MCP clients treat ``"description": ""`` and "no description key" as
    different — pin the stricter interpretation so clients don't render
    an empty tooltip.
    """
    arg = PromptArgument(name="x")
    assert arg.to_mcp() == {"name": "x"}


def test_prompt_argument_to_mcp_omits_required_when_false() -> None:
    """``required: false`` is the default; MCP implies it when the key is
    absent, so we omit rather than sending the default noise."""
    arg = PromptArgument(name="x", description="d")
    assert arg.to_mcp() == {"name": "x", "description": "d"}
    assert "required" not in arg.to_mcp()


def test_prompt_argument_to_mcp_emits_required_true_only() -> None:
    arg = PromptArgument(name="x", description="d", required=True)
    assert arg.to_mcp() == {"name": "x", "description": "d", "required": True}


# -- Registration invariants ----------------------------------------------


def test_registry_rejects_prompt_without_name() -> None:
    """A blank ``name`` class attribute is a programming error — catch at
    registration, not at invocation where it'd 500 a real client."""

    class _Nameless(Prompt):
        description = "no name"

        def render(self, arguments: dict[str, str]) -> list[dict[str, Any]]:
            return []

    reg = PromptRegistry()
    with pytest.raises(ValueError, match="has no `name`"):
        reg.register(_Nameless())


def test_registry_rejects_duplicate_name() -> None:
    reg = PromptRegistry()
    reg.register(_StubPrompt())
    with pytest.raises(ValueError, match="duplicate prompt name"):
        reg.register(_StubPrompt())


def test_registry_get_returns_none_for_unknown() -> None:
    reg = PromptRegistry()
    assert reg.get("not-there") is None


def test_registry_unregister_is_noop_for_unknown_name() -> None:
    """Mirror ``ToolRegistry.unregister`` — removing something that was
    never there must not raise. Allows idempotent teardown."""
    reg = PromptRegistry()
    reg.unregister("never-registered")  # must not raise


# -- mcp_prompt_list surface ----------------------------------------------


def test_mcp_prompt_list_is_sorted_by_name() -> None:
    """Deterministic ordering — matches the resources + tools surface.

    Clients use the order verbatim for UI; unstable ordering would
    cause menus to reshuffle between sessions.
    """
    reg = PromptRegistry()
    reg.register(ManufacturingHandoffPrompt())
    reg.register(DesignReviewPrompt())
    listing = reg.mcp_prompt_list()
    names = [entry["name"] for entry in listing]
    assert names == sorted(names)


def test_mcp_prompt_list_omits_arguments_when_empty() -> None:
    """A prompt with no declared args must not emit ``arguments: []``.

    Matches the same "omit defaults" discipline PromptArgument follows
    for ``description`` / ``required``.
    """

    class _NoArgs(Prompt):
        name = "no-args"
        description = "takes nothing"

        def render(self, arguments: dict[str, str]) -> list[dict[str, Any]]:
            return [{"role": "user", "content": {"type": "text", "text": "hi"}}]

    reg = PromptRegistry()
    reg.register(_NoArgs())
    listing = reg.mcp_prompt_list()
    assert listing == [{"name": "no-args", "description": "takes nothing"}]


def test_mcp_prompt_list_emits_arguments_when_declared() -> None:
    reg = PromptRegistry()
    reg.register(_StubPrompt())
    entry = reg.mcp_prompt_list()[0]
    assert entry["arguments"] == [
        {"name": "thing", "description": "the thing", "required": True},
        {"name": "optional_thing", "description": "optional"},
    ]


# -- render() validation --------------------------------------------------


def test_render_unknown_prompt_raises_method_not_found() -> None:
    """Matches how ``tools/call`` handles an unknown tool — clients branch
    on this code, so parity is load-bearing."""
    reg = PromptRegistry()
    with pytest.raises(RpcError) as exc_info:
        reg.render("ghost", {})
    assert exc_info.value.code == METHOD_NOT_FOUND


def test_render_missing_required_arg_raises_invalid_params() -> None:
    reg = PromptRegistry()
    reg.register(_StubPrompt())
    with pytest.raises(RpcError) as exc_info:
        reg.render("stub", {})
    assert exc_info.value.code == INVALID_PARAMS
    # The error `data` must name the missing arg(s) so the client can
    # point at the right form field.
    assert exc_info.value.data is not None
    assert exc_info.value.data["missing"] == ["thing"]


def test_render_empty_string_is_provided() -> None:
    """Explicit empty string satisfies the required-arg check.

    Prompts that want to reject empties handle it themselves; the
    registry only distinguishes "present" from "absent".
    """
    reg = PromptRegistry()
    reg.register(_StubPrompt())
    body = reg.render("stub", {"thing": ""})
    assert body["messages"][0]["content"]["text"] == "do the "


def test_render_coerces_non_string_scalars() -> None:
    """Some clients send ints / bools as arg values. Registry coerces
    so each prompt doesn't redo it.
    """
    reg = PromptRegistry()
    reg.register(_StubPrompt())
    body = reg.render("stub", {"thing": 42})
    assert body["messages"][0]["content"]["text"] == "do the 42"


def test_render_none_value_treated_as_absent() -> None:
    """An explicit ``null`` for a required arg must fail-closed the same
    way a missing key does. Otherwise a ``{"thing": null}`` payload would
    silently render "do the None" — the worst kind of bug."""
    reg = PromptRegistry()
    reg.register(_StubPrompt())
    with pytest.raises(RpcError) as exc_info:
        reg.render("stub", {"thing": None})
    assert exc_info.value.code == INVALID_PARAMS


def test_render_rejects_non_string_keys() -> None:
    reg = PromptRegistry()
    reg.register(_StubPrompt())
    with pytest.raises(RpcError) as exc_info:
        reg.render("stub", {42: "x"})  # type: ignore[dict-item]
    assert exc_info.value.code == INVALID_PARAMS


def test_render_body_includes_description_when_present() -> None:
    """``prompts/get`` body carries the prompt's description. MCP spec
    allows optional omission; we include so the client can re-render
    the prompt title next to its expanded text without a second
    ``prompts/list`` round-trip."""
    reg = PromptRegistry()
    reg.register(_StubPrompt())
    body = reg.render("stub", {"thing": "x"})
    assert body["description"] == "a stub prompt"


def test_render_body_omits_description_when_absent() -> None:
    class _NoDesc(Prompt):
        name = "nodesc"

        def render(self, arguments: dict[str, str]) -> list[dict[str, Any]]:
            return [{"role": "user", "content": {"type": "text", "text": "x"}}]

    reg = PromptRegistry()
    reg.register(_NoDesc())
    body = reg.render("nodesc", {})
    assert "description" not in body


# -- Builtin prompts: shape + argument mention ----------------------------


def test_design_review_mentions_both_tool_names() -> None:
    """The rendered message MUST name ``sch_erc`` and ``pcb_drc`` — they
    are the whole point of the prompt. If a future refactor renames
    tools, this fails loudly instead of quietly producing a prompt
    that instructs the LLM to call tools that don't exist.
    """
    reg = PromptRegistry()
    reg.register(DesignReviewPrompt())
    body = reg.render(
        "design-review",
        {"sch_path": "board.kicad_sch", "pcb_path": "board.kicad_pcb"},
    )
    text = body["messages"][0]["content"]["text"]
    assert "sch_erc" in text
    assert "pcb_drc" in text
    assert "board.kicad_sch" in text
    assert "board.kicad_pcb" in text


def test_design_review_requires_both_paths() -> None:
    reg = PromptRegistry()
    reg.register(DesignReviewPrompt())
    for arg in ({"sch_path": "a.kicad_sch"}, {"pcb_path": "a.kicad_pcb"}):
        with pytest.raises(RpcError) as exc_info:
            reg.render("design-review", arg)
        assert exc_info.value.code == INVALID_PARAMS


def test_manufacturing_handoff_mentions_all_export_tools() -> None:
    """All six tools (drc, gerbers, drill, pos, bom, step) must appear
    in the rendered message — they are the artifacts the fab expects."""
    reg = PromptRegistry()
    reg.register(ManufacturingHandoffPrompt())
    body = reg.render(
        "manufacturing-handoff",
        {"pcb_path": "b.kicad_pcb", "sch_path": "b.kicad_sch"},
    )
    text = body["messages"][0]["content"]["text"]
    for tool in (
        "pcb_drc",
        "pcb_export_gerbers",
        "pcb_export_drill",
        "pcb_export_pos",
        "sch_export_bom",
        "pcb_export_step",
    ):
        assert tool in text, f"manufacturing-handoff missing mention of {tool}"


def test_manufacturing_handoff_fab_profile_optional() -> None:
    """Omitting the optional ``fab_profile`` must still render. The
    prompt substitutes a generic-defaults line when absent — pin it
    so a regression that requires the arg fails here instead of in
    production."""
    reg = PromptRegistry()
    reg.register(ManufacturingHandoffPrompt())
    body = reg.render(
        "manufacturing-handoff",
        {"pcb_path": "b.kicad_pcb", "sch_path": "b.kicad_sch"},
    )
    text = body["messages"][0]["content"]["text"]
    assert "generic" in text.lower()


def test_manufacturing_handoff_fab_profile_when_provided() -> None:
    reg = PromptRegistry()
    reg.register(ManufacturingHandoffPrompt())
    body = reg.render(
        "manufacturing-handoff",
        {
            "pcb_path": "b.kicad_pcb",
            "sch_path": "b.kicad_sch",
            "fab_profile": "JLCPCB-2L",
        },
    )
    text = body["messages"][0]["content"]["text"]
    assert "JLCPCB-2L" in text
