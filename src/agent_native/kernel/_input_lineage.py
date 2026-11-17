"""Bounded native lineage using existing accepted-value transport.

No separate serializer, issuer registry, or model-visible authority is added.
"""
from hashlib import sha256
from agent_native.schema import Ref, EntityKey, CollectionRef
from agent_native.state import Patch, Create, Update, Record, thaw, deep_freeze, strict_equal

LINEAGE_MAX_BYTES = 2 * 1024 * 1024
DRAFT_KEYS = frozenset(("original_cut", "schema_manifest", "policy_revision", "claims", "selected_events", "summary", "skill_pins"))


def native_bytes(value):
    from agent_native.persistence.codec import encode_value
    return encode_value(value if type(value) is Record else deep_freeze(value))


def native_digest(value):
    return sha256(native_bytes(value)).hexdigest()


def detach(value):
    return thaw(deep_freeze(value))


def resource_ref(value):
    if type(value) is not dict or set(value) != {"entity_type", "entity_id", "generation", "collection_field"} or value["collection_field"] is not None:
        raise ValueError("Exact entity identity required")
    return Ref(EntityKey(value["entity_type"], value["entity_id"], value["generation"]))


def build_record(kernel, view, cut, request_ref, draft):
    """Consume only the genuine C4 producer's draft, with native Registry validation.

    No DTO seal/constructor registry is introduced: trusted host is trusted. C4
    still validates the consumed envelope; model input cannot provide this draft.
    """
    from . import _command_values as v
    if type(draft) is not dict or set(draft) != DRAFT_KEYS:
        raise ValueError("request_lineage_shape")
    request = view.resolve(request_ref).values
    session = view.resolve(request["session"]).values
    conversation = view.resolve(session["conversation"]).values
    if conversation["state"] != "active" or conversation["principal"] != session["principal"]:
        raise ValueError("request_conversation_denied")
    expected_cut = {"epoch": cut.epoch, "sequence": cut.sequence,
        "index_revision": cut.index_revision, "policy_revision": cut.policy_revision}
    if draft["original_cut"] != expected_cut or draft["schema_manifest"] != native_digest(cut.schema_manifest) or draft["policy_revision"] != cut.policy_revision:
        raise ValueError("request_lineage_cut_changed")
    # Arm is current-cut acceptance, unlike a later restore of historical DATA.
    # Even identity-only leaves bind the actual partition/incarnation now. Older
    # memory identities are retained by restore but may never be issued here.
    for leaf in draft["claims"]:
        source=leaf["source"]
        if (source is None)!=(leaf["source_partition"] is None):
            raise ValueError("request_lineage_partition")
        if source is not None:
            ref=resource_ref(dict(source,collection_field=None))
            if not view.exists(ref) or view.resolve(ref).partition!=leaf["source_partition"]:
                raise ValueError("request_lineage_source_unavailable")
        for ident in (*leaf["value_refs"],*leaf["associations"],*leaf["original_grants"]):
            ref=resource_ref(dict(ident,collection_field=None))
            if not view.exists(ref):raise ValueError("request_lineage_source_unavailable")
            if ident["collection_field"] is not None:
                view.resolve_collection(CollectionRef(ref.key,ident["collection_field"]))
    values = dict(detach(draft), request=v.resource_record(request_ref), conversation=session["conversation"],
        original_task=session["task"], task_generation=session["task_generation"], principal=session["principal"])
    values["digest"] = native_digest(values)
    # Exact Registry model is the one shape authority; defaults are all explicit.
    model = kernel.system.RequestInputLineage
    compiled = kernel.system.registry._definition("entity", model.__entity_type__)
    validated = compiled.validate(values, wire=False)
    if len(native_bytes(validated)) > LINEAGE_MAX_BYTES:
        raise ValueError("request_lineage_limit")
    return validated
