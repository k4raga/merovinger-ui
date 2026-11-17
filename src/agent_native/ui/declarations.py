"""Finite S1 Widgets declarations; these describe bindings, never domain values.

INPUT is the event value for an existing typed command argument. Commands and
their handlers are the ordinary Registry definitions used by agent delivery.
"""
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from collections.abc import Mapping

from agent_native.schema import Ref, CollectionRef
from agent_native.state.tracking import FieldRef


class _Input(Enum):
    VALUE = "input"


INPUT = _Input.VALUE


MAX_TEMPLATE_NODES = 4096
MAX_TEMPLATE_DEPTH = 32


def _template(value, *, detach):
    """One bounded algebra for detached arguments and exact resource leaves.

    Mappings (including record-shaped mappings) never become wire Ref authority.
    Only actual immutable Ref/CollectionRef leaves are collected; INPUT has none.
    """
    resources,active=set(),set()
    nodes=0
    def visit(item,depth):
        nonlocal nodes
        nodes+=1
        if nodes>MAX_TEMPLATE_NODES or depth>MAX_TEMPLATE_DEPTH:
            raise ValueError("UI action template limit")
        if type(item) in (Ref,CollectionRef):
            resources.add(item)
            return item
        if item is INPUT or type(item) in (str,int,float,bool,type(None)):
            return item
        if not isinstance(item,Mapping) and type(item) not in (tuple,list):
            raise TypeError("Use typed command values, not application callbacks")
        token=id(item)
        if token in active:
            raise ValueError("Cyclic UI action template")
        active.add(token)
        try:
            if isinstance(item,Mapping):
                result={} if detach else None
                for key,child in item.items():
                    if type(key) is not str:
                        raise TypeError("Command argument keys must be strings")
                    converted=visit(child,depth+1)
                    if detach:
                        result[key]=converted
                return MappingProxyType(result) if detach else item
            if detach:
                return tuple(visit(child,depth+1) for child in item)
            for child in item:
                visit(child,depth+1)
            return item
        finally:
            active.remove(token)
    normalized=visit(value,0)
    return normalized,frozenset(resources)


def _arguments(value):
    return _template(value,detach=True)[0]


def argument_resources(template):
    """Extract typed original identities from the exact frozen Action template."""
    return _template(template,detach=False)[1]


@dataclass(frozen=True, slots=True)
class Action:
    """Existing typed command and detached arguments.

    Nonempty create_types is a legacy exact-set assertion against the command
    profile, checked at mount. Empty omits the assertion. Neither grants rights;
    effects/Create/transfer come only from SAME registered command permissions.
    """
    command: str
    arguments: Mapping
    create_types: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.command or not isinstance(self.arguments, Mapping):
            raise ValueError("An action needs an existing command and arguments")
        object.__setattr__(self, "arguments", _arguments(self.arguments))
        object.__setattr__(self, "create_types", tuple(self.create_types))


@dataclass(frozen=True, slots=True)
class Control:
    path: str
    kind: str
    source: FieldRef | CollectionRef | Ref | None = None
    label: str = ""
    action: Action | None = None
    parent: str | None = None
    label_field: str = "title"
    minimum: int = 0
    maximum: int = 100
    height: int = 150
    fields: tuple[str, ...] = ("x", "y", "width", "height")
    visible: bool = True
    enabled: bool = True

    def __post_init__(self):
        if not self.path or self.kind not in ("text", "button", "editor", "list", "number", "toggle", "menu", "canvas"):
            raise ValueError("Unsupported managed control")
        if self.kind in ("text", "editor", "number", "toggle") and type(self.source) is not FieldRef:
            raise TypeError("Value controls require a direct C1 FieldRef")
        if self.kind in ("list", "canvas") and type(self.source) is not CollectionRef:
            raise TypeError("Collection controls require the original CollectionRef")
        if self.kind == "menu" and (self.source is not None or self.action is not None):
            raise ValueError("Menu is a managed disclosure group")
        if self.action is not None and type(self.action) is not Action:
            raise TypeError("Bind an existing typed Action, not a host callback")
        if self.kind == "button" and type(self.action) is not Action:
            raise ValueError("Domain controls require an existing typed command")
        if not 0 < self.height <= 2000 or self.minimum > self.maximum:
            raise ValueError("Invalid finite control bounds")


def text(path, source, **options):
    return Control(path, "text", source, **options)


def button(path, label, action, **options):
    return Control(path, "button", label=label, action=action, **options)


def editor(path, source, **options):
    return Control(path, "editor", source, **options)


def list_view(path, source, **options):
    return Control(path, "list", source, **options)


def number(path, source, action=None, **options):
    return Control(path, "number", source, action=action, **options)


def menu(path, label, **options):
    return Control(path, "menu", label=label, **options)


def canvas(path, source, **options):
    return Control(path, "canvas", source, **options)


def toggle(path, source, action=None, **options):
    """Canonical boolean field; optional typed Action receives INPUT on click.

    With no Action this is a disabled read-only boolean projection.
    """
    return Control(path, "toggle", source, action=action, **options)
