"""Canonical semantic UI definitions shared by headless context and future native UI.

Install once in the existing SYS Registry BEFORE constructing StateStore/Kernel.
Only source identities and binding paths are stored here; domain values remain
in their original records. Draft text/preedit are canonical semantic UI values.
"""
from dataclasses import dataclass
from typing import Literal, Annotated
from pydantic import Field

from agent_native.schema import Owned, Ref
from agent_native.kernel.system_definitions import SystemDefinitions, UiSystemDefinitions


@dataclass(frozen=True, slots=True)
class ContextDefinitions:
    Binding: type
    UiState: type
    EngineView: type
    Selection: type
    Draft: type
    ContextFocus: type


def install_context_definitions(system: SystemDefinitions) -> ContextDefinitions:
    """Bootstrap-only declaration in SYS, automatically private to CommandKernel.

    The host must create these entities in the memory partition. ContextIndex
    rejects durable UI records; accepted restore does not invent missing UI.
    Duplicate installation is deliberately a registration error.
    """
    if type(system) not in (SystemDefinitions, UiSystemDefinitions):
        raise TypeError("Use the common SystemDefinitions")
    registry = system.registry
    ResourceIdentity, TaskSession = system.ResourceIdentity, system.TaskSession

    @registry.entity("context_binding")
    class Binding:
        path: str
        role: Literal["value", "collection", "command", "menu"]
        source: ResourceIdentity | None = None
        field: str | None = None
        command_id: str | None = None
        parent: ResourceIdentity | None = None
        label: str = ""
        visible: bool = True
        enabled: bool = True

    @registry.entity("ui_state")
    class UiState:
        binding: Ref[Binding]
        opened: bool = False
        scroll_y: Annotated[int, Field(ge=0)] = 0

    @registry.entity("engine_view")
    class EngineView:
        principal: str
        bindings: Owned[Binding]
        state: Literal["open", "closed"] = "open"
        states: Owned[UiState] = ()

    @registry.entity("selection")
    class Selection:
        binding: Ref[Binding]
        resources: tuple[ResourceIdentity, ...] = ()

    @registry.entity("draft")
    class Draft:
        """Canonical editor state; native widgets are a projection.

        cursor/anchor and replacement_start are absolute UTF-16 offsets in
        committed text. preedit cursor/anchor are offsets in preedit. Qt's
        relative replacementStart must be normalized by the native adapter.
        focus_revision rejects events from a superseded composition owner.
        """
        binding: Ref[Binding]
        text: str = ""
        preedit: str = ""
        cursor_utf16: Annotated[int, Field(ge=0)] = 0
        anchor_utf16: Annotated[int, Field(ge=0)] = 0
        preedit_anchor_utf16: Annotated[int, Field(ge=0)] = 0
        preedit_cursor_utf16: Annotated[int, Field(ge=0)] = 0
        replacement_start_utf16: Annotated[int, Field(ge=0)] = 0
        replacement_length_utf16: Annotated[int, Field(ge=0)] = 0
        composing: bool = False
        focus_revision: Annotated[int, Field(ge=0)] = 0

    @registry.entity("context_focus")
    class ContextFocus:
        principal: str
        view: Ref[EngineView] | None = None
        selection: Ref[Selection] | None = None
        draft: Ref[Draft] | None = None
        task: Ref[TaskSession] | None = None

    return ContextDefinitions(Binding, UiState, EngineView, Selection, Draft, ContextFocus)
