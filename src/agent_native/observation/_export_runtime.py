"""Original C4 allocation lifetime; no bank/C1/SEND authority.

The constructor binds a real C4 runtime to the exact Coordinator object passed.
This is not independent C1 constructor provenance: future012 clock binding does
that join. Owner pins prove allocation lifetime/object identity only. The actual
023 caller must establish thread return/no leases/buffer disposal/charge.close
BEFORE releasing its pin; this helper does not invent bank quiescence.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields
from uuid import uuid4
from weakref import WeakKeyDictionary

from ._export_types import ExportError, ExportLimits, _Budget, _owner
from .assets import _AssetStore
from .envelope import _FragmentStore, _WireStore


_SEAL = object()
_ORIGINALS = {}             # Strong while live/draining: no GC release of data.
_BY_COORDINATOR = {}
_CONSTRUCTING = set()
_RETIRED = WeakKeyDictionary()  # Primitive-only tombstones AFTER actual drain.


def _require(condition, code="export_allocation_origin_invalid"):
    if not condition:
        raise ExportError(code)


def _coordinator(coordinator):
    from agent_native.kernel.coordinator import Coordinator
    _require(type(coordinator) is Coordinator)
    try:
        _require(type(coordinator._issuer) is object)
    except AttributeError:
        raise ExportError("export_allocation_origin_invalid") from None


def _idle(coordinator):
    from agent_native.kernel.coordinator import Phase
    _require(coordinator.phase is Phase.IDLE, "export_allocation_guarded")


def _limits_signature(limits):
    _require(type(limits) is ExportLimits)
    try:
        values = tuple(getattr(limits, field.name) for field in fields(ExportLimits))
    except AttributeError:
        raise ExportError("export_allocation_origin_invalid") from None
    _require(all(type(value) is int and 0 < value <= 2**63 - 1 for value in values))
    return values


class _Opaque:
    __slots__ = ()
    def __new__(cls, *args, **kwargs):
        raise TypeError("Allocation owners are issued, never constructed")


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class RuntimeAllocationOwner(_Opaque):
    handle: str
    runtime_epoch: str
    _issuer: object


@dataclass(frozen=True, slots=True)
class _AllocationIdentity:
    """Read-only DATA; not a bank constructor/physical/C1 or export proof."""
    runtime: object
    coordinator: object
    budget: object
    clock: object
    limits: tuple
    runtime_epoch: str


class _Original:
    def __init__(self, runtime, limits_signature):
        self.runtime, self.coordinator = runtime, runtime.coordinator
        self.coordinator_issuer = self.coordinator._issuer
        self.budget, self.clock = runtime.budget, runtime.budget.clock
        self.limits_object, self.limits = runtime.budget.limits, limits_signature
        self.owner_limit = min(runtime.budget.limits.max_demands, runtime.budget.limits.max_wire_handles)
        self.used = runtime.budget._used
        self.assets, self.fragments, self.wires = runtime.assets, runtime.fragments, runtime.wires
        self.executor, self.sessions, self.jobs = runtime.executor, runtime.sessions, runtime.jobs
        self.epoch, self.issuer, self.draining = uuid4().hex, object(), False
        self.owners, self.by_owner = {}, {}


def _closed_signature(runtime):
    return tuple(id(getattr(runtime, key)) for key in
        ("coordinator", "budget", "assets", "fragments", "wires", "executor", "sessions", "jobs"))


def _retired(runtime):
    if type(runtime) is not _ExportRuntime:
        return False
    signature = _RETIRED.get(runtime)
    if signature is None:
        return False
    try:
        return signature == _closed_signature(runtime)
    except AttributeError:
        return False


def _original(runtime):
    _owner()
    _require(type(runtime) is _ExportRuntime)
    original = _ORIGINALS.get(id(runtime))
    _require(original is not None and original.runtime is runtime)
    try:
        return _original_fields(runtime, original)
    except AttributeError:
        raise ExportError("export_allocation_origin_invalid") from None


def _original_fields(runtime, original):
    c = original.coordinator
    _require(runtime.coordinator is c and _BY_COORDINATOR.get(id(c)) is original
        and getattr(c, "_owned_export_runtime", None) is runtime
        and c._issuer is original.coordinator_issuer)
    _require(type(runtime.budget) is _Budget and runtime.budget is original.budget
        and runtime.budget.clock is original.clock and runtime.budget.limits is original.limits_object)
    _require(_limits_signature(runtime.budget.limits) == original.limits)
    _require(runtime.assets is original.assets and runtime.fragments is original.fragments
        and runtime.wires is original.wires and runtime.executor is original.executor
        and type(runtime.sessions) is dict and runtime.sessions is original.sessions
        and type(runtime.jobs) is dict and runtime.jobs is original.jobs
        and type(runtime.preparing) is int and runtime.preparing >= 0
        and runtime.stopping is original.draining)
    used = runtime.budget._used
    _require(type(used) is dict and used is original.used
        and len(used) == 2 and all(type(key) is str for key in used)
        and set(used) == {"retained", "working"}
        and all(type(value) is int and 0 <= value <= 2**63 - 1 for value in used.values()))
    return original


def _identity(original):
    return _AllocationIdentity(original.runtime, original.coordinator, original.budget,
        original.clock, original.limits, original.epoch)


def _allocation_identity(coordinator, budget, clock):
    """Validate actual singleton allocation; matching caller DATA is insufficient.

    Never invoke the clock or an owner callback. This read-only check can be
    repeated by a trusted acceptance verifier; it does not consume a pin.
    """
    _owner(); _coordinator(coordinator)
    original = _BY_COORDINATOR.get(id(coordinator))
    _require(original is not None and original.coordinator is coordinator)
    original = _original(original.runtime)
    _require(not original.draining, "export_allocation_draining")
    _require(budget is original.budget and clock is original.clock)
    return _identity(original)


def _owner_record(original, pin):
    _require(type(pin) is RuntimeAllocationOwner, "export_allocation_owner_invalid")
    record = original.owners.get(id(pin))
    _require(record is not None and record[0] is pin, "export_allocation_owner_invalid")
    try:
        # Shape before equality: no subclass/custom __eq__ callbacks.
        _require(type(pin.handle) is str and type(pin.runtime_epoch) is str
            and pin._issuer is original.issuer
            and pin.handle == record[1] and pin.runtime_epoch == original.epoch,
            "export_allocation_owner_invalid")
    except AttributeError:
        raise ExportError("export_allocation_owner_invalid") from None
    return record


class _ExportRuntime:
    def __init__(self, coordinator, limits, clock, *, _seal=None):
        _require(_seal is _SEAL)
        self.coordinator, self.budget = coordinator, _Budget(limits, clock)
        self.assets = _AssetStore(self.budget, self.authorize_metadata)
        self.fragments = _FragmentStore(self.budget)
        self.wires = _WireStore(self.budget, self.assets, self.authorize_metadata,
                                other_handles=self._model_handle_count)
        self.sessions, self.jobs = {}, {}
        self.executor = ThreadPoolExecutor(max_workers=limits.max_inflight_jobs, thread_name_prefix="owned-export")
        self.stopping, self.preparing = False, 0

    def _acquire_worker_owner(self, bank):
        """Pin a trusted owner OBJECT before charge/buffers, not a bank proof.

        The identity is retained strongly, with no methods/metadata inspected.
        Standalone object-owner tests cannot claim physical-bank readiness.
        """
        original = _original(self); _idle(original.coordinator)
        _require(not original.draining, "export_allocation_draining")
        _require(bank is not None, "export_allocation_owner_invalid")
        previous = original.by_owner.get(id(bank))
        if previous is not None:
            record = _owner_record(original, previous)
            _require(record[2] is bank, "export_allocation_owner_invalid")
            return previous
        _require(len(original.owners) < original.owner_limit, "export_allocation_owner_capacity")
        pin = object.__new__(RuntimeAllocationOwner)
        handle = uuid4().hex
        for key, value in (("handle", handle), ("runtime_epoch", original.epoch), ("_issuer", original.issuer)):
            object.__setattr__(pin, key, value)
        # Original primitive pin and strong owner exist independently of token fields.
        try:
            original.owners[id(pin)] = (pin, handle, bank)
            original.by_owner[id(bank)] = pin
        except BaseException:
            original.owners.pop(id(pin), None)
            original.by_owner.pop(id(bank), None)
            raise
        return pin

    def _validate_worker_owner(self, pin, bank):
        original = _original(self)
        _require(not original.draining, "export_allocation_draining")
        _require(_owner_record(original, pin)[2] is bank, "export_allocation_owner_invalid")
        return _identity(original)

    def _release_worker_owner(self, pin):
        """One-shot release; actual023 cleanup ordering is a separate contract.

        Does not close any charge or assume a caller status means quiescence.
        Remaining budget charges independently prevent singleton detach.
        """
        original = _original(self); _idle(original.coordinator)
        record = _owner_record(original, pin)
        del original.by_owner[id(record[2])]
        del original.owners[id(pin)]
        self._release_if_drained()

    def pump(self):
        _owner()
        if _retired(self):
            return
        _original(self)
        for token, job in tuple(self.jobs.items()):
            if job.future.done():
                self.jobs.pop(token, None)
                job.broker._finish_job(job)
        self.sweep()
        self._release_if_drained()

    def sweep(self):
        _owner()
        if _retired(self):
            return
        _original(self)
        self.wires.sweep(); self.assets.sweep(); self.fragments.sweep()

    def authorize_metadata(self, scope, traces):
        _original(self)
        session = next((s for s in self.sessions.values() if s._scope == scope), None)
        if session is None:
            raise ExportError()
        from agent_native.kernel.coordinator import Phase
        if self.coordinator.phase == Phase.IDLE:
            with self.coordinator.capture("export-metadata") as cut:
                session._traces(cut, traces)
        elif self.coordinator.phase == Phase.CAPTURING:
            with self.coordinator.read() as cut:
                session._traces(cut, traces)
        else:
            raise ExportError()

    def _model_handle_count(self):
        # Payloads remain attached to their broker until explicit close;
        # broker.close releases them before removing the session.
        return sum(len(session._model_payloads) for session in self.sessions.values())

    def _release_if_drained(self):
        _owner()
        if _retired(self):
            return
        original = _original(self)
        if self.sessions or self.jobs or self.preparing or original.owners:
            return
        _idle(original.coordinator)
        if not original.draining:
            original.draining = self.stopping = True
        # Fixed trusted methods; never caller-supplied cleanup callbacks.
        # Retry a partially failed close rather than publishing false drain.
        _WireStore.close(original.wires)
        _FragmentStore.close(original.fragments)
        _AssetStore.close(original.assets)
        ThreadPoolExecutor.shutdown(original.executor, wait=False, cancel_futures=False)
        # Real outstanding allocation charge remains a separate last-owner gate.
        # An early/mistaken pin release must not permit a fresh independent budget.
        if any(original.used.values()):
            return
        _RETIRED[self] = _closed_signature(self)
        del original.coordinator._owned_export_runtime
        del _BY_COORDINATOR[id(original.coordinator)]
        del _ORIGINALS[id(self)]


def _runtime(coordinator, limits, clock):
    _owner(); _coordinator(coordinator)
    signature = _limits_signature(limits)
    original = _BY_COORDINATOR.get(id(coordinator))
    if original is not None:
        runtime = original.runtime
        _original(runtime)
        _require(not original.draining and signature == original.limits and clock is original.clock,
                 "export_runtime_already_owned")
        return runtime
    _require(getattr(coordinator, "_owned_export_runtime", None) is None, "export_runtime_already_owned")
    _require(id(coordinator) not in _CONSTRUCTING, "export_runtime_constructing")
    _CONSTRUCTING.add(id(coordinator))
    runtime = None
    try:
        runtime = _ExportRuntime(coordinator, limits, clock, _seal=_SEAL)
        _require(getattr(coordinator, "_owned_export_runtime", None) is None,
                 "export_runtime_already_owned")
        original = _Original(runtime, signature)
        coordinator._owned_export_runtime = runtime
        _ORIGINALS[id(runtime)] = _BY_COORDINATOR[id(coordinator)] = original
        return runtime
    except BaseException:
        if runtime is not None:
            if getattr(coordinator, "_owned_export_runtime", None) is runtime:
                del coordinator._owned_export_runtime
            _ORIGINALS.pop(id(runtime), None)
            if _BY_COORDINATOR.get(id(coordinator)) is original:
                _BY_COORDINATOR.pop(id(coordinator), None)
            ThreadPoolExecutor.shutdown(runtime.executor, wait=False, cancel_futures=False)
        raise
    finally:
        _CONSTRUCTING.remove(id(coordinator))
