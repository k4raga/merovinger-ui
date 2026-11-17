"""Private accepted-value storage format. No input codecs or author callbacks.

Tags preserve native list/tuple, bool/int/float, identities and Record.version.
Registry remains the sole schema authority; this module owns only transport.
"""
from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
import json
from math import isfinite
from types import MappingProxyType

from agent_native.schema import EntityKey, Ref, CollectionRef
from agent_native.state import SnapshotData, Record, FrozenMap, FrozenList, thaw
from agent_native.kernel import CutIdentity

MAPS = ("records", "generations", "generation_partitions", "existence_versions",
        "field_versions", "collection_versions", "membership_versions", "collection_sources")
FORMAT = "agent-native.accepted-snapshot/1"
MAX_BYTES = 64 * 1024 * 1024


class StorageFormatError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
                      allow_nan=False).encode("ascii")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StorageFormatError("Duplicate JSON key")
        result[key] = value
    return result


def _parse(data):
    if type(data) is not bytes or len(data) > MAX_BYTES:
        raise StorageFormatError("Storage bytes required within size limit")
    try:
        return json.loads(data, object_pairs_hook=_unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(StorageFormatError("Non-finite JSON")))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise StorageFormatError("Malformed storage JSON") from error


def _pack(value, active, depth):
    if depth > 64:
        raise StorageFormatError("Storage nesting limit")
    typ = type(value)
    if value is None:
        return ["n"]
    if typ is bool:
        return ["b", value]
    if typ is int:
        return ["i", str(value)]
    if typ is float:
        if not isfinite(value):
            raise StorageFormatError("Non-finite float")
        return ["f", value.hex()]
    if typ is str:
        return ["s", value]
    if typ is EntityKey:
        return ["k", value.entity_type, value.entity_id, str(value.generation)]
    if typ is Ref:
        return ["r", _pack(value.key, active, depth + 1)]
    if typ is CollectionRef:
        return ["c", _pack(value.owner, active, depth + 1), value.field]
    if typ not in (tuple, FrozenList, FrozenMap, dict, MappingProxyType, Record):
        raise StorageFormatError("Unsupported accepted-value type")
    ident = id(value)
    if ident in active:
        raise StorageFormatError("Cyclic storage value")
    active.add(ident)
    try:
        child = lambda v: _pack(v, active, depth + 1)
        if typ is Record:
            return ["R", child(value.key), value.partition, child(value.values), child(value.version)]
        if typ in (tuple, FrozenList):
            return ["t" if typ is tuple else "l", [child(v) for v in value]]
        pairs = [[child(k), child(v)] for k, v in value.items()]
        pairs.sort(key=lambda pair: _json(pair[0]))
        return ["d" if typ is FrozenMap else "m", pairs]
    finally:
        active.remove(ident)


def encode_value(value):
    try:
        data = _json(_pack(value, set(), 0))
        if len(data) > MAX_BYTES:
            raise StorageFormatError("Storage size limit")
        return data
    except (TypeError, ValueError, RecursionError) as error:
        if isinstance(error, StorageFormatError):
            raise
        raise StorageFormatError("Invalid accepted value") from error


def _unpack(node, depth=0):
    if depth > 64 or type(node) is not list or not node or type(node[0]) is not str:
        raise StorageFormatError("Malformed storage tag")
    tag = node[0]
    arities = {"n": 1, "b": 2, "i": 2, "f": 2, "s": 2, "k": 4, "r": 2,
               "c": 3, "R": 5, "t": 2, "l": 2, "d": 2, "m": 2}
    if tag not in arities or len(node) != arities[tag]:
        raise StorageFormatError("Unknown storage tag/arity")
    child = lambda v: _unpack(v, depth + 1)
    if tag == "n":
        return None
    if tag in ("b", "s"):
        if type(node[1]) is not (bool if tag == "b" else str):
            raise StorageFormatError("Wrong scalar type")
        return node[1]
    if tag == "i":
        if type(node[1]) is not str:
            raise StorageFormatError("Integer text required")
        result = int(node[1])
        if str(result) != node[1]:
            raise StorageFormatError("Noncanonical integer")
        return result
    if tag == "f":
        if type(node[1]) is not str:
            raise StorageFormatError("Float text required")
        result = float.fromhex(node[1])
        if not isfinite(result) or result.hex() != node[1]:
            raise StorageFormatError("Noncanonical float")
        return result
    if tag == "k":
        if any(type(v) is not str for v in node[1:]):
            raise StorageFormatError("Malformed identity")
        generation = int(node[3])
        if str(generation) != node[3]:
            raise StorageFormatError("Noncanonical generation")
        return EntityKey(node[1], node[2], generation)
    if tag == "r":
        return Ref(child(node[1]))
    if tag == "c":
        return CollectionRef(child(node[1]), node[2])
    if tag == "R":
        return Record(child(node[1]), node[2], child(node[3]), child(node[4]))
    if type(node[1]) is not list:
        raise StorageFormatError("Container list required")
    if tag in ("t", "l"):
        values = tuple(child(v) for v in node[1])
        return values if tag == "t" else FrozenList(values)
    pairs = {}
    for pair in node[1]:
        if type(pair) is not list or len(pair) != 2:
            raise StorageFormatError("Malformed map entry")
        key, value = child(pair[0]), child(pair[1])
        if key in pairs:
            raise StorageFormatError("Duplicate decoded map key")
        pairs[key] = value
    return FrozenMap(pairs) if tag == "d" else MappingProxyType(pairs)


def decode_value(data):
    try:
        value = _unpack(_parse(data))
        if encode_value(value) != data:
            raise StorageFormatError("Noncanonical storage encoding")
        return value
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as error:
        if isinstance(error, StorageFormatError):
            raise
        raise StorageFormatError("Invalid storage value") from error


def identity_value(cut):
    return (cut.epoch, cut.sequence, cut.schema_manifest, cut.policy_revision, cut.index_revision)


def encode_cut(cut):
    if type(cut) is not CutIdentity:
        raise StorageFormatError("Typed cut required")
    return encode_value(identity_value(cut))


def decode_cut(data):
    return CutIdentity(*decode_value(data))


def encode_snapshot(snapshot):
    if type(snapshot) is not SnapshotData:
        raise StorageFormatError("Typed snapshot required")
    return encode_value((FORMAT, identity_value(CutIdentity.read(snapshot)),
                         tuple((name, getattr(snapshot, name)) for name in MAPS)))


def decode_snapshot(data):
    value = decode_value(data)
    if (type(value) is not tuple or len(value) != 3 or value[0] != FORMAT
            or type(value[2]) is not tuple or tuple(pair[0] for pair in value[2]) != MAPS):
        raise StorageFormatError("Unknown snapshot format/maps")
    cut = CutIdentity(*value[1])
    return SnapshotData(*[cut.epoch, cut.sequence], **dict(value[2]),
                        schema_manifest=cut.schema_manifest, policy_revision=cut.policy_revision,
                        index_revision=cut.index_revision)


def durable_projection(snapshot):
    durable = {ident for ident, partition in snapshot.generation_partitions.items() if partition == "durable"}
    keep = lambda key: (key.entity_type, key.entity_id) in durable
    maps = {
        "records": {k: v for k, v in snapshot.records.items() if keep(k)},
        "generations": {k: v for k, v in snapshot.generations.items() if k in durable},
        "generation_partitions": {k: "durable" for k in durable},
        "existence_versions": {k: v for k, v in snapshot.existence_versions.items() if keep(k)},
        "field_versions": {k: v for k, v in snapshot.field_versions.items() if keep(k[0])},
        "collection_versions": {k: v for k, v in snapshot.collection_versions.items() if keep(k[0])},
    }
    membership = {}
    for key, version in maps["existence_versions"].items():
        for token in ((key.entity_type, "durable"), (key.entity_type, None)):
            membership[token] = max(membership.get(token, 0), version)
    maps["membership_versions"] = membership
    return replace(snapshot, **{k: MappingProxyType(v) for k, v in maps.items()})


def check_snapshot(snapshot, registries):
    """Worker structural transport gate; full restore invariants belong to C1 factory."""
    _checked_snapshot_bytes(snapshot, registries)


def _checked_snapshot_bytes(snapshot, registries):
    """Validate fully and return canonical bytes for this same local snapshot."""
    CutIdentity.read(snapshot)
    entities = {d.definition_id: registry for registry in registries for d in registry.descriptors(kind="entity")}
    manifest = tuple(sorted((r.namespace, r.revision, tuple((d.definition_id, d.kind, d.revision, d.fingerprint)
                            for d in r.descriptors())) for r in registries))
    sources = {(d.definition_id, f.name): f.relation.target_type for r in registries
               for d in r.descriptors(kind="entity") for f in d.fields
               if f.relation is not None and f.relation.kind == "owned" and not f.relation.nullable}
    if encode_value(snapshot.schema_manifest) != encode_value(manifest) or dict(snapshot.collection_sources) != sources:
        raise StorageFormatError("Registry manifest/source mismatch")
    for name in MAPS:
        if not isinstance(getattr(snapshot, name), Mapping):
            raise StorageFormatError("Snapshot map required")
    if snapshot.generations.keys() != snapshot.generation_partitions.keys():
        raise StorageFormatError("Ledger partition mismatch")
    for ident, generation in snapshot.generations.items():
        if (type(ident) is not tuple or len(ident) != 2 or ident[0] not in entities
                or type(generation) is not int or not 1 <= generation <= snapshot.sequence
                or snapshot.generation_partitions[ident] not in ("durable", "memory")):
            raise StorageFormatError("Malformed generation ledger")
        EntityKey(*ident, generation)
    for key, record in snapshot.records.items():
        if type(key) is not EntityKey or type(record) is not Record or key != record.key:
            raise StorageFormatError("Malformed record")
        ident = key.entity_type, key.entity_id
        if (snapshot.generations.get(ident) != key.generation or
                snapshot.generation_partitions.get(ident) != record.partition or
                not 1 <= record.version <= snapshot.sequence):
            raise StorageFormatError("Record ledger mismatch")
        entities[key.entity_type].check_accepted_entity(key.entity_type, thaw(record.values))
    for name in ("existence_versions", "field_versions", "collection_versions", "membership_versions"):
        for key, version in getattr(snapshot, name).items():
            if type(version) is not int or not (0 if name == "membership_versions" else 1) <= version <= snapshot.sequence:
                raise StorageFormatError("Malformed version")
            owner = key if name == "existence_versions" else key[0]
            if name != "membership_versions" and (type(owner) is not EntityKey or
                    (owner.entity_type, owner.entity_id) not in snapshot.generations):
                raise StorageFormatError("Unknown version owner")
    # Exercise all tags before BEGIN, including immutable backing containers.
    return encode_snapshot(snapshot)


def digest(data):
    return sha256(data).hexdigest()
