"""One deterministic schema envelope; input and accepted semantics stay separate.

NEW accepted-record helpers share fixed logical limits: depth16, nodes4096,
container256, canonical full envelope128KiB. The VALUE's wire tree is counted:
root depth0; each child value adds depth1; each scalar/container is one node;
map keys are not nodes; container size is item/entry count. Native Ref counts
as its wire object (object + three scalar values); CollectionRef as its outer
object + owner object + four scalars. Envelope fields are outside tree quotas,
inside the byte cap. Thus successful accepted encode is quota-decodable.

Native preflight precedes our copies/encoding. Bytes are capped before parse;
structural preflight follows JSON parse before native reconstruction. These
logical limits do not promise pre-JSON structural allocation, pre-BLOB, RSS,
CPU or CPython sandbox bounds. Existing INPUT codec limits are unchanged.
"""
import json
from math import isfinite
import re

from .descriptors import canonical
from .registry import _wire
from .types import CollectionRef, EntityKey, Ref

_FORMAT = "agent-native.schema/1"
_FIELDS = {"format", "namespace", "registry_revision", "kind", "definition_id",
           "definition_revision", "fingerprint", "value"}
_ACCEPTED_BYTES = 128 * 1024
_ACCEPTED_DEPTH = 16
_ACCEPTED_NODES = 4096
_ACCEPTED_CONTAINER = 256


class SchemaDecodeError(ValueError):
    pass


class SchemaCompatibilityError(SchemaDecodeError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SchemaDecodeError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise SchemaDecodeError(f"Non-finite JSON constant: {value}")


def _envelope(registry, kind, definition, value):
    descriptor = definition.descriptor
    return {"format": _FORMAT, "namespace": registry.namespace,
            "registry_revision": registry.revision, "kind": kind,
            "definition_id": descriptor.definition_id,
            "definition_revision": descriptor.revision,
            "fingerprint": descriptor.fingerprint, "value": value}


def encode(registry, kind, name, value):
    definition = registry._definition(kind, name)
    validated = definition.validate(value)
    return canonical(_envelope(registry, kind, definition, _wire(validated)))


def _read_envelope(registry, expected_kind, data, *, accepted=False):
    if type(data) is not bytes:
        raise SchemaDecodeError("Decode requires UTF-8 bytes")
    if accepted and len(data) > _ACCEPTED_BYTES:
        raise SchemaDecodeError("Accepted record envelope byte limit")
    try:
        envelope = json.loads(data.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
    except RecursionError as error:
        if not accepted:
            raise  # Preserve the existing input codec's separate failure contract.
        raise SchemaDecodeError("Malformed UTF-8 JSON") from error
    except (UnicodeError, ValueError) as error:
        if isinstance(error, SchemaDecodeError):
            raise
        raise SchemaDecodeError("Malformed UTF-8 JSON") from error
    if type(envelope) is not dict or set(envelope) != _FIELDS:
        raise SchemaDecodeError("Unknown/missing codec envelope fields")
    if envelope["format"] != _FORMAT or envelope["kind"] != expected_kind:
        raise SchemaCompatibilityError("Unsupported codec format or definition kind")
    if type(envelope["namespace"]) is not str or envelope["namespace"] != registry.namespace:
        raise SchemaCompatibilityError("Registry namespace mismatch")
    for field in ("registry_revision", "definition_revision"):
        if type(envelope[field]) is not int or envelope[field] < 1:
            raise SchemaDecodeError("Revision must be a strict positive integer")
    if type(envelope["fingerprint"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", envelope["fingerprint"]):
        raise SchemaDecodeError("Invalid definition fingerprint")
    if type(envelope["definition_id"]) is not str or not envelope["definition_id"].startswith(registry.namespace + "."):
        raise SchemaCompatibilityError("Definition ID is not in this registry namespace")
    try:
        definition = registry._definition(expected_kind, envelope["definition_id"])
    except (KeyError, ValueError) as error:
        raise SchemaCompatibilityError("Unknown definition ID") from error
    return definition, envelope


def _identity(registry, definition, envelope):
    descriptor = definition.descriptor
    return ((envelope["registry_revision"], envelope["definition_revision"], envelope["fingerprint"])
            == (registry.revision, descriptor.revision, descriptor.fingerprint))


def decode(registry, expected_kind, data):
    definition, envelope = _read_envelope(registry, expected_kind, data)
    if _identity(registry, definition, envelope):
        return definition.validate(envelope["value"], wire=True)
    key = (envelope["registry_revision"], expected_kind, envelope["definition_id"],
           envelope["definition_revision"], envelope["fingerprint"])
    migration = registry._migrations.get(key)
    if migration is None:
        raise SchemaCompatibilityError("Incompatible schema identity; an explicit migration is required")
    source, migrate = migration
    previous = source.validate(envelope["value"], wire=True)
    return definition.validate(migrate(previous))


def _accepted_preflight(value, *, wire=False):
    """Bound the same virtual wire VALUE tree before copies or native rebuilding."""
    stack = [(False, value, 0, False)]
    active = set()
    nodes = text_bytes = 0

    def text_size(text):
        if type(text) is not str or len(text) > _ACCEPTED_BYTES:
            raise SchemaDecodeError("Accepted record text/envelope byte limit")
        try:
            return len(text.encode("utf-8"))
        except UnicodeError as error:
            raise SchemaDecodeError("Accepted record text requires valid UTF-8") from error

    while stack:
        leaving, item, depth, owner = stack.pop()
        if leaving:
            active.remove(item)
            continue
        nodes += 1
        if nodes > _ACCEPTED_NODES:
            raise SchemaDecodeError("Accepted record node limit")
        if depth > _ACCEPTED_DEPTH:
            raise SchemaDecodeError("Accepted record depth limit")
        typ = type(item)
        children = None
        if typ is str:
            text_bytes += text_size(item)
        elif item is None or typ in (bool, int, float):
            if typ is float and not isfinite(item):
                raise SchemaDecodeError("Accepted record requires finite floats")
            if typ is int and item.bit_length() > _ACCEPTED_BYTES * 4:
                raise SchemaDecodeError("Accepted record integer/envelope byte limit")
        elif not wire and (typ is Ref or (owner and typ is EntityKey)):
            key = item.key if typ is Ref else item
            if type(key) is not EntityKey:
                raise SchemaDecodeError("Accepted Ref requires an exact EntityKey")
            children = [(key.entity_type, False), (key.entity_id, False), (key.generation, False)]
            text_bytes += sum(map(len, ("entity_type", "entity_id", "generation")))
        elif not wire and typ is CollectionRef:
            if type(item.owner) is not EntityKey:
                raise SchemaDecodeError("Accepted CollectionRef requires an exact EntityKey owner")
            children = [(item.owner, True), (item.field, False)]
            text_bytes += len("owner") + len("field")
        elif typ is dict or (typ in (list, tuple) and (not wire or typ is list)):
            if len(item) > _ACCEPTED_CONTAINER:
                raise SchemaDecodeError("Accepted record container limit")
            ident = id(item)
            if ident in active:
                raise SchemaDecodeError("Cyclic accepted record")
            active.add(ident)
            stack.append((True, ident, 0, False))
            if typ is dict:
                for key in item:
                    text_bytes += text_size(key)
                children = [(child, False) for child in item.values()]
            else:
                children = [(child, False) for child in item]
        else:
            raise SchemaDecodeError("Unsupported accepted record native type")
        # This necessary lower bound prevents unbounded aggregate string copying.
        # Exact escaped canonical envelope size is checked after bounded encoding.
        if text_bytes > _ACCEPTED_BYTES:
            raise SchemaDecodeError("Accepted record text/envelope byte limit")
        if children is not None:
            stack.extend((False, child, depth + 1, is_owner)
                         for child, is_owner in reversed(children))


def encode_accepted_record(registry, name, accepted):
    definition = registry._definition("record", name)
    _accepted_preflight(accepted)
    definition.accepted.check(accepted, registry)
    try:
        data = canonical(_envelope(registry, "record", definition, _wire(accepted)))
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise SchemaDecodeError("Invalid accepted record encoding") from error
    if len(data) > _ACCEPTED_BYTES:
        raise SchemaDecodeError("Accepted record envelope byte limit")
    return data


def decode_accepted_record(registry, data):
    definition, envelope = _read_envelope(registry, "record", data, accepted=True)
    if not _identity(registry, definition, envelope):
        raise SchemaCompatibilityError("Accepted record requires the exact current schema identity")
    _accepted_preflight(envelope["value"], wire=True)
    result = definition.accepted.from_wire(envelope["value"], registry)
    # Enforce the SAME canonical-envelope cap, including noncanonical input forms.
    encode_accepted_record(registry, definition.descriptor.definition_id, result)
    return result
