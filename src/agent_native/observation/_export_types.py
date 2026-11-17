"""Closed export values and logical allocation accounting.

These limits bound SDK-held data, not process RSS or arbitrary trusted Python.
No source payload or exception message is interpolated into public errors.
"""
from dataclasses import dataclass, fields
from collections.abc import Mapping
from threading import current_thread, main_thread
from time import monotonic_ns
import math


class ExportError(RuntimeError):
    def __init__(self, code="export_unavailable"):
        self.code = code
        super().__init__(code)


def _owner():
    if current_thread() is not main_thread():
        raise ExportError("export_owner_thread_required")


@dataclass(frozen=True, slots=True)
class ExportLimits:
    max_assets: int = 8
    max_retained_bytes: int = 128 * 1024 * 1024
    max_working_bytes: int = 128 * 1024 * 1024
    max_fragments: int = 128
    max_fragment_bytes: int = 4 * 1024 * 1024
    max_envelope_bytes: int = 32 * 1024 * 1024
    max_demands: int = 8
    max_inflight_jobs: int = 2
    max_wire_handles: int = 8
    ttl_ns: int = 10_000_000_000

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in (getattr(self,f.name) for f in fields(self))):
            raise ValueError("Use positive exact export limits")


@dataclass(frozen=True, slots=True)
class ExportMeasurements:
    retained_bytes: int
    working_bytes: int
    peak_retained_bytes: int
    peak_working_bytes: int
    captures: int
    encodes: int
    wires: int


class _Charge:
    __slots__ = ("budget", "kind", "size", "closed")
    def __init__(self, budget, kind, size):
        self.budget, self.kind, self.size, self.closed = budget, kind, size, False

    def resize(self, size):
        _owner()
        if self.closed or type(size) is not int or not 0 <= size <= self.size:
            raise ExportError("export_reservation_invalid")
        self.budget._used[self.kind] -= self.size - size
        self.size = size

    def close(self):
        _owner()
        if not self.closed:
            self.closed = True
            self.budget._used[self.kind] -= self.size


class _Budget:
    def __init__(self, limits, clock_ns=monotonic_ns):
        self.limits, self.clock = limits, clock_ns
        self._used = {"retained": 0, "working": 0}
        self._peak = dict(self._used)
        self._last = -1
        self.captures = self.encodes = self.wires = 0
        self.now()

    def now(self):
        _owner()
        value = self.clock()
        if type(value) is not int or value < self._last or value < 0:
            raise ValueError("Use a monotonic nonnegative integer clock")
        self._last = value
        return value

    def reserve(self, kind, size):
        _owner()
        if kind not in self._used or type(size) is not int or size < 0:
            raise ValueError("Use an exact bounded allocation request")
        limit = self.limits.max_retained_bytes if kind == "retained" else self.limits.max_working_bytes
        if self._used[kind] + size > limit:
            raise ExportError("export_quota_exceeded")
        charge = _Charge(self, kind, size)
        self._used[kind] += size
        self._peak[kind] = max(self._peak[kind], self._used[kind])
        return charge

    def measurements(self):
        _owner()
        return ExportMeasurements(self._used["retained"], self._used["working"],
            self._peak["retained"], self._peak["working"], self.captures, self.encodes, self.wires)


def _json_bound(value, *, max_nodes=65536, max_depth=32, projected=False):
    """Conservative canonical-JSON byte bound BEFORE serializer allocation."""
    nodes = 0
    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ExportError("export_quota_exceeded")
        if item is None or type(item) is bool:
            return 5
        if type(item) is str:
            # ensure_ascii emits up to six bytes per UTF-16 code unit. A
            # non-BMP Python code point needs TWO surrogate escapes; lone
            # surrogate code points need one. Count without allocating an
            # encoded copy before admission.
            return 2 + 6 * len(item) + 6 * sum(ord(char) > 0xFFFF for char in item)
        if type(item) is int:
            return 3 + (item.bit_length() * 30103 // 100000)
        if type(item) is float:
            if not math.isfinite(item):
                raise ExportError("export_invalid_value")
            return 32
        if projected:
            from agent_native.schema import Ref,CollectionRef
            if type(item)is Ref:
                return visit(item.key.as_dict(),depth+1)
            if type(item)is CollectionRef:
                return visit(item.as_dict(),depth+1)
        if isinstance(item, Mapping):
            if any(type(k) is not str for k in item):
                raise ExportError("export_invalid_value")
            return 2 + sum(visit(k, depth+1)+visit(v, depth+1)+2 for k,v in item.items())
        if type(item) in (tuple, list):
            return 2 + sum(visit(v,depth+1)+1 for v in item)
        from agent_native.state import FrozenList
        if type(item) is FrozenList:
            return 2 + sum(visit(v,depth+1)+1 for v in item)
        raise ExportError("export_invalid_value")
    return visit(value, 0)


def _proof_bound(traces):
    """Conservative detached-claim accounting; no private values are serialized."""
    from agent_native.schema import Ref,CollectionRef
    from agent_native.state import FrozenList
    def sampled(value,depth=0):
        if depth>32:
            raise ExportError("export_quota_exceeded")
        if type(value)is Ref:
            return 128+_json_bound(value.key.as_dict())
        if type(value)is CollectionRef:
            return 128+_json_bound(value.as_dict())
        if isinstance(value,Mapping):
            return 128+sum(sampled(k,depth+1)+sampled(v,depth+1) for k,v in value.items())
        if type(value)in(tuple,list,FrozenList):
            return 128+sum(sampled(v,depth+1) for v in value)
        return 32+_json_bound(value)
    seen=set();size=0
    for trace in traces:
        if id(trace)in seen:
            continue
        seen.add(id(trace))
        size+=1024+512*len(trace.accesses)+128*len(trace.read_set.events)
        size+=sampled(trace.sampled_value)
        size+=sum(512+sampled(s.value) for s in trace.samples)
    return size
