"""Closed data transport for raw command plans; no domain schema or callbacks."""
from hashlib import sha256
from inspect import isawaitable, isgenerator, isasyncgen, iscoroutine
from agent_native.schema import Ref, CollectionRef
from agent_native.schema.registry import _detach
from agent_native.state import Patch, Create, Update, deep_freeze, thaw
from agent_native.state.tracking import ReadSet, QueryDependency, UnresolvedQueryRead
from agent_native.state.snapshots import ReadEvent
from agent_native.state.query import QueryKey, QueryVersion


def sync(value):
    if isawaitable(value) or isgenerator(value) or isasyncgen(value):
        if iscoroutine(value) or isgenerator(value):
            value.close()
        raise TypeError("Trusted command callbacks must return synchronously")
    return value


def text(value, label="identity"):
    if type(value) is not str or not value:
        raise TypeError(label + " requires a nonempty exact string")
    return value


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise TypeError("Expected an exact bounded nonnegative integer")
    return value


def encode(value):
    # Lazy import avoids persistence -> kernel package initialization cycles.
    from agent_native.persistence.codec import encode_value
    return encode_value(deep_freeze(_detach(value))).decode("ascii")


def decode(value):
    from agent_native.persistence.codec import decode_value
    if type(value) is not str:
        raise TypeError("Protocol payload must be text")
    return thaw(decode_value(value.encode("ascii")))


def digest(value):
    return sha256(encode(value).encode("ascii")).hexdigest()


def envelope(format, value):
    payload = encode(value)
    return {"format": format, "payload": payload,
            "sha256": sha256(payload.encode("ascii")).hexdigest()}


def open_envelope(value, format):
    if (type(value) is not dict or set(value) != {"format", "payload", "sha256"}
            or value["format"] != format or type(value["payload"]) is not str
            or type(value["sha256"]) is not str
            or sha256(value["payload"].encode("ascii")).hexdigest() != value["sha256"]):
        raise ValueError("Invalid versioned command payload")
    return decode(value["payload"])


def patch_data(patch):
    if type(patch) is not Patch:
        raise TypeError("Plan requires a real C1 Patch")
    if any(type(getattr(patch, name)) is not tuple for name in ("creates", "updates", "deletes")):
        raise TypeError("Patch groups must be immutable tuples")
    creates, updates, deletes = [], [], []
    for item in patch.creates:
        if type(item) is not Create or type(item.values) is not dict:
            raise TypeError("Invalid raw Create")
        creates.append((text(item.entity_type), text(item.entity_id),
                        _detach(item.values), item.partition))
    for item in patch.updates:
        if type(item) is not Update or type(item.ref) is not Ref or type(item.changes) is not dict:
            raise TypeError("Invalid raw Update")
        updates.append((item.ref, _detach(item.changes)))
    for ref in patch.deletes:
        if type(ref) is not Ref:
            raise TypeError("Invalid raw Delete")
        deletes.append(ref)
    # Roundtrip detaches caller-owned aliases and preserves native list/tuple.
    return decode(encode({"creates": tuple(creates), "updates": tuple(updates), "deletes": tuple(deletes)}))


def patch(value):
    if type(value) is not dict or set(value) != {"creates", "updates", "deletes"}:
        raise ValueError("Unknown raw patch fields")
    if any(type(value[name]) is not tuple for name in value):
        raise TypeError("Invalid raw patch groups")
    result = Patch(tuple(Create(*entry) for entry in value["creates"]),
                   tuple(Update(*entry) for entry in value["updates"]), value["deletes"])
    patch_data(result)
    return result


def reads_data(reads):
    from agent_native.persistence.codec import encode_value
    return {
        "epoch": reads.epoch, "schema": reads.schema_manifest,
        "events": tuple((e.kind, None if e.key is None else Ref(e.key), e.field,
                         e.version, e.entity_type, e.partition) for e in reads.events),
        "queries": tuple((q.key.name, encode_value(q.key.parameters).decode("ascii"),
                           q.key.revision, q.generation.cache_incarnation,
                           q.generation.counter) for q in reads.queries),
        "policy": reads.policy_revision, "index": reads.index_revision,
        "sequence": reads.sequence, "complete": reads.complete,
    }


def read_set(value):
    from agent_native.persistence.codec import decode_value
    events = tuple(ReadEvent(kind, None if ref is None else ref.key, field, version, typ, part)
                   for kind, ref, field, version, typ, part in value["events"])
    queries = tuple(QueryDependency(QueryKey(name, decode_value(params.encode("ascii")), revision),
                                    QueryVersion(incarnation, counter))
                    for name, params, revision, incarnation, counter in value["queries"])
    unresolved = () if value["complete"] else (UnresolvedQueryRead(None, "persisted_unresolved"),)
    return ReadSet(value["epoch"], value["schema"], events, queries, value["policy"],
                   value["index"], value["sequence"], unresolved)


def resources(value):
    if type(value) is Ref:
        return {identity(value)}
    if type(value) is CollectionRef:
        return {identity(value), identity(Ref(value.owner))}
    if type(value) in (tuple, list):
        return set().union(*(resources(v) for v in value)) if value else set()
    if type(value) is dict:
        return set().union(*(resources(v) for v in value.values())) if value else set()
    return set()


def identity(ref):
    if type(ref) is Ref:
        key, field = ref.key, None
    elif type(ref) is CollectionRef:
        key, field = ref.owner, ref.field
    else:
        raise TypeError("Resources require Ref or CollectionRef identity")
    return (key.entity_type, key.entity_id, key.generation, field)


def resource_record(ref):
    typ, ident, generation, field = identity(ref)
    return {"entity_type": typ, "entity_id": ident, "generation": generation,
            "collection_field": field}


def record_identity(value):
    return (value["entity_type"], value["entity_id"], value["generation"], value["collection_field"])


def projection(raw, base, staged):
    """Actual normalized semantic projection, intentionally excluding Record.version."""
    from agent_native.schema import EntityKey
    output, ledgers = [], []
    for typ, ident, _, partition in raw["creates"]:
        previous = base.generations.get((typ, ident), 0)
        prior_partition = base.generation_partitions.get((typ, ident))
        ref = Ref(EntityKey(typ, ident, previous + 1))
        record = staged.resolve(ref)
        output.append(("create", ref, record.partition, thaw(record.values)))
        ledgers.append((typ, ident, previous, prior_partition))
    for ref, _ in raw["updates"]:
        record = staged.resolve(ref)
        output.append(("update", ref, record.partition, thaw(record.values)))
    for ref in raw["deletes"]:
        if staged.exists(ref):
            raise ValueError("Delete was not applied")
        output.append(("delete", ref, base.records[ref.key].partition))
    return tuple(output), tuple(ledgers)


def working_resources(value):
    """Context root identities: collection aliases stay aliases, never their members."""
    if type(value) in (Ref, CollectionRef):
        return {identity(value)}
    if type(value) in (tuple, list):
        return set().union(*(working_resources(item) for item in value)) if value else set()
    if type(value) is dict:
        return set().union(*(working_resources(item) for item in value.values())) if value else set()
    return set()
