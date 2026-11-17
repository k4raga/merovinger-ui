"""Main-thread publication boundary for the one C1 graph and derived cache.

Trusted host callbacks are synchronous. This is not command admission, a Python
sandbox, a Qt event pump, a SQL engine or an effect dispatcher. Worker outcomes
only enter a reserved mailbox; the host schedules pump() on the process main
thread. No subscriber observes the interval between graph and cache publication.
"""
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from inspect import isawaitable, iscoroutine, iscoroutinefunction, isgenerator, isgeneratorfunction, isasyncgen, isasyncgenfunction
from threading import Lock, current_thread, main_thread
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4
from weakref import WeakValueDictionary, ref as weakref_ref

from agent_native.schema import EntityKey, Ref
from agent_native.state import Patch, ReadView, Record, SnapshotData, StageRejected, StagedReadView, StateStore, WriterOwnershipError
from agent_native.state.records import deep_freeze, strict_equal
from agent_native.state.derived import (
    DerivedCache, DerivedDeltaRejected, DerivedEntry, DerivedQueryFailed, QueryCompletion, QueryDefinition, QueryPending, QueryWork, RecomputeBudget,
)
from agent_native.state.tracking import ReadSet, ReadTracker


class Phase(StrEnum):
    IDLE = "idle"
    PREPARING = "preparing"
    STAGING = "staging"
    AWAITING_DB = "awaiting_db"
    PUBLISHING = "publishing"
    NOTIFYING = "notifying"
    CAPTURING = "capturing"
    RECONCILING = "reconciling"


class BusyPolicy(StrEnum):
    QUEUE = "queue"
    REJECT = "reject"


class Status(StrEnum):
    COMMITTED = "committed"
    QUEUED = "queued"
    BUSY = "busy"
    AWAITING_DB = "awaiting_db"
    FAILED = "failed"
    RECONCILING = "reconciling"


class SubmitDisposition(StrEnum):
    ACCEPTED = "accepted"
    NOT_SUBMITTED = "not_submitted"


class DBOutcome(StrEnum):
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    UNKNOWN = "unknown"


class MailboxDisposition(StrEnum):
    STORED = "stored"
    DUPLICATE = "duplicate"
    STALE = "stale"
    RECONCILE_REQUIRED = "reconcile_required"


class CoordinatorBusy(RuntimeError):
    pass


class CoordinatorUnavailable(RuntimeError):
    pass


def _main_thread():
    if current_thread() is not main_thread():
        raise WriterOwnershipError("Coordinator semantic operations require the process main thread")


def _text(value, name):
    if type(value) is not str or not value:
        raise TypeError(f"{name} must be a non-empty exact string")


@dataclass(frozen=True, slots=True)
class CutIdentity:
    epoch: str
    sequence: int
    schema_manifest: tuple
    policy_revision: int
    index_revision: int

    def __post_init__(self):
        _text(self.epoch, "epoch")
        if type(self.schema_manifest) is not tuple:
            raise TypeError("schema_manifest must be an immutable tuple")
        object.__setattr__(self, "schema_manifest", deep_freeze(self.schema_manifest))
        for name in ("sequence", "policy_revision", "index_revision"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise TypeError("Cut revisions must be exact non-negative integers")

    @classmethod
    def read(cls, reader):
        return cls(reader.epoch, reader.sequence, reader.schema_manifest,
                   reader.policy_revision, reader.index_revision)


def _snapshot_copy(snapshot):
    """Detach the typed persistence envelope, including map backing storage.

    The input is a trusted StateStore export, not model JSON. Values have passed
    Registry validation/final freeze; we still reject malformed keys/leaves and
    copy every map before crossing the worker boundary.
    """
    if type(snapshot) is not SnapshotData:
        raise TypeError("Durable submission requires SnapshotData")
    identity = CutIdentity.read(snapshot)
    maps = {}
    for name in ("records", "generations", "generation_partitions", "existence_versions",
                 "field_versions", "collection_versions", "membership_versions", "collection_sources"):
        source = getattr(snapshot, name)
        if not isinstance(source, Mapping):
            raise TypeError("Snapshot maps must be mappings")
        copied = {}
        for key, value in source.items():
            if name in ("records", "existence_versions"):
                valid_key = type(key) is EntityKey
            elif name in ("field_versions", "collection_versions"):
                valid_key = (type(key) is tuple and len(key) == 2 and type(key[0]) is EntityKey
                             and type(key[1]) is str)
            else:
                valid_key = (type(key) is tuple and len(key) == 2 and type(key[0]) is str
                             and (type(key[1]) is str or (name == "membership_versions" and key[1] is None)))
            if not valid_key:
                raise TypeError("Malformed typed SnapshotData key")
            if name == "records":
                if type(value) is not Record or value.key != key:
                    raise TypeError("Snapshot records require immutable matching Records")
            elif name in ("generation_partitions", "collection_sources"):
                if type(value) is not str:
                    raise TypeError("Snapshot metadata requires exact strings")
            elif type(value) is not int or value < 0:
                raise TypeError("Snapshot versions require exact non-negative integers")
            copied[key] = value
        maps[name] = MappingProxyType(copied)
    return replace(snapshot, **maps, schema_manifest=identity.schema_manifest)


@dataclass(frozen=True, slots=True)
class DurableSubmission:
    transaction_id: str
    operation_id: str
    base: CutIdentity
    target: CutIdentity
    snapshot: SnapshotData
    kernel_bundle: object = None

    def __post_init__(self):
        _text(self.transaction_id, "transaction_id")
        _text(self.operation_id, "operation_id")
        if type(self.base) is not CutIdentity or type(self.target) is not CutIdentity:
            raise TypeError("Typed base and target identities required")
        snapshot = _snapshot_copy(self.snapshot)
        if CutIdentity.read(snapshot) != self.target:
            raise ValueError("Submission snapshot does not match target")
        if (self.target.epoch != self.base.epoch or self.target.schema_manifest != self.base.schema_manifest
                or self.target.sequence != self.base.sequence + 1):
            raise ValueError("Submission must be a successor of the base cut")
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(self, "kernel_bundle", deep_freeze(self.kernel_bundle))


@dataclass(frozen=True, slots=True)
class DurableOutcome:
    transaction_id: str
    target: CutIdentity
    outcome: DBOutcome
    detail: str | None = None

    def __post_init__(self):
        _text(self.transaction_id, "transaction_id")
        if type(self.target) is not CutIdentity or type(self.outcome) is not DBOutcome:
            raise TypeError("Typed target and DBOutcome required")
        if self.detail is not None and type(self.detail) is not str:
            raise TypeError("Outcome detail must be a string or None")


class DurableSubmitPort(Protocol):
    def submit(self, submission: DurableSubmission) -> SubmitDisposition:
        """Nonblocking enqueue only, no DB/network I/O or await on the host.

        NOT_SUBMITTED guarantees that no DB I/O began. Any exception, timeout or
        other return is ambiguous and leaves the semantic write barrier active.
        """
        ...


def _sync_callable(value, name):
    if (not callable(value) or iscoroutinefunction(value) or isgeneratorfunction(value)
            or isasyncgenfunction(value)):
        raise TypeError(f"{name} requires a trusted synchronous callable")


@dataclass(frozen=True, slots=True)
class RejectedDecision:
    """C2-owned stable rejection code; no semantic mutation or durable intent."""
    code: str
    detail: str | None = None

    def __post_init__(self):
        _text(self.code, "rejection code")
        if self.detail is not None and type(self.detail) is not str:
            raise TypeError("Rejection detail must be an exact string or None")


@dataclass(frozen=True, slots=True)
class AcceptedDecision:
    """Final accepted kernel bundle; never an additional Patch or stage writer."""
    kernel_bundle: object = None

    def __post_init__(self):
        object.__setattr__(self, "kernel_bundle", deep_freeze(self.kernel_bundle))


@dataclass(frozen=True, slots=True)
class StagedAcceptance:
    """Engine-only verifier, not an author/provider post-stage callback API.

    verify(expectation, StagedInput) returns an exact AcceptedDecision or
    RejectedDecision synchronously. Expectation is detached frozen algebra.
    C2 owns footprint/approval/dependency semantics; Coordinator owns all writes.
    """
    expectation: object
    verify: object

    def __post_init__(self):
        object.__setattr__(self, "expectation", deep_freeze(self.expectation))
        _sync_callable(self.verify, "staged verifier")


@dataclass(frozen=True, slots=True)
class PreparedPreview:
    """Raw domain patch for normalization preview; no bundle/policy transition."""
    patch: Patch
    expectation: object = None

    def __post_init__(self):
        if type(self.patch) is not Patch:
            raise TypeError("Preview requires a trusted raw Patch")
        object.__setattr__(self, "expectation", deep_freeze(self.expectation))


@dataclass(frozen=True, slots=True)
class PreviewCandidate:
    """Only frozen data may escape a dry-run; never a view, stage or callback."""
    payload: object

    def __post_init__(self):
        object.__setattr__(self, "payload", deep_freeze(self.payload))


class PreviewStatus(StrEnum):
    READY = "ready"
    BUSY = "busy"
    REJECTED = "rejected"
    FAILED = "failed"
    RECONCILING = "reconciling"


@dataclass(frozen=True, slots=True)
class PreviewResult:
    status: PreviewStatus
    operation_id: str
    base: CutIdentity | None = None
    candidate: PreviewCandidate | None = None
    rejection: RejectedDecision | None = None
    error: str | None = None


class _StagedRejected(Exception):
    def __init__(self, decision):
        self.decision = decision
        super().__init__(decision.code)


@dataclass(frozen=True, slots=True)
class PreparedChange:
    """Raw domain+complete trusted system patch for one stage.

    C2 admission must supply acceptance; None preserves the closed host/system
    path only. A preliminary bundle is replaced by the verifier's frozen final
    bundle before cache work or persistence. Verifiers cannot amend this Patch.
    """
    patch: Patch
    kernel_bundle: object = None
    policy_revision: int | None = None
    acceptance: StagedAcceptance | None = None

    def __post_init__(self):
        if self.acceptance is not None and type(self.acceptance) is not StagedAcceptance:
            raise TypeError("Use an engine StagedAcceptance or None")
        if type(self.patch) is not Patch:
            raise TypeError("Trusted prepare must return a Patch inside PreparedChange")
        object.__setattr__(self, "kernel_bundle", deep_freeze(self.kernel_bundle))
        if self.policy_revision is not None and (type(self.policy_revision) is not int or self.policy_revision < 0):
            raise TypeError("policy_revision must be an exact non-negative integer")


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    status: Status
    operation_id: str
    ticket: str
    transaction_id: str | None = None
    error: str | None = None
    diagnostics: tuple[str, ...] = ()
    rejection: RejectedDecision | None = None


@dataclass(frozen=True, slots=True)
class PublishedCut:
    """An issued published lease and immutable derived projection of that cut.

    Capture/C4 consume this object, never StateStore.latest. Closing it releases
    only this consumer's lease. Already extracted immutable values stay valid.
    """
    identity: CutIdentity
    view: ReadView
    derived_entries: object
    derived_revision: int
    _issuer: object

    def query(self, key):
        # Touch the lease before returning a result from its fixed projection.
        if CutIdentity.read(self.view) != self.identity:
            raise CoordinatorUnavailable("PublishedCut identity mismatch")
        entry = self.derived_entries[key]
        if entry.status == "error":
            raise DerivedQueryFailed(entry.error)
        if entry.status != "current":
            raise QueryPending("Derived result is pending")
        return entry.value

    def close(self):
        self.view.close()

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class TerminalWitness:
    """Opaque definitive callback correlation; no source view or live writer.

    Whole-pair identity is captured internally, without reading source records or
    running validation in NOTIFYING. Only the issuing Coordinator can resolve it
    to a current IDLE lease. A copied result/sequence is never this capability.
    """
    result: SubmissionResult
    identity: CutIdentity
    derived_revision: int
    _published: object
    _issuer: object


@dataclass(frozen=True, slots=True)
class StagedInput:
    """Borrowed read-only current base and actual normalized staged successor.

    Borrowing ends when the callback returns; callers may not retain readers
    or mint leases. Preview closes/invalidates both readers before preview()
    returns. Final submission closes the base on callback-scope exit, while its
    staged reader remains Coordinator-owned through DB outcome resolution and is
    invalidated by publication/discard. No StageSnapshot, Patch or writer grant
    is provided. The base is current authority, not C2's historical basis.
    """
    base: PublishedCut
    base_snapshot: SnapshotData
    staged: StagedReadView


@dataclass(frozen=True, slots=True)
class RecoveryHandoff:
    """Engine-only immutable inputs for same-process recovery orchestration.

    Issued from retained pre-publication C1 exports, including RAM-only state.
    It contains no mutable coordinator/store/cache handle or authority grant.
    The trusted definition catalog contains callable capabilities, not results;
    rebuild with budget=0 must not invoke them. It is not a provider/SDK DTO.
    Durable installation still requires a definitive correlated COMMITTED result.
    submission retains the exact detached request, including kernel_bundle, so
    011 can verify its full journal fingerprint without replay or private access.
    """
    operation_id: str
    transaction_id: str | None
    base: CutIdentity
    target: CutIdentity
    base_snapshot: SnapshotData
    target_snapshot: SnapshotData
    definitions: tuple[QueryDefinition, ...]
    cache_incarnation: str
    required_outcome: DBOutcome | None
    authority_may_have_changed: bool
    submission: DurableSubmission | None


def _same_snapshot(left, right):
    """Exact recovery equality; Record semantic equality omits version."""
    if CutIdentity.read(left) != CutIdentity.read(right):
        return False
    if left.records.keys() != right.records.keys():
        return False
    for key, expected in right.records.items():
        actual = left.records[key]
        if not strict_equal(actual, expected) or actual.version != expected.version:
            return False
    return all(getattr(left, name) == getattr(right, name) for name in (
        "generations", "generation_partitions", "existence_versions", "field_versions",
        "collection_versions", "membership_versions", "collection_sources"))


def _same_definitions(left, right):
    # Callable identity avoids invoking author-defined __eq__ during recovery.
    return (type(left) is tuple and len(left) == len(right)
            and all(type(a) is QueryDefinition and a.key == b.key and a.compute is b.compute
                    for a, b in zip(left, right)))


def _require_fresh_recovery_cache(cache, handoff):
    """Alpha coupling to C1 lifecycle shape, isolated and strictly read-only.

    009 owns these fields. 010 validates absence of work before transferring
    ownership; 011 never receives them. An upstream representation change must
    update/review this seam together, otherwise installation fails closed.
    """
    try:
        tickets, deltas, reserved = cache._tickets, cache._deltas, cache._prevalidated
        entries = cache.entries
    except AttributeError as failure:
        raise ValueError("Unsupported recovery cache lifecycle shape") from failure
    if (type(tickets) is not MappingProxyType or type(deltas) is not dict
            or type(entries) is not MappingProxyType):
        raise ValueError("Unsupported recovery cache lifecycle shape")
    if tickets or deltas or reserved is not None:
        raise ValueError("Recovery cache contains outstanding runtime work")
    if (cache.incarnation == handoff.cache_incarnation
            or cache.revision != handoff.target.index_revision
            or not _same_definitions(cache.export_definitions(), handoff.definitions)):
        raise ValueError("Install a fresh cache rebuild with the retained definition catalog")
    if set(entries) != {definition.key for definition in handoff.definitions}:
        raise ValueError("Recovery cache entries do not match the retained catalog")
    empty = ReadSet(handoff.target.epoch, handoff.target.schema_manifest)
    for key, entry in entries.items():
        if type(entry) is not DerivedEntry:
            raise ValueError("Unsupported recovery cache entry shape")
        if (entry.key != key or entry.status != "pending" or entry.value is not None or entry.error is not None
                or entry.version.cache_incarnation != cache.incarnation or entry.version.counter != 1
                or entry.read_set != empty or entry.watch_set != empty):
            raise ValueError("Recovery cache must be fresh Pending without imported runtime results")


@dataclass(frozen=True, slots=True)
class _Published:
    identity: CutIdentity
    entries: object
    revision: int


@dataclass(frozen=True, slots=True)
class _Intention:
    operation_id: str
    ticket: str
    prepare: object
    memory_only: bool
    budget: RecomputeBudget
    completions: tuple
    on_terminal: object = None


def _terminal_same(actual, original):
    """Compare only the bounded original shape, never caller equality/hooks."""
    if type(actual) is not type(original):
        return False
    if type(original) is tuple:
        return len(actual) == len(original) and all(
            _terminal_same(a, b) for a, b in zip(actual, original))
    if type(original) in (str, int, type(None)):
        return actual == original
    return actual is original


def _terminal_primitives(value):
    if type(value) is tuple:
        return tuple(_terminal_primitives(item) for item in value)
    if type(value) not in (str, int, type(None)):
        raise CoordinatorUnavailable("Malformed terminal primitive")
    return value


def _terminal_result_signature(result, original=None):
    if type(result) is not SubmissionResult:
        raise CoordinatorUnavailable("Unissued terminal result shape")
    try:
        rejection = result.rejection
        if rejection is not None:
            if type(rejection) is not RejectedDecision:
                raise CoordinatorUnavailable("Malformed terminal rejection")
            rejection = (rejection.code, rejection.detail)
        signature = (result.status, result.operation_id, result.ticket, result.transaction_id,
                     result.error, result.diagnostics, rejection)
        if original is not None:
            if not _terminal_same(signature, original):
                raise CoordinatorUnavailable("Terminal result was modified")
            return original
        status, operation, ticket, transaction, error, diagnostics, rejection = signature
        if (type(status) is not Status or (status is not Status.COMMITTED and status is not Status.FAILED)
                or type(operation) is not str or type(ticket) is not str
                or (transaction is not None and type(transaction) is not str)
                or (error is not None and type(error) is not str)
                or type(diagnostics) is not tuple or any(type(x) is not str for x in diagnostics)
                or (rejection is not None and (type(rejection[0]) is not str
                    or (rejection[1] is not None and type(rejection[1]) is not str)))):
            raise CoordinatorUnavailable("Malformed definitive terminal result")
        return signature
    except AttributeError:
        raise CoordinatorUnavailable("Incomplete terminal result") from None


def _terminal_identity_signature(identity, original=None):
    if type(identity) is not CutIdentity:
        raise CoordinatorUnavailable("Malformed terminal identity")
    try:
        signature = (identity.epoch, identity.sequence, identity.schema_manifest,
                     identity.policy_revision, identity.index_revision)
        if original is not None:
            if not _terminal_same(signature, original):
                raise CoordinatorUnavailable("Terminal identity was modified")
            return original
        if (type(signature[0]) is not str or type(signature[2]) is not tuple
                or any(type(x) is not int or x < 0 for x in
                       (signature[1], signature[3], signature[4]))):
            raise CoordinatorUnavailable("Malformed terminal identity")
        return _terminal_primitives(signature)
    except AttributeError:
        raise CoordinatorUnavailable("Incomplete terminal identity") from None


def _terminal_result_copy(signature):
    rejection = None if signature[6] is None else RejectedDecision(*signature[6])
    return SubmissionResult(*signature[:6], rejection)


@dataclass(frozen=True, slots=True)
class _TerminalOriginal:
    issuer: object
    published: object
    identity: tuple
    revision: int
    result: tuple


@dataclass(slots=True)
class _Pending:
    intention: _Intention
    stage: object
    reader: object
    delta: object
    publication: object
    new_state: _Published
    recovery: RecoveryHandoff
    submission: DurableSubmission | None = None
    authority_started: bool = False
    db_committed: bool = False
    terminal_result: SubmissionResult | None = None
    terminal_original: object = None


class Coordinator:
    def __init__(self, store: StateStore, cache: DerivedCache, *, durable_port: DurableSubmitPort | None = None,
                 queue_limit=64, hook: Callable[[str, str], None] | None = None, priority_keys=()):
        _main_thread()
        if type(store) is not StateStore or type(cache) is not DerivedCache:
            raise TypeError("Use the real StateStore and DerivedCache")
        if type(queue_limit) is not int or queue_limit < 1:
            raise ValueError("queue_limit must be a positive exact integer")
        if hook is not None and not callable(hook):
            raise TypeError("hook must be a synchronous instrumentation callback")
        self._store, self._cache, self._port = store, cache, durable_port
        self._phase = Phase.IDLE
        self._queue_limit, self._queue = queue_limit, deque()
        self._hook, self._subscribers = hook, []
        self._issuer = object()
        self._terminal_witnesses = WeakValueDictionary()
        self._terminal_pins = {}
        self._host_turn = None
        self._turn_budget = None
        self._pending = None
        self._mailbox_lock = Lock()
        self._expected = self._mailbox = self._last_outcome = None
        self._mailbox_conflict = False
        self._published = self._inspect_pair(store, cache)
        # One trusted bootstrap tuple is shared with ContextIndex export gating.
        # Query failure may publish Error/Pending; priority is not write authority.
        self._priority_keys = cache.validate_priority_keys(priority_keys)

    @staticmethod
    def _inspect_pair(store, cache):
        # Trusted dependency ownership checks: a pair constructed on a worker
        # cannot become a usable main-thread publication authority.
        store._write_check()
        cache._write_check()
        with store.read() as reader:
            identity = CutIdentity.read(reader)
            if cache.sequence != identity.sequence:
                raise ValueError("Graph and cache sequence differ")
            # Verify full cache identity through its public tracked result API,
            # even when entries are pending or there are no registered queries.
            sentinel = object()
            with ReadTracker(reader) as tracker:
                try:
                    cache.result(sentinel, tracker=tracker)
                except KeyError:
                    pass
            return _Published(identity, cache.entries, cache.revision)

    @property
    def host_turn(self):
        """One Qt-free owner turn, independent of window lifetime."""
        _main_thread()
        if self._host_turn is None:
            self._idle()
            from .host_turn import HostTurn
            self._host_turn = HostTurn(self)
        return self._host_turn

    @property
    def terminal_retry_pending(self):
        _main_thread()
        return self._pending is not None and self._pending.terminal_result is not None

    def _turn_action(self):
        _main_thread()
        if self._turn_budget is not None:
            if self._turn_budget == 0:
                raise CoordinatorBusy("HostTurn callback permits at most one semantic action")
            self._turn_budget -= 1

    # Retained for private consumers; both helpers reject caller-defined shapes.
    _terminal_result_signature = staticmethod(_terminal_result_signature)
    _terminal_identity_signature = staticmethod(_terminal_identity_signature)

    def _terminal_pin(self, witness):
        if type(witness) is not TerminalWitness:
            return None
        pin = self._terminal_pins.get(id(witness))
        if pin is None or pin["weak"]() is not witness:
            return None
        try:
            if (witness._issuer is not self._issuer or witness._issuer is not pin["issuer"]
                    or witness._published is not pin["published"]
                    or not _terminal_same(witness.derived_revision, pin["revision"])):
                return None
            _terminal_identity_signature(witness.identity, pin["identity"])
            _terminal_result_signature(witness.result, pin["result"])
        except (AttributeError, CoordinatorUnavailable):
            return None
        return pin

    def _terminal_delivery(self, result):
        pending = self._pending
        if (self._phase is not Phase.NOTIFYING or pending is None
                or pending.terminal_result is not result or pending.terminal_original is None):
            raise CoordinatorUnavailable("No correlated terminal callback is active")
        original = pending.terminal_original
        if original.issuer is not self._issuer:
            raise CoordinatorUnavailable("Foreign original terminal issuer")
        _terminal_result_signature(result, original.result)
        return original

    def terminal_witness(self, result):
        """Retain original definitive correlation, pinned BEFORE author delivery.

        All paths, including cached lookup, validate the exact whole result
        against producer originals before deriving a key. No source view,
        validation, capture, submit or caller comparison is used.
        """
        _main_thread()
        original = self._terminal_delivery(result)
        signature = original.result
        key = (signature[1], signature[2], signature[3], signature[0])
        previous = self._terminal_witnesses.get(key)
        if previous is not None:
            if self._terminal_pin(previous) is None:
                raise CoordinatorUnavailable("Issued terminal witness was modified")
            return previous
        witness = object.__new__(TerminalWitness)
        for name, value in (("result", _terminal_result_copy(signature)),
                ("identity", CutIdentity(*original.identity)),
                ("derived_revision", original.revision),
                ("_published", original.published), ("_issuer", original.issuer)):
            object.__setattr__(witness, name, value)
        ident = id(witness)
        self._terminal_witnesses[key] = witness
        self._terminal_pins[ident] = {"weak": weakref_ref(witness, lambda _, key=ident: self._terminal_pins.pop(key, None)),
            "issuer": original.issuer, "published": original.published, "identity": original.identity,
            "revision": original.revision, "result": signature}
        return witness

    def terminal_cut(self, witness):
        """Resolve the ORIGINAL COMMITTED whole pair with callback-free checks."""
        self._idle()
        pin = self._terminal_pin(witness)
        if (pin is None or pin["result"][0] is not Status.COMMITTED
                or pin["published"] is not self._published
                or not _terminal_same(self._published.revision, pin["revision"])):
            raise CoordinatorUnavailable("Terminal whole pair is no longer current or witness was modified")
        _terminal_identity_signature(self._published.identity, pin["identity"])
        return self.read()

    @property
    def priority_keys(self):
        """Fixed validated query tuple; synchronous QoS, not C1 commit eligibility.

        Context/export readiness belongs to ContextIndex/C4. Callers can use an
        ordinary memory-only Patch() tick to refresh a fresh Pending rebuild.
        """
        return self._priority_keys

    @property
    def phase(self):
        return self._phase

    @property
    def queued(self):
        return len(self._queue)

    @property
    def pending_transaction_id(self):
        pending = self._pending
        return pending.submission.transaction_id if pending is not None and pending.submission is not None else None

    def _idle(self):
        _main_thread()
        if self._phase != Phase.IDLE:
            raise CoordinatorBusy(f"Coordinator phase is {self._phase}")

    def _hit(self, name, operation_id):
        if self._hook is not None:
            value = self._hook(name, operation_id)
            self._reject_async(value)

    @staticmethod
    def _reject_async(value):
        if isawaitable(value) or isgenerator(value) or isasyncgen(value):
            if iscoroutine(value):
                value.close()
            elif isgenerator(value):
                value.close()
            raise TypeError("Guarded callbacks must return synchronously, without an awaitable or generator")

    def read(self):
        _main_thread()
        if self._phase in (Phase.PUBLISHING, Phase.RECONCILING):
            raise CoordinatorUnavailable("No coherent current cut is available in this phase")
        state = self._published
        view = self._store.read(state.identity.sequence)
        return PublishedCut(state.identity, view, state.entries, state.revision, self._issuer)

    def validate_cut(self, cut):
        _main_thread()
        return (type(cut) is PublishedCut and cut._issuer is self._issuer
                and type(cut.view) is ReadView and CutIdentity.read(cut.view) == cut.identity)

    def subscribe(self, subscriber):
        self._idle()
        if not callable(subscriber) or iscoroutinefunction(subscriber) or isgeneratorfunction(subscriber):
            raise TypeError("Use a trusted synchronous subscriber")
        self._subscribers.append(subscriber)

    def register_query(self, key, compute):
        self._turn_action()
        self._idle()
        self._phase = Phase.PREPARING
        try:
            self._cache.register(key, compute)
            self._published = replace(self._published, entries=self._cache.entries, revision=self._cache.revision)
        finally:
            self._phase = Phase.IDLE

    def start_query(self, key):
        self._turn_action()
        self._idle()
        self._phase = Phase.PREPARING
        try:
            with self._store.read(self._published.identity.sequence) as reader:
                return self._cache.start(key, reader)
        finally:
            self._phase = Phase.IDLE

    def cancel_query(self, work: QueryWork):
        self._turn_action()
        self._idle()
        self._phase = Phase.PREPARING
        try:
            return self._cache.cancel(work)
        finally:
            self._phase = Phase.IDLE

    def submit(self, operation_id, prepare, *, memory_only=True, busy=BusyPolicy.QUEUE,
               budget=None, completions=(), on_terminal=None):
        """Submit a trusted intention with optional engine terminal notification.

        on_terminal(result) -> None is per-intention, synchronous and idempotent.
        It runs under NOTIFYING after known publication/rollback, never UNKNOWN.
        CommandKernel attaches it automatically; app authors do not route pump
        results. A failed callback retains one immutable proof and backpressure
        until retry_terminal(), without replaying SQL/stage/prepare/handler.
        """
        self._turn_action()
        _main_thread()
        _text(operation_id, "operation_id")
        if (not callable(prepare) or iscoroutinefunction(prepare) or isgeneratorfunction(prepare)
                or isasyncgenfunction(prepare) or type(memory_only) is not bool or type(busy) is not BusyPolicy):
            raise TypeError("Use a synchronous trusted prepare and typed admission options")
        if on_terminal is not None:
            _sync_callable(on_terminal, "terminal callback")
        budget = RecomputeBudget() if budget is None else budget
        if (type(budget) is not RecomputeBudget or type(completions) is not tuple
                or any(type(item) is not QueryCompletion for item in completions)):
            raise TypeError("Use typed budget and immutable completion tuple")
        request = _Intention(operation_id, uuid4().hex, prepare, memory_only, budget, completions, on_terminal)
        if self._phase != Phase.IDLE:
            if busy == BusyPolicy.REJECT or len(self._queue) >= self._queue_limit:
                return self._result(request, Status.BUSY, error="semantic_queue_full_or_guarded")
            self._queue.append(request)
            return self._result(request, Status.QUEUED)
        return self._execute(request)

    def _result(self, request, status, *, error=None, diagnostics=(), transaction_id=None, rejection=None):
        return SubmissionResult(status, request.operation_id, request.ticket, transaction_id, error, diagnostics, rejection)

    def _memory_check(self, change, reader):
        if change.kernel_bundle is not None:
            raise ValueError("A kernel durable bundle requires durable submission")
        for create in change.patch.creates:
            if create.partition != "memory":
                raise ValueError("Memory-only publication cannot create durable records")
        for ref in (*change.patch.deletes, *(update.ref for update in change.patch.updates)):
            if reader.resolve(ref).partition != "memory":
                raise ValueError("Memory-only publication cannot change durable records")

    def _accepted_bundle(self, change, staged_input):
        acceptance = change.acceptance
        if acceptance is None:
            return change.kernel_bundle  # Existing closed host/system path only.
        decision = acceptance.verify(acceptance.expectation, staged_input)
        self._reject_async(decision)
        if type(decision) is RejectedDecision:
            raise _StagedRejected(decision)
        if type(decision) is not AcceptedDecision:
            raise TypeError("Staged verifier must return a typed acceptance decision")
        return deep_freeze(decision.kernel_bundle)

    def _discard_preview(self, stage, reader, basis):
        # Attempt every cleanup even if one operation fails; a failure still
        # leaves the entire coordinator failclosed until explicit recovery.
        failure = None
        for close in (None if reader is None else reader.close,
                      None if stage is None else lambda: self._store.discard(stage),
                      None if basis is None else basis.close):
            if close is not None:
                try:
                    close()
                except BaseException as error:
                    if failure is None or (isinstance(failure, Exception) and not isinstance(error, Exception)):
                        failure = error
        if failure is not None:
            raise failure

    def preview(self, operation_id, prepare, *, extract):
        """Engine-only RAW normalization dry-run; never publish/cache/DB enqueue.

        prepare(current ManagedView) -> PreparedPreview. C2 may call its author
        handler against an independently issued old basis inside that guard.
        extract(frozen expectation, borrowed StagedInput) -> PreviewCandidate or
        RejectedDecision. Only frozen candidate data returns, after discard.
        Busy previews are not queued or replayed. No stage/reader escapes output.
        """
        _main_thread()
        _text(operation_id, "operation_id")
        _sync_callable(prepare, "preview prepare")
        _sync_callable(extract, "preview extractor")
        if self._phase != Phase.IDLE:
            return PreviewResult(PreviewStatus.BUSY, operation_id, error="semantic_guarded")
        stage = reader = basis = None
        base = None
        self._phase = Phase.PREPARING
        try:
            self._hit("before_admission", operation_id)
            basis = self._store.read(self._published.identity.sequence)
            base = CutIdentity.read(basis)
            with ReadTracker(basis, query=lambda key, tracker: self._cache.result(key, tracker=tracker)) as reads:
                prepared = prepare(reads.view)
                self._reject_async(prepared)
            if type(prepared) is not PreparedPreview:
                raise TypeError("Preview prepare must return PreparedPreview")
            if not reads.read_set.complete:
                raise QueryPending("Preview consumed an unresolved derived query read")
            self._hit("after_prepare", operation_id)
            self._phase = Phase.STAGING
            base_snapshot = self._store.export_snapshot()
            stage = self._store.stage(prepared.patch, index_revision=self._cache.revision + 1)
            reader = self._store.read_staged(stage)
            inputs = StagedInput(PublishedCut(base, basis, self._published.entries,
                self._published.revision, self._issuer), base_snapshot, reader)
            extracted = extract(prepared.expectation, inputs)
            self._reject_async(extracted)
            if type(extracted) is RejectedDecision:
                result = PreviewResult(PreviewStatus.REJECTED, operation_id, base,
                                       rejection=extracted, error=extracted.code)
            elif type(extracted) is PreviewCandidate:
                # Re-freeze at the boundary even if a trusted caller bypassed a
                # dataclass constructor; no views/callbacks enter output.
                candidate = PreviewCandidate(extracted.payload)
                result = PreviewResult(PreviewStatus.READY, operation_id, base, candidate=candidate)
            else:
                raise TypeError("Preview extractor must return a typed candidate or rejection")
        except Exception as failure:
            result = PreviewResult(PreviewStatus.FAILED, operation_id, base, error=type(failure).__name__)
        except BaseException as fatal:
            try:
                self._discard_preview(stage, reader, basis)
            except BaseException as cleanup_failure:
                fatal.add_note("Preview cleanup also failed: " + type(cleanup_failure).__name__)
            finally:
                self._phase = Phase.RECONCILING
            raise
        try:
            self._discard_preview(stage, reader, basis)
        except Exception as failure:
            self._phase = Phase.RECONCILING
            return PreviewResult(PreviewStatus.RECONCILING, operation_id, base, error=type(failure).__name__)
        except BaseException:
            self._phase = Phase.RECONCILING
            raise
        self._phase = Phase.IDLE
        return result

    def _execute(self, request):
        self._idle()
        stage = reader = delta = None
        self._phase = Phase.PREPARING
        try:
            self._hit("before_admission", request.operation_id)
            with self._store.read(self._published.identity.sequence) as basis:
                with ReadTracker(basis, query=lambda key, tracker: self._cache.result(key, tracker=tracker)) as reads:
                    change = request.prepare(reads.view)
                    self._reject_async(change)
                if type(change) is not PreparedChange:
                    raise TypeError("Prepare must return PreparedChange")
                if not reads.read_set.complete:
                    raise QueryPending("Prepare consumed an unresolved derived query read")
                self._hit("after_prepare", request.operation_id)
                if request.memory_only:
                    self._memory_check(change, basis)
                elif self._port is None:
                    raise ValueError("No durable submit port is installed")
                self._phase = Phase.STAGING
                base_snapshot = self._store.export_snapshot()
                definitions = self._cache.export_definitions()
                cache_incarnation = self._cache.incarnation
                stage = self._store.stage(change.patch, policy_revision=change.policy_revision,
                                          index_revision=self._cache.revision + 1)
                reader = self._store.read_staged(stage)
                staged_input = StagedInput(PublishedCut(self._published.identity, basis,
                    self._published.entries, self._published.revision, self._issuer), base_snapshot, reader)
                final_bundle = self._accepted_bundle(change, staged_input)
                if request.memory_only and final_bundle is not None:
                    raise ValueError("Memory-only acceptance cannot introduce a durable kernel bundle")
            delta = self._cache.prepare(reader, budget=request.budget, completions=request.completions,
                                        priority_keys=self._priority_keys)
            publication = self._cache.prevalidate(delta, reader)
            target = CutIdentity.read(reader)
            submission = None if request.memory_only else DurableSubmission(
                uuid4().hex, request.operation_id, self._published.identity,
                target, stage.snapshot, final_bundle)
            recovery = RecoveryHandoff(request.operation_id,
                None if submission is None else submission.transaction_id,
                self._published.identity, target, base_snapshot, stage.snapshot,
                definitions, cache_incarnation,
                None if submission is None else DBOutcome.COMMITTED, False, submission)
            pending = _Pending(request, stage, reader, delta, publication,
                               _Published(target, delta.entries, self._cache.revision + 1),
                               recovery, submission)
            self._pending = pending
        except Exception as failure:
            try:
                self._discard_parts(stage, reader, delta)
            except BaseException:
                self._phase = Phase.RECONCILING
                raise
            self._phase = Phase.IDLE
            rejection = failure.decision if type(failure) is _StagedRejected else None
            return self._result(request, Status.FAILED, error=type(failure).__name__ if rejection is None else rejection.code,
                                rejection=rejection)
        except BaseException:
            self._phase = Phase.RECONCILING
            raise
        if request.memory_only:
            return self._publish(pending)
        with self._mailbox_lock:
            self._expected = (pending.submission.transaction_id, pending.submission.target)
            self._mailbox = None
            self._mailbox_conflict = False
        # submit itself is guarded: a direct delivery callback may offer an Ack,
        # but cannot pump it recursively before enqueue has returned.
        self._phase = Phase.STAGING
        try:
            disposition = self._port.submit(pending.submission)
            self._reject_async(disposition)
            if type(disposition) is not SubmitDisposition:
                raise TypeError("Durable port must return SubmitDisposition")
        except Exception as failure:
            self._phase = Phase.RECONCILING
            return self._pending_result(Status.RECONCILING, error=type(failure).__name__)
        except BaseException:
            self._phase = Phase.RECONCILING
            raise
        if disposition == SubmitDisposition.NOT_SUBMITTED:
            # This guarantee is legal only before any DB I/O. An already offered
            # outcome contradicts it and must not be turned into known rollback.
            with self._mailbox_lock:
                contradicted = self._mailbox is not None or self._mailbox_conflict
            if contradicted:
                self._phase = Phase.RECONCILING
                return self._pending_result(Status.RECONCILING, error="contradictory_not_submitted")
            return self._rollback("not_submitted")
        self._phase = Phase.AWAITING_DB
        return self._pending_result(Status.AWAITING_DB)

    def _pending_result(self, status, *, error=None, diagnostics=()):
        pending = self._pending
        return self._result(pending.intention, status, error=error, diagnostics=diagnostics,
                            transaction_id=self.pending_transaction_id)

    def _discard_parts(self, stage, reader, delta):
        if delta is not None:
            self._cache.discard(delta)
        if reader is not None:
            reader.close()
        if stage is not None:
            self._store.discard(stage)

    def _clear_mailbox(self):
        with self._mailbox_lock:
            if self._mailbox is not None:
                self._last_outcome = self._mailbox
            self._expected = self._mailbox = None
            self._mailbox_conflict = False

    def _rollback(self, reason):
        pending = self._pending
        try:
            self._discard_parts(pending.stage, pending.reader, pending.delta)
        except BaseException:
            self._phase = Phase.RECONCILING
            raise
        result = self._pending_result(Status.FAILED, error=reason)
        return self._finish_terminal(pending, result)

    @staticmethod
    def _terminal_delivery_intact(pending):
        try:
            _terminal_result_signature(pending.terminal_result, pending.terminal_original.result)
            return True
        except CoordinatorUnavailable:
            return False

    @staticmethod
    def _terminal_reconciling(original, error, *, tampered=False):
        signature = original.result
        diagnostics = signature[5] + (("terminal_result_tampered",) if tampered else ())
        return SubmissionResult(Status.RECONCILING, signature[1], signature[2], signature[3],
                                error, diagnostics)

    def _finish_terminal(self, pending, result):
        # Pin engine-owned outcome and whole pair BEFORE the first author call.
        # Retrying never derives a new original from delivery or current latest.
        if pending.terminal_original is None:
            identity = _terminal_identity_signature(self._published.identity)
            revision = self._published.revision
            if type(revision) is not int or revision < 0:
                raise CoordinatorUnavailable("Malformed terminal revision")
            pending.terminal_original = _TerminalOriginal(self._issuer, self._published,
                identity, revision, _terminal_result_signature(result))
            pending.terminal_result = result
        elif not self._terminal_delivery_intact(pending):
            # Old corrupted alias stays corrupted and is never actual delivery
            # again. Ordinary exception retry preserves the identical clean DTO.
            pending.terminal_result = _terminal_result_copy(pending.terminal_original.result)
        original = pending.terminal_original
        result = pending.terminal_result
        self._phase = Phase.NOTIFYING
        try:
            callback = pending.intention.on_terminal
            if callback is not None:
                returned = callback(result)
                self._reject_async(returned)
                if returned is not None:
                    raise TypeError("Terminal callback must acknowledge by returning None")
        except Exception as failure:
            self._phase = Phase.RECONCILING
            tampered = not self._terminal_delivery_intact(pending)
            return self._terminal_reconciling(original,
                "terminal_result_tampered" if tampered else "terminal_callback:" + type(failure).__name__,
                tampered=tampered)
        except BaseException:
            self._phase = Phase.RECONCILING
            raise
        if not self._terminal_delivery_intact(pending):
            self._phase = Phase.RECONCILING
            return self._terminal_reconciling(original, "terminal_result_tampered", tampered=True)
        self._pending = None
        self._clear_mailbox()
        self._phase = Phase.IDLE
        return result

    def _require_no_terminal_retry(self):
        if self._pending is not None and self._pending.terminal_result is not None:
            raise CoordinatorBusy("A definitive terminal notification is retained; use retry_terminal()")

    def retry_terminal(self):
        """Retry only the failed idempotent terminal callback, on the main thread.

        Ordinary callback failure retains identical delivery/witness objects.
        A corrupted delivery is replaced from its pre-callback original pin;
        the old alias is never accepted again. No graph, stage, SQL, handler,
        verifier or ordinary UI subscriber is replayed.
        Other reconcile/rebuild/pump routes cannot consume this delivery proof.
        """
        self._turn_action()
        _main_thread()
        pending = self._pending
        if (self._phase != Phase.RECONCILING or pending is None
                or pending.terminal_result is None):
            raise CoordinatorBusy("No failed terminal notification is pending")
        return self._finish_terminal(pending, pending.terminal_result)

    def _publish(self, pending):
        self._phase = Phase.PUBLISHING
        published = None
        try:
            self._hit("before_publication", pending.intention.operation_id)
            # prevalidate completed all allocation/target validation. No callbacks
            # or reader accesses occur between the two authority pointer updates.
            pending.recovery = replace(pending.recovery, authority_may_have_changed=True)
            pending.authority_started = True
            published = self._store.publish(pending.stage)
            self._cache.publish(pending.publication, published)
            self._published = pending.new_state
            self._hit("after_publication", pending.intention.operation_id)
        except Exception as failure:
            if not pending.authority_started and not pending.db_committed:
                return self._rollback(type(failure).__name__)
            self._phase = Phase.RECONCILING
            return self._pending_result(Status.RECONCILING, error=type(failure).__name__)
        except BaseException:
            self._phase = Phase.RECONCILING
            raise
        finally:
            if published is not None:
                published.close()
        self._phase = Phase.NOTIFYING
        diagnostics = []
        try:
            try:
                self._hit("before_notify", pending.intention.operation_id)
            except Exception as failure:
                diagnostics.append("before_notify:" + type(failure).__name__)
            for subscriber in tuple(self._subscribers):
                try:
                    with self.read() as cut:
                        value = subscriber(cut)
                        self._reject_async(value)
                except Exception as failure:
                    diagnostics.append("subscriber:" + type(failure).__name__)
            try:
                self._hit("after_notify", pending.intention.operation_id)
            except Exception as failure:
                diagnostics.append("after_notify:" + type(failure).__name__)
        except BaseException:
            self._phase = Phase.RECONCILING
            raise
        result = self._pending_result(Status.COMMITTED, diagnostics=tuple(diagnostics))
        return self._finish_terminal(pending, result)

    def offer_db_outcome(self, outcome: DurableOutcome):
        """Any thread may offer data; only pump/reconcile can mutate authority."""
        if type(outcome) is not DurableOutcome:
            raise TypeError("Offer a typed immutable DurableOutcome")
        with self._mailbox_lock:
            expected = self._expected
            if expected is None or outcome.transaction_id != expected[0]:
                if (self._last_outcome is not None and outcome.transaction_id == self._last_outcome.transaction_id
                        and outcome.target == self._last_outcome.target and outcome.outcome == self._last_outcome.outcome):
                    return MailboxDisposition.DUPLICATE
                return MailboxDisposition.STALE
            if outcome.target != expected[1]:
                self._mailbox_conflict = True
                return MailboxDisposition.RECONCILE_REQUIRED
            old = self._mailbox
            if old is not None:
                if old.outcome == outcome.outcome or outcome.outcome == DBOutcome.UNKNOWN:
                    return MailboxDisposition.DUPLICATE
                if old.outcome != DBOutcome.UNKNOWN:
                    self._mailbox_conflict = True
                    return MailboxDisposition.RECONCILE_REQUIRED
            self._mailbox = outcome
            return MailboxDisposition.STORED

    def pump(self, *, max_intentions=1):
        if self._turn_budget is not None and max_intentions != 0:
            raise CoordinatorBusy("HostTurn callbacks cannot dequeue intentions")
        self._turn_action()
        _main_thread()
        if type(max_intentions) is not int or max_intentions < 0:
            raise ValueError("max_intentions must be an exact non-negative integer")
        if self._phase not in (Phase.IDLE, Phase.AWAITING_DB, Phase.RECONCILING):
            raise CoordinatorBusy("A guarded callback cannot pump nested semantic operations")
        self._require_no_terminal_retry()
        results = []
        with self._mailbox_lock:
            outcome, conflict = self._mailbox, self._mailbox_conflict
        if self._pending is not None and self._pending.submission is not None:
            if conflict:
                self._phase = Phase.RECONCILING
                return (self._pending_result(Status.RECONCILING, error="conflicting_db_outcomes"),)
            if outcome is not None:
                results.append(self._apply_outcome(outcome))
        for _ in range(max_intentions):
            if self._phase != Phase.IDLE or not self._queue:
                break
            results.append(self._execute(self._queue.popleft()))
        return tuple(results)

    def _apply_outcome(self, outcome):
        pending = self._pending
        if outcome.outcome == DBOutcome.UNKNOWN:
            self._phase = Phase.RECONCILING
            return self._pending_result(Status.RECONCILING, error="unknown_db_outcome")
        if pending.authority_started:
            self._phase = Phase.RECONCILING
            return self._pending_result(Status.RECONCILING, error="publication_rebuild_required")
        self._phase = Phase.STAGING
        try:
            self._hit("worker_completion", pending.intention.operation_id)
        except BaseException:
            self._phase = Phase.RECONCILING
            raise
        if outcome.outcome == DBOutcome.ROLLED_BACK:
            return self._rollback("confirmed_rollback")
        pending.db_committed = True
        return self._publish(pending)

    def reconcile(self, outcome: DurableOutcome):
        self._turn_action()
        _main_thread()
        self._require_no_terminal_retry()
        if self._phase != Phase.RECONCILING or self._pending is None or self._pending.submission is None:
            raise CoordinatorBusy("Reconcile requires an unresolved durable operation")
        if (type(outcome) is not DurableOutcome or outcome.outcome == DBOutcome.UNKNOWN
                or (outcome.transaction_id, outcome.target) != self._expected):
            raise ValueError("Reconcile requires a correlated definitive durable outcome")
        with self._mailbox_lock:
            self._mailbox = outcome
            self._mailbox_conflict = False
        return self._apply_outcome(outcome)

    def recovery_handoff(self):
        """Owner-only retained recovery image; never read a half-published pair.

        011 uses target_snapshot with StateStore.from_snapshot and the frozen
        definitions with DerivedCache.rebuild(..., budget=0). Base is retained
        for diagnostics/readback; known rollback preserves the untouched old pair.
        """
        _main_thread()
        self._require_no_terminal_retry()
        if self._phase != Phase.RECONCILING or self._pending is None:
            raise CoordinatorBusy("No retained recovery operation is pending")
        return self._pending.recovery

    def install_rebuilt(self, store, cache, *, outcome=None):
        """Install a factory-restored exact RAM target and fresh Pending cache.

        No handler/query runs here. Durable UNKNOWN cannot authorize installation:
        011 must first prove correlated COMMITTED and check the DB projection.
        Known rollback without authority writes uses reconcile(), retaining the
        old pair. Failed validation/factory construction leaves recovery closed.
        """
        self._turn_action()
        _main_thread()
        self._require_no_terminal_retry()
        pending = self._pending
        if self._phase != Phase.RECONCILING or pending is None:
            raise CoordinatorBusy("No publication recovery is pending")
        if type(store) is not StateStore or type(cache) is not DerivedCache:
            raise TypeError("Install real fully rebuilt C1 authorities")
        handoff = pending.recovery
        if handoff.required_outcome == DBOutcome.COMMITTED:
            if (type(outcome) is not DurableOutcome or outcome.outcome != DBOutcome.COMMITTED
                    or (outcome.transaction_id, outcome.target) != self._expected):
                raise ValueError("Durable rebuild requires a definitive correlated COMMITTED outcome")
        elif outcome is not None:
            raise ValueError("Memory rebuild has no durable transaction outcome")
        self._phase = Phase.STAGING
        try:
            rebuilt = self._inspect_pair(store, cache)
            if rebuilt.identity != handoff.target or not _same_snapshot(store.export_snapshot(), handoff.target_snapshot):
                raise ValueError("Rebuilt image is not the exact retained target, including RAM and versions")
            _require_fresh_recovery_cache(cache, handoff)
            cache.validate_priority_keys(self._priority_keys)
            # Retire the abandoned pair's reservations. Consumed tokens are
            # expected after a partially completed publication, not new writes.
            pending.reader.close()
            try:
                self._cache.discard(pending.delta)
            except DerivedDeltaRejected:
                pass
            try:
                self._store.discard(pending.stage)
            except StageRejected:
                pass
            self._store, self._cache, self._published = store, cache, rebuilt
            if outcome is not None:
                with self._mailbox_lock:
                    self._mailbox = outcome
        except BaseException:
            self._phase = Phase.RECONCILING
            raise
        # The exact pair is now installed. Terminal delivery is separate from
        # ordinary UI publication subscribers and can fail without another rebuild.
        return self._finish_terminal(pending, self._pending_result(Status.COMMITTED))

    @contextmanager
    def capture(self, operation_id="capture"):
        self._idle()
        _text(operation_id, "operation_id")
        self._phase = Phase.CAPTURING
        cut = None
        try:
            self._hit("before_capture", operation_id)
            cut = self.read()
            yield cut
            self._hit("after_capture", operation_id)
        finally:
            if cut is not None:
                cut.close()
            self._phase = Phase.IDLE
