"""Declarative interaction shared by a managed surface and its agent clients."""
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
import re
from types import MappingProxyType

from pydantic import TypeAdapter

from agent_native.schema import CollectionRef, Ref
from agent_native.state.tracking import FieldRef


_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}\Z")
_MAX_DATA = 64 * 1024
_MAX_NODES = 256


def _name(value):
    if type(value) is not str or not _NAME.fullmatch(value):
        raise ValueError("Use a short stable interaction identifier")
    return value


def _json(value):
    """Detach one small closed JSON value. Never call a host object's encoder."""
    remaining = 4096

    def copy(item, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 16:
            raise ValueError("Interaction data exceeds its structural limit")
        if type(item) is dict:
            if any(type(key) is not str or len(key) > 256 for key in item):
                raise ValueError("Interaction object keys must be strings")
            return {key: copy(v, depth + 1) for key, v in item.items()}
        if type(item) in (tuple, list):
            return [copy(v, depth + 1) for v in item]
        if type(item) is str and len(item) <= _MAX_DATA:
            return item
        if item is None or type(item) is bool:
            return item
        if type(item) is int and -(2**53) < item < 2**53:
            return item
        if type(item) is float and math.isfinite(item):
            return item
        raise TypeError("Interaction data must be bounded JSON")

    result = copy(value, 0)
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > _MAX_DATA:
        raise ValueError("Interaction data exceeds its byte limit")
    return result


def _frozen(value):
    if type(value) is dict:
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_frozen(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if type(value) in (list, tuple):
        return [_thaw(item) for item in value]
    return value


class _Schema:
    def __init__(self, annotation):
        self.adapter = TypeAdapter(annotation)
        schema = _json(self.adapter.json_schema())
        root = schema.get("$defs", {}).get(schema.get("$ref", "").removeprefix("#/$defs/"), schema)
        if root.get("type") != "object" or root.get("additionalProperties") is not False:
            raise TypeError("Use a typed, closed object schema (extra='forbid')")
        self.schema = _frozen(schema)
        self.keys = frozenset(root.get("properties", ()))
        self.required = frozenset(root.get("required", ()))

    def parse(self, value):
        return self.adapter.validate_json(json.dumps(_json(value), ensure_ascii=False), strict=True)

    def data(self, value):
        return _json(self.adapter.dump_python(value, mode="json", warnings="error"))

    def public(self, editable):
        schema = _thaw(self.schema)
        defs = schema.get("$defs", {})
        reference = schema.get("$ref")
        if reference is not None:
            schema = dict(defs[reference.removeprefix("#/$defs/")])
            schema["$defs"] = defs
        schema["properties"] = {key: val for key, val in schema.get("properties", {}).items() if key in editable}
        schema["required"] = [key for key in schema.get("required", ()) if key in editable]
        return schema


@dataclass(frozen=True, slots=True)
class InteractionCollection:
    source: CollectionRef
    fields: tuple[str, ...]

    def __post_init__(self):
        if type(self.source) is not CollectionRef or type(self.fields) is not tuple or not self.fields:
            raise TypeError("Use a typed Owned collection and explicit exported fields")
        if "ref" in self.fields or len(set(self.fields)) != len(self.fields):
            raise ValueError("Duplicate collection field")
        for name in self.fields:
            _name(name)


@dataclass(frozen=True, slots=True)
class InteractionAction:
    """One typed reducer or mapper to an existing registered C2 command.

    A reducer returns the next JSON-shaped temporary state. A command mapper
    returns named arguments to the kernel; it cannot return a Patch or result.
    Both receive the typed input followed by immutable local state and sources.
    """
    name: str
    input_type: object
    handler: object
    command: str | None = None
    _input: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        _name(self.name)
        if not callable(self.handler):
            raise TypeError("An interaction needs a trusted synchronous handler")
        if self.command is not None and (type(self.command) is not str or not self.command):
            raise ValueError("Use the Registry's command identity")
        object.__setattr__(self, "_input", _Schema(self.input_type))


@dataclass(frozen=True, slots=True)
class InteractionBinding:
    """Attach an action to a real semantic input region.

    Fixed values are resolved from the original scene; dynamic input cannot
    overwrite them. Mouse drag supplies x/y absolute positions and dx/dy from
    drag start, selected by the optional axis keys. Editing supplies one value.
    """
    action: str
    fixed: Mapping = field(default_factory=dict)
    inputs: tuple[tuple[str, str], ...] = ()
    enabled: bool = True

    def __post_init__(self):
        _name(self.action)
        if not isinstance(self.fixed, Mapping) or type(self.inputs) is not tuple or type(self.enabled) is not bool:
            raise TypeError("Invalid interaction binding")
        fixed = _frozen(_json(_thaw(self.fixed)))
        target = set(fixed)
        for source, key in self.inputs:
            _name(source)
            _name(key)
            if key in target:
                raise ValueError("Duplicate or overridden interaction parameter")
            target.add(key)
        object.__setattr__(self, "fixed", fixed)


@dataclass(frozen=True, slots=True)
class InteractionNode:
    """SDK-rendered node. Custom components compose these into arbitrary scenes."""
    id: str
    kind: str
    rect: tuple[int, int, int, int]
    label: str
    value: str = ""
    activate: InteractionBinding | None = None
    edit: InteractionBinding | None = None
    drag_begin: InteractionBinding | None = None
    drag: InteractionBinding | None = None
    drag_end: InteractionBinding | None = None
    shortcut: str | None = None
    selected: bool = False
    outline: tuple[tuple[int, int], ...] = ()

    def __post_init__(self):
        _name(self.id)
        if self.kind not in ("label", "button", "field", "region"):
            raise ValueError("Unsupported rendering primitive")
        if (type(self.rect) is not tuple or len(self.rect) != 4
                or any(type(v) is not int for v in self.rect)):
            raise TypeError("Use integer logical geometry")
        x, y, width, height = self.rect
        if min(x, y) < 0 or min(width, height) <= 0 or max(x + width, y + height) > 2048:
            raise ValueError("Node is outside bounded logical geometry")
        if type(self.outline) is not tuple or self.outline and (
            self.kind != "region" or not 3 <= len(self.outline) <= 256
            or any(type(point) is not tuple or len(point) != 2
                   or any(type(axis) is not int for axis in point)
                   or not 0 <= point[0] <= width or not 0 <= point[1] <= height for point in self.outline)
        ):
            raise ValueError("Use a bounded local polygon for a region outline")
        if (type(self.label) is not str or not self.label.strip() or len(self.label) > 512
                or type(self.value) is not str or len(self.value) > 4096 or type(self.selected) is not bool):
            raise TypeError("Use bounded meaningful labels and text")
        for binding in (self.activate, self.edit, self.drag_begin, self.drag, self.drag_end):
            if binding is not None and type(binding) is not InteractionBinding:
                raise TypeError("Interactive regions require typed bindings")
        if self.kind == "label" and self.bindings():
            raise ValueError("A label cannot capture input")
        if self.kind == "button" and self.activate is None:
            raise ValueError("A button requires an activation")
        if (self.kind == "field") != (self.edit is not None):
            raise ValueError("Text entry requires a bound field")
        if self.kind == "field" and self.activate is not None:
            raise ValueError("A text field has no native activation gesture")
        if any(b is not None for b in (self.drag_begin, self.drag, self.drag_end)):
            if self.kind != "region" or self.drag is None:
                raise ValueError("Drag input requires a region and updated positions")
        if self.shortcut is not None:
            if (self.activate is None or type(self.shortcut) is not str
                    or not re.fullmatch(r"Escape|F(?:[1-9]|1[0-2])|Ctrl\+[A-Z0-9]", self.shortcut)):
                raise ValueError("Unbound or unsupported interaction shortcut")
        if (self.kind == "region" and self.activate is not None and self.activate.enabled
                and self.drag_begin is not None and self.drag_begin.enabled and self.shortcut is None):
            raise ValueError("A region with a press-to-drag gesture needs a shortcut for a separate activation")

    def bindings(self):
        return tuple((event, binding) for event, binding in (
            ("activate", self.activate), ("edit", self.edit), ("drag_begin", self.drag_begin),
            ("drag", self.drag), ("drag_end", self.drag_end),
        ) if binding is not None)


@dataclass(frozen=True, slots=True)
class InteractionDefinition:
    """Registered reusable component with one scene and one action vocabulary.

    The scene callback receives current typed read-only sources and local state
    represented by immutable JSON values. It returns detached InteractionNode
    objects, not QWidget instances or input callbacks.
    """
    state_type: object
    initial_state: Mapping
    sources: Mapping
    actions: tuple[InteractionAction, ...]
    scene: object
    size: tuple[int, int] = (640, 440)
    _state: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if not callable(self.scene) or not isinstance(self.sources, Mapping) or not isinstance(self.initial_state, Mapping):
            raise TypeError("Use a scene constructor and declared sources/state")
        if type(self.actions) is not tuple or len(self.actions) > 128 or any(
            type(a) is not InteractionAction for a in self.actions
        ) or len({a.name for a in self.actions}) != len(self.actions):
            raise ValueError("Use finite unique declared actions")
        if (type(self.size) is not tuple or len(self.size) != 2 or any(
            type(v) is not int or not 1 <= v <= 2048 for v in self.size
        )):
            raise ValueError("Invalid interaction surface size")
        if len(self.sources) > 64:
            raise ValueError("Too many declared interaction sources")
        for name, source in self.sources.items():
            _name(name)
            if type(source) not in (FieldRef, InteractionCollection):
                raise TypeError("Use a direct field or declared collection projection")
        schema = _Schema(self.state_type)
        object.__setattr__(self, "_state", schema)
        object.__setattr__(self, "initial_state", _frozen(schema.data(schema.parse(_thaw(self.initial_state)))))
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
