"""Detached descriptors shared by persistence, UI and tool projection."""
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Any
import json

from .permissions import CommandPermissionProfile


def freeze(value: Any) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and isfinite(value):
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("Metadata keys must be strings")
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if type(value) in (list, tuple):
        return tuple(freeze(item) for item in value)
    raise TypeError(f"Unsupported metadata value: {type(value).__name__}")


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if type(value) is tuple:
        return [thaw(item) for item in value]
    return value


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RelationDescriptor:
    field: str
    target_type: str
    kind: str
    many: bool
    nullable: bool


@dataclass(frozen=True, slots=True)
class FieldDescriptor:
    name: str
    required: bool
    metadata: Mapping
    relation: RelationDescriptor | None = None


@dataclass(frozen=True, slots=True)
class DefinitionDescriptor:
    definition_id: str
    kind: str
    revision: int
    fingerprint: str
    fields: tuple[FieldDescriptor, ...]
    metadata: Mapping
    json_schema: Mapping

    @property
    def command_permissions(self) -> CommandPermissionProfile | None:
        """Typed projection of the SAME canonical command metadata, never a grant."""
        if self.kind != "command":
            return None
        data = self.metadata["command_permissions"]
        return CommandPermissionProfile(data["effect_actions"], data["create_types"],
                                        data["allow_activity_transfer"])


@dataclass(frozen=True, slots=True)
class UiBindingDescriptor:
    definition_id: str
    kind: str
    fields: tuple[FieldDescriptor, ...]
    metadata: Mapping
