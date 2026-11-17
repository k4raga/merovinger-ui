"""Pure derived-cache preparation for the C1/C2 publication cut.

This module stores rebuildable results/dependency metadata, never domain records.
The coordinator prepares on StagedReadView, publishes graph and this delta inside
its exclusive Publishing phase, then notifies. There is no automatic graph/cache
publication, thread scheduler, I/O, authorization or second semantic writer here.
"""
from contextlib import nullcontext
from dataclasses import dataclass, field
from inspect import isasyncgenfunction, iscoroutinefunction, isgeneratorfunction
from threading import get_ident
from types import MappingProxyType
from uuid import uuid4

from .query import QueryKey, QueryVersion
from .records import deep_freeze
from .tracking import QueryQuotaExceeded, ReadQuota, ReadSet, ReadTracker
from .snapshots import WriterOwnershipError
from .lifetime import ReadViewUnavailable


class DerivedCutMismatch(RuntimeError):
    pass


class DerivedDeltaRejected(RuntimeError):
    pass


class DerivedPublicationInvariantError(RuntimeError):
    pass


class QueryPending(LookupError):
    pass


class DerivedQueryFailed(RuntimeError):
    pass


class QueryCycleError(RuntimeError):
    pass


class PriorityQueryViolation(DerivedQueryFailed):
    """A priority producer attempted an undeclared optional query dependency."""
    pass


@dataclass(frozen=True, slots=True)
class PriorityQueryLimits:
    max_keys: int = 64
    max_reads: int = 256
    max_items: int = 128
    max_matching_refs: int = 128
    max_nodes: int = 512
    max_bytes: int = 32768
    max_depth: int = 16

    def quota(self):
        return ReadQuota(max_reads=self.max_reads, max_items=self.max_items,
                         max_matching_refs=self.max_matching_refs, max_nodes=self.max_nodes,
                         max_bytes=self.max_bytes, max_depth=self.max_depth)


# One fixed engine preset, not per-app/per-rebuild mutable query policy.
PRIORITY_QUERY_LIMITS = PriorityQueryLimits()


@dataclass(frozen=True, slots=True)
class RecomputeBudget:
    max_queries: int = 0

    def __post_init__(self):
        if type(self.max_queries) is not int or self.max_queries < 0:
            raise ValueError("Recompute budget must be a non-negative exact integer")


@dataclass(frozen=True, slots=True)
class QueryDefinition:
    """One trusted pure-query definition; no command/model code transport."""
    key: QueryKey
    compute: object = field(repr=False)

    def __post_init__(self):
        if (type(self.key) is not QueryKey or not callable(self.compute)
                or iscoroutinefunction(self.compute) or isgeneratorfunction(self.compute)
                or isasyncgenfunction(self.compute)):
            raise TypeError("Use a QueryKey and trusted synchronous query callable")


@dataclass(frozen=True, slots=True)
class QueryTicket:
    key: QueryKey
    version: QueryVersion
    epoch: str
    sequence: int
    schema_manifest: tuple
    nonce: str

    def __post_init__(self):
        if type(self.version) is not QueryVersion:
            raise TypeError("Query tickets require a scoped QueryVersion")

    @property
    def input_generation(self):
        """Counter compatibility for diagnostics; freshness always uses version."""
        return self.version.counter


@dataclass(frozen=True, slots=True)
class QueryCompletion:
    ticket: QueryTicket
    value: object
    read_set: ReadSet

    def __post_init__(self):
        if type(self.ticket) is not QueryTicket or type(self.read_set) is not ReadSet:
            raise TypeError("A completion requires a ticket and full immutable readset")
        object.__setattr__(self, "value", deep_freeze(self.value))


@dataclass(frozen=True, slots=True)
class DerivedEntry:
    key: QueryKey
    version: QueryVersion
    status: str
    read_set: ReadSet
    watch_set: ReadSet
    value: object = None
    error: str | None = None
    priority_evaluated: bool = False

    def __post_init__(self):
        if type(self.version) is not QueryVersion:
            raise TypeError("Derived entries require a scoped QueryVersion")
        if type(self.priority_evaluated) is not bool:
            raise TypeError("Priority evaluation provenance must be an exact bool")
        if self.priority_evaluated and (self.status != "current" or not self.read_set.complete):
            raise ValueError("Only complete current entries can have priority provenance")

    @property
    def input_generation(self):
        """Counter compatibility for diagnostics; freshness always uses version."""
        return self.version.counter


@dataclass(frozen=True, slots=True)
class _Cut:
    epoch: str
    sequence: int
    schema_manifest: tuple
    policy_revision: int
    index_revision: int

    @classmethod
    def read(cls, reader):
        return cls(reader.epoch, reader.sequence, reader.schema_manifest,
                   reader.policy_revision, reader.index_revision)


@dataclass(frozen=True, slots=True)
class _CacheState:
    cut: _Cut
    revision: int
    entries: object
    tickets: object


@dataclass(frozen=True, slots=True)
class CompletionRejection:
    ticket: QueryTicket
    reason: str


@dataclass(frozen=True, slots=True)
class DerivedDelta:
    """Immutable candidate; only the issuing cache/coordinator can publish it."""
    cut: _Cut
    base_revision: int
    entries: object
    dirty: frozenset
    recomputed: tuple
    accepted: tuple
    rejected: tuple[CompletionRejection, ...]
    _token: str = field(repr=False)
    _tickets: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class PrevalidatedDerivedDelta:
    """Reserved publication: every allocation/check precedes graph publish."""
    delta: DerivedDelta
    _state: _CacheState = field(repr=False)


def _generations(entries):
    return {key: entry.version for key, entry in entries.items()}


def _unresolved(entries, key, tracker, outcome):
    if tracker is None:
        return
    entry = entries.get(key)
    tracker.unresolved_query(key, outcome, None if entry is None else entry.version)
    if entry is not None:
        for unresolved in entry.read_set.unresolved:
            tracker.unresolved_query(unresolved.key, unresolved.outcome, unresolved.generation)
        # Preserve transitive watch inputs at this pinned cut, without importing
        # an old result's stale tokens or clearing the sticky unresolved outcome.
        tracker.include(entry.watch_set.rebased(tracker._reader,
                                               query_versions=_generations(entries)))


def _result(entries, key, tracker=None):
    with tracker.operation() if tracker is not None else nullcontext():
        try:
            entry = entries[key]
        except KeyError:
            _unresolved(entries, key, tracker, "missing")
            raise KeyError(f"Unknown derived query: {key!r}") from None
        if tracker is not None:
            tracker.depend_on_query(key, entry.version)
        if entry.status == "error":
            _unresolved(entries, key, tracker, "error")
            raise DerivedQueryFailed(entry.error)
        if entry.status != "current":
            _unresolved(entries, key, tracker, "pending")
            raise QueryPending(f"Query is pending: {key.name}")
        if tracker is not None:
            if not entry.read_set.matches(tracker._reader, query_versions=_generations(entries)):
                _unresolved(entries, key, tracker, "stale")
                raise QueryPending(f"Query inputs do not match this scope: {key.name}")
            tracker.include(entry.read_set)
        return entry.value

@dataclass(frozen=True, slots=True)
class QueryWork:
    """Pinned immutable worker inputs. run() is pure and closes its borrowed lease."""
    ticket: QueryTicket
    _reader: object = field(repr=False)
    _compute: object = field(repr=False)
    _entries: object = field(repr=False)

    def run(self):
        try:
            with ReadTracker(self._reader, query=lambda key, tracker: _result(
                    self._entries, key, tracker)) as reads:
                value = self._compute(reads.view)
            return QueryCompletion(self.ticket, value, reads.read_set)
        finally:
            self._reader.close()

    def close(self):
        self._reader.close()


class _Evaluator:
    def __init__(self, reader, entries, definitions, budget):
        self.reader, self.entries, self.definitions = reader, entries, definitions
        self.remaining = budget.max_queries
        self.stack = []
        self.recomputed = []
        self.attempted = set()

    def read(self, key, tracker):
        if key not in self.entries:
            return _result(self.entries, key, tracker)
        tracker.depend_on_query(key, self.entries[key].version)
        try:
            self.compute(key)
        except (QueryPending, DerivedQueryFailed):
            # _result captures the unavailable outcome before propagating it.
            return _result(self.entries, key, tracker)
        except QueryCycleError:
            _unresolved(self.entries, key, tracker, "cycle")
            raise
        return _result(self.entries, key, tracker)

    def tracker(self):
        return ReadTracker(self.reader, query=self.read)

    def freeze(self, value):
        return deep_freeze(value)

    def recoverable(self, failure):
        return True

    def diagnostic(self, failure):
        return f"{type(failure).__name__}: {failure}"

    def priority_provenance(self, status):
        return False

    def compute(self, key):
        entry = self.entries[key]
        if entry.status == "current":
            return
        if key in self.stack:
            raise QueryCycleError("Derived query cycle: " + " -> ".join(
                item.name for item in (*self.stack, key)))
        if key in self.attempted:
            if entry.status == "error":
                raise DerivedQueryFailed(entry.error)
            raise QueryPending(f"Query is pending: {key.name}")
        if self.remaining == 0:
            raise QueryPending(f"No recompute budget for {key.name}")
        self.remaining -= 1
        self.attempted.add(key)
        self.stack.append(key)
        reads = self.tracker()
        status, value, error = "current", None, None
        try:
            with reads:
                value = self.freeze(self.definitions[key](reads.view))
        except QueryPending:
            status = "pending"
        except Exception as failure:
            if not self.recoverable(failure):
                raise
            status, error = "error", self.diagnostic(failure)
        finally:
            self.stack.pop()
        captured = reads.read_set
        if status == "current" and not captured.complete:
            status, value = "pending", None
        # An incomplete run may not have reached all formerly read inputs.
        # Keep the prior watch set as well, conservatively, until a full result.
        if status != "current":
            merged = ReadTracker(self.reader)
            merged.include(entry.watch_set)
            merged.include(captured)
            captured = merged.read_set
            merged.close()
        watch = captured.rebased(self.reader, query_versions=_generations(self.entries))
        self.entries[key] = DerivedEntry(key, entry.version, status,
                                        captured, watch, value if status == "current" else None, error,
                                        priority_evaluated=self.priority_provenance(status))
        if status == "current":
            self.recomputed.append(key)
        if status == "pending":
            raise QueryPending(f"Query is pending: {key.name}")
        if status == "error":
            raise DerivedQueryFailed(error)


class _PriorityEvaluator(_Evaluator):
    def __init__(self, reader, entries, definitions, keys):
        super().__init__(reader, entries, definitions, RecomputeBudget(len(keys)))
        self.allowed = frozenset(keys)

    def tracker(self):
        return ReadTracker(self.reader, query=self.read, quota=PRIORITY_QUERY_LIMITS.quota())

    def freeze(self, value):
        PRIORITY_QUERY_LIMITS.quota().value(value)
        return deep_freeze(value)

    def recoverable(self, failure):
        return not isinstance(failure, (DerivedCutMismatch, DerivedDeltaRejected,
            DerivedPublicationInvariantError, WriterOwnershipError, ReadViewUnavailable))

    def priority_provenance(self, status):
        return status == "current"

    def diagnostic(self, failure):
        # Internal status only; no hidden labels/identity/raw error text in roots.
        return "priority query unavailable: " + type(failure).__name__

    def read(self, key, tracker):
        if key not in self.allowed:
            entry = self.entries.get(key)
            tracker.unresolved_query(key, "priority_dependency",
                                     None if entry is None else entry.version)
            if entry is not None:
                tracker.depend_on_query(key, entry.version)
            raise PriorityQueryViolation("Priority dependency is outside the declared closed set")
        return super().read(key, tracker)


class _OptionalEvaluator(_Evaluator):
    def __init__(self, reader, entries, definitions, budget, priority):
        super().__init__(reader, entries, definitions, budget)
        self.priority = priority

    def compute(self, key):
        if key in self.priority:
            _result(self.entries, key)  # no optional recompute of failed mandatory roots
            return
        super().compute(key)


class DerivedCache:
    """Owner-thread prepared cache; no worker may publish or mutate its results."""
    def __init__(self, view, *, max_deltas=2):
        if view.published is not True:
            raise DerivedCutMismatch("Construct a derived cache on a published ReadView")
        if type(max_deltas) is not int or max_deltas < 1:
            raise ValueError("max_deltas must be a positive exact integer")
        self._owner = get_ident()
        self._incarnation = uuid4().hex
        empty = MappingProxyType({})
        cut = _Cut.read(view)
        self._state = _CacheState(cut, cut.index_revision, empty, empty)
        self._definitions = empty
        self._definition_catalog = ()
        self._deltas = {}
        self._max_deltas = max_deltas
        self._prevalidated = None

    @classmethod
    def rebuild(cls, view, definitions, *, budget=0):
        """Fresh runtime authority on the exact restored published graph cut.

        Batch catalog installation invokes no query/command callback by default.
        No entries, old tickets, reservations or query scopes are imported.
        Optional pure recompute is bounded and completes before this cache escapes.
        """
        if (type(definitions) is not tuple
                or any(type(definition) is not QueryDefinition for definition in definitions)):
            raise TypeError("Rebuild requires an immutable trusted QueryDefinition tuple")
        if len({definition.key for definition in definitions}) != len(definitions):
            raise ValueError("Duplicate query definition in rebuild catalog")
        budget = RecomputeBudget(budget) if type(budget) is int else budget
        if type(budget) is not RecomputeBudget:
            raise TypeError("Use an exact query count or RecomputeBudget")
        cache = cls(view)
        empty = ReadSet(cache._cut.epoch, cache._cut.schema_manifest)
        entries = {definition.key: DerivedEntry(
            definition.key, QueryVersion(cache.incarnation, 1), "pending", empty, empty)
            for definition in definitions}
        cache._definitions = MappingProxyType({definition.key: definition.compute for definition in definitions})
        cache._definition_catalog = definitions
        evaluator = _Evaluator(view, entries, cache._definitions, budget)
        for key in entries:
            if entries[key].status != "current":
                try:
                    evaluator.compute(key)
                except (QueryPending, DerivedQueryFailed):
                    pass
        cache._state = _CacheState(cache._cut, view.index_revision, MappingProxyType(entries),
                                   MappingProxyType({}))
        return cache

    @property
    def incarnation(self):
        return self._incarnation

    def export_definitions(self):
        """Read-only frozen trusted catalog; contains no results or runtime scope."""
        return self._definition_catalog

    def _write_check(self):
        if get_ident() != self._owner:
            raise WriterOwnershipError("Only the coordinator owner may change a derived cache")

    @property
    def _cut(self):
        return self._state.cut

    @property
    def _revision(self):
        return self._state.revision

    @property
    def _entries(self):
        return self._state.entries

    @property
    def _tickets(self):
        return self._state.tickets

    def _unreserved(self):
        if self._prevalidated is not None:
            raise DerivedDeltaRejected("Publish or discard the reserved derived publication first")

    @property
    def sequence(self):
        return self._cut.sequence

    @property
    def revision(self):
        return self._revision

    @property
    def entries(self):
        return self._entries

    @property
    def generations(self):
        return MappingProxyType(_generations(self._entries))

    def validate_priority_keys(self, keys):
        """Trusted fixed wiring; queries may fail later without vetoing C1 control."""
        if type(keys) is not tuple or any(type(key) is not QueryKey for key in keys):
            raise TypeError("priority_keys must be an exact immutable QueryKey tuple")
        if len(keys) > PRIORITY_QUERY_LIMITS.max_keys:
            raise ValueError("Priority closure key quota exceeded")
        if len(set(keys)) != len(keys) or any(key not in self._definitions for key in keys):
            raise ValueError("Priority keys must be unique known definitions")
        return keys

    def register(self, key, compute):
        """Trusted definition registration, not user/model executable code."""
        self._write_check()
        self._unreserved()
        definition = QueryDefinition(key, compute)
        if key in self._definitions:
            raise ValueError("Query definition is already registered")
        empty = ReadSet(self._cut.epoch, self._cut.schema_manifest)
        entries = dict(self._entries)
        entries[key] = DerivedEntry(key, QueryVersion(self.incarnation, 1), "pending", empty, empty)
        self._state = _CacheState(self._cut, self._revision + 1, MappingProxyType(entries), self._tickets)
        definitions = dict(self._definitions)
        definitions[key] = compute
        self._definitions = MappingProxyType(definitions)
        self._definition_catalog = (*self._definition_catalog, definition)

    def result(self, key, *, tracker=None):
        """Pending/error never exposes an old result as current."""
        with tracker.operation() if tracker is not None else nullcontext():
            state = self._state
            if tracker is not None and _Cut.read(tracker._reader) != state.cut:
                _unresolved(state.entries, key, tracker, "cut_mismatch")
                raise DerivedCutMismatch("Tracked query access requires the same graph/cache cut")
            return _result(state.entries, key, tracker)
    def start(self, key, view):
        """Create one active worker ticket; a later start supersedes its nonce."""
        self._write_check()
        self._unreserved()
        if view.published is not True or _Cut.read(view) != self._cut:
            raise DerivedCutMismatch("Workers start on the current published cache cut")
        entry = self._entries[key]
        if entry.status == "current":
            raise ValueError("A current query must first be invalidated in a prepared cut")
        reader = view.lease()
        ticket = QueryTicket(key, entry.version, self._cut.epoch,
                             self._cut.sequence, self._cut.schema_manifest, uuid4().hex)
        work = QueryWork(ticket, reader, self._definitions[key], self._entries)
        tickets = dict(self._tickets)
        tickets[key] = ticket
        self._state = _CacheState(self._cut, self._revision + 1, self._entries, MappingProxyType(tickets))
        return work

    def cancel(self, work):
        """Owner cancels a job/lease; a finished completion remains caller-owned.

        There is at most one active nonce per registered query. Work owns its
        lease until run()/close()/cancel(); StateStore also bounds all leases/TTL.
        Superseding a nonce does not claim to stop an already-running function.
        """
        self._write_check()
        self._unreserved()
        if type(work) is not QueryWork:
            raise TypeError("Expected a QueryWork issued to the caller")
        active = self._tickets.get(work.ticket.key) == work.ticket
        if active:
            tickets = dict(self._tickets)
            del tickets[work.ticket.key]
            self._state = _CacheState(self._cut, self._revision + 1, self._entries,
                                      MappingProxyType(tickets))
        work.close()
        return active

    def prepare(self, reader, *, budget=None, completions=(), invalidate=(), priority_keys=()):
        """Full invalidation, bounded priority sync, then ordinary optional work.

        Priority query Error/Pending is a coherent candidate, not a C1 revoke or
        cancel veto. Export readiness is a separate required-key gate.

        reader must be StateStore.read_staged(stage). Workers/completions are
        admitted into this candidate only. C2 must discard it on graph rollback.
        """
        self._write_check()
        self._unreserved()
        priority_keys = self.validate_priority_keys(priority_keys)
        priority = frozenset(priority_keys)
        if reader.published is not False:
            raise DerivedCutMismatch("Prepare derived results on an unpublished staged reader")
        cut = _Cut.read(reader)
        if (cut.epoch != self._cut.epoch or cut.schema_manifest != self._cut.schema_manifest
                or cut.sequence != self._cut.sequence + 1):
            raise DerivedCutMismatch("Rebuild required for another epoch/schema or skipped cut")
        if len(self._deltas) >= self._max_deltas:
            raise DerivedDeltaRejected("Prepared derived delta quota exceeded")
        budget = RecomputeBudget() if budget is None else budget
        if type(budget) is not RecomputeBudget or type(completions) is not tuple:
            raise TypeError("Expected a RecomputeBudget and immutable completion tuple")
        entries, tickets = dict(self._entries), dict(self._tickets)
        generations = _generations(entries)
        dirty = set(invalidate)
        if any(key not in entries for key in dirty):
            raise KeyError("Cannot invalidate an unknown query")
        dirty.update(key for key, entry in entries.items()
                     if not entry.watch_set.matches_inputs(reader, query_versions=generations))
        # Newly configured priority roots cannot reuse an optional/open or heavy
        # previous result merely because its ordinary input versions still match.
        for key in priority_keys:
            entry = entries[key]
            if entry.status != "current":
                continue
            closed = (entry.priority_evaluated and entry.read_set.complete
                      and all(dep.key in priority and entries[dep.key].status == "current"
                              and entries[dep.key].priority_evaluated
                              for dep in entry.read_set.queries))
            try:
                PRIORITY_QUERY_LIMITS.quota().value(entry.value)
                light = True
            except QueryQuotaExceeded:
                light = False
            if not closed or not light:
                dirty.add(key)
        # The closure is unbudgeted, including pending/error nodes and query edges.
        while True:
            dependents = {key for key, entry in entries.items()
                          if any(dep.key in dirty for dep in entry.watch_set.queries)}
            expanded = dirty | dependents
            if expanded == dirty:
                break
            dirty = expanded
        for key in dirty:
            previous = entries[key]
            entries[key] = DerivedEntry(key, previous.version.advanced(), "pending",
                                        previous.read_set, previous.watch_set)
            tickets.pop(key, None)
        generations = _generations(entries)
        for key, entry in tuple(entries.items()):
            watch = entry.watch_set.rebased(reader, query_versions=generations)
            entries[key] = DerivedEntry(key, entry.version, entry.status,
                                        entry.read_set, watch, entry.value, entry.error,
                                        priority_evaluated=entry.priority_evaluated)

        # Mandatory sync pass has its own closed, bounded budget. Revoking a
        # query ticket does not mutate published runtime state before publication.
        for key in priority:
            tickets.pop(key, None)
        mandatory = _PriorityEvaluator(reader, entries, self._definitions, priority_keys)
        for key in priority_keys:
            try:
                mandatory.compute(key)
            except (QueryPending, DerivedQueryFailed):
                pass
        # Failure must not leave any successful dependent value current. Already
        # pending/error nodes keep their generation on unrelated future cuts.
        failed = {key for key in priority if entries[key].status != "current"}
        closure = set(failed)
        while True:
            expanded = closure | {key for key, entry in entries.items()
                                  if any(dep.key in closure for dep in entry.watch_set.queries)}
            if expanded == closure:
                break
            closure = expanded
        for key in closure - failed:
            entry = entries[key]
            if entry.status == "current":
                dirty.add(key)
                entries[key] = DerivedEntry(key, entry.version.advanced(), "pending",
                                            entry.read_set, entry.watch_set)
                tickets.pop(key, None)
        generations = _generations(entries)
        for key, entry in tuple(entries.items()):
            entries[key] = DerivedEntry(key, entry.version, entry.status, entry.read_set,
                entry.watch_set.rebased(reader, query_versions=generations), entry.value, entry.error,
                priority_evaluated=entry.priority_evaluated)

        accepted, rejected = [], []
        for completion in completions:
            if type(completion) is not QueryCompletion:
                raise TypeError("Expected immutable QueryCompletion values")
            ticket = completion.ticket
            reason = None
            if ticket.key not in entries:
                reason = "unknown_query"
            elif ticket.version.cache_incarnation != self.incarnation:
                reason = "cache_incarnation"
            elif ticket.key in priority:
                reason = "priority_sync"
            elif tickets.get(ticket.key) != ticket:
                reason = "inactive_ticket"
            elif ticket.version != entries[ticket.key].version:
                reason = "input_generation"
            elif ticket.epoch != cut.epoch or ticket.schema_manifest != cut.schema_manifest:
                reason = "epoch_schema"
            elif (completion.read_set.epoch != ticket.epoch
                  or completion.read_set.schema_manifest != ticket.schema_manifest):
                reason = "worker_basis"
            elif not completion.read_set.complete:
                reason = "unresolved_query"
            elif not completion.read_set.matches(reader, query_versions=_generations(entries)):
                reason = "changed_dependencies"
            if reason is not None:
                rejected.append(CompletionRejection(ticket, reason))
                continue
            entry = entries[ticket.key]
            entries[ticket.key] = DerivedEntry(ticket.key, entry.version, "current",
                                              completion.read_set, completion.read_set, completion.value)
            tickets.pop(ticket.key, None)
            accepted.append(ticket.key)

        evaluator = _OptionalEvaluator(reader, entries, self._definitions, budget, priority)
        for key in entries:
            if key not in priority and entries[key].status != "current":
                try:
                    evaluator.compute(key)
                except (QueryPending, DerivedQueryFailed):
                    pass
        # Any sync recompute supersedes worker work for that query.
        for key in evaluator.recomputed:
            tickets.pop(key, None)
        token = uuid4().hex
        delta = DerivedDelta(cut, self._revision, MappingProxyType(entries),
                             frozenset(dirty), tuple((*mandatory.recomputed, *evaluator.recomputed)), tuple(accepted),
                             tuple(rejected), token, MappingProxyType(tickets))
        self._deltas[token] = delta
        return delta

    def prevalidate(self, delta, staged_reader):
        """Complete preflight/reservation before StateStore.publish.

        C2 must keep its exclusive phase guard from here through cache.publish.
        A rollback calls discard(delta); completion inputs are not consumed.
        """
        self._write_check()
        self._unreserved()
        if (type(delta) is not DerivedDelta or self._deltas.get(delta._token) is not delta
                or delta.base_revision != self._revision):
            raise DerivedDeltaRejected("Foreign, consumed or stale derived delta")
        if staged_reader.published is not False or _Cut.read(staged_reader) != delta.cut:
            raise DerivedDeltaRejected("Derived delta does not match the live staged graph cut")
        generations = _generations(delta.entries)
        for entry in delta.entries.values():
            if entry.status == "current":
                if (not entry.read_set.matches(staged_reader, query_versions=generations)
                        or any(delta.entries[dep.key].status != "current"
                               for dep in entry.read_set.queries)):
                    raise DerivedDeltaRejected("Derived candidate contains invalid current dependencies")
            elif entry.status not in ("pending", "error") or entry.value is not None:
                raise DerivedDeltaRejected("Derived candidate exposes a non-current old result")
        state = _CacheState(delta.cut, self._revision + 1, delta.entries,
                            MappingProxyType(dict(delta._tickets)))
        publication = PrevalidatedDerivedDelta(delta, state)
        self._prevalidated = publication
        return publication

    def publish(self, publication, published_view):
        """Only swap preallocated references, inside C2 Publishing before notify.

        The coordinator guarantees published_view is the graph cut prevalidated
        above. Deliberately no reader access, quota allocation, callbacks or
        ordinary conflict checks occur after graph publication. Breaking this
        owner/reservation contract is a catastrophic coordinator invariant error.
        """
        if (type(publication) is not PrevalidatedDerivedDelta
                or get_ident() != self._owner or self._prevalidated is not publication
                or self._revision != publication.delta.base_revision):
            raise DerivedPublicationInvariantError("Broken prevalidated publication reservation")
        self._state = publication._state  # one immutable graph/cache-cut pointer
        self._prevalidated = None
        self._deltas.clear()

    def discard(self, delta):
        self._write_check()
        if type(delta) is not DerivedDelta or self._deltas.get(delta._token) is not delta:
            raise DerivedDeltaRejected("Foreign, consumed or stale derived delta")
        if self._prevalidated is not None and self._prevalidated.delta is delta:
            self._prevalidated = None
        del self._deltas[delta._token]
