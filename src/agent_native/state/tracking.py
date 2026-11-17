"""Invocation-local managed reads over a published or staged C1 reader.

No graph data is copied here. Tracking borrows the reader, never extends its
lifetime, and checks versions without observing new reads. C2 owns admission,
permissions, write-target checks and the publication phase guard.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps

from agent_native.schema import CollectionRef, Ref
from agent_native.schema.types import valid_type_id
from .lifetime import ReadViewUnavailable
from .query import QueryVersion
from .records import FrozenList, FrozenMap, Record
from .snapshots import ReadEvent

Dependency = ReadEvent


def _guarded_read(method):
    """Guard a whole managed operation, without adding any reader/clock calls."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._operation():
            return method(self, *args, **kwargs)
    return guarded


class QueryQuotaExceeded(RuntimeError):
    """Recoverable pure-query work/output limit, never a publication success."""
    pass


class ReadQuota:
    """Invocation-local logical work meter for trusted lightweight producers.

    This bounds managed reads, visited immutable input/output nodes, collection
    sizes and physical scan preflight. It is not a Python CPU/security sandbox.
    """
    def __init__(self, *, max_reads, max_items, max_matching_refs, max_nodes, max_bytes, max_depth):
        self.reads = max_reads
        self.nodes = max_nodes
        self.bytes = max_bytes
        self.max_items, self.max_matching_refs, self.max_depth = max_items, max_matching_refs, max_depth

    def read(self):
        self.reads -= 1
        if self.reads < 0:
            raise QueryQuotaExceeded("managed read quota exceeded")

    def scan(self, reader, entity_type, partition):
        # Internal typed cardinality preflight, no copy/export. This remains O(N)
        # like existing C1 entities(), not a CPU/time cap or a new type index.
        try:
            valid_type_id(entity_type)
        except ValueError:
            raise QueryQuotaExceeded("priority enumeration requires an explicit entity type") from None
        if partition not in (None, "durable", "memory"):
            raise QueryQuotaExceeded("priority enumeration requires a valid partition")
        matches = 0
        for key, record in reader._data().records.items():
            if key.entity_type == entity_type and (partition is None or record.partition == partition):
                matches += 1
                if matches > self.max_matching_refs:
                    raise QueryQuotaExceeded("typed matching-ref quota exceeded")

    def value(self, value):
        active = set()
        def visit(item, depth):
            self.nodes -= 1
            if self.nodes < 0 or depth > self.max_depth:
                raise QueryQuotaExceeded("value node/depth quota exceeded")
            if type(item) is str:
                if len(item) > self.bytes:
                    raise QueryQuotaExceeded("value byte quota exceeded")
                self.bytes -= len(item.encode("utf-8"))
            elif item is None or type(item) is bool:
                self.bytes -= 1
            elif type(item) is int:
                self.bytes -= max(1, (item.bit_length() + 7) // 8)
            elif type(item) is float:
                self.bytes -= 8
            elif type(item) is Ref:
                self.bytes -= len(item.key.entity_type) + len(item.key.entity_id) + 8
            elif type(item) is CollectionRef:
                self.bytes -= len(item.owner.entity_type) + len(item.owner.entity_id) + len(item.field) + 8
            elif type(item) is Record:
                visit(item.values, depth + 1)
            elif type(item) in (dict, FrozenMap, list, tuple, FrozenList):
                if len(item) > self.max_items:
                    raise QueryQuotaExceeded("collection item quota exceeded")
                identity = id(item)
                if identity in active:
                    raise ValueError("Cyclic query value")
                active.add(identity)
                if type(item) in (dict, FrozenMap):
                    for key, child in item.items():
                        visit(key, depth + 1)
                        visit(child, depth + 1)
                else:
                    for child in item:
                        visit(child, depth + 1)
                active.remove(identity)
            else:
                raise TypeError("Unsupported query value")
            if self.bytes < 0:
                raise QueryQuotaExceeded("value byte quota exceeded")
        visit(value, 0)


class TrackingClosed(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FieldRef:
    ref: Ref
    name: str

    def __post_init__(self):
        if type(self.ref) is not Ref or type(self.name) is not str or not self.name:
            raise TypeError("FieldRef requires a schema Ref and a non-empty field name")


@dataclass(frozen=True, slots=True)
class QueryDependency:
    key: object
    generation: QueryVersion

    def __post_init__(self):
        if type(self.generation) is not QueryVersion:
            raise TypeError("Query dependencies require a scoped QueryVersion")


@dataclass(frozen=True, slots=True)
class UnresolvedQueryRead:
    """An invocation saw no current query value, even if author caught the error."""
    key: object
    outcome: str
    generation: QueryVersion | None = None

    def __post_init__(self):
        if self.generation is not None and type(self.generation) is not QueryVersion:
            raise TypeError("Known unresolved query inputs require a scoped QueryVersion")


@dataclass(frozen=True, slots=True)
class ReadSet:
    epoch: str
    schema_manifest: tuple
    events: tuple[ReadEvent, ...] = ()
    queries: tuple[QueryDependency, ...] = ()
    policy_revision: int | None = None
    index_revision: int | None = None
    sequence: int | None = None
    unresolved: tuple[UnresolvedQueryRead, ...] = ()

    def __post_init__(self):
        if (type(self.epoch) is not str or type(self.schema_manifest) is not tuple
                or type(self.events) is not tuple or any(type(e) is not ReadEvent for e in self.events)
                or type(self.queries) is not tuple
                or any(type(q) is not QueryDependency for q in self.queries)
                or type(self.unresolved) is not tuple
                or any(type(q) is not UnresolvedQueryRead for q in self.unresolved)):
            raise TypeError("ReadSet requires immutable snapshot identity and dependencies")

    @property
    def complete(self):
        return not self.unresolved

    def matches(self, reader, *, query_versions=None):
        """Current admission: caught unresolved reads never become a current value."""
        return self.complete and self.matches_inputs(reader, query_versions=query_versions)

    def matches_inputs(self, reader, *, query_versions=None):
        """Watch-token comparison only; this does NOT authorize a current result.

        Unresolved status is invocation-local admission information, not a version
        that invalidates the same pending node on every unrelated publication.
        """
        try:
            if reader.epoch != self.epoch or reader.schema_manifest != self.schema_manifest:
                return False
            for name in ("policy_revision", "index_revision", "sequence"):
                expected = getattr(self, name)
                if expected is not None and getattr(reader, name) != expected:
                    return False
            if any(reader.version(event) != event.version for event in self.events):
                return False
            if self.queries:
                if query_versions is None:
                    return False
                return all(query_versions.get(dep.key) == dep.generation for dep in self.queries)
            return True
        except ReadViewUnavailable:
            return False

    def rebased(self, reader, *, query_versions=None):
        """Watch tokens for a dirty result; never makes its old value current.

        Deleted field/record tokens are covered by their retained existence token.
        This prevents an already-dirty result from changing generation on every
        unrelated cut while a worker is recomputing it.
        """
        events = []
        for event in self.events:
            version = reader.version(event)
            if version is not None:
                events.append(ReadEvent(event.kind, event.key, event.field, version,
                                        event.entity_type, event.partition))
        queries = []
        for dep in self.queries:
            current = None if query_versions is None else query_versions.get(dep.key)
            # Rebase only inside the same runtime cache. Never turn an old
            # dependency into a fresh one by reusing counter=1 after recovery.
            if (type(current) is QueryVersion
                    and current.cache_incarnation == dep.generation.cache_incarnation):
                queries.append(QueryDependency(dep.key, current))
            else:
                queries.append(dep)  # retain a fail-closed missing/foreign scope
        queries = tuple(queries)
        return ReadSet(reader.epoch, reader.schema_manifest, tuple(events), queries,
                       None if self.policy_revision is None else reader.policy_revision,
                       None if self.index_revision is None else reader.index_revision,
                       None if self.sequence is None else reader.sequence)


class ReadTracker:
    """One synchronous invocation. The supplied reader remains caller-owned.

    Managed operations collect the same public ReadEvent/version tokens as C1.
    This also works with StagedReadView, which deliberately cannot mint a lease.
    """
    def __init__(self, reader, *, query=None, quota=None):
        if quota is not None and type(quota) is not ReadQuota:
            raise TypeError("Use an engine ReadQuota")
        self._quota = quota
        self._reader = reader
        self._epoch = reader.epoch
        self._manifest = reader.schema_manifest
        self._events = {}
        self._queries = {}
        self._unresolved = {}
        self._metadata = {}
        self._closed = False
        self._entered = False
        self._query = query
        self.view = ManagedView(self)

    def _reader_unavailable(self):
        # Latching must not read/check an already-expired lease.
        event = UnresolvedQueryRead(None, "reader_unavailable")
        self._unresolved.setdefault((None, event.outcome, None), event)

    @contextmanager
    def operation(self):
        """Capture expiry across actual reads AND subsequent dependency capture."""
        try:
            yield
        except ReadViewUnavailable:
            self._reader_unavailable()
            raise
        except QueryQuotaExceeded:
            self.unresolved_query(None, "query_quota")
            raise

    def _check(self):
        if self._closed:
            raise TrackingClosed("Managed read scope is closed")
        try:
            same_identity = (self._reader.epoch == self._epoch
                             and self._reader.schema_manifest == self._manifest)
        except ReadViewUnavailable:
            self._reader_unavailable()
            raise
        if not same_identity:
            raise ValueError("A read scope cannot mix snapshot identities")

    def _observe(self, event):
        self._check()
        if self._quota is not None:
            with self.operation():
                self._quota.read()
        identity = (event.kind, event.key, event.field, event.entity_type, event.partition)
        previous = self._events.setdefault(identity, event)
        if previous.version != event.version:
            raise ValueError("A read scope cannot mix input versions")

    def _token(self, kind, key=None, field=None, entity_type=None, partition=None):
        with self.operation():
            event = ReadEvent(kind, key, field, 0, entity_type, partition)
            version = self._reader.version(event)
            if version is None:
                raise ValueError("Cannot capture an absent field/record token")
            self._observe(ReadEvent(kind, key, field, version, entity_type, partition))

    def _value(self, value):
        if self._quota is not None:
            with self.operation():
                self._quota.value(value)
        return value

    def _scan(self, entity_type, partition):
        if self._quota is not None:
            with self.operation():
                self._quota.scan(self._reader, entity_type, partition)

    def depend_on_query(self, key, generation):
        self._check()
        if self._quota is not None:
            with self.operation():
                self._quota.read()
        previous = self._queries.setdefault(key, QueryDependency(key, generation))
        if previous.generation != generation:
            raise ValueError("A read scope cannot mix query generations")

    def unresolved_query(self, key, outcome, generation=None):
        if self._closed:
            raise TrackingClosed("Managed read scope is closed")
        event = UnresolvedQueryRead(key, outcome, generation)
        self._unresolved.setdefault((key, outcome, generation), event)

    def include(self, read_set):
        self._check()
        if read_set.epoch != self._epoch or read_set.schema_manifest != self._manifest:
            raise ValueError("Cannot import dependencies from another epoch/schema")
        for event in read_set.events:
            self._observe(event)
        for dep in read_set.queries:
            self.depend_on_query(dep.key, dep.generation)
        for unresolved in read_set.unresolved:
            self.unresolved_query(unresolved.key, unresolved.outcome, unresolved.generation)
        for name in ("policy_revision", "index_revision", "sequence"):
            value = getattr(read_set, name)
            if value is not None:
                self._metadata_token(name, value)

    def _metadata_token(self, name, value):
        self._check()
        previous = self._metadata.setdefault(name, value)
        if previous != value:
            raise ValueError("A read scope cannot mix metadata versions")
        return value

    @property
    def read_set(self):
        return ReadSet(self._epoch, self._manifest, tuple(self._events.values()),
                       tuple(self._queries.values()), unresolved=tuple(self._unresolved.values()),
                       **self._metadata)

    def close(self):
        self._closed = True

    def __enter__(self):
        self._check()
        if self._entered:
            raise RuntimeError("A ReadTracker is single-use")
        self._entered = True
        return self

    def __exit__(self, *error):
        self.close()


class ManagedRecord:
    """A Ref plus accessor scope, never an untracked Record/DTO."""
    __slots__ = ("_view", "ref")

    def __init__(self, view, ref):
        object.__setattr__(self, "_view", view)
        object.__setattr__(self, "ref", ref)

    def __setattr__(self, name, value):
        raise AttributeError("Managed records are read-only")

    def field(self, name):
        return self._view.field(self.ref, name)

    def field_ref(self, name):
        return FieldRef(self.ref, name)

    def related(self, name):
        return self._view.related(self.ref, name)

    def __getitem__(self, name):
        return self.field(name)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self.field(name)


class ManagedCollection:
    """A source identity resolved through the scope's pinned reader."""
    __slots__ = ("_view", "ref")

    def __init__(self, view, ref):
        object.__setattr__(self, "_view", view)
        object.__setattr__(self, "ref", ref)

    def _operation(self):
        return self._view._tracker.operation()

    def __setattr__(self, name, value):
        raise AttributeError("Managed collections are read-only")

    @property
    @_guarded_read
    def members(self):
        tracker = self._view._tracker
        tracker._check()
        owner = Ref(self.ref.owner)
        self._view.exists(owner)  # retain absence even if resolution raises
        if tracker._quota is not None:
            self._view.field(owner, self.ref.field)  # bound source before C1 schema traversal
        source = tracker._reader.collection(owner, self.ref.field)
        tracker._token("collection", self.ref.owner, self.ref.field)
        # Read the source field without introducing an unrelated whole-record read.
        members = tracker._reader.field(owner, self.ref.field)
        for member in members:
            self._view.exists(member)
        return source.members  # C1 enforces pinned schema/generation and dangling errors

    @property
    @_guarded_read
    def version(self):
        self.members
        return self._view._tracker._reader.version(
            ReadEvent("collection", self.ref.owner, self.ref.field, 0))

    @_guarded_read
    def records(self):
        return tuple(self._view.record(member) for member in self.members)

    @_guarded_read
    def __iter__(self):
        return iter(self.members)

    @_guarded_read
    def __len__(self):
        return len(self.members)

    @_guarded_read
    def __getitem__(self, index):
        return self.members[index]


class ManagedView:
    __slots__ = ("_tracker",)

    def __init__(self, tracker):
        self._tracker = tracker

    def _operation(self):
        return self._tracker.operation()

    @property
    @_guarded_read
    def epoch(self):
        self._tracker._check()
        return self._tracker._epoch

    @property
    @_guarded_read
    def schema_manifest(self):
        self._tracker._check()
        return self._tracker._manifest

    @property
    @_guarded_read
    def policy_revision(self):
        return self._metadata("policy_revision")

    @property
    @_guarded_read
    def index_revision(self):
        return self._metadata("index_revision")

    @property
    @_guarded_read
    def sequence(self):
        return self._metadata("sequence")

    @_guarded_read
    def _metadata(self, name):
        self._tracker._check()
        return self._tracker._metadata_token(name, getattr(self._tracker._reader, name))

    @_guarded_read
    def exists(self, ref):
        self._tracker._check()
        result = self._tracker._reader.exists(ref)
        self._tracker._token("existence", ref.key)
        return result

    @_guarded_read
    def record(self, ref):
        if type(ref) is not Ref:
            raise TypeError("ManagedRecord requires a schema Ref")
        self._tracker._check()
        return ManagedRecord(self, ref)

    @_guarded_read
    def field(self, ref, name):
        self.exists(ref)
        value = self._tracker._reader.field(ref, name)
        self._tracker._value(value)
        self._tracker._token("field", ref.key, name)
        return value

    @_guarded_read
    def read(self, field_ref):
        if type(field_ref) is not FieldRef:
            raise TypeError("Expected a FieldRef")
        return self.field(field_ref.ref, field_ref.name)

    @_guarded_read
    def resolve(self, ref):
        """Explicit whole-record dependency; normal UI uses record(ref).field."""
        self.exists(ref)
        record = self._tracker._reader.resolve(ref)
        self._tracker._value(record)
        self._tracker._token("record", ref.key)
        return record

    @_guarded_read
    def entities(self, entity_type, partition=None):
        self._tracker._check()
        self._tracker._scan(entity_type, partition)
        refs = self._tracker._reader.entities(entity_type, partition)
        self._tracker._value(refs)
        self._tracker._token("membership", entity_type=entity_type, partition=partition)
        return refs

    @_guarded_read
    def collection(self, owner, field):
        self.exists(owner)
        if self._tracker._quota is not None:
            self.field(owner, field)  # bounded direct source before C1 member validation
        source = self._tracker._reader.collection(owner, field)
        self._tracker._token("collection", owner.key, field)
        return ManagedCollection(self, source.ref)

    @_guarded_read
    def resolve_collection(self, ref):
        if type(ref) is not CollectionRef:
            raise TypeError("Expected a schema CollectionRef")
        return self.collection(Ref(ref.owner), ref.field)

    @_guarded_read
    def related(self, owner, field):
        relation = self.field(owner, field)  # alias retarget is a separate dependency
        if type(relation) is CollectionRef:
            return self.resolve_collection(relation)
        if type(relation) is tuple and all(type(member) is Ref for member in relation):
            return self.collection(owner, field)
        raise TypeError("related requires an Owned or ResourceCollection relation")

    @_guarded_read
    def query(self, key):
        self._tracker._check()
        if self._tracker._query is None:
            self._tracker.unresolved_query(key, "no_resolver")
            raise RuntimeError("This scope has no derived-query resolver")
        try:
            return self._tracker._query(key, self._tracker)
        except Exception:
            # Any failed resolution produced no current query value. Engine
            # resolvers add the precise pending/error/missing/stale outcome too.
            self._tracker.unresolved_query(key, "unavailable")
            raise

    @_guarded_read
    def where(self, entity_type, predicate, *, partition=None):
        from .query import where
        return where(self, entity_type, predicate, partition=partition)

    @_guarded_read
    def range(self, entity_type, field, **options):
        from .query import range_query
        return range_query(self, entity_type, field, **options)
