"""Managed conservative scans, not a second graph or range-index authority.

Empty queries retain the type/partition membership token. A range reads its
predicate/order field for every candidate, so moving a previously excluded row
into the range is detected. Insertions outside a range may also invalidate it;
unread fields on existing rows do not.
"""
from dataclasses import dataclass, field as dc_field
from inspect import iscoroutinefunction, isgeneratorfunction
from math import isfinite
from uuid import UUID

from agent_native.schema import CollectionRef, EntityKey, Ref


def _parameter(value):
    if value is None or type(value) in (str, bool, int):
        return (type(value).__name__, value)
    if type(value) is float and isfinite(value):
        return ("float", value)
    if type(value) is tuple:
        return ("tuple", tuple(_parameter(item) for item in value))
    if type(value) is Ref:
        return ("ref", _parameter(value.key))
    if type(value) is EntityKey:
        return ("key", value.entity_type, value.entity_id, value.generation)
    if type(value) is CollectionRef:
        return ("collection", _parameter(value.owner), value.field)
    raise TypeError("Query parameters require immutable scalar/identity/tuple values")


@dataclass(frozen=True, slots=True)
class QueryVersion:
    """Runtime cache scope plus monotonic input counter; never graph identity."""
    cache_incarnation: str
    counter: int

    def __post_init__(self):
        if type(self.cache_incarnation) is not str:
            raise TypeError("Query cache incarnation must be UUID hex")
        try:
            valid = UUID(hex=self.cache_incarnation).hex == self.cache_incarnation
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Query cache incarnation must be canonical UUID hex")
        if type(self.counter) is not int or self.counter < 1:
            raise ValueError("Query version counter must be a strict positive integer")

    def advanced(self):
        return QueryVersion(self.cache_incarnation, self.counter + 1)


@dataclass(frozen=True, slots=True)
class QueryKey:
    name: str
    parameters: tuple = dc_field(default=(), compare=False, hash=False)
    revision: int = 1
    _identity: tuple = dc_field(init=False, repr=False)

    def __post_init__(self):
        if type(self.name) is not str or not self.name or type(self.parameters) is not tuple:
            raise TypeError("QueryKey requires a stable name and immutable parameter tuple")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Query definition revision must be a strict positive integer")
        object.__setattr__(self, "_identity", _parameter(self.parameters))


@dataclass(frozen=True, slots=True)
class QueryResult:
    refs: tuple[Ref, ...]

    def __post_init__(self):
        if type(self.refs) is not tuple or any(type(ref) is not Ref for ref in self.refs):
            raise TypeError("QueryResult requires immutable Ref members")

    def __len__(self):
        return len(self.refs)

    def __iter__(self):
        return iter(self.refs)

    def __getitem__(self, index):
        return self.refs[index]


def where(view, entity_type, predicate, *, partition=None):
    """Trusted synchronous predicate receives a ManagedRecord."""
    if not callable(predicate) or iscoroutinefunction(predicate) or isgeneratorfunction(predicate):
        raise TypeError("A query predicate must be callable")
    selected = []
    for ref in view.entities(entity_type, partition):
        decision = predicate(view.record(ref))
        if type(decision) is not bool:
            raise TypeError("A query predicate must return an exact bool")
        if decision:
            selected.append(ref)
    return QueryResult(tuple(selected))


_UNBOUNDED = object()


def range_query(view, entity_type, field, *, lower=_UNBOUNDED, upper=_UNBOUNDED,
                include_lower=True, include_upper=False, partition=None, limit=None):
    """Sorted field range; all candidates are read before applying a limit."""
    if type(include_lower) is not bool or type(include_upper) is not bool:
        raise TypeError("Range boundary flags must be exact booleans")
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("Range limit must be a non-negative exact integer")
    selected = []
    for ref in view.entities(entity_type, partition):
        value = view.field(ref, field)
        inside_lower = lower is _UNBOUNDED or (value >= lower if include_lower else value > lower)
        inside_upper = upper is _UNBOUNDED or (value <= upper if include_upper else value < upper)
        if inside_lower and inside_upper:
            selected.append((value, ref.key.entity_id, ref.key.generation, ref))
    selected.sort(key=lambda row: row[:3])
    return QueryResult(tuple(row[3] for row in selected[:limit]))
