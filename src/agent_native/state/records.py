"""Detached immutable semantic values, after the Registry validation boundary."""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType

from agent_native.schema import CollectionRef, EntityKey, Ref


def strict_equal(left, right):
    """Semantic equality is recursive and exact-type, including bool/int unions."""
    if type(left) is not type(right):
        return False
    if type(left) in (FrozenMap, dict, MappingProxyType):
        return (left.keys() == right.keys()
                and all(strict_equal(left[key], right[key]) for key in left))
    if type(left) in (FrozenList, tuple, list):
        return len(left) == len(right) and all(strict_equal(a, b) for a, b in zip(left, right))
    if type(left) is Record:
        return (left.key == right.key and left.partition == right.partition
                and strict_equal(left.values, right.values))
    return left == right


class FrozenMap(Mapping):
    """A copied map with recursively frozen values; never wraps a caller's dict."""
    __slots__ = ("_data",)
    __hash__ = None

    def __init__(self, value):
        if type(value) is not dict or any(type(key) is not str for key in value):
            raise TypeError("FrozenMap requires an exact string-keyed dict")
        frozen = deep_freeze(value)
        object.__setattr__(self, "_data", frozen._data)

    @classmethod
    def _from_frozen(cls, value):
        result = object.__new__(cls)
        object.__setattr__(result, "_data", MappingProxyType(value))
        return result

    def __setattr__(self, name, value):
        raise AttributeError("Semantic maps are immutable")

    def __delattr__(self, name):
        raise AttributeError("Semantic maps are immutable")

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __eq__(self, other):
        return strict_equal(self, other)

    def __repr__(self):
        return f"FrozenMap({dict(self)!r})"


class FrozenList(Sequence):
    """Immutable sequence tagged as an author list for validation/codec round trips."""
    __slots__ = ("_items",)
    __hash__ = None

    def __init__(self, items):
        if type(items) not in (list, tuple):
            raise TypeError("FrozenList requires a list or tuple")
        object.__setattr__(self, "_items", tuple(deep_freeze(item) for item in items))

    @classmethod
    def _from_frozen(cls, items):
        result = object.__new__(cls)
        object.__setattr__(result, "_items", items)
        return result

    def __setattr__(self, name, value):
        raise AttributeError("Semantic sequences are immutable")

    def __delattr__(self, name):
        raise AttributeError("Semantic sequences are immutable")

    def __getitem__(self, index):
        result = self._items[index]
        return FrozenList._from_frozen(result) if isinstance(index, slice) else result

    def __len__(self):
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def __eq__(self, other):
        return strict_equal(self, other)

    def __repr__(self):
        return f"FrozenList({self._items!r})"


def deep_freeze(value):
    """Reject unknown objects/cycles. Called on FINAL detached validated trees."""
    active = set()

    def visit(item):
        if item is None or type(item) in (str, bool, int):
            return item
        if type(item) is float:
            if not isfinite(item):
                raise ValueError("Non-finite semantic values are unsupported")
            return item
        if type(item) in (Ref, CollectionRef, FrozenMap, FrozenList):
            return item
        if type(item) not in (dict, list, tuple):
            raise TypeError(f"Unsupported semantic value: {type(item).__name__}")
        identity = id(item)
        if identity in active:
            raise ValueError("Cyclic semantic values are unsupported")
        active.add(identity)
        try:
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    raise TypeError("Semantic map keys must be exact strings")
                return FrozenMap._from_frozen({key: visit(child) for key, child in item.items()})
            children = tuple(visit(child) for child in item)
            return FrozenList._from_frozen(children) if type(item) is list else children
        finally:
            active.remove(identity)

    return visit(value)


def thaw(value):
    """Fresh author-native inputs: preserve list/tuple semantics for Registry codecs."""
    if type(value) is FrozenMap:
        return {key: thaw(item) for key, item in value.items()}
    if type(value) is FrozenList:
        return [thaw(item) for item in value]
    if type(value) is tuple:
        return tuple(thaw(item) for item in value)
    if value is None or type(value) in (str, bool, int, float, Ref, CollectionRef):
        return value
    raise TypeError("Expected an engine-owned immutable semantic tree")


@dataclass(frozen=True, slots=True, eq=False)
class Record:
    key: EntityKey
    partition: str
    values: FrozenMap
    version: int

    def __post_init__(self):
        if (type(self.key) is not EntityKey or type(self.partition) is not str
                or self.partition not in ("durable", "memory")):
            raise ValueError("Invalid record identity or partition")
        if type(self.values) is not FrozenMap:
            raise TypeError("Record values must be final frozen values")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("Record version must be a positive integer")

    def __getitem__(self, field):
        return self.values[field]

    def __eq__(self, other):
        return strict_equal(self, other)


def _detach_frozen(value):
    """Recovery boundary: copy and check FINAL algebra, without author callbacks.

    Unlike deep_freeze this accepts no native mutable dict/list/model output, and
    never trusts an existing FrozenMap/FrozenList backing merely because of its type.
    """
    active = set()

    def key_copy(key):
        if type(key) is not EntityKey:
            raise TypeError("Expected an exact EntityKey")
        return EntityKey(key.entity_type, key.entity_id, key.generation)

    def copy(item):
        if item is None or type(item) in (str, bool, int):
            return item
        if type(item) is float:
            if not isfinite(item):
                raise ValueError("Non-finite frozen value")
            return item
        if type(item) is Ref:
            return Ref(key_copy(item.key))
        if type(item) is CollectionRef:
            return CollectionRef(key_copy(item.owner), item.field)
        if type(item) not in (FrozenMap, FrozenList, tuple):
            raise TypeError(f"Unsupported accepted frozen value: {type(item).__name__}")
        identity = id(item)
        if identity in active:
            raise ValueError("Cyclic accepted frozen value")
        active.add(identity)
        try:
            if type(item) is FrozenMap:
                if type(item._data) is not MappingProxyType or any(type(key) is not str for key in item):
                    raise TypeError("Malformed FrozenMap backing or keys")
                return FrozenMap._from_frozen({key: copy(child) for key, child in item.items()})
            if type(item) is FrozenList and type(item._items) is not tuple:
                raise TypeError("Malformed FrozenList backing")
            children = tuple(copy(child) for child in item)
            return FrozenList._from_frozen(children) if type(item) is FrozenList else children
        finally:
            active.remove(identity)

    return copy(value)
