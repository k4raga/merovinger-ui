"""Immutable command ceilings declared on the same Registry command.

Profiles are declarations, not caller grants or dynamic resource authority.
C2 owns per-invoked-command enforcement and complete cross-registry checks.
"""
from dataclasses import dataclass

from .types import valid_type_id


def _permission_ids(values, label, *, types=False):
    if type(values) is not tuple or len(values) > 64:
        raise TypeError(f"{label} requires an exact tuple of at most 64 IDs")
    for value in values:
        if type(value) is not str or not value or len(value) > 256:
            raise ValueError(f"{label} IDs require nonempty exact UTF-8 strings within 256 bytes")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError as error:
            raise ValueError(f"{label} IDs require valid UTF-8") from error
        if size > 256:
            raise ValueError(f"{label} IDs exceed 256 UTF-8 bytes")
        if types:
            valid_type_id(value)
    if len(set(values)) != len(values):
        raise ValueError(f"Duplicate {label} IDs")
    return tuple(sorted(values))


@dataclass(frozen=True, slots=True)
class CommandPermissionProfile:
    """Static per-command ceilings; omitted/empty denies effects/Create/transfer.

    Each set has at most 64 unique exact IDs of 1..256 UTF-8 bytes and a
    canonical sorted tuple order. Creation types use full stable namespaced IDs.
    This value/fingerprint does not identify arbitrary validator/handler code.
    """
    effect_actions: tuple[str, ...] = ()
    create_types: tuple[str, ...] = ()
    allow_activity_transfer: bool = False

    def __post_init__(self):
        object.__setattr__(self, "effect_actions", _permission_ids(self.effect_actions, "effect_actions"))
        object.__setattr__(self, "create_types", _permission_ids(self.create_types, "create_types", types=True))
        if type(self.allow_activity_transfer) is not bool:
            raise TypeError("allow_activity_transfer requires an exact boolean")

    def _metadata(self):
        return {"effect_actions": self.effect_actions, "create_types": self.create_types,
                "allow_activity_transfer": self.allow_activity_transfer}
