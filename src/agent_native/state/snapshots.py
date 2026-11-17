"""Trusted C1 staging/publication and managed immutable reads, without a coordinator."""
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from sys import getsizeof
from threading import get_ident
from time import monotonic_ns
from types import MappingProxyType
from uuid import uuid4

from agent_native.schema import CollectionRef, EntityKey, Ref
from .records import FrozenMap, FrozenList, Record, _detach_frozen, deep_freeze, strict_equal, thaw
from .lifetime import ReadViewUnavailable, SnapshotLimits, _SnapshotPool


class EntityUnavailable(LookupError):
    pass


class CollectionSchemaError(ValueError):
    pass


class StageRejected(RuntimeError):
    pass


class WriterOwnershipError(RuntimeError):
    pass


class SnapshotRestoreError(ValueError):
    """Malformed/incompatible accepted snapshot; restoration never falls back to empty."""


@dataclass(frozen=True, slots=True)
class Create:
    entity_type: str
    entity_id: str
    values: dict
    partition: str = "durable"


@dataclass(frozen=True, slots=True)
class Update:
    ref: Ref
    changes: dict


@dataclass(frozen=True, slots=True)
class Patch:
    creates: tuple[Create, ...] = ()
    updates: tuple[Update, ...] = ()
    deletes: tuple[Ref, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadEvent:
    kind: str
    key: EntityKey | None
    field: str | None
    version: int
    entity_type: str | None = None
    partition: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotData:
    epoch: str
    sequence: int
    records: object
    generations: object
    generation_partitions: object
    existence_versions: object
    field_versions: object
    collection_versions: object
    membership_versions: object
    collection_sources: object
    schema_manifest: tuple
    policy_revision: int
    index_revision: int


@dataclass(frozen=True, slots=True)
class StagedSnapshot:
    snapshot: SnapshotData
    base_sequence: int
    _store_id: str
    _token: str


def _key(ref):
    if type(ref) is not Ref:
        raise TypeError("Managed reads/writes require a schema Ref")
    return ref.key


def _partition(partition, *, optional=False):
    if partition is None and optional:
        return None
    if type(partition) is not str or partition not in ("durable", "memory"):
        raise ValueError("Unknown semantic partition; expected an exact partition string")
    return partition


def _logical_size(snapshot):
    """Deterministic runtime logical size, deduplicated within each snapshot only."""
    seen = set()
    def size(value):
        identity = id(value)
        if identity in seen:
            return 0
        seen.add(identity)
        result = getsizeof(value)
        if type(value) in (dict, MappingProxyType, FrozenMap):
            if type(value) is not dict:
                result += getsizeof(dict(value))  # include the owned backing map
            result += sum(size(k) + size(v) for k, v in value.items())
        elif type(value) in (tuple, list, FrozenList):
            if type(value) is FrozenList:
                result += getsizeof(value._items)  # include the owned backing tuple
            result += sum(size(item) for item in value)
        elif type(value) is Record:
            result += sum(size(item) for item in (value.key, value.partition, value.values, value.version))
        elif type(value) is Ref:
            result += size(value.key)
        elif type(value) is CollectionRef:
            result += size(value.owner) + size(value.field)
        elif type(value) is EntityKey:
            result += sum(size(item) for item in (value.entity_type, value.entity_id, value.generation))
        elif type(value) is SnapshotData:
            result += sum(size(getattr(value, name)) for name in value.__dataclass_fields__)
        return result
    return size(snapshot)


class CollectionView(Sequence):
    """One source collection resolved in a particular published or staged reader.

    The Ref identity has no members/revision. Traversal uses the pinned reader and
    its lifetime, never current head. An extracted immutable member tuple may outlive
    the reader just like any other immutable semantic value.
    """
    __slots__ = ("_reader", "ref")

    def __init__(self, reader, ref):
        object.__setattr__(self, "_reader", reader)
        object.__setattr__(self, "ref", ref)

    def __setattr__(self, name, value):
        raise AttributeError("CollectionView is read-only")

    def __delattr__(self, name):
        raise AttributeError("CollectionView is read-only")

    @property
    def members(self):
        return self._reader._collection_members(self.ref)

    @property
    def version(self):
        return self._reader._collection_state(self.ref)[2]

    def __len__(self):
        return len(self.members)

    def __iter__(self):
        return iter(self.members)

    def __getitem__(self, index):
        return self.members[index]


class _Reader:
    """Shared managed read operations; published and staged handles stay distinct."""
    __slots__ = ()

    def __init__(self, pool, token, expires, observer=None):
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_observer", observer)
        object.__setattr__(self, "expires_at_ns", expires)

    def __setattr__(self, name, value):
        raise AttributeError("ReadView is read-only")

    def __delattr__(self, name):
        raise AttributeError("ReadView is read-only")

    def _data(self):
        return self._pool.get(self._token)

    def _event(self, kind, key, field, version, entity_type=None, partition=None):
        if self._observer is not None:
            self._observer(ReadEvent(kind, key, field, version, entity_type, partition))

    @property
    def available(self):
        try:
            self._data()
            return True
        except ReadViewUnavailable:
            return False

    @property
    def epoch(self):
        return self._data().epoch

    @property
    def sequence(self):
        return self._data().sequence

    @property
    def schema_manifest(self):
        return self._data().schema_manifest

    @property
    def policy_revision(self):
        return self._data().policy_revision

    @property
    def index_revision(self):
        return self._data().index_revision

    def exists(self, ref):
        key, data = _key(ref), self._data()
        self._event("existence", key, None, data.existence_versions.get(key, 0))
        return key in data.records

    def existence_version(self, ref):
        return self._data().existence_versions.get(_key(ref), 0)

    def _record(self, data, key):
        self._event("existence", key, None, data.existence_versions.get(key, 0))
        try:
            return data.records[key]
        except KeyError as error:
            raise EntityUnavailable(f"Entity generation is absent: {key!r}") from error

    def resolve(self, ref):
        key, data = _key(ref), self._data()
        record = self._record(data, key)
        self._event("record", key, None, record.version)
        return record

    def field(self, ref, name):
        key, data = _key(ref), self._data()
        record = self._record(data, key)
        value = record.values[name]
        self._event("field", key, name, data.field_versions[key, name])
        return value

    def _collection_state(self, ref):
        if type(ref) is not CollectionRef:
            raise TypeError("Expected a schema CollectionRef identity")
        data = self._data()
        target = data.collection_sources.get((ref.owner.entity_type, ref.field))
        if target is None:
            raise CollectionSchemaError("Collection source must be a declared direct non-nullable Owned field")
        record = self._record(data, ref.owner)
        members = record.values.get(ref.field)
        if (type(members) is not tuple or any(type(member) is not Ref
                or member.key.entity_type != target for member in members)):
            raise CollectionSchemaError("Collection members do not match their pinned source schema")
        version = data.collection_versions.get((ref.owner, ref.field))
        if version is None:
            raise CollectionSchemaError("Collection source has no membership version")
        self._event("collection", ref.owner, ref.field, version)
        return data, members, version

    def _collection_members(self, ref):
        data, members, _ = self._collection_state(ref)
        for member in members:
            self._record(data, member.key)  # existence/generation, without reading member fields
        return members

    def collection(self, owner, field):
        ref = CollectionRef(_key(owner), field)
        self._collection_state(ref)
        return CollectionView(self, ref)

    def resolve_collection(self, ref):
        self._collection_members(ref)  # explicit dangling/target/schema errors, no fallback
        return CollectionView(self, ref)

    def field_version(self, ref, name):
        key, data = _key(ref), self._data()
        if key not in data.records:
            raise EntityUnavailable(f"Entity generation is absent: {key!r}")
        return data.field_versions[key, name]

    def collection_version(self, ref, name):
        key, data = _key(ref), self._data()
        if key not in data.records:
            raise EntityUnavailable(f"Entity generation is absent: {key!r}")
        return data.collection_versions[key, name]

    def membership_version(self, entity_type, partition=None):
        _partition(partition, optional=True)
        return self._data().membership_versions.get((entity_type, partition), 0)

    def version(self, event):
        """Observer-free dependency validation; absent entity/field/collection is None."""
        if type(event) is not ReadEvent:
            raise TypeError("Expected an immutable ReadEvent")
        data = self._data()
        if event.kind == "existence":
            return data.existence_versions.get(event.key, 0)
        if event.kind == "membership":
            _partition(event.partition, optional=True)
            return data.membership_versions.get((event.entity_type, event.partition), 0)
        if event.kind == "record":
            record = data.records.get(event.key)
            return None if record is None else record.version
        if event.kind in ("field", "collection"):
            if event.key not in data.records:
                return None
            versions = data.field_versions if event.kind == "field" else data.collection_versions
            return versions.get((event.key, event.field))
        raise ValueError(f"Unknown read event kind: {event.kind!r}")

    def entities(self, entity_type, partition=None):
        _partition(partition, optional=True)
        data = self._data()
        version = data.membership_versions.get((entity_type, partition), 0)
        self._event("membership", None, None, version, entity_type, partition)
        keys = [key for key, record in data.records.items()
                if key.entity_type == entity_type and (partition is None or record.partition == partition)]
        return tuple(Ref(key) for key in sorted(keys, key=lambda key: (key.entity_id, key.generation)))

    def lease(self, *, observer=None):
        sequence = self._data().sequence
        token, expires = self._pool.acquire(sequence)
        return ReadView(self._pool, token, expires, observer)

    def close(self):
        self._pool.release(self._token)

    def __enter__(self):
        self._data()
        return self

    def __exit__(self, *error):
        self.close()


class ReadView(_Reader):
    """Published expiring SDK lease; extracted values remain immutable after expiry."""
    __slots__ = ("_pool", "_token", "_observer", "expires_at_ns")

    @property
    def published(self):
        return True


class StagedReadView(_Reader):
    """Trusted pure-recompute handle, deliberately not a ReadView/C4 publication.

    It shares the stage reservation, has no lease/TTL and expires on consumption,
    discard, stale-base publication or explicit close. It cannot mint published leases.
    """
    __slots__ = ("_pool", "_token", "_observer", "expires_at_ns", "_closed")

    def __init__(self, pool, token, observer=None):
        super().__init__(pool, token, None, observer)
        object.__setattr__(self, "_closed", False)

    @property
    def published(self):
        return False

    def _data(self):
        if self._closed:
            raise ReadViewUnavailable("Staged reader was closed")
        return self._pool.get_staged(self._token)

    def lease(self, *, observer=None):
        raise StageRejected("Staged readers cannot issue published ReadView leases")

    def close(self):
        object.__setattr__(self, "_closed", True)


class StateStore:
    """C1 authority for a trusted coordinator; not a model-facing command executor.

    Stage/publish/discard require the construction thread. C2 owns reentrancy,
    barriers, index preparation and notifications. No SQL, Qt or network here.
    """
    def __init__(self, registries, *, epoch=None, clock_ns=monotonic_ns, limits=None):
        self._configure(registries, epoch, clock_ns)
        empty = MappingProxyType({})
        self._head = SnapshotData(
            epoch=self._epoch, sequence=0, records=empty, generations=empty, generation_partitions=empty,
            existence_versions=empty, field_versions=empty, collection_versions=empty,
            membership_versions=empty, collection_sources=self._collection_sources,
            schema_manifest=self._manifest, policy_revision=0, index_revision=0)
        self._pool = _SnapshotPool(self._head, _logical_size(self._head),
                                   SnapshotLimits() if limits is None else limits, clock_ns)
        self._stages = {}

    def _configure(self, registries, epoch, clock_ns):
        self._registries = tuple(registries)
        if not self._registries:
            raise ValueError("At least one Registry is required")
        if epoch is None:
            epoch = uuid4().hex
        if type(epoch) is not str or not epoch:
            raise ValueError("Epoch must be a non-empty string")
        if not isinstance(clock_ns, Callable):
            raise TypeError("clock_ns must be callable")
        self._epoch, self._owner, self._id = epoch, get_ident(), uuid4().hex
        self._entities = {}
        for registry in self._registries:
            for descriptor in registry.descriptors(kind="entity"):
                if descriptor.definition_id in self._entities:
                    raise ValueError("Duplicate entity type across registries")
                self._entities[descriptor.definition_id] = registry
        self._manifest = self._schema_manifest()
        self._collection_sources = MappingProxyType({
            (descriptor.definition_id, field.name): field.relation.target_type
            for registry in self._registries for descriptor in registry.descriptors(kind="entity")
            for field in descriptor.fields
            if field.relation is not None and field.relation.kind == "owned"
            and not field.relation.nullable})

    @classmethod
    def from_snapshot(cls, registries, snapshot, *, epoch=None, limits=None, clock_ns=monotonic_ns):
        """Create a fresh C1 authority from an accepted, structurally checked cut.

        No stage/Create, normal validators, defaults, codecs or handlers execute.
        The default preserves the exact RAM cut, including epoch and memory state.
        Restart callers supply a durable-only projection and an explicit fresh epoch.
        This factory proves neither a DB commit nor provenance: S1-011 must correlate
        the immutable image with its definitive journal outcome before installation.
        No old leases/stages/reservations migrate to this new pool. Independent old
        stores remain historical; expired/released old leases are never revived.
        """
        if type(snapshot) is not SnapshotData:
            raise SnapshotRestoreError("Expected an exact SnapshotData")
        restored = object.__new__(cls)
        try:
            restored._configure(registries, snapshot.epoch if epoch is None else epoch, clock_ns)
            restored._head = _restore_data(restored, snapshot)
        except (TypeError, ValueError, KeyError, AttributeError) as error:
            if isinstance(error, SnapshotRestoreError):
                raise
            raise SnapshotRestoreError(f"Invalid accepted snapshot: {error}") from error
        restored._pool = _SnapshotPool(restored._head, _logical_size(restored._head),
                                      SnapshotLimits() if limits is None else limits, clock_ns)
        restored._stages = {}
        return restored

    def export_snapshot(self):
        """Owner-only immutable recovery handoff; no lease, writer grant or DB proof."""
        self._write_check()
        return self._head

    def _schema_manifest(self):
        return tuple(sorted((registry.namespace, registry.revision,
                             tuple((d.definition_id, d.kind, d.revision, d.fingerprint)
                                   for d in registry.descriptors())) for registry in self._registries))

    def _write_check(self):
        if get_ident() != self._owner:
            raise WriterOwnershipError("Only the StateStore owner thread may stage/publish/discard")

    @property
    def head_sequence(self):
        return self._head.sequence

    def read(self, sequence=None, *, observer=None):
        token, expires = self._pool.acquire(sequence)
        return ReadView(self._pool, token, expires, observer)

    def read_staged(self, stage, *, observer=None):
        self._stage_check(stage)
        return StagedReadView(self._pool, stage._token, observer)

    def measurements(self):
        return self._pool.measurements()

    def stage(self, patch, *, policy_revision=None, index_revision=None):
        self._write_check()
        if type(patch) is not Patch:
            raise TypeError("Expected a trusted Patch")
        if self._schema_manifest() != self._manifest:
            raise StageRejected("Registry changed; explicit schema rebuild/migration is required")
        for name in ("creates", "updates", "deletes"):
            if type(getattr(patch, name)) is not tuple:
                raise TypeError("Patch operation groups must be exact immutable tuples")
        head = self._head
        sequence = head.sequence + 1
        records, generations = dict(head.records), dict(head.generations)
        generation_partitions = dict(head.generation_partitions)
        existence, fields = dict(head.existence_versions), dict(head.field_versions)
        collections, membership = dict(head.collection_versions), dict(head.membership_versions)
        touched = set()

        def touch(entity_type, entity_id):
            identity = (entity_type, entity_id)
            if identity in touched:
                raise StageRejected("A patch may target each entity identity only once")
            touched.add(identity)
            return identity

        def member(key, partition):
            membership[key.entity_type, None] = sequence
            membership[key.entity_type, partition] = sequence

        def validate(entity_type, values):
            if type(values) is not dict:
                raise TypeError("Record values/changes require an exact dictionary")
            try:
                registry = self._entities[entity_type]
            except KeyError as error:
                raise StageRejected(f"Unknown entity type: {entity_type!r}") from error
            # The ONLY publish value boundary: validation/defaults/validators -> final freeze.
            return deep_freeze(registry.validate_entity(entity_type, values))

        for operation in patch.deletes:
            key = _key(operation)
            touch(key.entity_type, key.entity_id)
            if key not in records:
                raise EntityUnavailable(f"Delete target is absent: {key!r}")
            record = records.pop(key)
            existence[key] = sequence
            member(key, record.partition)

        for operation in patch.creates:
            if type(operation) is not Create:
                raise TypeError("Expected a Create operation")
            _partition(operation.partition)
            # Validate stable identity even before accessing the generation ledger.
            EntityKey(operation.entity_type, operation.entity_id, 1)
            identity = touch(operation.entity_type, operation.entity_id)
            if any((key.entity_type, key.entity_id) == identity for key in records):
                raise StageRejected("Entity identity already exists")
            if identity in generation_partitions and generation_partitions[identity] != operation.partition:
                raise StageRejected("Entity identity cannot change semantic partition")
            key = EntityKey(*identity, generations.get(identity, 0) + 1)
            values = validate(key.entity_type, operation.values)
            records[key] = Record(key, operation.partition, values, sequence)
            generations[identity] = key.generation
            generation_partitions[identity] = operation.partition
            existence[key] = sequence
            for field, value in values.items():
                fields[key, field] = sequence
                if type(value) in (FrozenMap, FrozenList, tuple):
                    collections[key, field] = sequence
            member(key, operation.partition)

        for operation in patch.updates:
            if type(operation) is not Update:
                raise TypeError("Expected an Update operation")
            key = _key(operation.ref)
            touch(key.entity_type, key.entity_id)
            if key not in records:
                raise EntityUnavailable(f"Update target is absent: {key!r}")
            previous = records[key]
            if type(operation.changes) is not dict:
                raise TypeError("Update changes require an exact dictionary")
            merged = thaw(previous.values)
            merged.update(operation.changes)
            values = validate(key.entity_type, merged)
            if strict_equal(previous.values, values):
                continue
            records[key] = Record(key, previous.partition, values, sequence)
            for field, value in values.items():
                if not strict_equal(previous.values[field], value):
                    fields[key, field] = sequence
                    if type(value) in (FrozenMap, FrozenList, tuple):
                        collections[key, field] = sequence
                    else:
                        collections.pop((key, field), None)

        revisions = []
        for previous, requested in ((head.policy_revision, policy_revision), (head.index_revision, index_revision)):
            value = previous if requested is None else requested
            if type(value) is not int or value < previous:
                raise ValueError("Cut revisions must be monotonic exact non-negative integers")
            revisions.append(value)
        snapshot = SnapshotData(
            epoch=head.epoch, sequence=sequence, records=MappingProxyType(records),
            generations=MappingProxyType(generations),
            generation_partitions=MappingProxyType(generation_partitions),
            existence_versions=MappingProxyType(existence), field_versions=MappingProxyType(fields),
            collection_versions=MappingProxyType(collections), membership_versions=MappingProxyType(membership),
            collection_sources=head.collection_sources, schema_manifest=self._manifest,
            policy_revision=revisions[0], index_revision=revisions[1])
        token = uuid4().hex
        staged = StagedSnapshot(snapshot, head.sequence, self._id, token)
        self._pool.reserve(token, snapshot, _logical_size(snapshot))
        self._stages[token] = staged
        return staged

    def _stage_check(self, stage):
        self._write_check()
        if type(stage) is not StagedSnapshot or stage._store_id != self._id:
            raise StageRejected("Foreign stage")
        if stage.base_sequence != self._head.sequence:
            raise StageRejected("Stale stage base sequence")
        if self._stages.get(stage._token) is not stage:
            raise StageRejected("Discarded or already published stage")

    def publish(self, stage):
        self._stage_check(stage)
        token, expires = self._pool.publish(stage._token)
        self._head = stage.snapshot
        del self._stages[stage._token]
        # Every sibling prepared on the preceding cut is stale; release SDK ownership.
        for stale_token in tuple(self._stages):
            self._pool.discard(stale_token)
            del self._stages[stale_token]
        return ReadView(self._pool, token, expires)

    def discard(self, stage):
        self._stage_check(stage)
        del self._stages[stage._token]
        self._pool.discard(stage._token)


def _restore_data(store, snapshot):
    """Validate the actual C1 metadata invariants, retaining historical tombstones."""
    if type(snapshot.epoch) is not str or not snapshot.epoch:
        raise SnapshotRestoreError("Invalid source epoch")
    sequence = snapshot.sequence
    if type(sequence) is not int or sequence < 0:
        raise SnapshotRestoreError("Invalid snapshot sequence")
    for revision in (snapshot.policy_revision, snapshot.index_revision):
        if type(revision) is not int or revision < 0:
            raise SnapshotRestoreError("Invalid policy/index revision")
    if not strict_equal(snapshot.schema_manifest, store._manifest):
        raise SnapshotRestoreError("Schema manifest mismatch")

    def mapping(value):
        if type(value) not in (dict, MappingProxyType):
            raise SnapshotRestoreError("Snapshot metadata requires exact dict/mapproxy backings")
        return value.items()

    def identity(key):
        if type(key) is not tuple or len(key) != 2:
            raise SnapshotRestoreError("Malformed identity ledger key")
        EntityKey(key[0], key[1], 1)
        if key[0] not in store._entities:
            raise SnapshotRestoreError("Unknown ledger entity type")
        return key

    def entity_key(key):
        if type(key) is not EntityKey:
            raise SnapshotRestoreError("Expected an exact EntityKey")
        copied = EntityKey(key.entity_type, key.entity_id, key.generation)
        ident = identity((copied.entity_type, copied.entity_id))
        if ident not in generations or copied.generation > generations[ident]:
            raise SnapshotRestoreError("Entity key contradicts generation ledger")
        return copied

    def version(value):
        if type(value) is not int or not 1 <= value <= sequence:
            raise SnapshotRestoreError("Written version outside snapshot range")
        return value

    sources = {}
    for key, target in mapping(snapshot.collection_sources):
        if (type(key) is not tuple or len(key) != 2 or any(type(item) is not str for item in key)
                or type(target) is not str):
            raise SnapshotRestoreError("Malformed collection source metadata")
        sources[key] = target
    if not strict_equal(sources, dict(store._collection_sources)):
        raise SnapshotRestoreError("Collection source schema mismatch")
    generations = {}
    for key, generation in mapping(snapshot.generations):
        key = identity(key)
        if type(generation) is not int or not 1 <= generation <= sequence:
            raise SnapshotRestoreError("Invalid generation ledger value")
        generations[key] = generation
    partitions = {identity(key): _partition(value) for key, value in mapping(snapshot.generation_partitions)}
    if partitions.keys() != generations.keys():
        raise SnapshotRestoreError("Generation partition ledger mismatch")

    existence = {entity_key(key): version(value) for key, value in mapping(snapshot.existence_versions)}
    histories = {}
    membership_expected = {}
    for key, value in existence.items():
        ident = (key.entity_type, key.entity_id)
        histories.setdefault(ident, []).append((key.generation, value))
        partition = partitions[ident]
        for token in ((key.entity_type, partition), (key.entity_type, None)):
            membership_expected[token] = max(membership_expected.get(token, 0), value)
    if histories.keys() != generations.keys():
        raise SnapshotRestoreError("Missing generation existence history")
    for ident, history in histories.items():
        # Count + validated bounds ensures 1..highest coverage without corrupt-input enumeration.
        if len(history) != generations[ident]:
            raise SnapshotRestoreError("Incomplete generation existence history")
        ordered_versions = [value for _, value in sorted(history)]
        if any(left >= right for left, right in zip(ordered_versions, ordered_versions[1:])):
            raise SnapshotRestoreError("Generation existence history is not monotonic")

    definitions = {typ: {field.name for field in registry.entity_descriptor(typ).fields}
                   for typ, registry in store._entities.items()}
    records, live = {}, set()
    for key, record in mapping(snapshot.records):
        key = entity_key(key)
        if type(record) is not Record or type(record.key) is not EntityKey or key != record.key:
            raise SnapshotRestoreError("Record key mismatch/type")
        # Reconstruct the record identity as well; dataclass frozen is not structural validation.
        if EntityKey(record.key.entity_type, record.key.entity_id, record.key.generation) != key:
            raise SnapshotRestoreError("Malformed record identity")
        ident = (key.entity_type, key.entity_id)
        if ident in live or key.generation != generations[ident]:
            raise SnapshotRestoreError("Contradictory live generation")
        live.add(ident)
        if _partition(record.partition) != partitions[ident]:
            raise SnapshotRestoreError("Record partition mismatch")
        values = _detach_frozen(record.values)
        if type(values) is not FrozenMap or set(values) != definitions[key.entity_type]:
            raise SnapshotRestoreError("Record fields mismatch")
        store._entities[key.entity_type].check_accepted_entity(key.entity_type, thaw(values))
        records[key] = Record(key, record.partition, values, version(record.version))

    def field_map(value):
        result = {}
        for token, value in mapping(value):
            if type(token) is not tuple or len(token) != 2 or type(token[1]) is not str:
                raise SnapshotRestoreError("Malformed field version token")
            key, field = entity_key(token[0]), token[1]
            if key not in existence or field not in definitions[key.entity_type]:
                raise SnapshotRestoreError("Unknown field version owner/field")
            result[key, field] = version(value)
        return result
    fields, collections = field_map(snapshot.field_versions), field_map(snapshot.collection_versions)
    for key, exist_version in existence.items():
        names = definitions[key.entity_type]
        if any((key, field) not in fields for field in names):
            raise SnapshotRestoreError("Missing historical/live field version")
        record = records.get(key)
        if record is None:
            if any(fields[key, field] > exist_version for field in names):
                raise SnapshotRestoreError("Historical field version follows deletion")
        else:
            if any(fields[key, field] < exist_version for field in names):
                raise SnapshotRestoreError("Live field version precedes creation")
            if record.version != max([exist_version, *(fields[key, field] for field in names)]):
                raise SnapshotRestoreError("Record version disagrees with field/existence versions")
            for field, value in record.values.items():
                is_collection = type(value) in (FrozenMap, FrozenList, tuple)
                if is_collection != ((key, field) in collections):
                    raise SnapshotRestoreError("Live collection version shape mismatch")
    if any(token not in fields or value != fields[token] for token, value in collections.items()):
        raise SnapshotRestoreError("Collection version disagrees with field version")
    membership = {}
    for token, value in mapping(snapshot.membership_versions):
        if (type(token) is not tuple or len(token) != 2 or type(token[0]) is not str
                or token[0] not in store._entities):
            raise SnapshotRestoreError("Malformed membership token")
        _partition(token[1], optional=True)
        if type(value) is not int or not 0 <= value <= sequence:
            raise SnapshotRestoreError("Invalid membership version")
        if value != membership_expected.get(token, 0):
            raise SnapshotRestoreError("Membership version disagrees with existence history")
        membership[token] = value
    if not membership_expected.keys() <= membership.keys():
        raise SnapshotRestoreError("Missing membership version")
    return SnapshotData(
        epoch=store._epoch, sequence=sequence, records=MappingProxyType(records),
        generations=MappingProxyType(generations), generation_partitions=MappingProxyType(partitions),
        existence_versions=MappingProxyType(existence), field_versions=MappingProxyType(fields),
        collection_versions=MappingProxyType(collections), membership_versions=MappingProxyType(membership),
        collection_sources=store._collection_sources, schema_manifest=store._manifest,
        policy_revision=snapshot.policy_revision, index_revision=snapshot.index_revision)
