"""SYS7 closed SDK capacity ledger on the sole C1 graph, without callbacks.

Profile /2 narrows the dynamic result to 4096 UTF-8 bytes and 256 nodes.
The native tree bound 6*B + 64*N = 40960 includes scalar tags, escaped
text/keys, Ref/CollectionRef and punctuation. ProtocolPayload is ASCII:
quoting that tree costs at most twice its size. S measures BOTH the full
native skeleton and full schema envelope. Thus the joint future schema
size is <= 2*40960 + 2*16384 + 1024 = 115712 < 128KiB; the same
bound also satisfies native256KiB. Static tree <=2048 nodes; the dynamic
256-node virtual wire tree replaces placeholders, remaining below4096.

The byte ledger is incremental (unchanged tuple prefixes are not copied):
retain creates Fact(4), Reservation(4), Event(10):18 field-version keys.
Three new rows add <=21 other identity occurrences (record keys/headers,
existence, generation, partition and two memberships). <=6 changed SDK
fields and <=16 value/header links give 61*S, rounded to64*S. The external
native envelope is <=256KiB; all fixed fingerprints, U63 counters, numeric
map versions and punctuation fit the remaining256KiB of the512KiB base.

Apply creates Application(<=16), Evidence(8), Event(10):34 field keys,
plus <=21 other identities. <=16 touched rows change only fixed SDK state,
links and <=64 allowed domain fields. Even charging those64 field keys,
<=32 other SDK keys and <=32 body/header link occurrences gives183*S,
rounded to192*S. The actual Application native values are capped1MiB;
Evidence/Event bodies, fixed fingerprints, U63 version growth and one
<=256B evidence append fit the128KiB remainder of1152KiB. S includes ALL
these SDK type/field labels and original identities. The original admission
and no-effect/terminal proof links are subsets of the measured header.
Arbitrary normalized domain value growth is separately charged beyond ALL
outstanding claims. Updater failure leaves original retention intact.
"""
from dataclasses import replace
from types import MappingProxyType
from agent_native.schema import Ref, EntityKey
from agent_native.state import Patch, Create, Update, thaw
from ._input_lineage import native_bytes, native_digest

PROFILE = "agent-native.effect-result/2"
STATIC_BYTES = 16 * 1024
RESULT_BYTES = 4 * 1024
RESULT_NODES = 256
APPLICATION_BYTES = 1024 * 1024
SOURCE_FIELDS = ("adapter_id", "remote_identity", "source", "source_epoch")


def require(value, code):
    if not value:
        raise ValueError(code)


def identity(ref):
    return {"entity_type": ref.key.entity_type, "entity_id": ref.key.entity_id,
        "generation": ref.key.generation, "collection_field": None}


def reference(value):
    require(value["collection_field"] is None, "effect_capacity_entity_identity")
    return Ref(EntityKey(value["entity_type"], value["entity_id"], value["generation"]))


def reservation_ref(d, owner):
    return Ref(EntityKey(d.RuntimeStorageReservation.__entity_type__,
        native_digest(("effect-capacity", owner)), 1))


def reservation_create(d, owner, principal, amount):
    ref = reservation_ref(d, owner)
    return Create(ref.key.entity_type, ref.key.entity_id,
        {"principal": principal, "owner": identity(owner), "bytes": amount, "state": "reserved"})


def release(d, owner):
    return Update(reservation_ref(d, owner), {"state": "released"})


def source_key(identity_or_adapter):
    return tuple(identity_or_adapter[n] for n in SOURCE_FIELDS)


def link_from_request(request):
    from . import _command_values as v
    return {"dispatch": identity(request.dispatch), "effect": request.effect,
        "invocation": request.invocation, "operation_grant": request.operation_grant,
        "operation_id": request.operation_id, "resource": thaw(request.resource),
        "desired_generation": request.desired_generation, "claim_fence": request.fence,
        "mode": request.mode, "effect_action": request.effect_action, "request_action": request.action,
        "input_digest": v.digest(thaw(request.payload)), "claim_witness": request.claim_witness,
        "claim_witness_digest": request.claim_witness_digest,
        "claim_capability_digest": request.claim_capability_digest}


def static_measure(d, header):
    labels = tuple((model.__entity_type__, tuple(model.model_fields)) for model in
        (d.EffectDispatch, d.ExternalFact, d.FactApplication, d.EffectEvidence,
         d.ConversationEvent, d.Conversation, d.RuntimeStorageReservation,
         d.EffectIntent, d.EffectResource, d.Grant, d.ObservationCursor))
    from . import _command_values as v
    link, adapter = header["link"], header["adapter"]
    skeleton = {"identity": {"variant": "effect", "dispatch": link["dispatch"],
        "adapter_id": adapter["adapter_id"], "remote_identity": adapter["remote_identity"],
        "source": adapter["source"], "source_epoch": adapter["source_epoch"],
        "ordering_group": header["ordering_group"],
        "topic": link["effect_action"], "event_id": "", "operation_id": link["operation_id"],
        "claim_fence": link["claim_fence"], "mode": link["mode"]},
        "adapter": adapter, "producer_schema": header["producer_schema"], "effect_link": link,
        "effect_result": {"evidence_kind": "failed_no_effect", "proof": "operation_terminal",
            "observed_generation_claim": None, "causal_claim": None},
        "observation": {"source_sequence": None, "proof": "snapshot_baseline", "remote_subject": None, "objects": ()},
        "original_payload": v.envelope("agent-native.fact-input/1", None)}
    # Measure schema header/identity as well as native DATA; no validator or
    # updater is invoked by this arithmetic-only skeleton serialization.
    from agent_native.schema.codec import _envelope
    from agent_native.schema.registry import _wire
    from agent_native.schema.descriptors import canonical
    from agent_native.effects._bounds import preflight
    preflight(skeleton, max_bytes=65536, max_nodes=2048, max_depth=12, max_items=256)
    definition = d.registry._definition("record", d.ExternalEventEnvelope.__record_type__)
    schema_size = len(canonical(_envelope(d.registry, "record", definition, _wire(skeleton))))
    return max(len(native_bytes((header, labels))), len(native_bytes(skeleton)), schema_size)


def ledger():
    """Inspectable finite term inventory for native staged incremental growth.

    Every identity occurrence is charged the ENTIRE measured static skeleton S,
    rather than a guessed maximum label length. Numeric/map punctuation uses
    the fixed constants. The 64 domain keys are overcharged in SDK allowance;
    their arbitrary values remain separately measured beyond all claims.
    """
    return {
        "retain": {"new_field_versions": 18, "new_identity_maps": 21,
            "changed_sdk_keys": 6, "value_header_links": 16},
        "apply": {"new_field_versions": 34, "new_identity_maps": 21,
            "domain_field_versions": 64, "changed_sdk_keys": 32, "value_header_links": 32},
    }


def bounds(static):
    terms = ledger()
    require(sum(terms["retain"].values()) <= 64 and sum(terms["apply"].values()) <= 192,
        "effect_capacity_ledger")
    return 512 * 1024 + 64 * static, 1152 * 1024 + 192 * static


def pin_dispatch(dispatcher, reader, request, adapter, binding, claims, *, readback_grant=None):
    """Measure the exact original admission before I/O; no updater is invoked."""
    from ._conversation import Overlay, source_context
    from . import _command_values as v
    from agent_native.effects.types import EffectError
    d = dispatcher.definitions
    link = link_from_request(request)
    no_effect, reads = dispatcher._no_effect_origin(reader, binding, link, thaw(request.payload))
    if request.stop_prerequisite is not None:
        target = thaw(request.stop_prerequisite)["target"]
        claims = tuple(target if x["slot"] == target["slot"] else x for x in claims)
    local = None
    if adapter.source is not None and request.remote_subject is not None and binding.ordering_group is not None:
        key = dispatcher._cursor_key(binding, claims, thaw(request.remote_subject))
        if key is not None:
            cursor = dispatcher._ref(d.ObservationCursor, v.digest(key))
            if reader.exists(cursor):
                local = {"cursor_key": key, "selection_generation": reader.field(cursor, "selection_generation")}
    # Prefer the actual immutable accepted Invocation event over today's
    # task/session association when pinning original conversation provenance.
    contexts = {(reader.field(r,"conversation"), reader.field(r,"group_id"), reader.field(r,"input_lineage"))
        for r in reader.entities(d.ConversationEvent.__entity_type__)
        if thaw(reader.field(r,"source")) == identity(request.invocation)}
    if len(contexts) > 1:
        raise EffectError("capacity_profile_unavailable")
    context = next(iter(contexts)) if contexts else source_context(d, Overlay(reader, Patch()), request.invocation)
    conversation, group, lineage = context if context is not None else (None, None, None)
    header = {"link": link, "adapter": dispatcher._capability(adapter),
        "producer_schema": thaw(request.producer_schema), "ordering_group": request.ordering_group,
        "admission": {"output_binding": dispatcher.catalog.identity(binding), "targets": claims,
            "observation": local, "activity_origin": thaw(request.stop_origin), "no_effect_origin": no_effect},
        "conversation": None if conversation is None else identity(conversation), "group_id": group,
        "input_lineage": None if lineage is None else identity(lineage), "readback_grant": readback_grant,
        "profile": PROFILE}
    size = static_measure(d, header)
    if size > STATIC_BYTES or len(adapter.actions) > 64:
        raise EffectError("capacity_profile_unavailable")
    retain, apply = bounds(size)
    return {**header, "static_bytes": size, "retain_bytes": retain, "apply_bytes": apply,
        "state": "reserved", "first_fact": None}, reads


def validate_result(message):
    from agent_native.effects._bounds import preflight
    preflight((message.payload, message.evidence_id, message.remote_subject, message.remote_objects,
        message.causal_claim, message.source_sequence, message.observed_generation, message.observation_proof),
        max_bytes=RESULT_BYTES, max_nodes=RESULT_NODES, max_depth=8, max_items=64)


def pending_counts(d, reader, key):
    active = [reader.field(r, "external")["identity"] for r in reader.entities(d.ExternalFact.__entity_type__)
        if not reader.exists(Ref(EntityKey(d.FactApplication.__entity_type__, r.key.entity_id, 1)))]
    active.extend(reader.field(r, "adapter") for r in reader.entities(d.EffectDispatch.__entity_type__)
        if reader.field(r, "state") == "reserved")
    return len(active), sum(source_key(x) == key for x in active)


def admit_pending(d, reader, key):
    total, own = pending_counts(d, reader, key)
    require(total < 256 and own < 64, "pending_fact_quota")


def principal(d, reader, dispatch):
    return reader.field(dispatch["link"]["invocation"], "principal")


def owner_conversation(d, view, owner):
    if owner.key.entity_type == d.EffectDispatch.__entity_type__:
        return view.values(owner)["conversation"]
    if owner.key.entity_type == d.ExternalFact.__entity_type__:
        link = view.values(owner)["external"]["effect_link"]
        return None if link is None else view.values(reference(link["dispatch"]))["conversation"]
    return None


def validate_graph(d, reader):
    from . import _command_values as v
    from ._capacity import check_counter_capacity
    check_counter_capacity(d, reader)
    for ref in reader.entities(d.EffectDispatch.__entity_type__):
        row = thaw(reader.resolve(ref).values)
        require(reader.resolve(ref).partition == "durable", "effect_dispatch_durable")
        require(row["link"]["dispatch"] == identity(ref), "effect_dispatch_identity")
        require(row["ordering_group"] is None or type(row["ordering_group"]) is str
            and 0 < len(row["ordering_group"].encode("utf-8")) <= 256, "effect_dispatch_group")
        header = {k: val for k, val in row.items() if k not in
            ("static_bytes", "retain_bytes", "apply_bytes", "state", "first_fact")}
        size = static_measure(d, header)
        require(row["profile"] == PROFILE and row["static_bytes"] == size <= STATIC_BYTES
            and len(row["adapter"]["actions"]) <= 64 and bounds(size) == (row["retain_bytes"], row["apply_bytes"]),
            "effect_dispatch_profile")
        link = row["link"]
        effect = thaw(reader.resolve(link["effect"]).values)
        require(effect["invocation"] == link["invocation"] and effect["grant"] == link["operation_grant"]
            and effect["operation_id"] == link["operation_id"] and effect["resource"] == link["resource"]
            and effect["desired_generation"] == link["desired_generation"] and effect["action"] == link["effect_action"]
            and effect["claim_fence"] >= link["claim_fence"], "effect_dispatch_original")
        witness = thaw(reader.resolve(link["claim_witness"]).values)
        require(v.digest(witness) == link["claim_witness_digest"], "effect_dispatch_witness")
        require((row["conversation"] is None) == (row["group_id"] is None), "effect_dispatch_context")
        if row["conversation"] is not None:
            conv = reference(row["conversation"])
            require(conv.key.entity_type == d.Conversation.__entity_type__ and reader.exists(conv), "effect_dispatch_conversation")
        reservation = thaw(reader.resolve(reservation_ref(d, ref)).values)
        require(reservation["owner"] == identity(ref) and reservation["principal"] == principal(d, reader, row)
            and reservation["bytes"] == row["retain_bytes"] + row["apply_bytes"]
            and (reservation["state"] == "reserved") == (row["state"] == "reserved"), "effect_dispatch_reservation")
        require((row["first_fact"] is None) == (row["state"] in ("reserved", "not_sent")), "effect_dispatch_first")
        if row["first_fact"] is not None:
            fact = reference(row["first_fact"])
            require(thaw(reader.field(fact, "external"))["effect_link"]["dispatch"] == identity(ref), "effect_dispatch_fact")
            app = Ref(EntityKey(d.FactApplication.__entity_type__, fact.key.entity_id, 1))
            require((row["state"] == "applied") == reader.exists(app), "effect_dispatch_application")
    keys = set()
    for fact in reader.entities(d.ExternalFact.__entity_type__):
        external = thaw(reader.field(fact, "external"))
        key = source_key(external["identity"])
        keys.add(key)
        app = Ref(EntityKey(d.FactApplication.__entity_type__, fact.key.entity_id, 1))
        reservation = thaw(reader.resolve(reservation_ref(d, fact)).values)
        require(reservation["owner"] == identity(fact) and (reservation["state"] == "reserved") == (not reader.exists(app)),
            "effect_fact_reservation")
        if external["effect_link"] is not None:
            dispatch = thaw(reader.resolve(reference(external["effect_link"]["dispatch"])).values)
            require(external["identity"]["ordering_group"] == (dispatch["ordering_group"]
                if external["observation"] is not None else None), "effect_fact_original_group")
            require(external["effect_link"] == dispatch["link"] and external["adapter"] == dispatch["adapter"]
                and external["producer_schema"] == dispatch["producer_schema"]
                and thaw(reader.field(fact, "admission")) == dispatch["admission"], "effect_fact_original_pin")
            require(reservation["bytes"] == dispatch["apply_bytes"], "effect_fact_apply_bytes")
    keys.update(source_key(reader.field(r, "adapter")) for r in reader.entities(d.EffectDispatch.__entity_type__))
    require(all(pending_counts(d, reader, k)[0] <= 256 and pending_counts(d, reader, k)[1] <= 64 for k in keys),
        "pending_fact_quota")


def validate_transition(d, base, staged):
    before, after = base._data().records, staged._data().records
    for key, old_record in before.items():
        if key.entity_type != d.EffectDispatch.__entity_type__:
            continue
        require(key in after, "effect_dispatch_deletion")
        old, new = old_record.values, after[key].values
        require(all(old[n] == new[n] for n in old if n not in ("state", "first_fact")), "effect_dispatch_header")
        require(old["first_fact"] is None or old["first_fact"] == new["first_fact"], "effect_dispatch_first_rewrite")
        allowed = {"reserved": ("reserved", "retained", "not_sent"), "retained": ("retained", "applied"),
            "applied": ("applied",), "not_sent": ("not_sent",)}
        require(new["state"] in allowed[old["state"]], "effect_dispatch_reactivation")
    for key, record in after.items():
        if key not in before and key.entity_type == d.EffectDispatch.__entity_type__:
            require(record.values["state"] == "reserved" and record.values["first_fact"] is None, "effect_dispatch_birth")
        if key not in before and key.entity_type == d.ExternalFact.__entity_type__:
            link = record.values["external"]["effect_link"]
            if link is not None:
                dispatch = reference(link["dispatch"])
                require(dispatch.key in before and before[dispatch.key].values["state"] != "not_sent", "effect_fact_dispatch_commit")
                if before[dispatch.key].values["first_fact"] is None:
                    require(thaw(after[dispatch.key].values["first_fact"]) == identity(Ref(key))
                        and after[dispatch.key].values["state"] == "retained", "effect_first_transfer_atomic")
    validate_graph(d, staged)


def check_application_growth(d, base, staged, domain_keys):
    """Actual normalized domain delta cannot borrow even its OWN SDK claim.

    Measuring an immutable DATA projection is not a second stage/writer. Restore
    only ordinary updated records/field versions to their base values; all SDK
    records, indices and final native counters remain exactly as normalized.
    """
    from agent_native.persistence.codec import encode_snapshot, durable_projection, MAX_BYTES
    old, new = base._data(), staged._data()
    applications = [r for k, r in new.records.items() if k not in old.records
        and k.entity_type == d.FactApplication.__entity_type__]
    if not applications:
        return
    require(len(applications) == 1, "effect_application_single_original")
    fact = applications[0].values["fact"]
    amount = base.field(reservation_ref(d, fact), "bytes")
    records = dict(new.records)
    versions = dict(new.field_versions)
    collections = dict(new.collection_versions)
    for key in domain_keys:
        records[key] = old.records[key]
        for target in (versions, collections):
            prior = old.field_versions if target is versions else old.collection_versions
            for token in tuple(target):
                if token[0] == key:
                    if token in prior:
                        target[token] = prior[token]
                    else:
                        target.pop(token)
    sdk = replace(new, records=MappingProxyType(records), field_versions=MappingProxyType(versions),
        collection_versions=MappingProxyType(collections))
    measure = lambda snapshot: len(encode_snapshot(durable_projection(snapshot)))
    old_size, sdk_size, final_size = measure(old), measure(sdk), measure(new)
    require(max(0, sdk_size-old_size) <= amount, "effect_sdk_application_bound")
    original_claims = sum(base.field(r, "bytes") for r in base.entities(d.RuntimeStorageReservation.__entity_type__)
        if base.field(r, "state") == "reserved")
    require(max(0, final_size-sdk_size) <= MAX_BYTES-old_size-original_claims,
        "effect_domain_requires_free_capacity")


def check_retention_growth(d, base, staged):
    from agent_native.persistence.codec import encode_snapshot, durable_projection
    before, after = base._data().records, staged._data().records
    for key, record in after.items():
        if key in before or key.entity_type != d.ExternalFact.__entity_type__:
            continue
        link = record.values["external"]["effect_link"]
        if link is None:
            continue
        dispatch = reference(link["dispatch"])
        original = before[dispatch.key].values
        if original["first_fact"] is None:
            growth = len(encode_snapshot(durable_projection(staged._data()))) - len(encode_snapshot(durable_projection(base._data())))
            require(growth <= original["retain_bytes"], "effect_sdk_retention_bound")
