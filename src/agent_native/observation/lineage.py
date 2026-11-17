"""Flat durable input DATA from a genuinely sealed C4 candidate.

Only the destination broker calls this producer after current C4 authorization.
It never restores a ProjectionTrace, grants permissions, samples newer values in
place of old typed identities, or serializes screenshot/text values as claims.
The caller reserves C4 working/retained space before invoking this module.
"""
from collections.abc import Mapping
from hashlib import sha256
from types import UnionType
from typing import Literal, Union, get_args, get_origin

from pydantic import BaseModel
from agent_native.schema import Ref, CollectionRef, Resource, ResourceCollection, Owned
from agent_native.state import FrozenList, thaw
from agent_native.persistence.codec import encode_value
from agent_native.kernel._input_lineage import native_bytes
from agent_native.context._projection import Direct, Activity, Member
from agent_native.context.semantic import base
from ._export_types import ExportError

MAX_CLAIMS = 2048
MAX_LINEAGE_BYTES = 2 * 1024 * 1024


def resource(value):
    if type(value) is Ref:
        key, field = value.key, None
    elif type(value) is CollectionRef:
        key, field = value.owner, value.field
    else:
        raise ExportError("lineage_source_unavailable")
    return {"entity_type": key.entity_type, "entity_id": key.entity_id,
            "generation": key.generation, "collection_field": field}


def _digest(value):
    return sha256(encode_value(value)).hexdigest()


def _identity(value):
    return value.key if type(value) is Ref else value.owner


def _refs(annotation, value, identity_record_type, *, depth=0, meter=None):
    """Read only ORIGINAL typed identity leaves; identity-looking strings stay text."""
    meter = [65536] if meter is None else meter
    meter[0] -= 1
    if meter[0] < 0 or depth > 32:
        raise ExportError("lineage_quota_exceeded")
    annotation = base(annotation)
    origin, args = get_origin(annotation), get_args(annotation)
    descend = lambda typ, item: _refs(typ, item, identity_record_type, depth=depth+1, meter=meter)
    if origin in (Ref, Resource):
        if type(value) is not Ref or value.key.entity_type != args[0].__entity_type__:
            raise ExportError("lineage_typed_value_unavailable")
        return (value,)
    if origin in (CollectionRef, ResourceCollection):
        if type(value) is not CollectionRef:
            raise ExportError("lineage_typed_value_unavailable")
        return (value,)
    if origin in (Union, UnionType):
        for item in args:
            try:
                return descend(item, value)
            except ExportError as error:
                if error.code != "lineage_typed_value_unavailable":
                    raise
        raise ExportError("lineage_typed_value_unavailable")
    if origin in (tuple, list, Owned):
        if type(value) not in (tuple, list, FrozenList):
            raise ExportError("lineage_typed_value_unavailable")
        if origin is Owned:
            kinds = (Ref[args[0]],) * len(value)
        elif origin is list or len(args) == 2 and args[1] is Ellipsis:
            kinds = (args[0],) * len(value)
        elif len(args) == len(value):
            kinds = args
        else:
            raise ExportError("lineage_typed_value_unavailable")
        return tuple(ref for typ, item in zip(kinds, value) for ref in descend(typ, item))
    if origin is dict:
        if not isinstance(value, Mapping) or any(type(k) is not str for k in value):
            raise ExportError("lineage_typed_value_unavailable")
        return tuple(ref for item in value.values() for ref in descend(args[1], item))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if not isinstance(value, Mapping):
            raise ExportError("lineage_typed_value_unavailable")
        if getattr(annotation, "__record_type__", None) == identity_record_type:
            from agent_native.context.bindings import identity
            return (identity(value),)
        fields = dict(annotation.__schema_annotations__)
        if not set(value) <= set(fields):
            raise ExportError("lineage_typed_value_unavailable")
        return tuple(ref for name, item in value.items() for ref in descend(fields[name], item))
    if annotation in (str, bool, int, float, type(None)):
        valid = type(value) is annotation
    elif origin is Literal:
        valid = any(type(value) is type(item) and value == item for item in args)
    else:
        valid = False
    if not valid:
        raise ExportError("lineage_typed_value_unavailable")
    return ()


class _Collector:
    def __init__(self, broker, cut):
        self.broker, self.cut, self.rows = broker, cut, {}
        self.schemas = {descriptor.definition_id: (registry, descriptor)
            for registry in broker.kernel.registries for descriptor in registry.descriptors()}
        self.encoded_bytes = 0

    def schema(self, name):
        pair = self.schemas.get(name)
        if pair is None:
            raise ExportError("lineage_schema_unavailable")
        registry, descriptor = pair
        return {"namespace": registry.namespace, "registry_revision": registry.revision,
                "definition_id": descriptor.definition_id,
                "definition_revision": descriptor.revision, "fingerprint": descriptor.fingerprint}

    @staticmethod
    def version(trace, source, field=None, *, collection=False):
        key = _identity(source)
        kind = "collection" if collection else "field" if field is not None else "existence"
        matches = [event.version for event in trace.read_set.events
                   if event.key == key and event.kind == kind and event.field == field]
        if matches:
            return matches[-1]
        return None

    def route(self, access):
        via = access.route
        alias = via.alias if type(via) is Member else None
        original = via.via if type(via) is Member else via
        activity = original.activity if type(original) is Activity else None
        anchor = access.anchor
        invocation = operation = None
        if activity is not None:
            if anchor is None:
                raise ExportError("lineage_activity_unavailable")
            plan_id = self.cut.view.field(anchor, "plan_id")
            if type(plan_id) is not str or not plan_id:
                raise ExportError("lineage_activity_unavailable")
            invocation = self.broker.kernel._ref(self.broker.kernel.system.Invocation, plan_id)
            operation = self.cut.view.field(invocation, "grant")
            self.broker.index.authority.check_activity_occurrence(self.cut.view, activity,
                original_anchor=anchor, start_invocation=invocation, start_operation_grant=operation)
        return {"kind": "member" if alias is not None else "activity" if activity is not None else "direct",
                "alias": None if alias is None else resource(alias),
                "activity": None if activity is None else resource(activity),
                "anchor": None if anchor is None else resource(anchor),
                "invocation": None if invocation is None else resource(invocation),
                "operation": None if operation is None else resource(operation)}

    @staticmethod
    def sdk_route():
        return {"kind": "sdk", "alias": None, "activity": None, "anchor": None,
                "invocation": None, "operation": None}

    def add(self, kind, source, field, route, version, digest, schema, grants=(), refs=(), associations=(), sdk=None):
        def identities(values, limit, *, ordered=False):
            rows = {}
            for value in values:
                item = resource(value)
                rows[encode_value(item)] = item
                if len(rows) > limit:
                    raise ExportError("lineage_quota_exceeded")
            return tuple(rows.values()) if ordered else tuple(rows[key] for key in sorted(rows))
        partition = None if source is None else self.cut.view.resolve(Ref(_identity(source))).partition
        claim = {"kind": kind, "source": None if source is None else resource(source),
            "source_partition": partition, "field": field, "route": route, "source_version": version,
            "content_digest": digest, "schema_identity": schema,
            "original_grants": identities(grants, 32, ordered=True), "value_refs": identities(refs, 256),
            "associations": identities(associations, 256, ordered=True), "sdk_kind": sdk}
        # SAME semantic native representation as historical copy and C1
        # native_digest. Raw dict codec tag m differs from frozen semantic d.
        encoded = native_bytes(claim)
        if encoded not in self.rows:
            if len(self.rows) >= MAX_CLAIMS or self.encoded_bytes + len(encoded) > MAX_LINEAGE_BYTES:
                raise ExportError("lineage_quota_exceeded")
            self.rows[encoded] = claim
            self.encoded_bytes += len(encoded)

    def trace(self, trace):
        # Access-only claims also retain schema literals and relevance/count inputs.
        for access in trace.accesses:
            source = access.source
            self.add("field", source, None, self.route(access), self.version(trace, source),
                _digest(resource(source)), self.schema(_identity(source).entity_type),
                access.chain, (source,))
        for sample in trace.samples:
            if sample.kind == "extension":
                from agent_native.kernel._input_lineage import native_digest
                anchor=self.cut.view.field(sample.source,"anchor")
                from agent_native.extensions.views import source_pin
                pin=source_pin(self.broker.kernel,sample.value)
                descriptor=sample.value["descriptor"]
                config_refs=tuple(item["action"] for item in descriptor.get("actions",()))+tuple(
                    item["rule"] for item in descriptor.get("rules",()))
                self.add("sdk_source",sample.source,None,self.sdk_route(),self.cut.view.resolve(pin).version,
                    native_digest(sample.value),self.schema(sample.source.key.entity_type),
                    (anchor,),associations=(pin,*config_refs),sdk="extension_revision")
                continue
            if sample.kind in ("capability", "command"):
                self.add("schema", None, None, self.sdk_route(), None,
                    _digest((sample.kind, sample.source, sample.value)), self.schema(sample.source))
                continue
            access = next((a for a in trace.accesses if a.source == sample.source and a.route == sample.route), None)
            if access is None:
                raise ExportError("lineage_source_unavailable")
            source, refs, associations = sample.source, (), ()
            schema = self.schema(_identity(source).entity_type)
            if sample.kind == "membership":
                kind, field = "membership", source.field
                refs = sample.value
                version = self.version(trace, source, source.field, collection=True)
            elif sample.kind == "draft":
                kind, field = "draft", sample.name
                associations = (sample.view, sample.binding, sample.draft)
                version = self.version(trace, sample.draft, "text")
            elif sample.kind in ("field", "eligibility", "relation"):
                kind, field = "field", sample.name
                version = self.version(trace, source, field)
                definition = self.broker.index.declarations[source.key.entity_type]
                annotation = dict(definition.annotations)[field]
                if sample.kind == "relation":
                    relation = next(item.relation for item in definition.descriptor.fields if item.name == field)
                    if relation.kind == "owned":
                        refs = (CollectionRef(source.key, field),)
                    else:
                        if version is None or self.cut.view.field_version(source, field) != version:
                            raise ExportError("lineage_original_relation_unavailable")
                        refs = _refs(annotation, self.cut.view.field(source, field),
                            self.broker.kernel.system.ResourceIdentity.__record_type__)
                else:
                    refs = _refs(annotation, sample.value,
                        self.broker.kernel.system.ResourceIdentity.__record_type__)
            else:
                raise ExportError("lineage_source_unavailable")
            self.add(kind, source, field, self.route(access), version, _digest(sample.value), schema,
                access.chain, refs, associations)


def collect_input_lineage(broker, record, sealed, cut):
    """Return one exact detached draft; caller has authorized and reserved space."""
    collector = _Collector(broker, cut)
    seen = set()
    for trace in record.traces:
        if id(trace) not in seen:
            collector.trace(trace)
            seen.add(id(trace))
    session_ref = sealed.preparation.spec.session
    session = cut.view.resolve(session_ref).values
    contributors = record.prepared.get("fragment_digests")
    if type(contributors) is not tuple or not contributors:
        raise ExportError("lineage_contributors_unavailable")
    for kind, content_digest in contributors:
        if kind == "context":
            continue
        if kind == "sdk_literal":
            collector.add("sdk_source", None, None, collector.sdk_route(), None,
                content_digest, None, sdk="engine_literal")
        elif kind == "user_intent":
            from agent_native.kernel._command_values import open_envelope
            intent = open_envelope(thaw(session["intent"]), "agent-native.runner-intent/1")
            expected = intent.get("text") if isinstance(intent, Mapping) else intent
            if type(expected) is not str or content_digest != _digest(expected):
                raise ExportError("lineage_intent_mismatch")
            collector.add("sdk_source", session_ref, "intent", collector.sdk_route(),
                cut.view.field_version(session_ref, "intent"), content_digest,
                collector.schema(session_ref.key.entity_type), sdk="user_intent")
        elif kind == "skills":
            binding=record.prepared.get("skills")
            if binding is None or binding.fragment_digest!=content_digest:
                raise ExportError("lineage_skills_mismatch")
            from .history import _add_claim
            for claim in binding.claims:_add_claim(collector,claim)
        elif kind in ("history","summarize"):
            binding=record.prepared.get("history")
            if binding is None or binding.fragment_digest!=content_digest or (kind=="summarize")!=(sealed.preparation.spec.purpose=="summarize"):
                raise ExportError("lineage_history_mismatch")
            from .history import _add_claim
            for claim in binding.claims:_add_claim(collector,claim)
        else:
            # History/skill factories must supply their genuine durable source
            # union explicitly; no permissive legacy arbitrary-text fallback.
            raise ExportError("lineage_fragment_unsupported")
    # Actual approved pixel inputs retain their complete source trace union above.
    # Hash only; neither PNG bytes nor visual/user text enters persisted claims.
    for asset in record.assets:
        collector.add("sdk_source", None, None, collector.sdk_route(), None,
            sha256(asset.encoded).hexdigest(), None, sdk="engine_literal")
    original = sealed.plan.cut
    draft = {"original_cut": {"epoch": original.epoch, "sequence": original.sequence,
                "index_revision": original.index_revision, "policy_revision": original.policy_revision},
             "schema_manifest": sha256(encode_value(original.schema_manifest)).hexdigest(),
             "policy_revision": original.policy_revision,
             "claims": tuple(collector.rows[key] for key in sorted(collector.rows)),
             "selected_events": (), "summary": None, "skill_pins": ()}
    binding=record.prepared.get("history")
    if binding is not None:
        draft["selected_events"]=thaw(binding.selected_events) if any(type(x) is not dict for x in binding.selected_events) else binding.selected_events
        draft["summary"]=thaw(binding.summary) if binding.summary is not None and type(binding.summary) is not dict else binding.summary
    skills=record.prepared.get("skills")
    if skills is not None:draft["skill_pins"]=skills.pins
    if not draft["claims"] or len(encode_value(draft)) > MAX_LINEAGE_BYTES:
        raise ExportError("lineage_quota_exceeded")
    return draft
