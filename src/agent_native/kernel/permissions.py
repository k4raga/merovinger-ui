"""Closed host policy composition over SAME registered command profiles.

Profiles declare ceilings, never principal/resource rights. Flat Grant.actions
requires globally disjoint command/effect IDs and aggregate host-limit closure.
"""
from dataclasses import dataclass
from inspect import isasyncgenfunction, iscoroutinefunction, isgeneratorfunction
from types import MappingProxyType
from typing import Literal

from agent_native.schema import CommandPermissionProfile, Ref, CollectionRef


class PermissionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _ids(items, *, maximum=1024):
    if type(items) is not tuple or len(items) > maximum:
        raise TypeError("Permission IDs require a bounded exact tuple")
    for item in items:
        if type(item) is not str or not item or len(item.encode("utf-8")) > 256:
            raise TypeError("Permission IDs require nonempty UTF-8 strings <=256 bytes")
    if len(set(items)) != len(items):
        raise ValueError("Duplicate permission ID")
    return tuple(sorted(items))


@dataclass(frozen=True, slots=True)
class CommandPermissionRequest:
    principal: str
    channel: Literal["agent", "ui"]
    task: Ref
    command: str
    resources: tuple[Ref | CollectionRef, ...]


@dataclass(frozen=True, slots=True)
class CommandPermissionLimit:
    allowed: bool
    effect_actions: tuple[str, ...] = ()
    create_types: tuple[str, ...] = ()
    allow_activity_transfer: bool = False

    def __post_init__(self):
        if type(self.allowed) is not bool or type(self.allow_activity_transfer) is not bool:
            raise TypeError("Host permission decisions require exact booleans")
        object.__setattr__(self, "effect_actions", _ids(self.effect_actions))
        object.__setattr__(self, "create_types", _ids(self.create_types))


@dataclass(frozen=True, slots=True)
class ResolvedCommandPermissions:
    """Detached exact issuance expectation; no executable capability."""
    commands: tuple[str, ...]
    actions: tuple[str, ...]
    create_types: tuple[str, ...]
    allow_activity_transfer: bool
    resources: tuple[Ref | CollectionRef, ...]


def catalog(registries, private_types):
    """Whole-catalog bootstrap gate; no callbacks and no mutable author catalog."""
    profiles, entities = {}, set()
    for registry in registries:
        entities.update(d.definition_id for d in registry.descriptors(kind="entity"))
        for descriptor in registry.descriptors(kind="command"):
            if descriptor.definition_id in profiles:
                raise PermissionError("duplicate_command_schema")
            profile = descriptor.command_permissions
            if type(profile) is not CommandPermissionProfile:
                raise PermissionError("invalid_command_permission_profile")
            profiles[descriptor.definition_id] = profile
    commands = frozenset(profiles)
    effects = {action for profile in profiles.values() for action in profile.effect_actions}
    if commands & effects:
        raise PermissionError("command_effect_identity_collision")
    for profile in profiles.values():
        if any(typ not in entities or typ in private_types for typ in profile.create_types):
            raise PermissionError("invalid_profile_creation_type")
    return MappingProxyType(profiles)


def policy_callback(callback):
    if callback is not None and (not callable(callback) or iscoroutinefunction(callback)
            or isgeneratorfunction(callback) or isasyncgenfunction(callback)):
        raise TypeError("Use a trusted synchronous bootstrap command policy")
    return callback


def resolve(profiles, callback, *, principal, channel, task, commands, resources):
    commands = _ids(commands)
    if not commands or channel not in ("agent", "ui"):
        raise PermissionError("command_policy_denied")
    if type(resources) is not tuple or any(type(item) not in (Ref, CollectionRef) for item in resources):
        raise TypeError("Use an exact tuple of typed resource identities")
    if callback is None:
        raise PermissionError("command_policy_denied")
    limits = {}
    from ._command_values import sync
    for command in commands:
        if command not in profiles:
            raise PermissionError("unknown_command")
        limit = sync(callback(CommandPermissionRequest(principal, channel, task, command, resources)))
        if type(limit) is not CommandPermissionLimit or not limit.allowed:
            raise PermissionError("command_policy_denied")
        limits[command] = limit
    effects, creates = set(), set()
    transfer = False
    for command in commands:
        profile, limit = profiles[command], limits[command]
        effects.update(set(profile.effect_actions) & set(limit.effect_actions))
        creates.update(set(profile.create_types) & set(limit.create_types))
        transfer |= profile.allow_activity_transfer and limit.allow_activity_transfer
    for command in commands:
        profile, limit = profiles[command], limits[command]
        if (not effects.intersection(profile.effect_actions).issubset(limit.effect_actions)
                or not creates.intersection(profile.create_types).issubset(limit.create_types)
                or transfer and profile.allow_activity_transfer and not limit.allow_activity_transfer):
            raise PermissionError("aggregate_command_policy_conflict")
    actions = set(commands) | effects
    if actions.intersection(profiles) != set(commands):
        raise PermissionError("command_effect_identity_collision")
    return ResolvedCommandPermissions(commands, tuple(sorted(actions)), tuple(sorted(creates)),
                                      transfer, resources)
